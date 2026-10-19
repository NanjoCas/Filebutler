"""工具注册、Schema 生成、参数校验与分发。"""
import json
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, ValidationError

from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox, SandboxError

MAX_RESULT_CHARS = 2000
PATH_FIELDS = ("path", "src", "dst_dir")  # 写操作执行前要预检查的路径参数


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


@dataclass
class LocalWriteBatch:
    """Local executor owns preflight, one confirmation, journalling and verification."""
    execute: Callable


@dataclass
class Tool:
    name: str
    desc: str
    args_model: type[BaseModel]
    func: Callable  # (args, sandbox) -> str；写工具返回 (str, [(op, src, dst), ...]) 供记日志
    writes: bool
    batch: bool = False
    max_result_chars: int = MAX_RESULT_CHARS


TOOLS: dict[str, Tool] = {}


def tool(name: str, desc: str, args_model: type[BaseModel], writes: bool = False, batch: bool = False,
         max_result_chars: int = MAX_RESULT_CHARS):
    def deco(func):
        TOOLS[name] = Tool(name, desc, args_model, func, writes, batch, max_result_chars)
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
    """JSON 解析 → pydantic 校验 → 沙盒路径检查 →（写操作）确认 → 执行 →（写操作）记日志 → 截断。
    任何错误都转成一句 ERROR 文本返回给模型，不让程序崩溃。"""
    t = TOOLS.get(name)
    if t is None:
        return f"ERROR: 没有工具 {name}。可用工具：{', '.join(TOOLS)}"
    try:
        data = json.loads(raw_args or "{}")
    except json.JSONDecodeError as e:
        return f"ERROR: 参数不是合法 JSON：{e}。请重新生成"
    artifacts = getattr(sandbox, 'artifacts', None)
    try:
        if artifacts and isinstance(data, dict):
            for key in ('path', 'src'):
                value=data.get(key)
                if isinstance(value,str) and (value.startswith('file:') or value in artifacts.data['files']):
                    p, _ = artifacts.resolve(value.removeprefix('file:'), allow_trash=False)
                    data[key] = sandbox.rel(p)
        args = t.args_model.model_validate(data)
    except ValidationError as e:
        errs = "；".join(f"{'.'.join(map(str, x['loc'])) or '参数'}: {x['msg']}" for x in e.errors())
        return f"ERROR: 参数校验失败：{errs}"
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        if t.writes:
            # 先查路径再问用户，不让用户去确认一个本来就会被拦下的操作
            for f in PATH_FIELDS:
                if hasattr(args, f):
                    v = getattr(args, f)
                    sandbox.path(v, write=not t.batch and not (f == "dst_dir" and v.strip() in ("", ".")))
            if not t.batch and confirmer and not confirmer.allow(name, args.model_dump()):
                return "ERROR: 用户拒绝了这个操作。请调整计划，或在总结里说明没有执行的部分"
            if artifacts and not t.batch:
                for key in ('path', 'src'):
                    value = getattr(args, key, None)
                    if value and sandbox.path(value).is_file():
                        artifacts.register(sandbox.rel(sandbox.path(value)))
        result = t.func(args, sandbox)
        if t.batch:
            if not isinstance(result, (TrashBatch, LocalWriteBatch)) or not t.writes:
                raise ToolError("批量工具未返回有效回收计划")
            result = result.execute(sandbox, oplog, confirmer) if isinstance(result, LocalWriteBatch) else _trash_batch(result, sandbox, oplog, confirmer)
    except SandboxError as e:
        return f"ERROR: {e}"
    except ToolError as e:
        return f"ERROR: {e}"
    except Exception as e:  # 兜底：意外异常也回传给模型
        return f"ERROR: {type(e).__name__}: {e}"
    if isinstance(result, tuple):
        result, ops = result
        if oplog:
            for op in ops:
                oplog.record(*op)
                if artifacts and op[0] != 'make_dir':
                    artifacts.relocate(op[1], op[2])
    if len(result) > t.max_result_chars:
        result = result[:t.max_result_chars] + f"\n……[已截断，共 {len(result)} 字]"
    return result


def _trash_batch(plan: TrashBatch, sandbox: Sandbox, oplog: OpLog | None,
                 confirmer: Confirmer | None) -> str:
    from tools.fs_write import TrashArgs, trash

    if not oplog:
        raise ToolError("批量去重需要操作日志，才能保证可撤销")
    if len(set(plan.paths)) != len(plan.paths) or set(plan.paths).intersection(plan.keeps):
        raise ToolError("批量计划存在重复回收路径或会回收保留文件")
    for rel in (*plan.paths, *plan.keeps):
        sandbox.path(rel, write=True)
    plan.validate()  # 必须先核对，再显示确认；确认后重查，防止等待期间文件变化
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
    if confirmer and not confirmer.allow("trash_many", {"保留": plan.keeps, "移入回收区": plan.paths},
                                         file_count=len(plan.paths), review_groups=plan.groups or None):
        return "ERROR: 用户拒绝了批量去重，未移动任何文件"
    plan.validate()
    completed = 0
    moved = []
    def failed(message):
        if artifacts:
            rid = artifacts.receipt(groups=reference_groups, moved=moved, keeps=plan.keeps,
                                    verified=False, error=message, scan=plan.scan)
            runtime = getattr(sandbox, 'runtime', None)
            if runtime and runtime.task == 'dedup':
                runtime.final_answer = artifacts.receipt_markdown(rid)
                runtime.stop_reason = 'operation_error'
        return 'ERROR: ' + message
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
