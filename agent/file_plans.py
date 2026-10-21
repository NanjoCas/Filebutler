"""Immutable file manifests and a local, journalled executor.

The model proposes objects and intent. This module derives mechanical scopes,
parents and verification. Existing tasks never gain permissions from a plan.
"""
import copy
import hashlib
import json
import os
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from agent.artifacts import digest, signature, label
from agent.completion import selected, require_valid_snapshots, attribute, claims
from agent.task import StrictModel, TaskSubmission


class Action(StrictModel):
    effect: Literal['create', 'move', 'rename', 'trash']
    source: str | None = None
    destination: str | None = None

    @model_validator(mode='after')
    def endpoints(self):
        if self.effect != 'create' and not self.source:
            raise ValueError('此操作需要 source')
        if self.effect != 'trash' and not self.destination:
            raise ValueError('此操作需要 destination（完整目标路径）')
        if self.effect == 'create' and self.source or self.effect == 'trash' and self.destination:
            raise ValueError('create 只指定 destination；trash 只指定 source')
        return self


class Selection(StrictModel):
    path: str
    extensions: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    recursive: bool = True
    effect: Literal['move', 'trash'] = 'move'
    destination: str | None = None
    preserve_tree: bool = False


class PrepareFilePlan(StrictModel):
    description: str = Field(min_length=1, max_length=800)
    actions: list[Action] = Field(default_factory=list, max_length=200)
    selection: Selection | None = None
    classification_plan_id: str | None = Field(None, description='已有分类计划的精确 P 标识；本地展开全部来源和目标，不必抄写文件路径')
    goal_ids: list[str] = Field(default_factory=list, max_length=30,
        description='已有任务中此计划服务的目标；可省略，本地按完成条件逐项归属，无法确定时才要求提供')
    task_id: str | None = Field(None, description='续做近期任务时填写其 ID，沿用原限制；本轮已登记任务时留空')
    semantic_review: bool = Field(False, description='分类或相关性等语义结论须为 true，本地只能证明文件操作')


class ExecuteFilePlan(StrictModel):
    plan_id: str = Field(description='精确计划 ID；恢复仍使用同一 ID，禁止 latest')


def checksum(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _visible(sb, value, *, write=False):
    lexical = Path(value)
    if lexical.is_absolute() or '..' in lexical.parts:
        raise ValueError('计划路径必须是沙盒内相对路径：'+value)
    cursor = sb.root
    for part in lexical.parts:
        cursor = cursor / part
        if cursor.is_symlink(): raise ValueError('计划不能包含符号链接：'+value)
    path = sb.path(value, exact=True, write=write)
    if any(part.startswith('.') for part in Path(sb.rel(path)).parts if part != '.'):
        raise ValueError('计划不能直接引用隐藏对象：'+value)
    cursor = sb.root
    for part in Path(sb.rel(path)).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('计划不能包含符号链接：'+value)
    return path


def fingerprint(path):
    if not path.exists() and not path.is_symlink():
        raise ValueError('来源不存在，可能已被此前的操作移动或回收：'+path.name)
    before = signature(path)
    if path.is_file():
        result = dict(kind='file', stat=before, sha256=digest(path))
    elif path.is_dir() and not any(path.iterdir()):
        result = dict(kind='empty_directory', stat=before, sha256=None)
    else:
        raise ValueError('通用计划仅处理普通文件和空目录；非空目录应先明确其文件清单')
    if signature(path) != before:
        raise ValueError('生成文件快照期间对象变化，请重新规划')
    return result


def _same(path, expected, *, moved=False):
    try:
        actual = fingerprint(path)
        return (actual['kind'] == expected['kind'] and actual['sha256'] == expected['sha256']
                and (actual['stat'][:2] == expected['stat'][:2] if moved else actual['stat'] == expected['stat']))
    except (ValueError, OSError):
        return False


def _key(path):
    return unicodedata.normalize('NFKC', str(path)).casefold()


def prepare(runtime, args):
    sb = runtime.sb
    actions = [a.model_dump() for a in args.actions]
    classification = None
    if args.classification_plan_id:
        if actions or args.selection or args.classification_plan_id == 'latest':
            raise ValueError('分类计划只能使用精确标识，不能混合额外操作或选择集')
        from tools.classification import _plan, _move_plan
        cid, original = _plan(sb.artifacts, args.classification_plan_id)
        moves = _move_plan(sb, cid, original)
        classification = dict(id=cid, manifest=moves)
        if any(move['src'] == move['dst'] for move in moves):
            raise ValueError('此分类计划包含已在目标位置的文件，请先核对既有执行结果，不能重复执行')
        actions = [dict(effect='move', source=m['src'], destination=m['dst']) for m in moves]

    if args.selection:
        sel = args.selection.model_dump()
        if sel['effect'] == 'move' and not sel['destination']:
            raise ValueError('移动选择集需要 destination 目标目录')
        if runtime.current:
            from agent.constraints import within
            if not within(sb, sel['path'], runtime.current['read_roots']):
                raise ValueError('选择集超出已有任务读取范围：'+sel['path'])
        files, _, _ = selected(sb, sel)
        for src in files:
            dst = None
            if sel['effect'] == 'move':
                tail = Path(src).relative_to(sel['path']) if sel['preserve_tree'] else Path(src).name
                dst = (Path(sel['destination']) / tail).as_posix()
            actions.append(dict(effect=sel['effect'], source=src, destination=dst))
    actions = _normalize(runtime, actions)
    if not actions or len(actions) > 500:
        raise ValueError('计划必须包含 1–500 项明确操作，请缩小选择范围')
    pid = 'X'+uuid.uuid4().hex[:12]
    steps, dirs, sources, destinations = [], {}, set(), set()
    for action in actions:
        effect, src, dst = action['effect'], action['source'], action['destination']
        src = runtime._resolve(src) if src else None
        if runtime.current:
            from agent.constraints import within
            for value in (src, dst):
                if value and not within(sb, value, runtime.current['read_roots']):
                    raise ValueError('计划对象超出已有任务读取范围：'+value)
        source = _visible(sb, src, write=True) if src else None
        target = _visible(sb, dst, write=True) if dst else None
        src = sb.rel(source) if source else None
        dst = sb.rel(target) if target else None
        if source and _key(source) in sources:
            raise ValueError('来源重复：'+src)
        if source: sources.add(_key(source))
        if target and _key(target) in destinations:
            raise ValueError('目标重复或名称折叠后冲突：'+dst)
        if target: destinations.add(_key(target))
        if source and source == target:
            raise ValueError('来源与目标相同，无需执行：'+src)
        if effect == 'rename' and source.parent != target.parent:
            raise ValueError('rename 必须在原目录；跨目录使用 move')
        if effect == 'create':
            if target.exists() and not target.is_dir():
                raise ValueError('创建目标已被文件占用：'+dst)
        elif target and target.exists():
            raise ValueError('目标已存在，不会覆盖：'+dst)
        snap = fingerprint(source) if source else None
        if snap and snap['kind'] != 'file' and effect != 'trash':
            raise ValueError('移动/改名计划只接收普通文件；目录请先展开为明确文件清单')
        if target:
            parent = target if effect == 'create' else target.parent
            for p in reversed([parent, *parent.parents]):
                if p == sb.root or not p.is_relative_to(sb.root): continue
                if p.exists():
                    if not p.is_dir(): raise ValueError('目标父路径不是目录：'+sb.rel(p))
                else:
                    _visible(sb, sb.rel(p), write=True)
                    dirs.setdefault(sb.rel(p), dict(effect='create', source=None, destination=sb.rel(p), snapshot=None))
        if effect != 'create':
            if effect == 'trash': dst = f'.trash/{pid}_{len(steps)}_{source.name}'
            steps.append(dict(effect=effect, source=src, destination=dst, snapshot=snap))
    if sources & destinations:
        raise ValueError('计划包含互相依赖的来源与目标；请分为多个明确阶段，不能覆盖或循环移动')
    # A source directory cannot contain another action or target.
    for row in steps:
        if row['snapshot']['kind'] == 'empty_directory':
            p = sb.path(row['source'], exact=True)
            if any(sb.path(s['destination'], exact=True).is_relative_to(p) for s in [*dirs.values(), *steps] if s['destination'] and s is not row):
                raise ValueError('回收目录包含计划目标，请拆分阶段')
    steps = list(dirs.values()) + steps
    for i, row in enumerate(steps): row['id'] = f'{pid}:{i}'
    producers = ['file_plans'] + (['classification_plans'] if classification else [])
    # For an existing task, bindings are explicit or locally derived; neither grants access.
    task = runtime.current
    additions = {}
    if task:
        goal_ids = _attribute_steps(runtime, task, steps, args.goal_ids, producers)
        additions = _additions(runtime, task, steps)
    else:
        if args.goal_ids: raise ValueError('新计划的目标 ID 由本地生成，请省略 goal_ids')
        goal_ids = ['files']
        roots = list(dict.fromkeys(p for s in steps for p in (s['source'], s['destination'] if s['effect'] != 'trash' else None) if p))
        spec = TaskSubmission.model_validate(dict(
            goals=[dict(id='files', description=args.description, semantic_review=args.semantic_review or bool(classification),
                        conditions=[dict(kind='file_plan_verified', reference=pid)])],
            read_roots=roots or ['.'], write_roots=roots,
            allowed_effects=sorted({s['effect'] for s in steps})))
        task = runtime.store.submit(spec, runtime.instruction, runtime.run_id)
        runtime.current = task
        task['compiled_file_plan'] = pid
        runtime.adopt_observations()
        for row in steps: row['goal_ids'] = goal_ids
        runtime.store.checkpoint(task)
    payload = dict(task_id=task['id'], revision=task['revision'], goal_ids=goal_ids,
                   description=args.description, steps=steps, classification=classification)
    if additions: payload['scope_additions'] = additions
    plan = dict(id=pid, payload=payload, digest=checksum(payload), status='ready', progress={}, error='')
    _validate(runtime, plan)
    runtime.store.save_file_plan(plan)
    # Machine conditions bind here, deterministically: the plan to its goals and
    # a bridged classification plan to goals that verify classification plans.
    task['file_plan_bindings'] = dict(task.get('file_plan_bindings', {}), **{pid: dict(
        goal_ids=goal_ids, revision=task['revision'], pending=[])})
    if classification:
        for goal in task['goals']:
            if goal['id'] in goal_ids and any(c['kind'] in {'plan_ready', 'plan_applied'} and not c['reference']
                                              for c in goal['conditions']):
                task.setdefault('references', []).append(dict(
                    collection='classification_plans', id=classification['id'], goal_ids=[goal['id']], step_id=pid))
    runtime.store.checkpoint(task)
    return plan


def _normalize(runtime, actions):
    """`mv` semantics for a move whose destination is a directory.

    A destination names a directory when it exists as one, when this plan
    creates it, or when several moves share it (one file path cannot receive
    them all). The file keeps its name; the manifest shows the full target.
    """
    from collections import Counter
    sb = runtime.sb
    creates = {a['destination'] for a in actions if a['effect'] == 'create'}
    shared = Counter(a['destination'] for a in actions if a['effect'] == 'move')
    result, seen = [], set()
    for action in actions:
        action = dict(action)
        if action['effect'] == 'create':
            # Declaring a directory twice, or one the plan creates anyway, is one step.
            if action['destination'] in seen: continue
            seen.add(action['destination'])
        elif action['effect'] == 'move' and action['source'] and action['destination']:
            dst = action['destination'].rstrip('/')
            try: is_dir = sb.path(dst, exact=True).is_dir()
            except Exception: is_dir = False
            if is_dir or dst in creates or shared[action['destination']] > 1:
                name = Path(runtime._resolve(action['source'])).name
                action['destination'] = (Path(dst) / name).as_posix()
        result.append(action)
    return result


def _additions(runtime, task, steps):
    """Exact objects this plan writes outside the task's registered write roots.

    write_roots are the model's reading of the request, not the user's words,
    so a plan may go beyond them by listing the extra paths on its confirmation.
    Effects are not widened (a read-only task stays read-only), and user limits
    (constraints) still apply in full.
    """
    from agent.constraints import within
    if not task['allowed_effects']: return {}
    roots = []
    for s in steps:
        for path in (s['source'], s['destination'] if s['effect'] != 'trash' else None):
            if path and not within(runtime.sb, path, task['write_roots']) and path not in roots:
                roots.append(path)
    return dict(write_roots=roots) if roots else {}


def _granted(task, plan):
    """The task as this plan's confirmation would leave it."""
    extra = plan['payload'].get('scope_additions') or {}
    if not extra: return task
    return dict(task, write_roots=list(dict.fromkeys(task['write_roots'] + extra['write_roots'])))


def _grant(task, plan, run_id):
    extra = plan['payload'].get('scope_additions') or {}
    if not extra: return
    task['write_roots'] = _granted(task, plan)['write_roots']
    task.setdefault('scope_grants', []).append(dict(plan_id=plan['id'], run_id=run_id, **extra))


def conflicts(runtime, plan):
    """Completion conditions this plan leaves unmet or contradicts, as hints.

    Advisory only: a later plan may still place files, so nothing is rejected;
    the model learns of a contract/plan mismatch before executing, not at finish.
    """
    from agent.completion import condition_key
    task, sb = runtime.current, runtime.sb
    goal_ids = task.get('file_plan_bindings', {}).get(plan['id'], {}).get('goal_ids', [])
    steps = plan['payload']['steps']
    sources = {s['source'] for s in steps if s['effect'] in {'move', 'rename'}}
    removed = {s['source'] for s in steps if s['effect'] in {'move', 'rename', 'trash'}}
    created = {s['destination'] for s in steps if s.get('destination')}
    hints = []
    for goal in task['goals']:
        for index, c in enumerate(goal['conditions']):
            if c['kind'] == 'path_exists' and c['path'] in removed:
                hints.append(f"目标 {goal['id']} 要求 {c['path']} 存在，此计划会把它移走")
            elif c['kind'] == 'path_absent' and c['path'] in created:
                hints.append(f"目标 {goal['id']} 要求 {c['path']} 不存在，此计划会创建它")
            snapshot = task.get('completion_snapshots', {}).get(condition_key(goal['id'], index))
            if goal['id'] not in goal_ids or c['kind'] != 'files_placed' or not snapshot or snapshot['error']:
                continue
            missing = [row['dst'] for row in snapshot['files'] if row['src'] != row['dst']
                       and row['src'] not in sources and not sb.path(row['dst'], exact=True).exists()]
            if missing:
                hints.append(f"目标 {goal['id']} 的 files_placed 执行前清单仍有 {len(missing)} 项未归集（如 {'、'.join(missing[:3])}）")
    return hints


def _attribute_steps(runtime, task, steps, requested, producers):
    known = {g['id'] for g in task['goals']}
    if set(requested) - known:
        raise ValueError('goal_ids 引用了任务中不存在的目标：'+'、'.join(sorted(set(requested) - known)))
    candidates = requested or [g['id'] for g in task['goals']]
    explicit = bool(requested) or len(candidates) == 1
    goals = {g['id']: g for g in task['goals']}
    unresolved = []
    for row in steps:
        if row['effect'] == 'create': continue
        op = dict(effect=row['effect'], source=row['source'],
                  destination=row['destination'] if row['effect'] != 'trash' else None)
        row['goal_ids'] = attribute(task, runtime.sb, op, candidates, producers)
        if not explicit and row['goal_ids'] == candidates and not any(
                claims(task, runtime.sb, gid, i, c, op, producers)
                for gid in candidates for i, c in enumerate(goals[gid]['conditions'])):
            unresolved.append(row['source'])
    for row in steps:
        if row['effect'] != 'create': continue
        # A parent directory serves the actions placed inside it.
        served = [g for s in steps if s['effect'] != 'create' and s['destination'] and
                  Path(s['destination']).is_relative_to(row['destination']) for g in s['goal_ids']]
        op = dict(effect='create', source=None, destination=row['destination'])
        row['goal_ids'] = list(dict.fromkeys(served)) or attribute(task, runtime.sb, op, candidates, producers)
        if not served and not explicit and row['goal_ids'] == candidates:
            unresolved.append(row['destination'])
    if unresolved:
        raise ValueError('无法从完成条件确定以下动作服务的目标，请提供 goal_ids：'+'、'.join(unresolved[:8]))
    return list(dict.fromkeys(g for row in steps for g in row['goal_ids']))


def _operations(plan, pending=None):
    rows = plan['payload']['steps'] if pending is None else pending
    producers = ['file_plans'] + (['classification_plans'] if plan['payload'].get('classification') else [])
    return [dict(effect=s['effect'], source=s['source'],
                 destination=s['destination'] if s['effect'] != 'trash' else None,
                 goal_ids=s.get('goal_ids') or plan['payload']['goal_ids'], producers=producers) for s in rows]


def _validate(runtime, plan, pending=None):
    task = runtime.current
    payload = plan['payload']
    if checksum(payload) != plan['digest']: raise ValueError('计划内容摘要不一致')
    if not task or task['id'] != payload['task_id']:
        raise ValueError('计划不属于当前任务 '+((task or {}).get('id') or '（无）')+'，不能切换任务执行')
    if task['revision'] != payload['revision']:
        raise ValueError(f"任务版本已变化（计划基于第 {payload['revision']} 版，当前第 {task['revision']} 版），"
                         '必须重新预览计划')
    if task.get('unresolved_questions'): raise ValueError('任务仍有待确认问题')
    if task.get('constraint_failures'): raise ValueError('任务有未处理的约束失败')
    require_valid_snapshots(task)
    # Every action carries its own goals; no global step is borrowed for the plan.
    # Scope the plan lists for confirmation counts; user limits apply in full.
    runtime.current = _granted(task, plan)
    try: runtime.validate_operations(_operations(plan, pending))
    finally: runtime.current = task


def _check_remaining(runtime, plan):
    sb = runtime.sb
    for s in plan['payload']['steps']:
        if s['id'] in plan['progress']: continue
        if s['source']:
            if not _same(_visible(sb, s['source'], write=True), s['snapshot']):
                raise ValueError('源文件版本变化或不存在：'+s['source'])
        if s['effect'] == 'trash' and (sb.root/'.trash').is_symlink():
            raise ValueError('回收区不能是符号链接')
        target = sb.path(s['destination'], exact=True)
        if s['effect'] != 'trash': _visible(sb, s['destination'], write=True)
        if target.exists():
            raise ValueError('计划目标已存在或计划过期：'+s['destination'])


@contextmanager
def library_lock(sb):
    # Advisory lock serializes this executor across conversations and processes.
    # Legacy executors still recheck per action; they do not share this lock yet.
    import fcntl
    folder = sb.root/'.oplog'; folder.mkdir(exist_ok=True)
    if folder.is_symlink() or (folder/'file-plans.lock').is_symlink():
        raise ValueError('内部执行目录或锁不能是符号链接')
    with (folder/'file-plans.lock').open('a') as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('资料库已有计划正在执行，请稍后重试')
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


def _record(oplog, step):
    # Plan IDs make journal repair idempotent after a process interruption.
    rows = [json.loads(x) for x in oplog.path.read_text().splitlines() if x.strip()] if oplog.path.exists() else []
    if not any(row.get('action_id') == step['id'] for row in rows):
        oplog.record('make_dir' if step['effect']=='create' else step['effect'],
                     step['source'] or step['destination'], step['destination'], action_id=step['id'])


def _recover_inflight(runtime, plan):
    sid = plan['inflight']
    step = next((s for s in plan['payload']['steps'] if s['id'] == sid), None)
    if not step: raise ValueError('中断动作不存在，不能恢复此计划')
    if step['effect'] == 'trash' and (runtime.sb.root/'.trash').is_symlink():
        raise ValueError('回收区不能是符号链接')
    target = runtime.sb.path(step['destination'], exact=True)
    source = _visible(runtime.sb, step['source']) if step['source'] else None
    if not target.exists() and (source is None or _same(source, step['snapshot'])):
        # An intent was saved, but no physical mutation is visible. Resume only
        # after the ordinary preflight and fresh confirmation below.
        plan.pop('inflight'); runtime.store.save_file_plan(plan)
        return
    log = Path(plan.get('inflight_log', ''))
    records = []
    if log.parent == runtime.sb.root/'.oplog' and log.is_file() and not log.is_symlink():
        records = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    op = 'make_dir' if step['effect']=='create' else step['effect']
    recorded = any(r.get('action_id') == sid and r['op'] == op and
                   r['src'] == step['source'] and r['dst'] == step['destination'] for r in records)
    if source and not source.exists() and recorded and _same(target, step['snapshot'], moved=True):
        progress = dict(run_id=plan['inflight_run'], log_path=str(log), stat=signature(target))
        plan['progress'][sid] = progress
        plan.pop('inflight'); runtime.store.save_file_plan(plan)
        row = dict(op=op,src=step['source'],dst=step['destination'],action_id=sid,
                   run_id=progress['run_id'],goal_ids=step.get('goal_ids') or plan['payload']['goal_ids'],step_id=sid,
                   kind='文件夹' if step['snapshot']['kind']=='empty_directory' else '文件')
        if progress['run_id'] == runtime.run_id: runtime.operations.append(row)
        else: runtime.current.setdefault('operation_history', []).append(row)
        if getattr(runtime.sb,'artifacts',None):runtime.sb.artifacts.relocate(step['source'],step['destination'])
        return
    raise ValueError('上次操作中断，存在结果未确认的动作 '+sid+'；请核对日志及文件位置，禁止自动重放')


def execute(runtime, plan_id, oplog, confirmer):
    if not oplog or not confirmer: raise ValueError('执行计划需要日志和明确的确认策略')
    with library_lock(runtime.sb):
        plan = runtime.store.file_plan(plan_id)
        task = runtime.store.get(plan['payload']['task_id'])
        if runtime.current and runtime.current['id'] != task['id']:
            raise ValueError('计划不属于当前任务，不能切换任务绕过约束')
        runtime.current = task
        if task['revision'] != plan['payload']['revision'] and _rebasable(plan):
            plan = _rebase(runtime, plan, task)
            plan_id = plan['id']
        known = {row.get('action_id') for row in task.get('operation_history', [])+task.get('operations', [])+runtime.operations if row.get('action_id')}
        for step in plan['payload']['steps']:
            progress = plan['progress'].get(step['id'])
            if not progress or step['id'] in known: continue
            row = dict(op='make_dir' if step['effect']=='create' else step['effect'],
                       src=step['source'] or step['destination'], dst=step['destination'],
                       action_id=step['id'], run_id=progress['run_id'], goal_ids=step.get('goal_ids') or plan['payload']['goal_ids'],
                       step_id=step['id'], kind='文件夹' if step['effect']=='create' or step['snapshot']['kind']=='empty_directory' else '文件')
            task.setdefault('operations', []).append(row)
        history = task.setdefault('operation_history', [])
        def identity(row):
            return row.get('action_id') or (row.get('run_id'), row.get('seq'), row['op'], row['src'], row['dst'])
        history_ids = {identity(row) for row in history}
        current_ids = {identity(row) for row in runtime.operations}
        for row in task.get('operations', []):
            if row.get('run_id') != runtime.run_id:
                if identity(row) not in history_ids:
                    history.append(row); history_ids.add(identity(row))
            elif identity(row) not in current_ids:
                runtime.operations.append(row); current_ids.add(identity(row))
        task['operations'] = runtime.operations
        try:
            if task['revision'] != plan['payload']['revision']:
                # Unstarted plans were rebased above; this one already ran in part.
                raise ValueError(f"任务版本已变化（计划基于第 {plan['payload']['revision']} 版，当前第 {task['revision']} 版），"
                                 f"且此计划已执行 {len(plan['progress'])}/{len(plan['payload']['steps'])} 项，不能自动迁移。"
                                 '请用 inspect_file_plan 查看已完成项，只为剩余对象准备新计划，不要重复已完成的操作')
            if checksum(plan['payload']) != plan['digest']:
                raise ValueError('计划内容摘要不一致')
            if plan.get('inflight'):
                _recover_inflight(runtime, plan)
            remaining = [s for s in plan['payload']['steps'] if s['id'] not in plan['progress']]
            _validate(runtime, plan, remaining)
            if plan['status'] == 'completed':
                ok, detail = verify_plan(plan, runtime.sb)
                if not ok: raise ValueError(detail)
                task.setdefault('file_plan_receipts', {})[plan_id] = copy.deepcopy(plan)
                runtime.store.checkpoint(task)
                return 'OK: 此计划已经执行并核验，无重复操作'
            _check_remaining(runtime, plan)
            # Completed actions must still match before resuming the rest.
            ok, detail = verify_plan(plan, runtime.sb, partial=True)
            if not ok: raise ValueError(detail)
            review = dict(plan_id=plan_id, digest=plan['digest'], steps=[
                {k:s[k] for k in ('effect','source','destination')} for s in remaining])
            if plan['payload'].get('scope_additions'):
                review['scope_additions'] = plan['payload']['scope_additions']
            if not confirmer.allow('file_plan', review, file_count=len(remaining), force=True,
                                   **({'notes': runtime.text_limits()} if runtime.text_limits() else {})):
                raise ValueError('用户拒绝执行此计划，未执行剩余操作')
            _grant(task, plan, runtime.run_id)
            runtime.store.checkpoint(task)
            _validate(runtime, plan, remaining)
            _check_remaining(runtime, plan)
            ok, detail = verify_plan(plan, runtime.sb, partial=True)
            if not ok: raise ValueError(detail)
            plan['status'] = 'executing'; plan['error'] = ''
            runtime.store.save_file_plan(plan)
            for s in remaining:
                _validate(runtime, plan, [s])
                # Recheck immediately before each mutation, after any user wait.
                if s['source'] and not _same(_visible(runtime.sb, s['source'], write=True), s['snapshot']):
                    raise ValueError('执行期间源文件发生变化：'+s['source'])
                if s['effect'] == 'trash' and (runtime.sb.root/'.trash').is_symlink():
                    raise ValueError('回收区不能是符号链接')
                target = runtime.sb.path(s['destination'], exact=True)
                if s['effect'] != 'trash': _visible(runtime.sb, s['destination'], write=True)
                if target.exists(): raise ValueError('目标被其他操作占用：'+s['destination'])
                plan['inflight'] = s['id']
                plan['inflight_log'] = str(oplog.path)
                plan['inflight_run'] = runtime.run_id
                runtime.store.save_file_plan(plan)
                if s['effect'] == 'create': target.mkdir()
                else:
                    if s['effect'] == 'trash': target.parent.mkdir(exist_ok=True)
                    from safety.atomic import rename_no_replace
                    rename_no_replace(runtime.sb.path(s['source'], exact=True), target)
                _record(oplog, s)
                progress = dict(run_id=runtime.run_id, log_path=str(oplog.path), stat=signature(target))
                plan['progress'][s['id']] = progress
                plan.pop('inflight', None)
                runtime.store.save_file_plan(plan)
                row = dict(op='make_dir' if s['effect']=='create' else s['effect'],
                           src=s['source'] or s['destination'], dst=s['destination'],
                           run_id=runtime.run_id, action_id=s['id'], goal_ids=s.get('goal_ids') or plan['payload']['goal_ids'], step_id=s['id'],
                           kind='文件夹' if s['effect']=='create' or s['snapshot']['kind']=='empty_directory' else '文件')
                runtime.operations.append(row)
                runtime.current['operations'] = runtime.operations
                runtime.store.checkpoint(runtime.current)
                if s['source'] and getattr(runtime.sb, 'artifacts', None):
                    runtime.sb.artifacts.relocate(s['source'], s['destination'])
            ok, detail = verify_plan(plan, runtime.sb)
            if not ok: raise ValueError(detail)
            plan['status'] = 'completed'
            runtime.store.save_file_plan(plan)
            classification = plan['payload'].get('classification')
            artifacts = getattr(runtime.sb,'artifacts',None)
            if classification and artifacts and classification['id'] in artifacts.data['classification_plans']:
                original = artifacts.data['classification_plans'][classification['id']]
                original['status'] = 'applied'
                original['last_execution'] = dict(run_id=runtime.run_id, verified=True,
                    manifest=classification['manifest'],
                    moved=[(s['source'],s['destination']) for s in plan['payload']['steps'] if s['effect']=='move'],
                    created=[s['destination'] for s in plan['payload']['steps'] if s['effect']=='create'], error='')
                artifacts._save()

            runtime.current.setdefault('file_plan_receipts', {})[plan_id] = copy.deepcopy(plan)
            _binding(runtime.current, plan)['pending'] = []
            runtime.store.checkpoint(runtime.current)
            rebased = f"（任务已修订，原计划 {plan['rebased_from']} 已按第 {task['revision']} 版重新生成）" if plan.get('rebased_from') else ''
            return f"OK: plan_id={plan_id}{rebased}；本地完成并核验 {len(plan['progress'])} 项操作，无需逐文件重新调用工具"
        except (ValueError, OSError) as exc:
            plan['status'] = 'blocked'; plan['error'] = str(exc)
            runtime.store.save_file_plan(plan)
            if plan['progress'] or plan.get('inflight'):
                # A half-executed plan cannot satisfy its goals until the rest is done.
                from agent.verifier import _step_key
                _binding(runtime.current, plan)['pending'] = [
                    list(_step_key(s)) for s in plan['payload']['steps'] if s['id'] not in plan['progress']]
            # The controller records the blocker with this plan as its object.
            runtime.store.checkpoint(runtime.current)
            raise ValueError(str(exc)) from exc


def _rebasable(plan):
    return plan['status'] in {'ready', 'blocked'} and not plan['progress'] and not plan.get('inflight')


def _rebase(runtime, plan, task):
    """An unstarted plan, re-derived against the revised task under a new ID.

    Snapshots, destinations and steps are unchanged; goals, scope additions and
    the digest are recomputed, then the ordinary preflight and a fresh
    confirmation follow. Anything the new revision rejects is rejected.
    """
    old = plan['payload']
    known = {g['id'] for g in task['goals']}
    steps = [{k: v for k, v in s.items() if k != 'goal_ids'} for s in copy.deepcopy(old['steps'])]
    pid = 'X'+uuid.uuid4().hex[:12]
    for i, row in enumerate(steps): row['id'] = f'{pid}:{i}'
    producers = ['file_plans'] + (['classification_plans'] if old.get('classification') else [])
    goal_ids = _attribute_steps(runtime, task, steps, [g for g in old['goal_ids'] if g in known], producers)
    payload = dict(old, revision=task['revision'], goal_ids=goal_ids, steps=steps)
    payload.pop('scope_additions', None)
    additions = _additions(runtime, task, steps)
    if additions: payload['scope_additions'] = additions
    new = dict(id=pid, payload=payload, digest=checksum(payload), status='ready', progress={}, error='',
               rebased_from=plan['id'])
    plan.update(status='superseded', error='任务已修订，计划已重新生成为 '+pid)
    runtime.store.save_file_plan(plan)
    runtime.store.save_file_plan(new)
    bindings = task.setdefault('file_plan_bindings', {})
    bindings.pop(plan['id'], None)
    bindings[pid] = dict(goal_ids=goal_ids, revision=task['revision'], pending=[])
    runtime.store.checkpoint(task)
    return new


def _binding(task, plan):
    return task.setdefault('file_plan_bindings', {}).setdefault(plan['id'], dict(
        goal_ids=plan['payload']['goal_ids'], revision=plan['payload']['revision'], pending=[]))


def verify_plan(plan, sb, partial=False):
    if not partial and (plan['status'] not in {'completed','executing'} or len(plan['progress']) != len(plan['payload']['steps'])):
        return False, '计划尚未全部执行：'+plan['id']
    for s in plan['payload']['steps']:
        if s['id'] not in plan['progress']: continue
        if s['effect'] == 'trash' and (sb.root/'.trash').is_symlink():
            raise ValueError('回收区不能是符号链接')
        target = sb.path(s['destination'], exact=True)
        if s['effect'] == 'create':
            if not _visible(sb, s['destination']).is_dir() or signature(target)[:2] != plan['progress'][s['id']]['stat'][:2]:
                return False, '计划目录缺失或已替换：'+s['destination']
        else:
            # Internal trash destinations are compiler-generated, never model inputs.
            if s['effect'] != 'trash': _visible(sb, s['destination'])
            if not _same(target, s['snapshot'], moved=True): return False, '目标内容或身份不符：'+s['destination']
            if _visible(sb, s['source']).exists(): return False, '原位置仍存在：'+s['source']
    return True, '操作清单的位置与内容核验通过'


def markdown(plan):
    lines = [f"计划 {plan['id']} · {plan['status']} · {len(plan['payload']['steps'])} 项", '',
             '| 操作 | 来源 | 目标 |', '| --- | --- | --- |']
    for s in plan['payload']['steps']:
        lines.append('| '+' | '.join(label(x or '') for x in (s['effect'], s['source'], '回收区' if s['effect']=='trash' else s['destination']))+' |')
    if plan['error']: lines += ['', '阻塞原因：'+plan['error']]
    return '\n'.join(lines)
