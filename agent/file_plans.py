"""Immutable file manifests and a local, journalled executor.

The model proposes objects and intent. This module derives mechanical scopes,
parents and verification. Existing tasks never gain permissions from a plan.
"""
import copy
import errno
import hashlib
import json
import os
import stat as stat_flags
import unicodedata
import uuid
from safety.locking import library_lock
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from agent.artifacts import digest, signature, label
from agent.intent import InputResolution
from agent import choices, intent
from agent.completion import selected, walk_selection, is_package, attribute, claimed, step_owners
from agent.task import StrictModel


class Action(StrictModel):
    effect: Literal['create', 'move', 'rename', 'trash']
    source: str | None = None
    destination: str | None = None
    include_metadata: bool = Field(False, description='仅 trash 文件夹：同意连同其中的系统元数据（.DS_Store 等）一起回收')
    include_hidden: list[str] = Field(default_factory=list, max_length=20,
        description='仅 trash 文件夹：用户已通过提问选项同意一起回收的其他隐藏文件，逐个写出完整路径')
    part: str = Field('', description='动作的部分ID；多个候选部分时必填')

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
    part: str = Field('', description='展开的动作都属于此部分')


class PrepareFilePlan(StrictModel):
    description: str = Field(min_length=1, max_length=800)
    actions: list[Action] = Field(default_factory=list, max_length=200)
    selection: Selection | None = None
    classification_plan_id: str | None = Field(None, description='已有分类计划的精确 P 标识；本地展开全部来源和目标，不必抄写文件路径')
    goal_ids: list[str] = Field(default_factory=list, max_length=30,
        description='计划涉及的部分ID；多目标动作仍须part')
    task_id: str | None = Field(None, description='仅在续做以前的任务时填写，复制完整 ID，沿用原限制；本轮已调用 start_task 时不要填')
    input_resolution: InputResolution | None = Field(None, description='续做任务且消息回应上一问时必填')


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
FOLDER_KINDS = {'empty_directory', 'directory_tree', 'folder', 'package'}
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
                             + f"回收前必须先调用 finish_turn(mode=ask) 询问用户（subject 填 {src}，表示同意的选项设 consent=include_hidden）；不要猜测用户意愿")
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


def package_snapshot(folder):
    """A package (.pages, .app, ...) is one object to the user: its whole content is one
    digest, so its internals (links, nested bundles) are neither judged nor taken apart."""
    return dict(kind='package', stat=signature(folder), sha256=_tree_digest(folder))


def _same(path, expected, *, moved=False, pending=False):
    if expected['kind'] == 'package':
        try: return is_package(path) and _tree_digest(path) == expected['sha256'] and (
            signature(path)[:2] == expected['stat'][:2] if moved else True)
        except OSError: return False
    if expected['kind'] == 'folder':
        try: return _folder_same(path, expected, pending)
        except OSError: return False
    if expected['kind'] == 'directory_tree':
        try: return _tree_same(path, expected, pending)
        except OSError: return False
    try:
        actual = fingerprint(path)
        return (actual['kind'] == expected['kind'] and actual['sha256'] == expected['sha256']
                and (actual['stat'][:2] == expected['stat'][:2] if moved else actual['stat'] == expected['stat']))
    except (ValueError, OSError):
        return False


def on_disk(sb, rel):
    """The existing object the model meant when its spelling of a name is off.

    Exact first; then the sandbox's own tolerance (surrounding spaces, real on-disk
    spelling); then a single sibling equal under NFKC + casefold (full-width "（" and
    "("). Several matches are never guessed between. Only for sources, which exist.
    """
    path = sb.path(rel, exact=True)
    if path.exists() or path.is_symlink():
        return rel
    try:
        tolerant = sb.path(rel)
        if tolerant.exists() and tolerant.is_relative_to(sb.root):
            return sb.rel(tolerant)
    except Exception:
        pass
    if path.parent.is_dir():
        want = _key(path.name.strip())
        hits = [c for c in path.parent.iterdir() if _key(c.name.strip()) == want and not c.name.startswith('.')]
        if len(hits) == 1:
            return sb.rel(hits[0])
    return rel


def _writable(sb, source, src):
    """Refuse before the confirmation what the system would refuse after it."""
    flags = getattr(os.lstat(source), 'st_flags', 0)
    if flags & (getattr(stat_flags, 'UF_IMMUTABLE', 0) | getattr(stat_flags, 'SF_IMMUTABLE', 0)):
        raise ValueError(f'{src} 已被锁定（访达“显示简介”里勾选了“已锁定”），工具不会解锁。请告诉用户先取消锁定再试')
    if not os.access(source.parent, os.W_OK):
        raise ValueError(f'{src} 所在的文件夹 {sb.rel(source.parent)} 是只读的，无法移走其中的内容。请告诉用户检查文件夹权限')


def os_reason(exc, sb):
    """A system error as the user reads it: no English text, no absolute path."""
    where = ''
    if getattr(exc, 'filename', None):
        try: where = '：' + sb.rel(Path(exc.filename))
        except ValueError: where = ''
    text = {errno.EPERM: '没有权限（文件可能已被锁定）', errno.EACCES: '没有权限（文件夹可能是只读的）',
            errno.ENOSPC: '磁盘空间不足', errno.ENOENT: '文件已不存在', errno.EEXIST: '目标位置已有同名项',
            errno.EXDEV: '不能跨磁盘移动', errno.EROFS: '磁盘是只读的'}.get(exc.errno, '系统拒绝了这个操作')
    return text + where


def _key(path):
    return unicodedata.normalize('NFKC', str(path)).casefold()


def prepare(runtime, args):
    """A plan for the current task (the controller requires start_task, or a task_id to continue)."""
    sb, task = runtime.sb, runtime.current
    if not task:
        raise ValueError('文件计划必须属于已登记的任务')
    normalized, notes = intent.normalize_part_refs(args.model_dump(), task)
    args = PrepareFilePlan.model_validate(normalized)
    runtime._part_id_notes = list(dict.fromkeys(getattr(runtime, '_part_id_notes', []) + notes))
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
        from agent.constraints import within
        if not within(sb, sel['path'], task['read_roots']):
            raise ValueError('选择集超出已有任务读取范围：'+sel['path'])
        files, _, _, packages = walk_selection(sb, sel)
        for src in files + packages:  # a package (.pages, .app) moves whole
            dst = None
            if sel['effect'] == 'move':
                tail = Path(src).relative_to(sel['path']) if sel['preserve_tree'] else Path(src).name
                dst = (Path(sel['destination']) / tail).as_posix()
            actions.append(dict(effect=sel['effect'], source=src, destination=dst, part=sel['part']))
    actions = _normalize(runtime, actions)
    chosen = getattr(runtime, 'chosen_option', None)
    # A question about some parts bounds only those parts' actions (routing redesign S3, 5.1).
    scoped = runtime.scope_goals() if chosen is not None else None
    if chosen is not None and scoped is None:
        _within_choice(runtime, chosen, actions)  # before any other check, so the reason is this one
    if not actions and args.selection and not args.actions:
        return _empty_selection(runtime, args)
    if not actions or len(actions) > 500:
        raise ValueError('计划必须包含 1–500 项明确操作，请缩小选择范围')
    pid = 'X'+uuid.uuid4().hex[:12]
    steps, dirs, sources, destinations = [], {}, set(), set()
    moved_out = {on_disk(sb, runtime._resolve(a['source'])) for a in actions if a['effect'] in {'move', 'rename'} and a['source']}
    consent = choices.of(runtime.decisions, 'hidden')
    for action in actions:
        effect, src, dst = action['effect'], action['source'], action['destination']
        src = on_disk(sb, runtime._resolve(src)) if src else None
        from agent.constraints import within
        for value in (src, dst):
            if value and not within(sb, value, task['read_roots']):
                raise ValueError('计划对象超出已有任务读取范围：'+value)
        source = _visible(sb, src, write=True) if src else None
        target = _visible(sb, dst, write=True) if dst else None
        if source is not None and (source.exists() or source.is_symlink()):
            _writable(sb, source, sb.rel(source))
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
        if source is not None and is_package(source):
            snap = package_snapshot(source)
        elif effect == 'trash' and source.is_dir() and not source.is_symlink() and any(source.iterdir()):
            snap = tree_snapshot(source, src, moved_out, action, consent)
            if snap['hidden']:
                # Where the consent came from decides what the confirmation card says.
                snap['consent_source'] = next((v for f, v in consent.items()
                                               if Path(src) == Path(f) or Path(src).is_relative_to(f)), None)
        elif effect in {'move', 'rename'} and source.is_dir() and not source.is_symlink():
            snap = dict(kind='folder')  # filled in below, once every step of the plan is known
        else:
            snap = fingerprint(source, src) if source else None
        if snap and snap['kind'] not in {'file', 'folder', 'package'} and effect != 'trash':
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
            if effect == 'create' and action.get('part') and dst in dirs:
                dirs[dst]['part'] = action['part']  # a folder the user asked for proves its part
        if effect != 'create':
            if effect == 'trash': dst = f'.trash/{pid}_{len(steps)}_{source.name}'
            steps.append(dict(effect=effect, source=src, destination=dst, snapshot=snap,
                              **({'part': action['part']} if action.get('part') else {})))
    # A folder recycled whole carries its nested folders: a separate trash of one
    # of them is the same effect, merged. Recycled folders go last, after the
    # moves that empty them.
    folders = [Path(s['source']) for s in steps if s['effect'] == 'trash' and s['snapshot']['kind'] != 'file']
    steps = [s for s in steps if not (s['effect'] == 'trash' and any(
        Path(s['source']) != f and Path(s['source']).is_relative_to(f) for f in folders))]
    steps.sort(key=lambda s: s['effect'] == 'trash' and s['snapshot']['kind'] == 'directory_tree')
    for row in steps:
        if row['snapshot']['kind'] == 'folder':
            row['snapshot'] = folder_snapshot(sb, row['source'], steps)
    # A recycled folder holds no target. A folder moved whole may receive files
    # before it moves (they travel with it), but nothing at its new place.
    for row in steps:
        if row['snapshot']['kind'] in FOLDER_KINDS:
            ends = [sb.path(row['source' if row['effect'] == 'trash' else 'destination'], exact=True)]
            inside = [s['destination'] for s in [*dirs.values(), *steps] if s['destination'] and s is not row
                      and any(sb.path(s['destination'], exact=True).is_relative_to(p) for p in ends)]
            if inside:
                raise ValueError(f"本计划移动或回收的文件夹 {row['source']} 内还有计划目标（如 {_names(inside)}），"
                                 '请拆分阶段：先执行这一个计划，再为后续操作准备新计划')
    try:
        steps = _order(sb, steps)
    except ValueError as exc:
        if not str(exc).startswith('目标已存在'): raise
        dst = str(exc).split('：', 1)[1]
        raise ValueError(f"{exc}。同名文件的处理由用户决定：用户没交代时先 finish_turn(mode=ask)，给三个选项——"
                         f"保留两份（新文件改名为 {free_name(sb, dst, [s['destination'] for s in steps if s['destination']])}）、"
                         '不移动、覆盖（选项 actions 同时写 trash 旧文件和 move 新文件到原名；旧文件移入回收区，可撤销）') from exc
    # New folders are reversible and listed on the confirmation card; whether the user
    # meant one is the model's judgement (asked to ask), not a local refusal.
    fresh = [d for d in dirs if not any(d != o and Path(d).is_relative_to(o) for o in dirs)]
    replaced = overwrites(sb, steps)
    agreed = choices.of(runtime.decisions, 'overwrite')
    refused = [d for d in replaced if not any(_key(d) == _key(runtime._resolve(c)) for c in agreed)]
    if refused:
        raise ValueError(f"此计划会覆盖 {_names(refused)}：旧文件移入回收区，再放入新文件。覆盖必须由用户选择："
                         '先调用 finish_turn(mode=ask)（subject 填被覆盖的文件），给出保留两份、不移动、覆盖三个选项；不要自行决定')
    steps = list(dirs.values()) + steps
    for i, row in enumerate(steps): row['id'] = f'{pid}:{i}'
    producers = ['file_plans'] + (['classification_plans'] if classification else [])
    # For an existing task, bindings are explicit or locally derived; neither grants access.
    goal_ids = _attribute_steps(runtime, task, steps, args.goal_ids, producers)
    if chosen is not None and scoped is not None:
        mine = [s for s in steps if s['effect'] != 'create' and set(s['goal_ids']) & set(scoped)]
        if chosen.get('disposition') == 'skip' and mine:
            raise ValueError(f"用户选择 {chosen['key']}（{chosen['label']}）跳过部分 {'、'.join(scoped)}，"
                             f"不能为它们准备操作：{'、'.join(s['source'] for s in mine[:3])}。其他部分照常处理")
        _within_choice(runtime, chosen, mine)
    additions = _additions(runtime, task, steps)
    payload = dict(task_id=task['id'], revision=task['revision'], goal_ids=goal_ids,
                   description=args.description, steps=steps, classification=classification)
    if additions: payload['scope_additions'] = additions
    if fresh: payload['new_folders'] = fresh
    if replaced:
        payload['overwrites'] = [dict(target=d, consent_source=next(v for c, v in agreed.items()
                                      if _key(d) == _key(runtime._resolve(c)))) for d in replaced]
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

    A destination names a directory when it ends with "/", exists as one, is
    created by this plan, or is shared by several moves (one file path cannot
    receive them all). The file keeps its name; the manifest shows the full target.
    Round 28: "照片/2024" for a single c.jpg became a file named 2024. A move that
    would drop a file's extension onto a path that does not exist is ambiguous
    (folder or new name), so it is refused with both spellings.
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
            name = Path(on_disk(sb, runtime._resolve(action['source']))).name  # the real name, spaces included
            if action['destination'].endswith('/') or is_dir or dst in creates or shared[action['destination']] > 1:
                action['destination'] = (Path(dst) / name).as_posix()
            else:
                try: source_file = sb.path(runtime._resolve(action['source']), exact=True).is_file()
                except Exception: source_file = False
                try: missing = not sb.path(dst, exact=True).exists()
                except Exception: missing = False
                if source_file and missing and Path(name).suffix and not Path(dst).suffix:
                    raise ValueError(f"目标 {dst} 不存在且没有扩展名，{name} 会变成没有扩展名的文件 {Path(dst).name}。"
                                     f"要放进文件夹请写 {dst}/{name}（或 {dst}/）；要改名请写带扩展名的完整文件名")
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


def folder_snapshot(sb, src, steps):
    """A folder moved or renamed whole, with what it holds before and after this plan.

    Every visible file is recorded by relative path and SHA-256: "before" as it is
    now, "after" once this plan's steps inside it have run (files renamed, moved
    out, recycled or moved in). The folder must match "after" at its new place.
    Hidden files, open documents and special objects would travel unseen: refused.
    """
    folder = sb.path(src, exact=True)
    t = scan_tree(folder)
    if t['busy'] or t['special']:
        raise ValueError(f"{src} 含锁文件或特殊对象 {_names(t['busy'] + t['special'])}（正被打开的文档、隐藏文件夹、符号链接或应用包），"
                         '工具不移动此文件夹。请如实告诉用户由用户处理；其余操作可以照常进行')
    if t['hidden']:
        raise ValueError(f"{src} 含非系统隐藏文件 {_names(t['hidden'])}，整体移动会把它们一并带走，而确认卡片不会列出；"
                         '请如实告诉用户，由用户决定，不要换路径重试')
    root = Path(src)
    inner = lambda p: p is not None and Path(p) != root and Path(p).is_relative_to(root)
    leaving = [Path(s['source']).relative_to(root) for s in steps if s['source'] != src and inner(s['source'])]
    arriving = {}
    for s in steps:
        if s['effect'] in {'move', 'rename'} and s['source'] != src and inner(s['destination']):
            if s['snapshot']['kind'] != 'file':
                raise ValueError(f"本计划把文件夹 {s['source']} 放进同时整体移动的 {src}，请拆分阶段：先执行这一个计划，再为后续操作准备新计划")
            arriving[Path(s['destination']).relative_to(root).as_posix()] = s['snapshot']['sha256']
    before = {rel: digest(folder / rel) for rel in t['visible']}
    after = {rel: sha for rel, sha in before.items() if not any(Path(rel) == l or Path(rel).is_relative_to(l) for l in leaving)}
    after.update(arriving)
    return dict(kind='folder', stat=signature(folder), sha256=None, before=before, after=after,
                arriving=sorted(arriving), dirs=t['dirs'], metadata=sorted(t['metadata']), notes=t['notes'])


def _folder_same(path, expected, pending):
    """The folder holds exactly "after" (pending: any mix of "before" and "after",
    since a resumed plan may have run some of its inner steps). Finder metadata may
    come and go; any other difference stops the plan."""
    if not path.is_dir() or path.is_symlink(): return False
    t = scan_tree(path)
    if t['special'] or t['busy'] or t['hidden']: return False
    before, after = expected['before'], expected['after']
    if not pending:
        return set(t['visible']) == set(after) and all(digest(path / rel) == sha for rel, sha in after.items())
    stays = [rel for rel in after if rel not in expected['arriving']]
    if not set(stays) <= set(t['visible']) or not set(t['visible']) <= set(before) | set(after): return False
    return all(digest(path / rel) in {before.get(rel), after.get(rel)} for rel in t['visible'])


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
    targets = [sb.path(s['destination'], exact=True) if s['effect'] != 'trash' else None for s in steps]
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
            if s['snapshot']['kind'] == 'folder':
                after[i].update(j for j, t in enumerate(targets) if j != i and t is not None and t.is_relative_to(paths[i]))
    order, done = [], set()
    while len(order) < len(steps):
        ready = next((i for i in range(len(steps)) if i not in done and after[i] <= done), None)
        if ready is None:
            stuck = [steps[i]['source'] for i in range(len(steps)) if i not in done]
            raise ValueError(f"计划中的动作互相依赖，没有可执行的顺序（如两个文件互换名字）：{_names(stuck)}。"
                             '请分两次：先只把其中一个改成临时名并执行，再准备下一个计划')
        order.append(ready); done.add(ready)
    return [steps[i] for i in order]


def free_name(sb, rel, taken=()):
    """The first "name (n).ext" beside rel that is free on disk and in this plan."""
    path = Path(rel)
    keys = {_key(t) for t in taken}
    for n in range(1, 1000):
        candidate = path.with_name(f'{path.stem} ({n}){path.suffix}').as_posix()
        target = sb.path(candidate, exact=True)
        if not target.exists() and not target.is_symlink() and _key(candidate) not in keys:
            return candidate
    raise ValueError('找不到可用的新名字：'+rel)


def overwrites(sb, steps):
    """Targets this plan overwrites: a file recycled here and another put in its place."""
    trashed = [sb.path(s['source'], exact=True) for s in steps
               if s['effect'] == 'trash' and s['snapshot']['kind'] == 'file']
    return [s['destination'] for s in steps if s['effect'] in {'move', 'rename'}
            and any(_same_object(sb.path(s['destination'], exact=True), t) for t in trashed)]


def new_folders(plan):
    """The outermost folders this plan creates (a new folder's own subfolders are left out)."""
    created = [s['destination'] for s in plan['payload']['steps'] if s['effect'] == 'create']
    return [d for d in created if not any(d != o and Path(d).is_relative_to(o) for o in created)]


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
        raise ValueError('goal_ids 引用了任务中不存在的部分：' + '、'.join(sorted(set(args.goal_ids) - known))
                         + '。当前部分：' + intent.parts_text(task))
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
    task = runtime.current
    steps = plan['payload']['steps']
    hints = []
    # A plan never writes a dedup receipt; recycling copies here leaves that part undone.
    trashed = [s['source'] for s in steps if s['effect'] == 'trash' and s['snapshot']['kind'] == 'file']
    for goal in task['goals']:
        if trashed and any(c['kind'] == 'receipt_verified' for c in goal['conditions']):
            hints.append(f"部分 {goal['id']} 是去重，只能由 deduplicate 或 apply_dedup 的本地回执完成；"
                         f"本计划回收的文件（{'、'.join(trashed[:3])}）不会产生回执，该部分仍会是未完成。"
                         "重复副本请改用去重工具，计划中只保留其他操作")
    return hints


AMBIGUOUS_GOAL = 'AMBIGUOUS_GOAL'


def _attribute_steps(runtime, task, steps, requested, producers):
    """Each business action proves exactly one goal (routing redesign S1, 2026-10-24).

    An action naming its part serves that part. Without one, it serves the only
    candidate goal it can prove; when several could, the plan is refused even if
    goal_ids lists them all: one archived file proving "archive a" and "rename b"
    was the P0 this closes. A folder created for the actions serves their goals
    as a dependency and proves none of them on its own (verifier._goal_steps).
    """
    known = {g['id'] for g in task['goals']}
    if set(requested) - known:
        raise ValueError('goal_ids 引用了任务中不存在的目标：'+'、'.join(sorted(set(requested) - known))
                         + '。当前部分：' + intent.parts_text(task))
    named = {row['part'] for row in steps if row.get('part')}
    if named - known:
        raise ValueError('part 引用了任务中不存在的部分：'+'、'.join(sorted(named - known)) + '。当前部分：' + intent.parts_text(task))
    candidates = list(dict.fromkeys(requested + sorted(named))) if requested else [g['id'] for g in task['goals']]
    rest = [g for g in candidates if g not in named] or candidates
    unresolved, shared = [], []
    for row in steps:
        if row['effect'] == 'create': continue
        op = dict(effect=row['effect'], source=row['source'],
                  destination=row['destination'] if row['effect'] != 'trash' else None)
        if row.get('part'):
            if not claimed(task, runtime.sb, op, [row['part']], producers):
                raise ValueError(f"部分 {row['part']} 的完成条件不能由文件计划的 {row['effect']} 满足：{row['source']}")
            row['goal_ids'] = [row['part']]
            continue
        served = claimed(task, runtime.sb, op, rest, producers)
        if len(served) > 1:
            shared.append(row['source'])
        elif served or len(rest) == 1:
            row['goal_ids'] = served or list(rest)
        else:
            unresolved.append(row['source'])
    if shared:
        raise ValueError(f'{AMBIGUOUS_GOAL}：以下动作可能属于多个部分，本地无法确定是哪一个：' + '、'.join(shared[:8]) +
                         '。请在每个动作上写 part（一个部分一个 ID），或把各部分分成不同计划；写了 goal_ids 也不能代替。当前部分：'
                         + intent.parts_text(task))
    if unresolved:
        raise ValueError('无法从完成条件确定以下动作服务的目标，请在动作上写 part：'+'、'.join(unresolved[:8]))
    for row in steps:
        if row['effect'] != 'create': continue
        if row.get('part'):
            row['goal_ids'] = [row['part']]
            continue
        # A parent directory serves the actions placed inside it.
        served = [g for s in steps if s['effect'] != 'create' and s['destination'] and
                  Path(s['destination']).is_relative_to(row['destination']) for g in s['goal_ids']]
        op = dict(effect='create', source=None, destination=row['destination'])
        row['goal_ids'] = list(dict.fromkeys(served)) or attribute(task, runtime.sb, op, candidates, producers)
        if not served and len(row['goal_ids']) > 1:
            unresolved.append(row['destination'])
    if unresolved:
        raise ValueError(f'{AMBIGUOUS_GOAL}：无法确定以下新建文件夹属于哪个部分，请在动作上写 part：'+'、'.join(unresolved[:8]))
    # New plans never need the plan-level fallback of step_owners.
    assert all(row['goal_ids'] for row in steps), 'every step of a new plan has its own goal_ids'
    return list(dict.fromkeys(g for row in steps for g in row['goal_ids']))


def _operations(plan, pending=None):
    rows = plan['payload']['steps'] if pending is None else pending
    producers = ['file_plans'] + (['classification_plans'] if plan['payload'].get('classification') else [])
    return [dict(effect=s['effect'], source=s['source'],
                 destination=s['destination'] if s['effect'] != 'trash' else None,
                 goal_ids=step_owners(plan, s), producers=producers) for s in rows]


def _validate(runtime, plan, pending=None):
    task = runtime.current
    payload = plan['payload']
    if checksum(payload) != plan['digest']: raise ValueError('计划内容摘要不一致')
    if not task or task['id'] != payload['task_id']:
        raise ValueError('计划不属于当前任务 '+((task or {}).get('id') or '（无）')+'，不能切换任务执行')
    if task['revision'] != payload['revision']:
        raise ValueError(f"任务版本已变化（计划基于第 {payload['revision']} 版，当前第 {task['revision']} 版），"
                         '必须重新预览计划')
    if task.get('constraint_failures'): raise ValueError('任务有未处理的约束失败')
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
                   run_id=progress['run_id'],goal_ids=step_owners(plan, step),step_id=sid,
                   kind='文件夹' if step['snapshot']['kind'] in FOLDER_KINDS else '文件')
        if progress['run_id'] == runtime.run_id: runtime.operations.append(row)
        else: runtime.current.setdefault('operation_history', []).append(row)
        if getattr(runtime.sb,'artifacts',None):runtime.sb.artifacts.relocate(step['source'],step['destination'])
        return
    raise ValueError('上次操作中断，存在结果未确认的动作 '+sid+'；请核对日志及文件位置，禁止自动重放')


def execute(runtime, plan_id, oplog, confirmer):
    """Run a saved plan: reconcile, check, confirm, check again, run each step, verify.

    The objects are checked at three points, because the user may take minutes to
    confirm and files can change meanwhile: before the confirmation, after it, and
    immediately before each step. Any difference stops the plan; completed steps
    stay journalled and undoable.
    """
    if not oplog or not confirmer: raise ValueError('执行计划需要日志和明确的确认策略')
    with library_lock(runtime.sb):
        plan, task = _reconcile(runtime, plan_id)
        plan_id = plan['id']
        try:
            remaining = _loaded(runtime, plan, task)
            _validate(runtime, plan, remaining)
            if plan['status'] == 'completed':
                return _verified_again(runtime, plan, task)
            _recheck(runtime, plan)                      # 1. before the confirmation
            _stop_point(runtime)
            _confirm(runtime, plan, remaining, confirmer)
            _stop_point(runtime)                         # the user may have cancelled while the card was open
            _grant(task, plan, runtime.run_id)
            runtime.store.checkpoint(task)
            _validate(runtime, plan, remaining)
            _recheck(runtime, plan)                      # 2. after it: the user may have taken minutes
            plan['status'] = 'executing'; plan['error'] = ''
            runtime.store.save_file_plan(plan)
            for s in remaining:
                _stop_point(runtime)                     # a cancel stops before the next step, never inside one
                _run_step(runtime, plan, s, oplog)       # 3. each step checks its object first
            return _finish_plan(runtime, plan, task)
        except (ValueError, OSError) as exc:
            _blocked(runtime, plan, exc)


def _stop_point(runtime):
    """Routing redesign S3 (5.2): the user's cancel or pause takes effect here. A step already
    started finishes and is journalled; no later step runs. Done steps stay undoable."""
    if getattr(runtime, 'stop_requested', lambda: False)():
        raise ValueError('USER_CANCELLED：用户已取消或暂停这个请求，剩余操作未执行；已执行的操作保留，可撤销')


def _reconcile(runtime, plan_id):
    """(plan, task): the saved plan, rebased if the task was revised before it started,
    and the task's ledger completed with steps an interrupted run recorded only in the plan."""
    plan = runtime.store.file_plan(plan_id)
    task = runtime.store.get(plan['payload']['task_id'])
    if runtime.current and runtime.current['id'] != task['id']:
        raise ValueError('计划不属于当前任务，不能切换任务绕过约束')
    runtime.current = task
    if task['revision'] != plan['payload']['revision'] and _rebasable(plan):
        plan = _rebase(runtime, plan, task)
    known = {row.get('action_id') for row in task.get('operation_history', [])+task.get('operations', [])+runtime.operations if row.get('action_id')}
    for step in plan['payload']['steps']:
        progress = plan['progress'].get(step['id'])
        if not progress or step['id'] in known: continue
        row = dict(op='make_dir' if step['effect']=='create' else step['effect'],
                   src=step['source'] or step['destination'], dst=step['destination'],
                   action_id=step['id'], run_id=progress['run_id'], goal_ids=step_owners(plan, step),
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
    return plan, task


def _loaded(runtime, plan, task):
    """The steps still to run, once the plan is known intact and an interrupted step settled."""
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
    return remaining


def _verified_again(runtime, plan, task):
    """A plan already completed: verify it again and hand out its receipt, writing nothing."""
    plan_id = plan['id']
    ok, detail = verify_plan(plan, runtime.sb)
    if not ok: raise ValueError(detail)
    task.setdefault('file_plan_receipts', {})[plan_id] = copy.deepcopy(plan)
    runtime.store.checkpoint(task)
    return 'OK: 此计划已经执行并核验，无重复操作'


def _recheck(runtime, plan):
    """The remaining objects are as planned, and the completed steps' results still in place."""
    _check_remaining(runtime, plan)
    # Completed actions must still match before resuming the rest.
    ok, detail = verify_plan(plan, runtime.sb, partial=True)
    if not ok: raise ValueError(detail)


def _confirm(runtime, plan, remaining, confirmer):
    """One confirmation card for the remaining steps, with what needs the user's attention."""
    plan_id = plan['id']
    review = dict(plan_id=plan_id, digest=plan['digest'], steps=[
        {k:s[k] for k in ('effect','source','destination')} for s in remaining])
    if plan['payload'].get('scope_additions'):
        review['scope_additions'] = plan['payload']['scope_additions']
    if plan['payload'].get('overwrites'):
        review['overwrites'] = plan['payload']['overwrites']
    if plan['payload'].get('new_folders'):
        review['new_folders'] = plan['payload']['new_folders']
    whole = [dict(source=s['source'], destination=s['destination'], files=sorted(s['snapshot']['after']))
             for s in remaining if s['snapshot'] and s['snapshot']['kind'] == 'folder']
    if whole:
        review['folders'] = whole
    trees = [s for s in remaining if s['effect'] == 'trash' and s['snapshot'] and s['snapshot']['kind'] == 'directory_tree'
             and (s['snapshot']['metadata'] or s['snapshot']['hidden'])]
    if trees:
        review['hidden_contents'] = [dict(folder=s['source'], metadata=s['snapshot']['metadata'],
                                          hidden=s['snapshot']['hidden'], sensitive=s['snapshot'].get('sensitive', {}),
                                          notes=s['snapshot'].get('notes', {}),
                                          consent_source=s['snapshot'].get('consent_source'))
                                     for s in trees]
    outcome = confirmer.decide('file_plan', review, file_count=len(remaining), force=True,
                               manifest_key=manifest_key(plan, remaining),
                               **({'notes': runtime.text_limits()} if runtime.text_limits() else {}))
    if not outcome.approved:
        raise ValueError(not_confirmed(outcome, '此计划'))


def manifest_key(plan, steps):
    """What the user is asked to approve, without the plan's random identity.

    Effect, source and destination of each step (a trash destination carries the
    plan id, so it is left out), plus what recycling folders and overwrites take
    with them. File versions are not part of it: a declined list stays declined
    when a file changes. Used to keep a refused list from being asked again in
    the same run under a new plan id.
    """
    rows = sorted([s['effect'], s['source'], None if s['effect'] == 'trash' else s['destination'],
                   sorted((s['snapshot'] or {}).get('hidden') or []), sorted((s['snapshot'] or {}).get('metadata') or [])]
                  for s in steps)
    overwrites = sorted(o['target'] for o in plan['payload'].get('overwrites', []))
    return hashlib.sha256(json.dumps([rows, overwrites], ensure_ascii=False).encode()).hexdigest()[:24]


def not_confirmed(outcome, what):
    """The error for an unapproved confirmation, by what actually happened."""
    again = '本轮已拒绝过同一操作清单，没有再次询问。' if outcome.repeated else ''
    if outcome.decision == 'declined':
        return f'USER_DECLINED：用户拒绝执行{what}，未执行剩余操作。{again}本轮不要换个计划 ID 重新提交同样的操作'
    if outcome.decision == 'timed_out':
        return (f'CONFIRMATION_TIMEOUT：等待用户确认超时，未执行{what}的剩余操作；这不是用户拒绝。{again}'
                '本轮不要重新提交，告诉用户可以稍后再发起')
    return (f"CONFIRMATION_INTERRUPTED：确认被中断（{outcome.reason or '未知原因'}），未执行{what}的剩余操作。{again}"
            '本轮不要重新提交')


def _run_step(runtime, plan, s, oplog):
    """One step: recheck its object, mark it in flight, rename without replacing, journal it."""
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
               run_id=runtime.run_id, action_id=s['id'], goal_ids=step_owners(plan, s), step_id=s['id'],
               kind='文件夹' if s['effect']=='create' or s['snapshot']['kind'] in FOLDER_KINDS else '文件')
    runtime.operations.append(row)
    runtime.current['operations'] = runtime.operations
    runtime.store.checkpoint(runtime.current)
    if s['source'] and getattr(runtime.sb, 'artifacts', None):
        runtime.sb.artifacts.relocate(s['source'], s['destination'])


def _finish_plan(runtime, plan, task):
    """Verify every step's result, write the receipt, and update a bridged classification plan."""
    plan_id = plan['id']
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
    runtime.current.get('file_plan_partials', {}).pop(plan_id, None)
    _binding(runtime.current, plan)['pending'] = []
    runtime.store.checkpoint(runtime.current)
    rebased = f"（任务已修订，原计划 {plan['rebased_from']} 已按第 {task['revision']} 版重新生成）" if plan.get('rebased_from') else ''
    return f"OK: plan_id={plan_id}{rebased}；本地完成并核验 {len(plan['progress'])} 项操作，无需逐文件重新调用工具"


def _blocked(runtime, plan, exc):
    """Record why the plan stopped and which steps are still owed; re-raise for the model."""
    if isinstance(exc, OSError) and exc.errno:
        exc = ValueError(os_reason(exc, runtime.sb))
    plan['status'] = 'blocked'; plan['error'] = str(exc)
    runtime.store.save_file_plan(plan)
    if plan['progress'] or plan.get('inflight'):
        # A half-executed plan cannot satisfy its goals until the rest is done.
        from agent.verifier import _step_key
        _binding(runtime.current, plan)['pending'] = [
            list(_step_key(s)) for s in plan['payload']['steps'] if s['id'] not in plan['progress']]
        # Each goal is verified by its own executed steps (verifier.verify_bound_plans).
        runtime.current.setdefault('file_plan_partials', {})[plan['id']] = copy.deepcopy(plan)
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


def _handed_on(plan, step, later):
    """A later executed plan of the same task picked this step's result up and
    moved it on. Its snapshot equals the state this step left (full stat, so an
    earlier plan that once held the same inode there does not qualify; content
    for a file), and that plan's own verification covers the final place."""
    progress = plan['progress'][step['id']]
    for x in later:
        if x['effect'] not in {'move', 'rename'} or x['source'] != step['destination']: continue
        snap = x['snapshot']
        if snap['stat'] != progress['stat']: continue
        if step['snapshot'] and step['snapshot']['kind'] == 'file' and snap.get('sha256') != step['snapshot']['sha256']: continue
        return True
    return False


def verify_plan(plan, sb, partial=False, later=(), only=None):
    """``later``: executed steps of the task's other plans; see _handed_on.
    ``only``: check just these step IDs (one goal's share of the plan)."""
    sel = plan['payload'].get('empty_selection')
    if sel:
        count = _selection_count(sb, sel)
        return (True, f"执行前选择集 {sel['path']} 为空，无需操作；核验时仍为空") if not count else \
               (False, f"选择集 {sel['path']} 现在有 {count} 个文件，此前“无需操作”的结论不再成立")
    if not partial and (plan['status'] not in {'completed','executing'} or len(plan['progress']) != len(plan['payload']['steps'])):
        return False, '计划尚未全部执行：'+plan['id']
    done = [s for s in plan['payload']['steps'] if s['id'] in plan['progress']]
    moved_folders = [(Path(s['source']), Path(s['destination'])) for s in done if s['snapshot'] and s['snapshot']['kind'] == 'folder']
    def place(rel):
        """Where a result ended up once a folder of this plan moved whole after it."""
        p = Path(rel)
        for src, dst in moved_folders:
            if p != src and p.is_relative_to(src): return (dst / p.relative_to(src)).as_posix()
        return rel
    refilled = [sb.path(x['destination'], exact=True) for x in [*done, *later] if x['effect'] != 'trash']
    for s in done:
        if only is not None and s['id'] not in only: continue
        if s['effect'] == 'trash' and (sb.root/'.trash').is_symlink():
            raise ValueError('回收区不能是符号链接')
        if s['effect'] != 'trash' and _handed_on(plan, s, later):
            if s['source'] and _visible(sb, s['source']).exists() and not any(
                    _same_object(_visible(sb, s['source']), d) for d in refilled):
                return False, '原位置仍存在：'+s['source']
            continue
        target = sb.path(place(s['destination']), exact=True)
        if s['effect'] == 'create':
            if not _visible(sb, place(s['destination'])).is_dir() or signature(target)[:2] != plan['progress'][s['id']]['stat'][:2]:
                return False, '计划目录缺失或已替换：'+s['destination']
        else:
            # Internal trash destinations are compiler-generated, never model inputs.
            if s['effect'] != 'trash': _visible(sb, place(s['destination']))
            if not _same(target, s['snapshot'], moved=True): return False, '目标内容或身份不符：'+place(s['destination'])
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
