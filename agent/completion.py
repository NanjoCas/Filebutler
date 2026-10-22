"""Snapshot-backed file-set postconditions; no natural-language routing."""
import os
import unicodedata
from copy import deepcopy
from pathlib import Path

from agent.artifacts import digest
from agent.constraints import within


def condition_key(goal_id, index):
    return f'{goal_id}:{index}'


# Folders that apps treat as one document or program (macOS packages). Never taken apart.
PACKAGES = ('.app', '.bundle', '.framework', '.photoslibrary', '.pages', '.numbers', '.key', '.rtfd',
            '.band', '.logicx', '.fcpbundle', '.imovielibrary', '.musiclibrary', '.xcodeproj', '.playground')


def is_package(path):
    return path.is_dir() and not path.is_symlink() and path.name.lower().endswith(PACKAGES)


def selected(sb, condition):
    """Return a complete visible selection or fail, never silently skip read errors.
    Packages (.pages, .app, ...) are neither entered nor listed; see walk_selection."""
    return walk_selection(sb, condition)[:3]


def walk_selection(sb, condition):
    """(files, directories, skipped, packages): packages are whole objects, kept apart."""
    base = sb.path(condition['path'], exact=True)
    if not base.is_dir():
        raise ValueError('核验范围必须是存在的目录')
    excluded = [sb.path(p, exact=True) for p in condition.get('exclude_paths', [])]
    suffixes = {x.lower().lstrip('.') for x in condition.get('extensions', [])}
    def omitted(p):
        return any(p.is_relative_to(e) for e in excluded)
    files, directories, skipped, packages = [], [], 0, []
    def error(exc): raise exc
    for root, dirs, names in os.walk(base, followlinks=False, onerror=error):
        folder = Path(root)
        kept = []
        for name in sorted(dirs):
            p = folder / name
            if name.startswith('.') or p.is_symlink():
                skipped += 1
            elif is_package(p):
                if not omitted(p) and (not suffixes or p.suffix.lower().lstrip('.') in suffixes):
                    packages.append(sb.rel(p))
            elif not omitted(p):
                sb.path(sb.rel(p), exact=True)
                directories.append(sb.rel(p)); kept.append(name)
        dirs[:] = kept if condition.get('recursive', True) else []
        for name in sorted(names):
            p = folder / name
            if name.startswith('.') or p.is_symlink():
                skipped += 1; continue
            if omitted(p): continue
            if suffixes and p.suffix.lower().lstrip('.') not in suffixes: continue
            p = sb.path(sb.rel(p), exact=True)
            if p.is_file(): files.append(sb.rel(p))
    return files, directories, skipped, packages


def capture_conditions(task, sb, previous=None):
    snapshots = {}
    for goal in task['goals']:
        for index, c in enumerate(goal['conditions']):
            if c['kind'] not in {'files_placed', 'empty_directories_removed'}: continue
            # Adding another goal must not erase the original before-execution
            # manifest after some files have already moved out of its scope.
            prior_goal = next((g for g in (previous or {}).get('goals', []) if g['id']==goal['id']), None)
            if prior_goal:
                from agent.task import CompletionCondition
                prior_index = next((i for i, old in enumerate(prior_goal['conditions'])
                    if CompletionCondition.model_validate(old).model_dump() == CompletionCondition.model_validate(c).model_dump()), None)
                prior = (previous or {}).get('completion_snapshots', {}).get(condition_key(goal['id'], prior_index))
                if prior is not None:
                    snapshots[condition_key(goal['id'], index)] = deepcopy(prior)
                    continue
            snapshot = dict(files=[], directories=[], skipped=0, error='')
            try:
                files, dirs, skipped = selected(sb, c)
                snapshot.update(directories=dirs, skipped=skipped)
                if c['kind'] == 'files_placed':
                    targets = set()
                    if c.get('placement') == 'mapping' and set(c.get('placements', {})) != set(files):
                        raise ValueError('路径映射必须与执行前选定的文件清单完全一致')
                    for src in files:
                        if c.get('placement') == 'mapping': dst = c['placements'][src]
                        else:
                            relative = Path(src).relative_to(c['path']) if c.get('placement') == 'preserve_tree' else Path(src).name
                            dst = (Path(c['destination']) / relative).as_posix()
                        target = sb.path(dst, exact=True)
                        # normcase alone does not model APFS Unicode aliases.
                        key = unicodedata.normalize('NFKC', str(target)).casefold()
                        if key in targets: raise ValueError('执行前清单包含同名目标，请先解决冲突')
                        targets.add(key)
                        snapshot['files'].append(dict(src=src, dst=dst, sha256=digest(sb.path(src, exact=True))))
            except (OSError, ValueError) as exc:
                snapshot['error'] = str(exc)
            snapshots[condition_key(goal['id'], index)] = snapshot
    task['completion_snapshots'] = snapshots


def require_valid_snapshots(task):
    errors = [key+': '+row['error'] for key, row in task.get('completion_snapshots', {}).items() if row.get('error')]
    if errors:
        raise ValueError('执行前验收清单无效，尚未开放写入：'+'；'.join(errors))


def verify_selection(task, goal_id, index, c, sb):
    snapshot = task.get('completion_snapshots', {}).get(condition_key(goal_id, index))
    if snapshot is None:
        return False, '缺少执行前清单，不能追认全量完成'
    if snapshot['error']:
        return False, snapshot['error']
    if c['kind']=='files_placed' and not sb.path(c['path'],exact=True).exists():
        files,dirs=[],[]
    else:
        files, dirs, _ = selected(sb, c)
    if c['kind'] == 'empty_directories_removed':
        remaining = [p for p in dirs if not any(sb.path(p, exact=True).iterdir())]
        return not remaining, ('仍有空目录：'+'、'.join(remaining[:5])) if remaining else '可见范围内已无空子目录'
    targets = {row['dst'] for row in snapshot['files']}
    for row in snapshot['files']:
        src, dst = sb.path(row['src'], exact=True), sb.path(row['dst'], exact=True)
        if not within(sb, row['dst'], task['read_roots']):
            return False, '核验目标超出读取范围'
        if not dst.is_file() or digest(dst) != row['sha256']:
            return False, '目标缺失或内容不符：'+row['dst']
        if src != dst and row['src'] not in targets and src.exists():
            return False, '原位置仍有文件：'+row['src']
    unexpected = set(files) - targets
    if unexpected:
        return False, '范围内仍有未归集或新增的文件：'+'、'.join(sorted(unexpected)[:5])
    detail = f"执行前清单 {len(snapshot['files'])} 个文件的目标位置与内容核验通过"
    if snapshot['skipped']: detail += f"；隐藏项和符号链接 {snapshot['skipped']} 项不在清单内"
    return True, detail


# Execution-receipt conditions: the collection whose local executor writes the
# receipt, and the operations that receipt verifies. Protocol data, not routing.
RECEIPT_CONDITIONS = {
    'plan_applied': ('classification_plans', lambda sb, op: op['effect'] == 'move',
                     '分类计划执行（apply_classification，或 prepare_file_plan 的 classification_plan_id）'),
    'receipt_verified': ('receipts', lambda sb, op: op['effect'] == 'trash' and _kind(sb, op.get('source')) == 'file',
                         '去重执行（deduplicate 或 apply_dedup）'),
}


def _kind(sb, path):
    if not path: return None
    p = sb.path(path, exact=True)
    if p.is_file(): return 'file'
    if p.is_dir(): return 'empty_directory' if not any(p.iterdir()) else 'directory'
    return None


def _in_cleanup(sb, c, path):
    return (path is not None and within(sb, path, [c['path']]) and
            not any(within(sb, path, [x]) for x in c.get('exclude_paths', [])))


def claims(task, sb, goal_id, index, c, op, producers=()):
    """Whether this condition is the purpose of the operation, decided locally."""
    kind, effect, src, dst = c['kind'], op['effect'], op.get('source'), op.get('destination')
    if kind in RECEIPT_CONDITIONS:
        product, work, _ = RECEIPT_CONDITIONS[kind]
        return product in producers and work(sb, op)
    if kind == 'file_plan_verified':
        return 'file_plans' in producers
    if kind == 'files_placed':
        rows = (task.get('completion_snapshots', {}).get(condition_key(goal_id, index)) or {}).get('files', [])
        if effect in {'move', 'rename'}:
            return any(r['src'] == src and r['dst'] == dst for r in rows)
        return effect == 'create' and any(Path(r['dst']).is_relative_to(dst) for r in rows)
    if kind == 'empty_directories_removed':
        return effect == 'trash' and _in_cleanup(sb, c, src) and _kind(sb, src) == 'empty_directory'
    if kind == 'path_absent':
        return effect != 'create' and bool(src) and within(sb, c['path'], [src])
    if kind == 'path_exists':
        return effect != 'trash' and bool(dst) and within(sb, c['path'], [dst])
    if kind == 'directory_empty':
        return effect != 'create' and bool(src) and within(sb, src, [c['path']])
    return False


def _claimed(task, sb, goal_ids, op, producers, skip=()):
    goals = {g['id']: g for g in task['goals']}
    return [gid for gid in goal_ids if any(c['kind'] not in skip and claims(task, sb, gid, i, c, op, producers)
                                           for i, c in enumerate(goals[gid]['conditions']))]


def attribute(task, sb, op, candidates, producers=()):
    """Goals an operation serves: those whose conditions claim it, else all candidates.

    The fallback is conservative: an unexplained action carries every candidate
    goal's constraints. Task-wide constraints apply regardless.
    """
    return _claimed(task, sb, candidates, op, producers) or list(candidates)


def require_receipt_producers(task, sb, bound):
    """Reject work whose receipt condition this executor can never satisfy."""
    from agent.verifier import _reference
    goals = {g['id']: g for g in task['goals']}
    for op, goal_ids, producers in bound:
        for gid in goal_ids:
            for c in goals[gid]['conditions']:
                if c['kind'] not in RECEIPT_CONDITIONS: continue
                product, work, executor = RECEIPT_CONDITIONS[c['kind']]
                if product in producers or not work(sb, op): continue
                try: bound_refs = _reference(c, task, product, gid)
                except ValueError: bound_refs = ['ambiguous']
                if bound_refs: continue
                if getattr(getattr(sb, 'runtime', None), 'protocol', 'v2') == 'v3':
                    fix = '若这部分并非用户要求的去重，先用 revise_task 修正部分类型'
                else:
                    fix = ('若此条件与操作不符，先 edit_task 改为相符的条件'
                           '（普通文件计划用 file_plan_verified，reference 留空由本地绑定）')
                raise ValueError(f"目标 {gid} 的完成条件 {c['kind']} 只能由{executor}的回执满足；"
                                 f"当前对 {op.get('source')} 的 {op['effect']} 不会产生该回执，执行后也无法核验。"
                                 "请改用对应执行器；" + fix)


def enforce_cleanup_condition(task, sb, bound):
    """Per action: cleanup protects actions attributed to it, not other purposes."""
    goals = {g['id']: g for g in task['goals']}
    for op, goal_ids, producers in bound:
        src = op.get('source')
        if op['effect'] != 'trash' or not src: continue
        for gid in goal_ids:
            for c in goals[gid]['conditions']:
                if c['kind'] != 'empty_directories_removed' or not _in_cleanup(sb, c, src): continue
                # A missing source is the tool's own error, not a cleanup violation.
                if _kind(sb, src) in {None, 'empty_directory'}: continue
                # Another condition (e.g. a dedup receipt) explains this action.
                if _claimed(task, sb, goal_ids, op, producers, skip={'empty_directories_removed'}): continue
                raise ValueError('空目录清理目标只允许回收空文件夹：'+src)
