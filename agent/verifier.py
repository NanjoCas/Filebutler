"""Local completion predicates. Evidence validity is not semantic correctness."""
from agent.artifacts import digest
from safety.sandbox import SandboxError


def _in_scope(task, sb, path):
    return any(sb.path(path, exact=True).is_relative_to(sb.path(root, exact=True))
               for root in task['read_roots'])


def _reference(condition, task, collection, goal_id=None):
    explicit = condition.get('reference', '').removeprefix('evidence:').removeprefix('receipt:')
    if explicit:
        return [explicit]
    refs = list(task.get('references', [])) + [ref for obs in task['observations'] if obs['status']=='ok'
                                            for ref in obs.get('references', [])]
    # Untagged legacy refs cannot be guessed across multiple goals.
    matches = list(dict.fromkeys(ref['id'] for ref in refs if ref['collection']==collection and
        (goal_id in ref.get('goal_ids', []) or
         (not ref.get('goal_ids') and len(task['goals']) == 1))))
    if len(matches) > 1 and collection == 'classification_plans':
        raise ValueError('完成条件存在多个候选引用，请明确 reference，不能把历史对象全部当作本目标证据')
    return matches


def _step_key(step):
    return (step['effect'], step['source'], None if step['effect'] == 'trash' else step['destination'])


def verify_bound_plans(task, goal_id, condition, sb):
    """file_plan_verified: an explicit X, or every plan locally bound to this goal."""
    from agent.file_plans import verify_plan
    receipts = task.get('file_plan_receipts', {})
    bindings = task.get('file_plan_bindings', {})
    if condition.get('reference'):
        plan = receipts.get(condition['reference'])
        if not plan:
            return False, '尚无此计划的完整执行回执：'+condition['reference']
        return verify_plan(plan, sb)
    bound = [pid for pid, b in bindings.items() if goal_id in b['goal_ids']]
    done = [pid for pid in bound if pid in receipts]
    if not done:
        waiting = [pid for pid in bound if pid not in receipts]
        return False, ('已准备的计划尚未执行完成：'+'、'.join(waiting[:3]) if waiting
                       else '本目标尚无已执行的文件计划')
    for pid in done:
        ok, detail = verify_plan(receipts[pid], sb)
        if not ok: return False, detail
    # A started plan that stopped midway must be finished by a later bound plan.
    covered = {_step_key(s) for pid in done for s in receipts[pid]['payload']['steps']}
    for pid in bound:
        missing = [k for k in bindings[pid].get('pending', []) if tuple(k) not in covered]
        if pid not in receipts and missing:
            return False, f'计划 {pid} 部分执行后未完成：'+'、'.join(str(k[1] or k[2]) for k in missing[:3])
    return True, f"本目标 {len(done)} 个文件计划的位置与内容核验通过"


def verify_task(task, sb):
    checks = []
    store = getattr(sb, 'artifacts', None)
    for goal in task['goals']:
        rows = []
        for index, c in enumerate(goal['conditions']):
            kind, ok, review, detail = c['kind'], False, False, ''
            try:
                if kind == 'file_plan_verified':
                    ok, detail = verify_bound_plans(task, goal['id'], c, sb)
                elif kind in {'files_placed', 'empty_directories_removed'}:
                    from agent.completion import verify_selection
                    ok, detail = verify_selection(task, goal['id'], index, c, sb)
                elif kind in {'path_exists', 'path_absent', 'directory_empty', 'sha256'}:
                    if not c['path']:
                        raise ValueError('验证路径不能为空')
                    p = sb.path(c['path'], exact=True)
                    if not _in_scope(task, sb, c['path']):
                        raise ValueError('验证对象超出当前任务读取范围')
                    exists = p.exists()
                    if kind == 'path_exists': ok = exists
                    elif kind == 'path_absent': ok = not exists
                    elif kind == 'directory_empty': ok = p.is_dir() and not any(p.iterdir())
                    else: ok = p.is_file() and digest(p) == c['sha256']
                    if not ok:
                        detail = {'path_exists':'路径不存在：','path_absent':'路径仍然存在：',
                                  'directory_empty':'目录不存在或仍有内容：','sha256':'文件缺失或内容哈希不符：'}[kind]+c['path']
                elif kind == 'tool_success':
                    # Explicit arguments are themselves the binding, so an untagged
                    # call (e.g. before set_task in this run) matches; another goal's
                    # step does not. Without arguments only this goal's calls count.
                    ok = any(o['status'] == 'ok' and o['tool'] == c['tool'] and
                             all(o['arguments'].get(k) == v for k, v in c['arguments'].items())
                             and (goal['id'] in o.get('goal_ids', []) or
                                  (not o.get('goal_ids') and (bool(c['arguments']) or len(task['goals']) == 1)))
                             for o in task['observations'])
                    # A successful invocation alone does not prove goal completion.
                    review = True
                    if not ok:detail='未找到满足参数的成功调用：'+c['tool']
                elif kind == 'manual_review':
                    ok, review = True, True
                elif kind == 'evidence_valid':
                    # A goal may use several independent fragments.
                    refs = _reference(c, task, 'evidence', goal['id'])
                    ok = bool(refs)
                    for eid in refs:
                        ev = store.resolve_evidence(eid)
                        _, row = store.resolve(ev['file_id'], allow_trash=False)
                        ok = ok and _in_scope(task, sb, row['path'])
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
                        historical = kind == 'plan_applied' and c.get('evaluation') == 'execution_fact'
                        if not historical:
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
                                if historical: continue
                                if move['src'] == move['dst']:
                                    continue
                                ok = ok and not sb.path(move['src'], exact=True).exists()
                                target = sb.path(move['dst'], exact=True)
                                ok = ok and target.is_file() and digest(target) == plan['versions'][move['file_id']]
                            if not ok:detail='分类计划尚未全部执行，或执行后位置/内容核验未通过：'+pid
                            elif historical: detail='本地回执证明此计划已执行；该条件不声明文件当前仍在原目标位置'
                    if not refs:detail=f'{kind} 只核验分类计划（P 标识）；本目标没有绑定的分类计划，普通文件计划不能满足此条件'
                elif kind == 'receipt_verified':
                    refs = _reference(c, task, 'receipts', goal['id'])
                    ok = bool(refs)
                    for rid in refs:
                        receipt = store.data['receipts'][rid]
                        ok = ok and receipt['verified']
                        for src, dst in receipt['moved']:
                            ok = ok and _in_scope(task, sb, src)
                            if c.get('evaluation') == 'execution_fact': continue
                            # Trash is an internal location, inaccessible as an ordinary tool path.
                            target = store._path(dst, trash=True)
                            ok = ok and digest(target) == receipt.get('versions', {}).get(dst)
                            ok = ok and not sb.path(src, exact=True).exists()
                        for keeper in receipt['keeps']:
                            ok = ok and _in_scope(task, sb, keeper)
                            if c.get('evaluation') == 'execution_fact': continue
                            p = sb.path(keeper, exact=True)
                            ok = ok and p.is_file()
                            ok = ok and p.is_file() and digest(p) == receipt.get('versions', {}).get(keeper)
                if not ok and not detail:
                    detail = '完成条件未满足'
            except (ValueError, KeyError, OSError, AttributeError, SandboxError) as exc:
                ok, detail = False, str(exc)
            rows.append(dict(kind=kind, passed=bool(ok), needs_review=review, detail=detail))
        checks.append(dict(goal_id=goal['id'], description=goal['description'],
                           passed=all(r['passed'] for r in rows),
                           needs_review=goal['semantic_review'] or any(r['needs_review'] for r in rows),
                           conditions=rows))
    return checks
