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
    include_metadata: bool = Field(False, description='仅 trash 文件夹：同意连同其中的系统元数据（.DS_Store 等）一起回收')
    include_hidden: list[str] = Field(default_factory=list, max_length=20,
        description='仅 trash 文件夹：用户已通过 ask_user 同意一起回收的其他隐藏文件，逐个写出完整路径')

    @model_validator(mode='after')
    def endpoints(self):
        if self.effect != 'create' and not self.source:
            raise ValueError('此操作需要 source')
        if self.effect != 'trash' and not self.destination:
            raise ValueError('此操作需要 destination（完整目标路径）')
        if self.effect == 'create' and self.source or self.effect == 'trash' and self.destination:
            raise ValueError('create 只指定 destination；trash 只指定 source')
        if self.effect != 'trash' and (self.include_metadata or self.include_hidden):
            raise ValueError('include_metadata / include_hidden 只用于回收文件夹')
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
    task_id: str | None = Field(None, description='仅在续做以前的任务时填写，复制完整 ID，沿用原限制；本轮已调用 start_task 时不要填')
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


def not_empty(path, label=None):
    """Why a directory is not empty, in facts the model cannot see itself."""
    entries = sorted(path.iterdir(), key=lambda p: p.name)
    hidden = [p.name for p in entries if p.name.startswith('.')]
    visible = [p.name for p in entries if not p.name.startswith('.')]
    name = label or path.name
    if not visible:
        return (f"{name} 不是空目录：只含隐藏文件 {'、'.join(hidden[:5])}（列表工具不显示隐藏项）。"
                '本工具不处理隐藏文件，不能回收此文件夹；请如实告诉用户，可由用户在 Finder 中手动删除，或保留该文件夹，不要再换路径重试')
    text = f"{name} 不是空目录：含 {len(visible)} 个可见项（如 {'、'.join(visible[:5])}）"
    if hidden: text += f"，另有隐藏文件 {'、'.join(hidden[:5])}"
    return text + '。通用计划仅处理普通文件和空目录，非空目录应先明确其文件清单'


def fingerprint(path, label=None):
    if not path.exists() and not path.is_symlink():
        raise ValueError('来源不存在，可能已被此前的操作移动或回收：'+path.name)
    before = signature(path)
    if path.is_file():
        result = dict(kind='file', stat=before, sha256=digest(path))
    elif path.is_dir() and not any(path.iterdir()):
        result = dict(kind='empty_directory', stat=before, sha256=None)
    else:
        raise ValueError(not_empty(path, label))
    if signature(path) != before:
        raise ValueError('生成文件快照期间对象变化，请重新规划')
    return result


# Risk tiers for what a recycled folder still holds (decided locally, never by the model):
#   metadata  system-generated files from the reviewed list (agent/system_files.json) that
#             pass their content check: recycled with include_metadata, shown on the card
#   hidden    other dot-files (.env, ...): only after the user agreed via ask_user, listed exactly
#   busy      lock files of open documents: refused until the program is closed
#   special   hidden folders, symlinks, packages, volume system folders: never recycled
FOLDER_KINDS = {'empty_directory', 'directory_tree'}
PACKAGES = ('.app', '.bundle', '.framework', '.photoslibrary', '.pages', '.numbers', '.key', '.rtfd')


def _tree_digest(folder):
    rows = sorted((p.relative_to(folder).as_posix(), digest(p)) for p in folder.rglob('*') if p.is_file())
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def scan_tree(folder):
    """Everything under a folder, by risk tier; paths relative to the folder.

    Order matters: structure and the reviewed list come before "is it hidden",
    so visible-named system files (Thumbs.db, desktop.ini, __MACOSX) are known.
    """
    from agent.system_files import match, verified, metadata_dir_ok
    out = dict(visible=[], dirs=[], metadata={}, hidden={}, special=[], busy=[], notes={})
    for base, dirnames, files in os.walk(folder, followlinks=False):
        base = Path(base)
        for name in sorted(dirnames):
            p = base / name
            rel = p.relative_to(folder).as_posix()
            entry = None if p.is_symlink() else match(name, is_dir=True)
            if entry and entry['tier'] == 'metadata_dir':
                dirnames.remove(name)
                if metadata_dir_ok(p, entry):
                    out['metadata'][rel + '/'] = _tree_digest(p)
                    if entry['note']: out['notes'][rel + '/'] = entry['note']
                else:
                    out['special'].append(rel)  # a generated folder mixed with other content
            elif p.is_symlink() or entry or name.startswith('.') or name.lower().endswith(PACKAGES):
                out['special'].append(rel); dirnames.remove(name)
            else:
                out['dirs'].append(rel)
        for name in sorted(files):
            p = base / name
            rel = p.relative_to(folder).as_posix()
            if p.is_symlink() or not p.is_file():
                out['special'].append(rel); continue
            entry = match(name, is_dir=False)
            tier = entry['tier'] if entry else None
            if tier == 'busy': out['busy'].append(rel)
            elif tier == 'special': out['special'].append(rel)
            elif tier == 'metadata' and verified(p, entry):
                out['metadata'][rel] = digest(p)
                if entry['note']: out['notes'][rel] = entry['note']
            elif name.startswith('.'):
                out['hidden'][rel] = digest(p)
                if tier == 'metadata': out['notes'][rel] = '名称像系统文件，但内容或大小与系统生成的不符'
                elif entry and entry['note']: out['notes'][rel] = entry['note']
            else:
                out['visible'].append(rel)
    return out


def _names(rows, limit=5):
    rows = list(rows)
    return '、'.join(rows[:limit]) + ('等' if len(rows) > limit else '')


def tree_snapshot(folder, src, moved_out, action, consent):
    """A folder recycled with what this plan leaves in it, or a precise refusal."""
    t = scan_tree(folder)
    if t['busy']:
        from agent.system_files import match
        why = '、'.join(dict.fromkeys(match(Path(b).name, is_dir=False)['note'] for b in t['busy']))
        raise ValueError(f"{src} 中有文件正被程序打开（{_names(t['busy'])}：{why}）。请告诉用户先保存并关闭相关程序后再试；"
                         '锁文件会在关闭后自动消失，不要换路径重试，也不要单独回收锁文件')
    if t['special']:
        raise ValueError(f"{src} 含特殊对象 {_names(t['special'])}（隐藏文件夹、符号链接或应用包），"
                         '可能是代码仓库或程序目录，工具不会回收此文件夹。请如实告诉用户由用户自行处理；其余操作可以照常进行')
    moving = {p for p in t['visible'] if f'{src}/{p}' in moved_out}
    left = [p for p in t['visible'] if p not in moving]
    if left:
        raise ValueError(not_empty(folder, src) if not moving else
                         f"{src} 还有 {len(left)} 个可见文件不在本计划的移动中（如 {_names(left)}），回收它会连带删除这些文件；"
                         '如需回收，先在计划中移走它们，或只回收其中的空子文件夹')
    if t['metadata'] and not action.get('include_metadata'):
        raise ValueError(f"{src} 含系统元数据文件 {_names(t['metadata'])}（macOS/Windows 自动生成，列表工具不显示）。"
                         '若要连同回收，在这条 trash 动作中设置 include_metadata=true，确认卡片会把它们列给用户')
    sensitive = hidden_reports(folder, t['hidden'])
    if t['hidden']:
        listed = {Path(p).relative_to(src).as_posix() if Path(p).is_relative_to(src) else p
                  for p in action.get('include_hidden') or []}
        agreed = any(Path(src) == Path(c) or Path(src).is_relative_to(c) for c in consent)
        if not agreed:
            from agent.secrets_scan import describe
            # Kind and line only: secret values and even masked prefixes stay local.
            found = '；'.join(describe(f'{src}/{rel}', r, masked=False) for rel, r in sensitive.items())
            raise ValueError(f"{src} 含非系统隐藏文件 {_names(t['hidden'])}，可能是配置或密钥。"
                             + (f"本地检查：{found}。建议默认保留。" if found else '')
                             + f"回收前必须先调用 ask_user 询问用户（subject 填 {src}，表示同意的选项设 consent=include_hidden）；不要猜测用户意愿")
        if listed != set(t['hidden']):
            raise ValueError(f"include_hidden 必须逐个列出 {src} 中的全部隐藏文件，不多不少："
                             + _names([f'{src}/{p}' for p in t['hidden']], 20))
    elif action.get('include_hidden'):
        raise ValueError(f'{src} 没有需要 include_hidden 的隐藏文件')
    return dict(kind='directory_tree', stat=signature(folder), sha256=None, moved_out=sorted(moving),
                dirs=t['dirs'], files={**t['metadata'], **t['hidden']},
                metadata=sorted(t['metadata']), hidden=sorted(t['hidden']), notes=t['notes'],
                sensitive={rel: dict(name_rule=r['name_rule'], findings=r['findings']) for rel, r in sensitive.items()})


def hidden_reports(folder, hidden):
    """Secret-scan reports for a folder's hidden files; only files with a finding.
    Raises the warning, never the permission: these files still need consent."""
    from agent.secrets_scan import scan_file, is_sensitive
    reports = {rel: scan_file(Path(folder) / rel) for rel in hidden}
    return {rel: r for rel, r in reports.items() if is_sensitive(r)}


def _tree_same(path, expected, pending):
    """The folder holds what was confirmed. Before the plan's moves (pending) the
    files they will move may still be there; afterwards nothing visible may be.
    Finder may add metadata at any time; other new contents stop the plan."""
    if not path.is_dir() or path.is_symlink(): return False
    t = scan_tree(path)
    if t['special'] or t['busy'] or not set(t['dirs']) <= set(expected['dirs']): return False
    if not set(t['visible']) <= (set(expected['moved_out']) if pending else set()): return False
    files = {**t['metadata'], **t['hidden']}
    for rel, sha in expected['files'].items():
        if rel in expected['metadata'] and rel not in files: continue  # Finder may remove its own files
        if files.get(rel) != sha: return False
    return all(rel in expected['files'] or rel in t['metadata'] for rel in files)


def _same(path, expected, *, moved=False, pending=False):
    if expected['kind'] == 'directory_tree':
        try: return _tree_same(path, expected, pending)
        except OSError: return False
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
    chosen = getattr(runtime, 'chosen_option', None)
    if chosen is not None:
        _within_choice(runtime, chosen, actions)  # before any other check, so the reason is this one
    if not actions and args.selection and not args.actions and runtime.current:
        return _empty_selection(runtime, args)
    if not actions or len(actions) > 500:
        raise ValueError('计划必须包含 1–500 项明确操作，请缩小选择范围')
    pid = 'X'+uuid.uuid4().hex[:12]
    steps, dirs, sources, destinations = [], {}, set(), set()
    moved_out = {runtime._resolve(a['source']) for a in actions if a['effect'] in {'move', 'rename'} and a['source']}
    consent = getattr(runtime, 'hidden_consent', set())
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
        if source and source.is_file():
            from agent.system_files import match
            entry = match(source.name, is_dir=False)
            if entry and entry['tier'] == 'busy':
                raise ValueError(f"{src} 是锁文件（{entry['note']}），表明对应文档正被程序打开。请告诉用户先保存并关闭相关程序；"
                                 '计划中不要包含锁文件，关闭程序后它会自动消失')
        if effect == 'trash' and source.is_dir() and not source.is_symlink() and any(source.iterdir()):
            snap = tree_snapshot(source, src, moved_out, action, consent)
            if snap['hidden']:
                # Where the consent came from decides what the confirmation card says.
                consent_sources = getattr(runtime, 'consent_source', {})
                snap['consent_source'] = next((v for f, v in consent_sources.items()
                                               if Path(src) == Path(f) or Path(src).is_relative_to(f)), None)
        elif effect in {'move', 'rename'} and source.is_dir() and not source.is_symlink():
            snap = folder_snapshot(source, src, moved_out)
        else:
            snap = fingerprint(source, src) if source else None
        if snap and snap['kind'] not in {'file', 'directory_tree'} and effect != 'trash':
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
    # A folder recycled whole carries its nested folders: a separate trash of one
    # of them is the same effect, merged. Recycled folders go last, after the
    # moves that empty them.
    folders = [Path(s['source']) for s in steps if s['effect'] == 'trash' and s['snapshot']['kind'] != 'file']
    steps = [s for s in steps if not (s['effect'] == 'trash' and any(
        Path(s['source']) != f and Path(s['source']).is_relative_to(f) for f in folders))]
    steps.sort(key=lambda s: s['effect'] == 'trash' and s['snapshot']['kind'] == 'directory_tree')
    # A folder moved or recycled cannot hold another target, at either end.
    for row in steps:
        if row['snapshot']['kind'] in FOLDER_KINDS:
            ends = [sb.path(row['source'], exact=True)]
            if row['effect'] != 'trash': ends.append(sb.path(row['destination'], exact=True))
            inside = [s['destination'] for s in [*dirs.values(), *steps] if s['destination'] and s is not row
                      and any(sb.path(s['destination'], exact=True).is_relative_to(p) for p in ends)]
            if inside:
                raise ValueError(f"本计划移动或回收的文件夹 {row['source']} 内还有计划目标（如 {_names(inside)}），"
                                 '请拆分阶段：先执行这一个计划，再为后续操作准备新计划')
    steps = _order(sb, steps)
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
            if is_dir:
                try: is_dir = not os.path.samefile(sb.path(runtime._resolve(action['source']), exact=True), sb.path(dst, exact=True))
                except OSError: pass
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


def _within_choice(runtime, chosen, steps):
    """The plan after the user picked an option stays inside that option's declared actions."""
    declared = [dict(a, path=runtime._resolve(a['path'])) for a in chosen['actions']]
    def allowed(s):
        source = runtime._resolve(s['source']) if s['source'] else None
        for a in declared:
            if s['effect'] == 'trash' and a['effect'] == 'trash' and a['path'] == source: return True
            if s['effect'] in {'move', 'rename'} and a['effect'] in {'move', 'rename'} and a['path'] == source:
                dst = a.get('destination')
                if not dst or s['destination'] == dst or Path(s['destination']).parent == Path(dst): return True
        return False
    extra = [f"{s['effect']} {s['source']}" for s in steps if s['effect'] != 'create' and not allowed(s)]
    if extra:
        raise ValueError(f"计划超出用户所选选项 {chosen['key']}（{chosen['label']}）的范围：{'、'.join(extra[:3])}。"
                         '请只执行该选项声明的操作；需要更多操作时先向用户说明')


def folder_snapshot(folder, src, moved_out):
    """A folder moved or renamed after this plan has moved its files out.

    Only what travels harmlessly may stay: empty subfolders and verified system
    metadata. A folder still holding files is not moved whole, so every file
    keeps its own snapshot and line on the confirmation card.
    """
    t = scan_tree(folder)
    if t['busy'] or t['special']:
        raise ValueError(f"{src} 含锁文件或特殊对象 {_names(t['busy'] + t['special'])}（正被打开的文档、隐藏文件夹、符号链接或应用包），"
                         '工具不移动此文件夹。请如实告诉用户由用户处理；其余操作可以照常进行')
    moving = sorted(p for p in t['visible'] if f'{src}/{p}' in moved_out)
    left = [p for p in t['visible'] if p not in moving]
    if left:
        raise ValueError(f"{src} 还有 {len(left)} 个可见文件不在本计划的移动中（如 {_names(left)}）。"
                         '移动或改名文件夹前，须在同一计划中先把其中的文件逐个移出；不想动这些文件时，请改为只移动需要的文件')
    if t['hidden']:
        raise ValueError(f"{src} 含非系统隐藏文件 {_names(t['hidden'])}，移动文件夹会把它们一并带走，而确认卡片不会列出；"
                         '请如实告诉用户，由用户决定，不要换路径重试')
    return dict(kind='directory_tree', stat=signature(folder), sha256=None, moved_out=moving,
                dirs=t['dirs'], files=dict(t['metadata']), metadata=sorted(t['metadata']), hidden=[],
                notes=t['notes'], sensitive={})


def _same_object(a, b):
    try: return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError: return False


def _present(path):
    """The exact name is on disk; a case-insensitive volume also "finds" a.jpg as a.JPG."""
    try: return path.name in os.listdir(path.parent)
    except OSError: return False


def _case_only(sb, step):
    """A rename that changes only letter case (or another folding) of the same object."""
    if not step['source'] or step['effect'] == 'trash': return False
    source, target = sb.path(step['source'], exact=True), sb.path(step['destination'], exact=True)
    return source != target and _same_object(source, target)


def _order(sb, steps):
    """Steps in an order that runs. Each source names an object as it is now.

    A target still occupied must be vacated by an earlier step of this plan
    (B→C before A→B; a case-only rename vacates its own target), and a folder
    moved or recycled goes after the moves that empty it. A cycle (two names
    swapped) has no such order and is refused, not routed via a temporary name.
    """
    paths = [sb.path(s['source'], exact=True) for s in steps]
    after = [set() for _ in steps]
    for i, s in enumerate(steps):
        if s['effect'] != 'trash':
            target = sb.path(s['destination'], exact=True)
            if target.exists() or target.is_symlink():
                vacating = [j for j, p in enumerate(paths) if _same_object(p, target)]
                if not vacating: raise ValueError('目标已存在，不会覆盖：'+s['destination'])
                after[i].update(j for j in vacating if j != i)
        if s['snapshot']['kind'] in FOLDER_KINDS:
            after[i].update(j for j, p in enumerate(paths) if j != i and p.is_relative_to(paths[i]))
    order, done = [], set()
    while len(order) < len(steps):
        ready = next((i for i in range(len(steps)) if i not in done and after[i] <= done), None)
        if ready is None:
            stuck = [steps[i]['source'] for i in range(len(steps)) if i not in done]
            raise ValueError(f"计划中的动作互相依赖，没有可执行的顺序（如两个文件互换名字）：{_names(stuck)}。"
                             '请分两次：先只把其中一个改成临时名并执行，再准备下一个计划')
        order.append(ready); done.add(ready)
    return [steps[i] for i in order]


def _selection_count(sb, sel):
    """Visible files a selection picks now; a folder that no longer exists picks none."""
    if not sb.path(sel['path'], exact=True).exists(): return 0
    return len(selected(sb, sel)[0])


def _empty_selection(runtime, args):
    """A part with nothing to do, proved locally: the selection picks 0 files.

    Recorded as a completed plan with no steps, bound to its part. No write, no
    confirmation. It holds only while the selection stays empty (verify_plan).
    """
    task, sel = runtime.current, args.selection.model_dump()
    known = {g['id'] for g in task['goals']}
    if set(args.goal_ids) - known:
        raise ValueError('goal_ids 引用了任务中不存在的部分：' + '、'.join(sorted(set(args.goal_ids) - known)))
    candidates = [g['id'] for g in task['goals'] if any(c['kind'] == 'file_plan_verified' for c in g['conditions'])]
    goal_ids = list(args.goal_ids) or (candidates if len(candidates) == 1 else [])
    if not goal_ids:
        from agent.intent import parts_text
        raise ValueError(f"选择集 {sel['path']} 当前没有可处理的文件。若某个部分因此无需操作，"
                         f"请在 goal_ids 中写明该部分，本地会记录这个核验结果。当前部分：{parts_text(task)}")
    pid = 'X'+uuid.uuid4().hex[:12]
    payload = dict(task_id=task['id'], revision=task['revision'], goal_ids=goal_ids, description=args.description,
                   steps=[], classification=None, empty_selection=sel)
    plan = dict(id=pid, payload=payload, digest=checksum(payload), status='completed', progress={}, error='')
    runtime.store.save_file_plan(plan)
    task['file_plan_bindings'] = dict(task.get('file_plan_bindings', {}), **{pid: dict(goal_ids=goal_ids, revision=task['revision'], pending=[])})
    task.setdefault('file_plan_receipts', {})[pid] = copy.deepcopy(plan)
    runtime.store.checkpoint(task)
    return plan


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
    # A plan never writes a dedup receipt; recycling copies here leaves that part undone.
    trashed = [s['source'] for s in steps if s['effect'] == 'trash' and s['snapshot']['kind'] == 'file']
    for goal in task['goals']:
        if trashed and any(c['kind'] == 'receipt_verified' for c in goal['conditions']):
            hints.append(f"部分 {goal['id']} 是去重，只能由 deduplicate 或 apply_dedup 的本地回执完成；"
                         f"本计划回收的文件（{'、'.join(trashed[:3])}）不会产生回执，该部分仍会是未完成。"
                         "重复副本请改用去重工具，计划中只保留其他操作")
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
    remaining = [s for s in plan['payload']['steps'] if s['id'] not in plan['progress']]
    for i, s in enumerate(remaining):
        if s['source']:
            if not _same(_visible(sb, s['source'], write=True), s['snapshot'], pending=True):
                raise ValueError('源文件版本变化或不存在：'+s['source'])
        if s['effect'] == 'trash' and (sb.root/'.trash').is_symlink():
            raise ValueError('回收区不能是符号链接')
        target = sb.path(s['destination'], exact=True)
        if s['effect'] != 'trash': _visible(sb, s['destination'], write=True)
        # Vacated by an earlier step of this plan, or by this case-only rename itself.
        if target.exists() and not any(_same_object(sb.path(r['source'], exact=True), target)
                                       for r in remaining[:i + 1] if r['source']):
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
    if source is not None and _same_object(source, target):
        # Case-only rename: both names "exist"; only the exact listing tells.
        target_there, source_there = _present(target), _present(source)
    else:
        target_there, source_there = target.exists(), source is not None and source.exists()
    if not target_there and (source is None or _same(source, step['snapshot'])):
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
    if source and not source_there and recorded and _same(target, step['snapshot'], moved=True):
        progress = dict(run_id=plan['inflight_run'], log_path=str(log), stat=signature(target))
        plan['progress'][sid] = progress
        plan.pop('inflight'); runtime.store.save_file_plan(plan)
        row = dict(op=op,src=step['source'],dst=step['destination'],action_id=sid,
                   run_id=progress['run_id'],goal_ids=step.get('goal_ids') or plan['payload']['goal_ids'],step_id=sid,
                   kind='文件夹' if step['snapshot']['kind'] in FOLDER_KINDS else '文件')
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
                       step_id=step['id'], kind='文件夹' if step['effect']=='create' or step['snapshot']['kind'] in FOLDER_KINDS else '文件')
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
            trees = [s for s in remaining if s['effect'] == 'trash' and s['snapshot'] and s['snapshot']['kind'] == 'directory_tree'
                     and (s['snapshot']['metadata'] or s['snapshot']['hidden'])]
            if trees:
                review['hidden_contents'] = [dict(folder=s['source'], metadata=s['snapshot']['metadata'],
                                                  hidden=s['snapshot']['hidden'], sensitive=s['snapshot'].get('sensitive', {}),
                                                  notes=s['snapshot'].get('notes', {}),
                                                  consent_source=s['snapshot'].get('consent_source'))
                                             for s in trees]
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
                if target.exists() and not _case_only(runtime.sb, s):
                    raise ValueError('目标被其他操作占用：'+s['destination'])
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
                           kind='文件夹' if s['effect']=='create' or s['snapshot']['kind'] in FOLDER_KINDS else '文件')
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
    sel = plan['payload'].get('empty_selection')
    if sel:
        count = _selection_count(sb, sel)
        return (True, f"执行前选择集 {sel['path']} 为空，无需操作；核验时仍为空") if not count else \
               (False, f"选择集 {sel['path']} 现在有 {count} 个文件，此前“无需操作”的结论不再成立")
    if not partial and (plan['status'] not in {'completed','executing'} or len(plan['progress']) != len(plan['payload']['steps'])):
        return False, '计划尚未全部执行：'+plan['id']
    done = [s for s in plan['payload']['steps'] if s['id'] in plan['progress']]
    refilled = [sb.path(s['destination'], exact=True) for s in done if s['effect'] != 'trash']
    for s in done:
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
            source = _visible(sb, s['source'])
            if _same_object(source, target) and not _present(target):
                return False, '改名未生效（仅大小写不同）：'+s['destination']
            # The old place may hold what a later step of this plan put there (A→B after B→C).
            if source.exists() and not any(_same_object(source, d) for d in refilled):
                return False, '原位置仍存在：'+s['source']
    return True, '操作清单的位置与内容核验通过'


def markdown(plan):
    lines = [f"计划 {plan['id']} · {plan['status']} · {len(plan['payload']['steps'])} 项", '',
             '| 操作 | 来源 | 目标 |', '| --- | --- | --- |']
    for s in plan['payload']['steps']:
        lines.append('| '+' | '.join(label(x or '') for x in (s['effect'], s['source'], '回收区' if s['effect']=='trash' else s['destination']))+' |')
    if plan['error']: lines += ['', '阻塞原因：'+plan['error']]
    return '\n'.join(lines)
