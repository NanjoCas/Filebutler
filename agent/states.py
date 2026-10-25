"""Flow, result and verification as separate dimensions (routing redesign S6a, 7).

A pure reducer over facts the run already keeps: the stop reason, the report, the
task (checks, operations, questions) and file plans. It decides nothing new: the
old task status, ``report.state`` and ``display`` stay the product's answer, and
these dimensions sit beside them in ``report['states']``. The model cannot raise
any of them; verification comes from the local checks only.

    flow_state    active | waiting_input | ended | interrupted | cancelled
    result_state  answered | fulfilled | partial | no_result
    verification  mechanical: not_applicable | pending | passed | failed | unknown
                  semantic:   not_required | unreviewed | accepted | rejected
    next_action   none | answer_question | confirm_result | recover | retry

Parts keep their old outcome (done/review/unfinished/not_needed/by_choice) and get
the same two verification fields; a part the user's choice left out is
``skipped_by_user``. Applied to saved turns too (eval/judge.py), so historical
results are projected by the same code: ``PROJECTION_VERSION`` names this reducer.
"""

PROJECTION_VERSION = 1
MACHINE = {'evidence_valid', 'file_plan_verified', 'receipt_verified', 'plan_ready', 'plan_applied'}
# Stops where the run gave up before an ending; the user can retry the same request.
RETRY = {'max_steps', 'cost_budget', 'token_budget', 'repair_limit', 'repeated_call',
         'unstructured_terminal', 'exception', 'interrupted'}


def _ops(task, operations):
    rows = list((task or {}).get('operation_history') or []) + list((task or {}).get('operations') or [])
    rows += [o for o in operations or () if o not in rows]
    return rows


def _accepted(task):
    """Goal ids the user accepted or rejected in this task revision (later reviews win)."""
    latest = {}
    for r in (task or {}).get('reviews', []):
        if r.get('revision') == task.get('revision'):
            for g in r.get('goal_ids', []): latest[g] = r['decision']
    return latest


def part_states(task):
    """Each part: its old outcome plus mechanical and semantic verification."""
    reviews = _accepted(task)
    parts = []
    for check in (task or {}).get('checks', []):
        machine = [c for c in check.get('conditions', []) if c.get('kind') in MACHINE]
        if check.get('outcome') == 'by_choice':
            mechanical = 'not_applicable'
        elif not check.get('passed'):
            mechanical = 'failed' if machine or check.get('conditions') else 'pending'
        else:
            mechanical = 'passed' if machine else 'not_applicable'
        decision = reviews.get(check['goal_id'])
        if decision == 'accept': semantic = 'accepted'
        elif decision == 'reject': semantic = 'rejected'
        elif check.get('needs_review') and check.get('passed'): semantic = 'unreviewed'
        else: semantic = 'not_required'
        parts.append(dict(id=check['goal_id'], outcome=check.get('outcome') or ('done' if check.get('passed') else 'unfinished'),
                          mechanical=mechanical, semantic=semantic,
                          skipped_by_user=check.get('outcome') == 'by_choice'))
    return parts


def _mechanical(parts, has_task, unknown):
    if unknown: return 'unknown'
    if not has_task: return 'not_applicable'
    if not parts: return 'pending'
    kinds = {p['mechanical'] for p in parts}
    if 'failed' in kinds: return 'failed'
    if 'pending' in kinds: return 'pending'
    return 'passed' if 'passed' in kinds else 'not_applicable'


def _constraints_unreviewed(task):
    """A user limit only a person can judge (a free-text note), as task_state reads it:
    round 39 dedup_cleanup's needs_review came from such a note, not from a part."""
    if not task: return False
    accepted = {c for r in task.get('reviews', []) if r.get('revision') == task.get('revision')
                and r.get('decision') == 'accept' for c in r.get('constraint_ids', [])}
    return any(not c.get('forbidden_effects') and not c.get('rules') and c.get('id') not in accepted
               for c in task.get('constraints', []))


def _semantic(parts, task=None):
    kinds = {p['semantic'] for p in parts}
    if _constraints_unreviewed(task): kinds.add('unreviewed')
    for kind in ('rejected', 'unreviewed', 'accepted'):
        if kind in kinds: return kind
    return 'not_required'


def project(stop_reason, report=None, task=None, *, operations=(), plans=(), error_type=None):
    """The dimensions of one finished run. ``plans``: the task's file plans (for inflight steps)."""
    report = report or {}
    task = task or None
    state = report.get('state')
    ops = _ops(task, operations)
    unknown = any(p.get('inflight') for p in plans or ())
    parts = part_states(task)
    has_task = bool(task and task.get('goals'))
    mechanical = _mechanical(parts, has_task, unknown)
    semantic = _semantic(parts, task)
    by_choice = report.get('result') == 'by_choice'
    if error_type or stop_reason in RETRY or state in {'failed', 'interrupted', 'unstructured_terminal'}:
        flow = 'interrupted'
    elif stop_reason == 'paused' or state == 'paused' or (task or {}).get('status') == 'paused':
        flow = 'interrupted'
    elif stop_reason == 'cancelled' or state == 'cancelled':
        flow = 'cancelled'
    elif stop_reason == 'waiting_input' or state == 'waiting_input':
        flow = 'waiting_input'
    else:
        flow = 'ended'
    if flow in {'interrupted', 'cancelled', 'waiting_input'}:
        result = 'partial' if ops else 'no_result'
    elif by_choice:
        result = 'fulfilled'  # the request as the user narrowed it (S5 audit 4.3), with or without a task
    elif not has_task:
        # unverified_answer: blocked calls, nothing provable.
        result = 'partial' if stop_reason == 'unverified_answer' and ops else \
                 'no_result' if stop_reason == 'unverified_answer' else 'answered'
    elif mechanical in {'failed', 'pending'} or (task or {}).get('constraint_failures') or state == 'partial':
        result = 'partial'
    else:
        result = 'fulfilled' if task.get('allowed_effects') or ops else 'answered'
    if flow == 'waiting_input': next_action = 'answer_question'
    elif unknown: next_action = 'recover'
    elif flow == 'interrupted' and stop_reason != 'paused' and state != 'paused' and (task or {}).get('status') != 'paused':
        next_action = 'retry'
    elif flow == 'ended' and ops and semantic == 'unreviewed': next_action = 'confirm_result'
    else: next_action = 'none'
    return dict(version=PROJECTION_VERSION, flow_state=flow, result_state=result,
                verification=dict(mechanical=mechanical, semantic=semantic), next_action=next_action,
                business=report.get('result') or None, registered=has_task, old_state=state, parts=parts)


def project_run(runtime, stop_reason, report, error_type=None):
    """Project a live run: the runtime's task, this run's operations and the task's plans."""
    task = getattr(runtime, 'current', None)
    plans = []
    if task:
        for pid in (task.get('file_plan_bindings') or {}):
            try: plans.append(runtime.store.file_plan(pid))
            except (ValueError, KeyError, AttributeError): pass
    return project(stop_reason, report, task, operations=getattr(runtime, 'operations', ()), plans=plans,
                   error_type=error_type)
