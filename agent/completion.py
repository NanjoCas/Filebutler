"""File selections, packages and the local attribution of operations to task goals."""
import os
from pathlib import Path


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


# Execution-receipt conditions: the collection whose local executor writes the
# receipt, and the operations that receipt verifies. Protocol data, not routing.
RECEIPT_CONDITIONS = {
    'plan_applied': ('classification_plans', lambda sb, op: op['effect'] == 'move',
                     '分类计划执行（prepare_file_plan 的 classification_plan_id）'),
    'receipt_verified': ('receipts', lambda sb, op: op['effect'] == 'trash' and _kind(sb, op.get('source')) == 'file',
                         '去重执行（deduplicate 或 apply_dedup）'),
}


def _kind(sb, path):
    if not path: return None
    p = sb.path(path, exact=True)
    if p.is_file(): return 'file'
    if p.is_dir(): return 'empty_directory' if not any(p.iterdir()) else 'directory'
    return None


def claims(task, sb, goal_id, index, c, op, producers=()):
    """Whether this condition is the purpose of the operation, decided locally."""
    kind = c['kind']
    if kind in RECEIPT_CONDITIONS:
        product, work, _ = RECEIPT_CONDITIONS[kind]
        return product in producers and work(sb, op)
    return kind == 'file_plan_verified' and 'file_plans' in producers


def _claimed(task, sb, goal_ids, op, producers):
    goals = {g['id']: g for g in task['goals']}
    return [gid for gid in goal_ids if any(claims(task, sb, gid, i, c, op, producers)
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
                fix = '若这部分并非用户要求的去重，先用 revise_task 修正部分类型'
                raise ValueError(f"目标 {gid} 的完成条件 {c['kind']} 只能由{executor}的回执满足；"
                                 f"当前对 {op.get('source')} 的 {op['effect']} 不会产生该回执，执行后也无法核验。"
                                 "请改用对应执行器；" + fix)
