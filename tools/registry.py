"""工具注册、Schema 生成、参数校验与分发。"""
import json
from dataclasses import dataclass, field
from typing import Callable
from typing import Literal

from pydantic import BaseModel, ValidationError

from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox, SandboxError

MAX_RESULT_CHARS = 2000


class ToolError(Exception):
    """工具内部的可预期错误（文件不存在、重名等），信息会原样回传给模型。"""


@dataclass
class TrashBatch:
    """本地准备的批量回收计划；注册层负责确认、逐文件记日志、失败时保留已完成记录。"""
    paths: list[str]
    keeps: list[str]
    validate: Callable
    groups: list[dict] = field(default_factory=list)  # exact duplicate families from the executable plan
    verify_keeps: Callable | None = None
    scan: dict = field(default_factory=dict)


def error_code(text, default='CONTROL_REJECTED'):
    """The machine code a rejected call states first ("ERROR: TURN_CLOSED：…"), so the
    structured result and its text agree (S5 audit 2026-10-25, P2-C)."""
    head = (text or '').removeprefix('ERROR: ').split('：', 1)[0]
    return head if head and head.replace('_', '').isalpha() and head.isupper() else default


@dataclass
class Tool:
    name: str
    desc: str
    args_model: type[BaseModel]
    func: Callable  # (args, sandbox) -> str | ToolResult；写工具返回 TrashBatch，由本地批量执行
    writes: bool
    batch: bool = False
    max_result_chars: int = MAX_RESULT_CHARS
    effects: tuple[str, ...] = ()
    display_result: bool = False
    read_scope: Callable | None = None
    write_scope: Callable | None = None
    file_fields: tuple[str, ...] = ()
    usage: str | None = None
    reference_inputs: tuple[tuple[str, str], ...] = ()
    produces: tuple[str, ...] = ()  # artifact collections whose execution receipts this tool writes


class ToolResult(BaseModel):
    """One tool call's result (routing redesign S6b, 8.2). The model sees only
    ``for_model()``: status, text, error_code, retryable, as before; the rest stays
    local (trace, observations) and is produced by tools and executors only."""
    status: Literal['ok', 'error']
    text: str
    error_code: str | None = None
    retryable: bool = False
    result_id: str | None = None
    data: dict = {}
    references: list[dict] = []     # [{kind, id}] objects this call issued, from the artifact store
    operation_refs: list[str] = []  # journal:<run>:<line> of the writes this call recorded
    progress: dict | None = None    # {completed, remaining, unknown}: step ids of a partial execution
    error_class: str | None = None  # one of ERROR_CLASSES
    recovery: dict | None = None    # {action, target}; never an authorisation to retry by itself
    display_ref: str | None = None

    def for_model(self):
        return json.dumps(dict(status=self.status, text=self.text, error_code=self.error_code, retryable=self.retryable),
                          ensure_ascii=False)


# The minimal error classes (8.2). The specific code (AMBIGUOUS_GOAL, TURN_CLOSED, ...)
# stays in error_code; error_class groups it for recovery and statistics.
ERROR_CLASSES = ('INVALID_ARGUMENTS', 'AMBIGUOUS_GOAL', 'QUESTION_STALE', 'IDEMPOTENCY_CONFLICT', 'OUT_OF_SCOPE',
                 'CONSENT_REQUIRED', 'TASK_REVISION_CONFLICT', 'PLAN_STALE', 'LIBRARY_BUSY', 'USER_DECLINED',
                 'CONFIRMATION_TIMEOUT', 'BUDGET_EXCEEDED', 'EXTRACTION_FAILED', 'PARTIAL_EXECUTION',
                 'OUTCOME_UNKNOWN', 'INTERNAL_ERROR')
# (error class, retryable, recovery action) by specific code, then by the wording local
# checks already use. First match wins; order matters (partial execution before declines).
_BY_CODE = {
    'INVALID_ARGUMENTS': ('INVALID_ARGUMENTS', False, 'replan'),
    'AMBIGUOUS_GOAL': ('AMBIGUOUS_GOAL', False, 'replan'),
    'UNRESOLVED_QUESTION_SCOPE': ('AMBIGUOUS_GOAL', False, 'replan'),
    'QUESTION_STALE': ('QUESTION_STALE', False, 'none'), 'QUESTION_ALREADY_ANSWERED': ('QUESTION_STALE', False, 'none'),
    'IDEMPOTENCY_CONFLICT': ('IDEMPOTENCY_CONFLICT', False, 'none'),
    'INPUT_RELATION_REQUIRED': ('CONSENT_REQUIRED', False, 'ask'),
    'TURN_CLOSED': ('INVALID_ARGUMENTS', False, 'none'),
    'TERMINAL_REQUIRED': ('INVALID_ARGUMENTS', False, 'replan'),
    'USER_DECLINED': ('USER_DECLINED', False, 'none'),
    # Not approved without a decline: the card timed out or was interrupted (S1 keeps which, in error_code).
    'CONFIRMATION_TIMEOUT': ('CONFIRMATION_TIMEOUT', False, 'none'),
    'CONFIRMATION_INTERRUPTED': ('CONFIRMATION_TIMEOUT', False, 'none'),
    'RUN_INTERRUPTED': ('OUTCOME_UNKNOWN', False, 'reconcile'),
    'INVALID_RESULT': ('INTERNAL_ERROR', False, 'none'),
}
_BY_TEXT = (
    (('中途停止', '只执行了一部分', '部分执行', '已回收', '已执行的步骤保留'), ('PARTIAL_EXECUTION', False, 'reconcile')),
    (('结果不明', '未确认是否完成', '执行中断'), ('OUTCOME_UNKNOWN', False, 'reconcile')),
    (('用户拒绝', 'USER_DECLINED'), ('USER_DECLINED', False, 'none')),
    (('超时',), ('CONFIRMATION_TIMEOUT', False, 'none')),  # the user retries explicitly (8.1)
    (('已有计划正在执行', '资料库正有另一项'), ('LIBRARY_BUSY', True, 'retry')),
    (('任务版本已改变', '任务已修订', 'revision'), ('TASK_REVISION_CONFLICT', False, 'replan')),
    (('计划已过期', '文件变化', '已变化', '请重新检索', '请重新查询'), ('PLAN_STALE', False, 'replan')),
    (('预算', '费用上限'), ('BUDGET_EXCEEDED', False, 'none')),
    (('超出', '越权', '不在允许', '读取范围', '写入范围', '不能引用隐藏', '回收区', '符号链接'), ('OUT_OF_SCOPE', False, 'replan')),
    (('必须由用户选择', '需用户授权', '用户授权', '不会覆盖', '先问', '先 finish_turn(mode=ask)'), ('CONSENT_REQUIRED', False, 'ask')),
    (('无法读取', '读取失败', '已加密', '损坏', '无法识别', '不是有效的'), ('EXTRACTION_FAILED', False, 'none')),
)


def classify(code, text):
    """(error_class, retryable, recovery) of a failed call, from its code and wording."""
    body = (text or '').removeprefix('ERROR: ')
    if code in _BY_CODE and code not in {'TOOL_ERROR', 'CONTROL_REJECTED'}:
        return _BY_CODE[code]
    head = body.split(':', 1)[0].split('：', 1)[0]
    if head.endswith(('Error', 'Exception')) and head.isidentifier():
        return ('INTERNAL_ERROR', False, 'retry')  # an unexpected exception, by its class name
    for words, typed in _BY_TEXT:
        if any(w in body for w in words):
            return typed
    if code in _BY_CODE:
        return _BY_CODE[code]
    return ('INVALID_ARGUMENTS', False, 'replan')  # a request the tool refused as stated


def typed(outcome, result_id=None):
    """Fill the local fields a result needs before it is recorded: an ID, and for an
    error its class, retryability and recovery. Existing values are kept."""
    if result_id and not outcome.result_id:
        outcome.result_id = result_id
    if outcome.status == 'error' and not outcome.error_class:
        cls, retry, action = classify(outcome.error_code, outcome.text)
        outcome.error_class = cls
        outcome.retryable = outcome.retryable or retry
        outcome.recovery = outcome.recovery or dict(action=action, target=None)
    return outcome


def _failure(text, code='TOOL_ERROR'):
    return ToolResult(status='error', text=text, error_code=code)



TOOLS: dict[str, Tool] = {}


def tool(name: str, desc: str, args_model: type[BaseModel], writes: bool = False, batch: bool = False,
         max_result_chars: int = MAX_RESULT_CHARS, *, effects: tuple[str, ...] | None = None,
         display_result: bool = False, read_scope: Callable | None = None, write_scope: Callable | None = None,
         file_fields: tuple[str, ...] = (), usage: str | None = None,
         reference_inputs: tuple[tuple[str, str], ...] = (), produces: tuple[str, ...] = ()):
    # Single-step writes were retired (2026-10-23): files change through file plans,
    # and a write tool returns a batch the local executor confirms, runs and journals.
    if writes and not batch:
        raise ValueError(f'{name}: 写工具必须是批量工具（返回 TrashBatch）；文件改动请走文件计划')
    def deco(func):
        declared = effects if effects is not None else (('unknown_write',) if writes else ())
        TOOLS[name] = Tool(name, desc, args_model, func, writes, batch, max_result_chars,
                           declared, display_result, read_scope, write_scope,
                           file_fields, usage, reference_inputs, produces)
        return func
    return deco


def schemas() -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.desc,
                      "parameters": t.args_model.model_json_schema()}}
        for t in TOOLS.values()
    ]


def dispatch(name: str, raw_args: str, sandbox: Sandbox,
             oplog: OpLog | None = None, confirmer: Confirmer | None = None) -> str:
    return dispatch_result(name, raw_args, sandbox, oplog, confirmer).text


REFERENCE_KINDS = ('evidence', 'receipts', 'classification_plans', 'catalogs')


def _issued_ids(artifacts):
    return {k: set(artifacts.data.get(k, {})) for k in REFERENCE_KINDS} if artifacts else {}


def dispatch_result(name, raw_args, sandbox, oplog=None, confirmer=None):
    """Like ``_dispatch``, and the result names the objects this call issued (S6b, 8.2):
    references come from the artifact store at the producing call, not from a later
    guess over everything that changed."""
    artifacts = getattr(sandbox, 'artifacts', None)
    before = _issued_ids(artifacts)
    lines = len(oplog.path.read_text().splitlines()) if oplog and oplog.path.exists() else 0
    result = _dispatch(name, raw_args, sandbox, oplog, confirmer)
    if artifacts:
        after = _issued_ids(artifacts)
        result.references = result.references or [dict(kind=k, id=i) for k in REFERENCE_KINDS
                                                   for i in sorted(after[k] - before[k])]
    if oplog and oplog.path.exists():
        count = len(oplog.path.read_text().splitlines())
        result.operation_refs = result.operation_refs or [f'journal:{oplog.path.stem}:{n}' for n in range(lines, count)]
    return result


def _dispatch(name, raw_args, sandbox, oplog=None, confirmer=None):
    """JSON 解析 → pydantic 校验 → 执行（工具内做沙盒路径检查；批量写入由本地确认、执行、记日志）→ 截断。
    任何错误都转成一句 ERROR 文本返回给模型，不让程序崩溃。"""
    t = TOOLS.get(name)
    if t is None:
        return _failure(f"ERROR: 没有工具 {name}。可用工具：{', '.join(TOOLS)}", 'TOOL_ERROR')
    try:
        data = json.loads(raw_args or "{}")
    except json.JSONDecodeError as e:
        return _failure(f"ERROR: 参数不是合法 JSON：{e}。请重新生成", 'INVALID_ARGUMENTS')
    artifacts = getattr(sandbox, 'artifacts', None)
    try:
        if artifacts and isinstance(data, dict):
            for key in t.file_fields:
                value=data.get(key)
                if isinstance(value,str) and (value.startswith('file:') or value in artifacts.data['files']):
                    p, _ = artifacts.resolve(value.removeprefix('file:'), allow_trash=False)
                    data[key] = sandbox.rel(p)
        args = t.args_model.model_validate(data)
    except ValidationError as e:
        errs = "；".join(f"{'.'.join(map(str, x['loc'])) or '参数'}: {x['msg']}" for x in e.errors())
        return _failure(f"ERROR: 参数校验失败：{errs}", 'INVALID_ARGUMENTS')
    except ValueError as e:
        return _failure(f"ERROR: {e}", 'TOOL_ERROR')
    try:
        result = t.func(args, sandbox)
        if t.batch:
            if not isinstance(result, TrashBatch) or not t.writes:
                raise ToolError("批量工具未返回有效回收计划")
            result = _trash_batch(result, sandbox, oplog, confirmer)
    except SandboxError as e:
        return _failure(f"ERROR: {e}", 'TOOL_ERROR')
    except ToolError as e:
        return _failure(f"ERROR: {e}", 'TOOL_ERROR')
    except Exception as e:  # 兜底：意外异常也回传给模型
        return _failure(f"ERROR: {type(e).__name__}: {e}", 'TOOL_ERROR')
    if isinstance(result, ToolResult):
        result.text = result.text[:t.max_result_chars] if len(result.text) > t.max_result_chars else result.text
        return result
    if not isinstance(result, str):
        return _failure('ERROR: 工具返回了不支持的结果类型', 'INVALID_RESULT')
    if len(result) > t.max_result_chars:
        result = result[:t.max_result_chars] + f"\n……[已截断，共 {len(result)} 字]"
    # Compatibility boundary for existing string-returning tools. New tools
    # may return ToolResult directly; controller decisions use the typed status.
    return _failure(result) if result.startswith('ERROR:') else ToolResult(status='ok', text=result)


def _trash_batch(plan: TrashBatch, sandbox: Sandbox, oplog: OpLog | None,
                 confirmer: Confirmer | None) -> str:
    """Dedup's batch recycle holds the same library lock as file plans, so two
    conversations never write to one library at once."""
    from agent.file_plans import library_lock
    try:
        with library_lock(sandbox):
            return _trash_batch_locked(plan, sandbox, oplog, confirmer)
    except ValueError as exc:
        if '已有计划正在执行' in str(exc):
            raise ToolError('资料库正有另一项整理在执行，请稍后重试') from exc
        raise


def _trash_batch_locked(plan: TrashBatch, sandbox: Sandbox, oplog: OpLog | None,
                        confirmer: Confirmer | None) -> str:
    from tools.fs_write import TrashArgs, trash

    if not oplog:
        raise ToolError("批量去重需要操作日志，才能保证可撤销")
    if len(set(plan.paths)) != len(plan.paths) or set(plan.paths).intersection(plan.keeps):
        raise ToolError("批量计划存在重复回收路径或会回收保留文件")
    for rel in (*plan.paths, *plan.keeps):
        sandbox.path(rel, write=True)
    plan.validate()  # 必须先核对，再显示确认；确认后重查，防止等待期间文件变化
    from agent.constraints import enforce, operation
    enforce(sandbox, [operation('trash', p) for p in plan.paths])
    artifacts = getattr(sandbox, 'artifacts', None)
    reference_groups = []
    if artifacts:
        for group in plan.groups:
            ids = {p: artifacts.register(p) for p in group['members']}
            reference_groups.append(dict(kind=group['kind'], keeper=ids[group['keeper']], members=list(ids.values())))
    if not plan.paths:
        if artifacts:
            artifacts.receipt(groups=[], moved=[], keeps=[], verified=True, scan=plan.scan)
        return "OK: 没有需要移入回收区的文件"
    if confirmer:
        import hashlib
        from agent.file_plans import not_confirmed
        key = hashlib.sha256(json.dumps(['trash_many', sorted(plan.paths)], ensure_ascii=False).encode()).hexdigest()[:24]
        answer = confirmer.decide("trash_many", {"保留": plan.keeps, "移入回收区": plan.paths},
                                  file_count=len(plan.paths), review_groups=plan.groups or None, manifest_key=key)
        if answer.decision == 'declined':
            return "ERROR: 用户拒绝了批量去重，未移动任何文件（USER_DECLINED）"
        if not answer.approved:
            return "ERROR: " + not_confirmed(answer, '批量去重')
    plan.validate()
    enforce(sandbox, [operation('trash', p) for p in plan.paths])
    completed = 0
    moved = []
    def failed(message):
        if artifacts:
            artifacts.receipt(groups=reference_groups, moved=moved, keeps=plan.keeps,
                              verified=False, error=message, scan=plan.scan)
        # What ran stays done and journalled (undoable); what did not is named, never re-run blindly.
        done = [src for src, _ in moved]
        return ToolResult(status='error', text='ERROR: ' + message, error_code='PARTIAL_EXECUTION' if done else 'TOOL_ERROR',
                          error_class='PARTIAL_EXECUTION' if done else None,
                          recovery=dict(action='reconcile', target=None) if done else None,
                          progress=dict(completed=done, remaining=[p for p in plan.paths if p not in done], unknown=[]))
    for path in plan.paths:
        try:
            _, ops = trash(TrashArgs(path=path), sandbox)
            for op, src, dst in ops:
                try:
                    oplog.record(op, src, dst)
                except Exception:
                    # 当前文件不能留在无日志状态；之前已完成的操作仍有各自的日志。
                    original = sandbox.path(src, exact=True)
                    if original.exists():
                        raise ToolError(f"日志写入失败且原路径被占用，未覆盖；当前文件保留在 {dst}，需手动恢复")
                    sandbox.path(dst, exact=True).rename(original)
                    raise
            moved.extend((src, dst) for _, src, dst in ops)
            completed += 1
            if artifacts:
                for _, src, dst in ops:
                    artifacts.relocate(src, dst)
        except Exception as e:
            return failed(f"批量去重中途停止，已回收 {completed} 个文件且有日志可 undo；{type(e).__name__}: {e}")
    # Verify only the executed plan. This is not a claim that no new duplicate
    # appeared elsewhere while the user reviewed the confirmation.
    for src, dst in moved:
        original = sandbox.path(src, exact=True)
        if original.exists() or original.is_symlink() or not sandbox.path(dst, exact=True).is_file():
            return failed(f"已回收 {completed} 个文件且有日志可 undo，但本地位置复核失败：{src}")
    try:
        if plan.verify_keeps:
            plan.verify_keeps()
    except Exception as exc:
        return failed(f"已回收 {completed} 个文件且有日志可 undo，但保留文件复核失败：{exc}")
    receipt = ''
    if artifacts:
        rid = artifacts.receipt(groups=reference_groups, moved=moved, keeps=plan.keeps, verified=True, scan=plan.scan)
        receipt = f'\n本地结果回执 receipt:{rid}；分组表格由本地生成，最终回复无需抄写路径或重新制作清理表格。'
    return (f"OK: 已将 {completed} 个重复文件移入回收区，保留 {len(plan.keeps)} 份；每个文件已记录，可 undo\n"
            f"本地执行后复核通过：{len(moved)} 个原路径已不存在、回收文件均存在，所有保留文件 SHA256 与计划一致。"
            "复核范围仅为本次计划；无需再逐文件 file_info 或全量重扫来核对这些操作。" + receipt)
