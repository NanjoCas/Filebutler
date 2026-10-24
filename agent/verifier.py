"""Local completion predicates. Evidence validity is not semantic correctness."""
from agent import choices
from agent.artifacts import digest
from agent.task import LEGACY_NOTE, is_legacy
from safety.sandbox import SandboxError


def _in_scope(task, sb, path):
    return any(sb.path(path, exact=True).is_relative_to(sb.path(root, exact=True))
               for root in task['read_roots'])


CONSUMERS = {'evidence': {'evidence_valid'}, 'classification_plans': {'plan_ready', 'plan_applied'},
             'receipts': {'receipt_verified'}}


def _reference(condition, task, collection, goal_id=None):
    explicit = condition.get('reference', '').removeprefix('evidence:').removeprefix('receipt:')
    if explicit:
        return [explicit]
    refs = list(task.get('references', [])) + [ref for obs in task['observations'] if obs['status']=='ok'
                                            for ref in obs.get('references', [])]
    # Untagged refs (produced outside any step) bind only when one goal alone
    # verifies this collection; otherwise they cannot be guessed across goals.
    consumers = [g['id'] for g in task['goals']
                 if any(c['kind'] in CONSUMERS.get(collection, ()) for c in g.get('conditions', []))]
    sole = len(task['goals']) == 1 or consumers == [goal_id]
    matches = list(dict.fromkeys(ref['id'] for ref in refs if ref['collection']==collection and
        (goal_id in ref.get('goal_ids', []) or (not ref.get('goal_ids') and sole))))
    if len(matches) > 1 and collection == 'classification_plans':
        raise ValueError('完成条件存在多个候选引用，请明确 reference，不能把历史对象全部当作本目标证据')
    return matches


def _step_key(step):
    return (step['effect'], step['source'], None if step['effect'] == 'trash' else step['destination'])


def _later(receipts, pid):
    """Executed steps of the task's other plans, which may carry this plan's results on."""
    return [s for other, r in receipts.items() if other != pid
            for s in r['payload']['steps'] if s['id'] in r.get('progress', {})]


def _goal_steps(plan, goal_id):
    """The steps attributed to this goal, and whether any of them proves it.

    A parent directory serves its actions' goals but proves none of them unless it
    is this goal's alone (a folder the user asked for). A business step owned by
    several goals proves none: such bindings only exist in plans prepared before
    strict attribution (2026-10-24) and stay frozen rather than pass every goal.
    """
    mine, proof = set(), False
    for s in plan['payload']['steps']:
        owners = s.get('goal_ids') or plan['payload']['goal_ids']
        if goal_id not in owners: continue
        if s['effect'] != 'create' and len(owners) > 1: continue
        mine.add(s['id'])
        proof = proof or s['effect'] != 'create' or len(owners) == 1
    return mine if proof else set()


def verify_bound_plans(task, goal_id, condition, sb):
    """file_plan_verified: an explicit X, or this goal's own steps in the plans bound to it.

    A plan serving several goals proves each one only by the steps attributed to
    it, so one goal's finished steps pass even when another goal's steps in the
    same plan stopped; a goal with no executed step of its own does not pass.
    """
    from agent.file_plans import verify_plan
    receipts = task.get('file_plan_receipts', {})
    # Plans that stopped midway keep their executed steps; a completed receipt supersedes them.
    executed = {**task.get('file_plan_partials', {}), **receipts}
    bindings = task.get('file_plan_bindings', {})
    if condition.get('reference'):
        plan = receipts.get(condition['reference'])
        if not plan:
            return False, '尚无此计划的完整执行回执：'+condition['reference']
        return verify_plan(plan, sb, later=_later(receipts, condition['reference']))
    bound = [pid for pid, b in bindings.items() if goal_id in b['goal_ids']]
    # A step a later plan of this task carried out counts as done.
    covered = {_step_key(s) for p in executed.values() for s in p['payload']['steps'] if s['id'] in p.get('progress', {})}
    checked, waiting = 0, []
    for pid in bound:
        plan = executed.get(pid)
        if plan is None:
            waiting.append(pid)
            continue
        if plan['payload'].get('empty_selection'):
            ok, detail = verify_plan(plan, sb)
            if not ok: return False, detail
            checked += 1
            continue
        mine = _goal_steps(plan, goal_id)
        if not mine: continue
        if plan.get('inflight') in mine:
            return False, f"计划 {pid} 的一项操作中断，结果不明：{plan['inflight']}"
        missing = [s for s in plan['payload']['steps'] if s['id'] in mine and s['id'] not in plan['progress']
                   and _step_key(s) not in covered]
        if missing:
            return False, f'计划 {pid} 部分执行后未完成：'+'、'.join(str(s['source'] or s['destination']) for s in missing[:3])
        ok, detail = verify_plan(plan, sb, partial=True, later=_later(executed, pid),
                                 only=mine & set(plan['progress']))
        if not ok: return False, detail
        checked += 1
    if not checked:
        return False, ('已准备的计划尚未执行完成：'+'、'.join(waiting[:3]) if waiting
                       else '本目标尚无已执行的文件计划')
    return True, f"本目标在 {checked} 个文件计划中的操作，位置与内容核验通过"


def _followed(task, receipt, path):
    """Where this task's own later logged moves put a receipt object.

    Only operations recorded after the receipt's actions count, so a later step
    of the same task (e.g. filing the kept copy) does not undo the receipt; an
    unlogged move still fails the content check at the old path.
    """
    ops = task.get('operation_history', []) + task.get('operations', [])
    moved = {tuple(m) for m in receipt.get('moved', [])}
    done = [i for i, op in enumerate(ops) if (op.get('src'), op.get('dst')) in moved]
    if moved and len(done) < len(moved):
        return path
    for op in ops[max(done) + 1 if done else 0:]:
        if op.get('op') in {'move', 'rename'} and op.get('src') and (
                path == op['src'] or path.startswith(op['src'].rstrip('/') + '/')):
            path = op['dst'] + path[len(op['src']):]
    return path


def verify_task(task, sb):
    if is_legacy(task):
        # v2-era conditions are no longer verified; the task is shown read-only.
        return [dict(goal_id=g['id'], description=g['description'], outcome='unfinished', passed=False, needs_review=False,
                     legacy=True, notes=[],
                     conditions=[dict(kind=c['kind'], passed=False, needs_review=False, detail=LEGACY_NOTE)
                                 for c in g['conditions']]) for g in task['goals']]
    checks = []
    store = getattr(sb, 'artifacts', None)
    # After a choice (user decision 2026-10-22), completion follows the chosen scope.
    scope, skipped = choices.chosen_scope(task)
    for goal in task['goals']:
        rows = []
        for index, c in enumerate(goal['conditions']):
            kind, ok, review, detail = c['kind'], False, False, ''
            try:
                if kind == 'file_plan_verified':
                    ok, detail = verify_bound_plans(task, goal['id'], c, sb)
                elif kind == 'manual_review':
                    ok, review = True, True
                elif kind == 'evidence_valid':
                    # A goal may use several independent fragments.
                    refs = _reference(c, task, 'evidence', goal['id'])
                    ok = bool(refs)
                    trashed = {op['dst']: op['src'] for op in task.get('operation_history', []) + task.get('operations', [])
                               if op.get('op') == 'trash'}
                    for eid in refs:
                        ev = store.resolve_evidence(eid)
                        _, row = store.resolve(ev['file_id'])
                        # A fragment stays valid only if the file is visible, or
                        # was recycled by this task's own logged operation.
                        path = row['path'] if row['status'] != 'trash' else trashed.get(row['path'])
                        ok = ok and bool(path) and _in_scope(task, sb, path)
                    review = True
                elif kind in {'plan_ready', 'plan_applied'}:
                    refs = _reference(c, task, 'classification_plans', goal['id'])
                    ok = bool(refs)
                    for pid in refs:
                        plan = store.data['classification_plans'][pid]
                        assignments = plan['assignments']
                        ids = [a['file_id'] for a in assignments]
                        formed = (bool(ids) and len(ids)==len(set(ids)) and set(ids)==set(plan['versions'])
                                  and all(1 <= a['category'] <= len(plan['categories']) for a in assignments))
                        ok = ok and formed
                        for fid, version in plan['versions'].items():
                            _, row = store.resolve(fid, allow_trash=False)
                            ok = ok and row['version'] == version and _in_scope(task, sb, row['path'])
                        if kind == 'plan_ready':
                            # A formed plan remains formed after execution. The
                            # execution state is checked separately below.
                            ok = ok and plan['status'] in {'ready','applied','partial'}
                            detail = '分类方案已完整形成，文件版本与范围有效' if ok else '分类方案不完整、文件版本变化或范围不符'
                            review = True
                        else:
                            execution = plan.get('last_execution', {})
                            ok = ok and plan['status'] == 'applied' and execution.get('verified', False)
                            # Recheck current locations, not only the old applied flag.
                            ok = ok and 'manifest' in execution
                            for move in execution.get('manifest', []):
                                if not _in_scope(task, sb, move['src']) or not _in_scope(task, sb, move['dst']):
                                    ok = False
                                if move['src'] == move['dst']:
                                    continue
                                ok = ok and not sb.path(move['src'], exact=True).exists()
                                target = sb.path(move['dst'], exact=True)
                                ok = ok and target.is_file() and digest(target) == plan['versions'][move['file_id']]
                            if not ok:detail='分类计划尚未全部执行，或执行后位置/内容核验未通过：'+pid
                    if not refs:detail=f'{kind} 只核验分类计划（P 标识）；本目标没有绑定的分类计划，普通文件计划不能满足此条件'
                elif kind == 'receipt_verified':
                    refs = _reference(c, task, 'receipts', goal['id'])
                    ok = bool(refs)
                    for rid in refs:
                        receipt = store.data['receipts'][rid]
                        ok = ok and receipt['verified']
                        for src, dst in receipt['moved']:
                            ok = ok and _in_scope(task, sb, src)
                            # Trash is an internal location, inaccessible as an ordinary tool path.
                            target = store._path(dst, trash=True)
                            ok = ok and digest(target) == receipt.get('versions', {}).get(dst)
                            ok = ok and not sb.path(src, exact=True).exists()
                        for keeper in receipt['keeps']:
                            ok = ok and _in_scope(task, sb, keeper)
                            p = sb.path(_followed(task, receipt, keeper), exact=True)
                            ok = ok and p.is_file()
                            ok = ok and p.is_file() and digest(p) == receipt.get('versions', {}).get(keeper)
                if not ok and not detail:
                    detail = '完成条件未满足'
            except (ValueError, KeyError, OSError, AttributeError, SandboxError) as exc:
                ok, detail = False, str(exc)
            rows.append(dict(kind=kind, passed=bool(ok), needs_review=review, detail=detail))
        checks.append(_settle(task, goal, rows, scope, skipped))
    return checks


# A part ends in one of five outcomes. The first three are settled locally from the
# conditions; the last two only explain why a part needs no work.
OUTCOMES = ('done', 'review', 'unfinished', 'not_needed', 'by_choice')


def _settle(task, goal, rows, scope, skipped):
    """One part's check: its condition rows, an outcome, and notes saying who explained what.

    A note is ``by`` the model (its declared reason), the user's choice, or local
    (a declaration the local facts contradict). Notes never pass a failed condition.
    """
    gid, notes, failed = goal['id'], [], False
    outcome = None
    if gid in skipped:
        rows, outcome = [], 'by_choice'
        notes.append(dict(by='choice', text=f'按你选择的选项 {scope}，此部分不执行'))
    declared = task.get('declared_unfinished') or {}
    if declared.get('revision') == task['revision']:
        for u in declared.get('not_needed', []):
            if u['part_id'] != gid: continue
            # "Nothing to do" cannot be proved here: allowed only for a part that
            # prepared no plan and wrote nothing, and then only as review.
            touched = any(gid in b['goal_ids'] for b in task.get('file_plan_bindings', {}).values()) or any(
                gid in (op.get('goal_ids') or []) for op in task.get('operation_history', []) + task.get('operations', []))
            if touched:
                failed = True
                notes.append(dict(by='local', text='模型声明无需操作，但本部分已有计划或操作，需按其结果核验：' + u['reason']))
            else:
                rows, outcome = [], 'not_needed'
                notes.append(dict(by='model', text='模型说明无需操作（本地未能证明）：' + u['reason']))
        for u in declared.get('items', []):
            if u['part_id'] != gid: continue
            if scope:
                notes.append(dict(by='choice', text=f'模型说明（已按你选择的选项 {scope} 完成）：' + u['reason']))
            else:
                failed = True
                notes.append(dict(by='model', text=u['reason']))
    passed = not failed and all(r['passed'] for r in rows)
    # A part the user's choice excluded has nothing left to judge.
    review = outcome == 'not_needed' or (goal['semantic_review'] and gid not in skipped) or any(r['needs_review'] for r in rows)
    if outcome is None or not passed:
        outcome = 'unfinished' if not passed else 'review' if review else 'done'
    return dict(goal_id=gid, description=goal['description'], outcome=outcome, passed=passed,
                needs_review=review, conditions=rows, notes=notes)
