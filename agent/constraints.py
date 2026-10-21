"""Declarative operation predicates, evaluated locally before each mutation."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from agent.artifacts import digest
from agent.policy import active_goals, applies


def within(sb, path, roots):
    p = sb.path(path, exact=True)
    return any(p.is_relative_to(sb.path(root, exact=True)) for root in roots)


def validate_spec(task, sb):
    for root in task['read_roots'] + task['write_roots']:
        if any(p.startswith('.') for p in Path(root).parts if p != '.'):
            raise ValueError('任务范围不能包含内部或隐藏目录')
        sb.path(root, exact=True)
    for goal in task['goals']:
        for c in goal['conditions']:
            if c.get('evaluation', 'current_state') == 'execution_fact' and c['kind'] not in {'plan_applied', 'receipt_verified'}:
                raise ValueError('execution_fact 仅支持有本地执行回执的条件')
            if c['kind'] in {'path_exists', 'path_absent', 'directory_empty', 'sha256', 'files_placed', 'empty_directories_removed'}:
                if not c['path'] or not within(sb, c['path'], task['read_roots']):
                    raise ValueError('完成条件路径超出读取范围')
            if c['kind'] == 'files_placed':
                if c.get('placement') == 'mapping':
                    if not c.get('placements') or any(not within(sb, dst, task['read_roots']) for dst in c['placements'].values()):
                        raise ValueError('路径映射必须提供读取范围内的完整目标')
                elif not c.get('destination') or not within(sb, c['destination'], task['read_roots']):
                    raise ValueError('全量归集必须提供读取范围内的 destination')
            for excluded in c.get('exclude_paths', []): sb.path(excluded, exact=True)
            if c['kind'] == 'tool_success':
                from tools.registry import TOOLS
                tool = TOOLS.get(c['tool'])
                if tool and tool.writes:
                    raise ValueError('写操作不能仅以 tool_success 作为完成条件；全量归集用 files_placed，空目录清理用 empty_directories_removed，或使用路径/哈希/执行回执条件')
    for constraint in task['constraints']:
        if set(constraint.get('goal_ids', [])) - {g['id'] for g in task['goals']}:
            raise ValueError('约束引用了不存在的目标')
        for rule in constraint.get('rules', []):
            if rule['predicate'] in {'within', 'outside'}:
                for path in rule['values']: sb.path(path, exact=True)


def operation(effect, source=None, destination=None):
    return dict(effect=effect, source=source, destination=destination)


def creations(sb, path):
    target = sb.path(path, exact=True)
    return [operation('create', destination=sb.rel(p)) for p in [*reversed(target.parents), target]
            if p.is_relative_to(sb.root) and p != sb.root and not p.exists()]


def _members(sb, op):
    """Directory effects include descendants; scope checks must include them."""
    source = op.get('source')
    if not source:
        return [op]
    p = sb.path(source, exact=True)
    if not p.is_dir():
        return [op]
    members = [op]
    for base, dirs, files in os.walk(p, followlinks=False):
        for name in dirs + files:
            child = Path(base) / name
            if child.is_symlink():
                raise ValueError('目录操作包含符号链接，需先单独处理')
            dst = op.get('destination')
            members.append(operation(op['effect'], sb.rel(child),
                                     (Path(dst)/child.relative_to(p)).as_posix() if dst else None))
    return members


def validate_operations(runtime, operations):
    task, sb = runtime.current, runtime.sb
    if not task:
        raise ValueError('没有任务，不能执行写操作')
    if task.get('cancelled'):
        raise ValueError('任务已取消')
    if runtime.store.get(task['id'])['revision'] != task['revision']:
        raise ValueError('任务版本已改变，旧操作未执行')
    operations = list({(o['effect'], o.get('source'), o.get('destination')): o for o in operations}.values())
    from agent.completion import attribute, enforce_cleanup_condition, require_receipt_producers
    goal_ids = active_goals(runtime)
    fallback = tuple(getattr(runtime, '_producers', ()) or ())
    # Each action carries its own goals; one step label never stands for all uses.
    bound = []
    for op in operations:
        candidates = op.get('goal_ids') or goal_ids
        if not candidates:
            raise ValueError('复合任务必须先明确当前步骤对应目标')
        producers = tuple(op.get('producers') or fallback)
        bound.append((op, attribute(task, sb, op, candidates, producers), producers))
    require_receipt_producers(task, sb, bound)
    enforce_cleanup_condition(task, sb, bound)
    decisions = {}
    for r in task.get('reviews', []):
        if r['revision'] == task['revision']:
            for cid in r.get('constraint_ids', []): decisions[cid] = r['decision']
    accepted = {cid for cid, decision in decisions.items() if decision == 'accept'}
    for op, op_goals, _ in bound:
        constraints = [c for c in task['constraints'] if applies(c, op_goals)]
        if any(not c['forbidden_effects'] and not c.get('rules') and c['id'] not in accepted
               for c in constraints):
            raise ValueError('存在尚未映射为本地检查、也未经用户复核的文本约束')
        forbidden = {e for c in constraints for e in c['forbidden_effects']}
        if op['effect'] in forbidden or op['effect'] not in task['allowed_effects']:
            raise ValueError('操作副作用违反任务约束')
        for member in _members(sb, op):
            for path in (member.get('source'), member.get('destination')):
                if path is not None and not within(sb, path, task['write_roots']):
                    raise ValueError('操作包含任务写入范围外的对象：'+path+'；effect='+member['effect'])
        attributions = getattr(runtime, '_attributions', None)
        if attributions is not None:
            attributions[op_key(op['effect'], op.get('source'), op.get('destination'))] = op_goals
    pending = []
    oplog = getattr(runtime, '_oplog', None)
    if oplog and oplog.path.exists():
        pending = [dict(json.loads(line), goal_ids=goal_ids) for line in
                   oplog.path.read_text().splitlines()[runtime._journal_offset:] if line.strip()]
    prior_run = runtime.operations + pending
    for constraint in task['constraints']:
        scoped = [op for op, op_goals, _ in bound if applies(constraint, op_goals)]
        if not scoped: continue
        for rule in constraint.get('rules', []):
            selected = [o for o in scoped if o['effect'] in rule['effects']]
            kind = rule['predicate']
            if kind == 'max_count':
                prior = prior_run
                if rule.get('count_scope', 'run') == 'task':
                    prior = task.get('operation_history', []) + prior_run
                else:
                    prior = [row for row in task.get('operation_history', [])
                             if row.get('run_id') == runtime.run_id] + prior_run
                # Legacy rows with unknown attribution count conservatively.
                prior = [row for row in prior if not row.get('goal_ids') or applies(constraint, row['goal_ids'])]
                count = sum(('create' if row['op'] == 'make_dir' else row['op']) in rule['effects'] for row in prior)
                if count + len(selected) > rule['limit']:
                    raise ValueError(f"约束 {constraint['id']}：超过{rule.get('count_scope', 'run')}操作数量上限 {rule['limit']}")
                continue
            for op in selected:
                members = _members(sb, op) if kind in {'within', 'outside', 'extensions', 'name_glob', 'content_unchanged'} else [op]
                for member in members:
                    source, dest = member.get('source'), member.get('destination')
                    path = member.get(rule['selector'])
                    ok = True
                    if kind in {'within', 'outside'}:
                        ok = path is not None and within(sb, path, rule['values'])
                        if kind == 'outside': ok = path is not None and not within(sb, path, rule['values'])
                    elif kind == 'extensions':
                        # Folder parents have no extension; inspect their file members.
                        ok = path is not None
                        if path and not sb.path(path, exact=True).is_dir():
                            ok = Path(path).suffix.lower().lstrip('.') in {v.lower().lstrip('.') for v in rule['values']}
                    elif kind == 'name_glob':
                        ok = path is not None and any(fnmatch.fnmatchcase(Path(path).name, v) for v in rule['values'])
                    elif kind == 'empty_directory':
                        p = sb.path(source, exact=True) if source else None
                        ok = p is not None and p.is_dir() and not any(p.iterdir())
                    elif kind == 'same_extension':
                        ok = bool(source and dest) and Path(source).suffix.lower() == Path(dest).suffix.lower()
                    elif kind == 'content_unchanged' and source:
                        p = sb.path(source, exact=True)
                        if p.is_file():
                            actual = digest(p)
                            if source in runtime._content_fingerprints and runtime._content_fingerprints[source] != actual:
                                raise ValueError('确认期间源文件内容改变，操作未执行')
                            runtime._content_fingerprints.setdefault(source, actual)
                    if not ok:
                        raise ValueError(f"约束 {constraint['id']} 未满足：{kind}，对象 {path or source or dest}")
    return bound


def op_key(effect, source=None, destination=None):
    # Trash destinations are generated; create has no source.
    if effect == 'create': return ('create', destination)
    if effect == 'trash': return ('trash', source)
    return (effect, source, destination)


def enforce(sb, operations):
    runtime = getattr(sb, 'runtime', None)
    if hasattr(runtime, 'validate_operations'):
        runtime.validate_operations(operations)
