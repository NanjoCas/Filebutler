"""Multi-dimension acceptance judge for live policy runs; pure, offline, no API.

Each turn is judged on five dimensions:

- files        physical outcome checks computed by the runner (content, location,
               protection); undo/preservation are case-level file checks.
- status       the normalized run status must be in the case's accepted set.
               ``needs_review`` (all local checks passed, semantic review pending)
               is kept apart from ``partial``/``failed`` (genuinely unfinished).
- answer       main answer (``result.report.summary`` or ``result.final``):
               no recovered blocker shown as current, report state consistent
               with the runtime, no "unfinished" wording on a finished state,
               plus a few declared key fragments and structured finding checks.
- constraints  effects and write scope of this turn's operations, forbidden tool
               kinds (scan/write), product constraint failures and violations.
- efficiency   requests, tokens, cost, seconds, answer length: observation only,
               optional threshold warnings; never part of acceptance.

``accepted`` is true only when every required dimension passes.

Judgement v4 (routing redesign S6a, 7) is reported beside it, never instead:
``status_v4`` reads flow/result/verification/next_action (``report.states``, or
the same reducer, agent/states.py, applied to a saved turn) against expectations
derived from the frozen v3 status list by ``V4_FROM_V3``, frozen 2026-10-25
before any result was re-judged. ``accepted_v4`` = files, answer and constraints
as in v3, plus ``status_v4``. Exit codes still follow v3. Inputs are the
dicts the runner saves (``report.json`` turns, ``turn_N.trace.jsonl``), so the same
code judges a fresh run and re-judges frozen historical results.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


REQUIRED = ('files', 'status', 'answer', 'constraints')
DIMENSIONS = REQUIRED + ('efficiency',)
STATUS_CLASS = {'done': 'complete', 'needs_review': 'review', 'answered': 'answered',
                'waiting_input': 'waiting', 'partial': 'incomplete', 'cancelled': 'incomplete',
                'failed': 'failed'}
INCOMPLETE_WORDING = ('尚未全部完成',)
EFFECT = {'make_dir': 'create', 'create': 'create', 'move': 'move', 'rename': 'rename', 'trash': 'trash'}
# Control tools that act on files; registry tools are classified by their `writes` flag.
CONTROL_WRITE = {'execute_file_plan', 'apply_task_edit'}
CONTROL_SCAN = {'prepare_file_plan', 'inspect_file_plan'}
POSITIVE_RELATIONS = ('method', 'survey', '方法相关', '综述入口')
DEFAULT_WARN = dict(requests=15, cost=0.08, active_seconds=40, answer_chars=800)
JUDGE_VERSION = 'v3+v4.1'
# Frozen 2026-10-25 (docs/s6-state-dimensions-2026-10-25.md 3). A v3 status accepts a
# run in any of these states; "done" no longer requires a read-only answer's
# semantics to be reviewed (7: it is answered, its answer is checked separately),
# and a write waiting for the user's confirmation is still not "done".
V4_FROM_V3 = {
    # v3 "done" is a finished task (or an explanation of an earlier run); a reply without a
    # task is "answered", so here a bare answer needs a registered part, as before.
    'done': [dict(flow={'ended'}, result={'fulfilled'}, mechanical={'passed', 'not_applicable'}, next_action={'none'}),
             dict(flow={'ended'}, result={'answered'}, registered={True}, mechanical={'passed', 'not_applicable'},
                  next_action={'none'}),
             dict(flow={'ended'}, old_state={'explained'})],
    'answered': [dict(flow={'ended'}, result={'answered'}, mechanical={'not_applicable', 'passed'}, next_action={'none'}),
                 dict(flow={'ended'}, result={'fulfilled'}, business={'by_choice'}, next_action={'none'})],
    'needs_review': [dict(flow={'ended'}, result={'fulfilled', 'answered'}, mechanical={'passed', 'not_applicable'},
                          semantic={'unreviewed'})],
    'waiting_input': [dict(flow={'waiting_input'}, next_action={'answer_question'})],
    'partial': [dict(flow={'ended', 'interrupted'}, result={'partial'})],
    'cancelled': [dict(flow={'cancelled'})],
}


def _registry():
    try:
        from tools import registry
        import main  # noqa: F401  (registers all tools)
        return {name: tool.writes for name, tool in registry.TOOLS.items()}
    except Exception:  # judge must still run if product imports change
        return {}


_WRITES = None


def tool_kind(name):
    global _WRITES
    if _WRITES is None: _WRITES = _registry()
    if name in CONTROL_WRITE: return 'write' if name == 'execute_file_plan' else 'control'
    if name in CONTROL_SCAN: return 'scan'
    if name in _WRITES: return 'write' if _WRITES[name] else 'scan'
    return 'control'


# --------------------------------------------------------------------------- inputs

def load_trace(path):
    """Tool calls of one turn trace, in order. Missing file -> empty list."""
    path = Path(path)
    if not path.is_file(): return []
    calls = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip(): continue
        row = json.loads(line)
        if 'tool' in row and 'event' not in row:
            calls.append(dict(tool=row['tool'], ok=bool(row.get('ok')), executed=row.get('executed', True),
                              args=row.get('args') or '', result=row.get('result') or '', step=row.get('step')))
    return calls


def main_answer(turn):
    report = turn.get('report') or {}
    return report.get('summary') or turn.get('final') or ''


def normalize_status(turn):
    """Map stop_reason/task_status to done/needs_review/partial/answered/waiting_input/failed."""
    runtime = turn.get('runtime') or {}
    stop, task_status = turn.get('stop_reason'), runtime.get('task_status')
    if turn.get('error_type'): status = 'failed'
    elif stop == 'done' and task_status in {'completed', None, 'not_registered'}: status = 'done'
    elif stop in {'needs_review', 'partial', 'waiting_input', 'cancelled'}: status = stop
    elif stop == 'answered' and task_status in {None, 'not_registered'}: status = 'answered'
    else: status = 'failed'
    goals = runtime.get('checks') or []
    unmet = [g['goal_id'] for g in goals if not g.get('passed')]
    review = [g['goal_id'] for g in goals if g.get('passed') and g.get('needs_review')]
    if status == 'needs_review' and unmet:
        status = 'partial'  # never let "review" hide an unmet machine goal
    # The business result (S5 audit 2026-10-25): informational only; the status judgement
    # above is unchanged, and a versioned judgement may read this later.
    business = (turn.get('report') or {}).get('result') or {'done': 'fulfilled'}.get(status, status)
    return dict(status=status, klass=STATUS_CLASS[status], stop_reason=stop, task_status=task_status,
                unmet_goals=unmet, review_goals=review, business=business)


def turn_states(turn):
    """The run's dimensions: saved with it, or projected now by the same reducer."""
    states = (turn.get('report') or {}).get('states')
    if states: return states, 'saved'
    from agent.states import project
    plans = [p for p in turn.get('plans') or [] if isinstance(p, dict)]
    task = dict(turn['task']) if turn.get('task') else None
    checks = (turn.get('runtime') or {}).get('checks') or []
    if task is not None and 'checks' not in task and checks:
        # A minimal saved task: the parts as the runtime summary recorded them.
        task.update(checks=checks, goals=[dict(id=c['goal_id']) for c in checks])
    return project(turn.get('stop_reason'), turn.get('report') or {}, task,
                   plans=plans, error_type=turn.get('error_type')), 'projected'


def _matches(states, rule):
    values = dict(flow=states['flow_state'], result=states['result_state'],
                  mechanical=states['verification']['mechanical'], semantic=states['verification']['semantic'],
                  next_action=states['next_action'], business=states.get('business'),
                  registered=states.get('registered'), old_state=states.get('old_state'))
    return all(values[k] in allowed for k, allowed in rule.items())


def judge_status_v4(turn, expect):
    states, source = turn_states(turn)
    rules = [r for status in (expect.get('status') or ['done']) for r in V4_FROM_V3.get(status, [])]
    checks = dict(states_in_expected=any(_matches(states, r) for r in rules))
    if 'business' in expect:
        business = states.get('business') or {'fulfilled': 'fulfilled', 'answered': 'answered'}.get(states['result_state'])
        if states['flow_state'] == 'cancelled': business = states.get('business') or 'cancelled'
        checks['business_in_expected'] = business in expect['business']
    return checks, dict(states={k: v for k, v in states.items() if k != 'parts'}, source=source)


def turn_operations(turn, run_id):
    """This turn's recorded operations (ledger rows carry the oplog run id)."""
    task = turn.get('task') or {}
    rows, seen = [], set()
    for op in (task.get('operation_history') or []) + (task.get('operations') or []):
        if run_id and op.get('run_id', run_id) != run_id: continue
        key = op.get('action_id') or (op.get('op'), op.get('src'), op.get('dst'))
        if key in seen: continue
        seen.add(key); rows.append(op)
    return rows


def _first_line(message):
    for line in str(message).splitlines():
        if line.strip(): return line.strip()
    return ''


def _strip_error(text):
    text = str(text)
    for prefix in ('ERROR:', 'ValueError:', 'ToolError:'):
        text = text.strip()
        if text.startswith(prefix): text = text[len(prefix):]
    return text.strip()


def blocker_states(task, calls):
    """Blockers with a lifecycle status.

    Structured rows (status field, current product) are used as recorded. Legacy
    rows ({tool, message}) are inferred from the cumulative trace: the latest
    failed call of the same source whose error contains the message; resolved if
    a later call of that source succeeded, or a later execute_file_plan succeeded
    for a prepare_file_plan blocker. Unattributable rows stay ``open``.
    """
    out = []
    for b in (task or {}).get('blockers') or []:
        source = b.get('source') or b.get('tool') or ''
        message = b.get('message', '')
        if 'status' in b:
            out.append(dict(source=source, message=message, status=b['status'], inferred=False,
                            resolved_by=b.get('resolved_by', '')))
            continue
        head = _first_line(message)
        failed = [i for i, c in enumerate(calls) if c['tool'] == source and not c['ok']
                  and head and head in _strip_error(c['result'])]
        status, by = 'open', ''
        if failed:
            later = calls[failed[-1] + 1:]
            ok_same = [c for c in later if c['tool'] == source and c['ok']]
            ok_exec = [c for c in later if source == 'prepare_file_plan' and c['tool'] == 'execute_file_plan' and c['ok']]
            if ok_same or ok_exec:
                status, by = 'resolved', f"trace: later successful {(ok_same or ok_exec)[0]['tool']}"
        out.append(dict(source=source, message=message, status=status, inferred=True, resolved_by=by))
    return out


# --------------------------------------------------------------------------- dimensions

def _dim(checks, required=True, **extra):
    failed = [k for k, v in checks.items() if v is False]
    return dict(passed=not failed, required=required, checks=checks, failed=failed, **extra)


def judge_files(turn):
    checks = {k: bool(v) for k, v in (turn.get('checks') or {}).items()}
    if not checks: checks['physical_checks_recorded'] = False
    return checks


def judge_status(norm, expect):
    accepted = expect.get('status') or ['done']
    checks = dict(status_in_expected=norm['status'] in accepted)
    # Cases written after the S5 audit (eval/s5_cases.py) also state the business result;
    # cases that do not declare it are judged exactly as before.
    if 'business' in expect:
        checks['business_in_expected'] = norm['business'] in expect['business']
    return checks, dict(accepted=accepted, **norm)


def _findings(calls):
    rows = []
    for c in calls:
        if c['tool'] != 'report_findings' or not c['ok']: continue
        try: rows += json.loads(c['args']).get('findings', [])
        except (ValueError, AttributeError): pass
    return rows


def _table_relations(task):
    rels = []
    for rec in (task or {}).get('output_records') or []:
        if rec.get('tool') != 'report_findings': continue
        for line in rec.get('text', '').splitlines():
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) >= 3 and cells[0] not in {'文件', '---'} and not set(cells[0]) <= set('-: '):
                rels.append(cells[1])
    return rels


def judge_answer(turn, norm, expect, blockers, calls):
    spec = expect.get('answer') or {}
    text = main_answer(turn)
    checks, info = {}, {}
    stale = [b for b in blockers if b['status'] != 'open' and _first_line(b['message'])
             and _first_line(b['message']) in text]
    checks['no_recovered_blocker_in_answer'] = not stale
    info['recovered_blockers_shown'] = [dict(source=b['source'], message=_first_line(b['message'])[:160],
                                             inferred=b['inferred'], resolved_by=b['resolved_by']) for b in stale]
    report = turn.get('report') or {}
    if report.get('state') and norm['task_status'] not in {None, 'not_registered'}:
        checks['report_state_matches_runtime'] = report['state'] == norm['task_status']
    if norm['klass'] in {'complete', 'review'}:
        checks['no_unfinished_wording'] = not any(w in text for w in INCOMPLETE_WORDING)
    for i, group in enumerate(spec.get('include_any') or []):
        group = [group] if isinstance(group, str) else group
        checks[f'include_any[{i}]'] = any(g in text for g in group)
    for frag in spec.get('exclude') or []:
        checks[f'exclude:{frag}'] = frag not in text
    forbidden = spec.get('forbid_finding_relations')
    if forbidden:
        rels = [f.get('relation', '') for f in _findings(calls)] or _table_relations(turn.get('task'))
        bad = [r for r in rels if r in forbidden]
        checks['no_forbidden_finding_relation'] = not bad
        info['finding_relations'] = rels
    return checks, info


def _within(path, roots):
    p = PurePosixPath(str(path).strip('/'))
    return any(str(r) in {'', '.'} or p == PurePosixPath(r) or PurePosixPath(r) in p.parents for r in roots)


def snapshot_changes(before, after):
    """Relative paths added, removed or changed between two snapshots (trash excluded)."""
    if before is None or after is None: return None
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k) and not k.startswith('.trash'))


def judge_constraints(turn, expect, ops, calls, changes=None):
    spec = expect.get('constraints') or {}
    checks, info = {}, {}
    effects = sorted({EFFECT.get(o.get('op'), o.get('op')) for o in ops})
    info['effects'] = effects
    info['operations'] = [dict(op=o.get('op'), src=o.get('src'), dst=o.get('dst')) for o in ops]
    if 'effects' in spec:
        checks['effects_allowed'] = set(effects) <= set(spec['effects'])
    scope = spec.get('write_scope')
    if scope is not None:
        outside = []
        for o in ops:
            paths = [o.get('src')] if o.get('op') == 'trash' else [o.get('src'), o.get('dst')]
            outside += [p for p in paths if p and not _within(p, scope)]
        if changes is not None:
            outside += [p for p in changes if not _within(p, scope)]
        checks['writes_within_scope'] = not outside
        info['outside_scope'] = sorted(set(outside))
    if changes is not None and spec.get('effects') == []:
        checks['no_filesystem_change'] = not changes
    executed = [c for c in calls if c.get('executed', True)]
    kinds = {}
    for c in executed:
        kind = tool_kind(c['tool'])
        if kind == 'write' and not c['ok']: continue  # rejected writes changed nothing
        kinds.setdefault(kind, []).append(c['tool'])
    for kind in spec.get('forbid_tools') or []:
        checks[f'no_{kind}_tools'] = not kinds.get(kind)
    info['tool_kinds'] = {k: sorted(set(v)) for k, v in kinds.items()}
    task = turn.get('task') or {}
    checks['no_product_constraint_failure'] = not task.get('constraint_failures')
    checks['no_sandbox_violation'] = not turn.get('violations')
    return checks, info


def judge_efficiency(turn, warn):
    runtime = turn.get('runtime') or {}
    metrics = dict(requests=turn.get('actual_requests'), tokens=turn.get('tokens'),
                   cost=round(turn.get('cost') or 0.0, 6), active_seconds=round(turn.get('active_seconds') or 0.0, 3),
                   tool_calls=turn.get('tool_calls'), blocked_calls=runtime.get('blocked_calls'),
                   answer_chars=len(main_answer(turn)))
    limits = dict(DEFAULT_WARN, **(warn or {}))
    warnings = [f'{k}={metrics[k]} > {v}' for k, v in limits.items()
                if v is not None and metrics.get(k) is not None and metrics[k] > v]
    return dict(passed=True, required=False, metrics=metrics, thresholds=limits, warnings=warnings)


# --------------------------------------------------------------------------- aggregation

def judge_turn(turn, expect, *, calls, history_calls, run_id, changes=None, required=REQUIRED):
    """``calls``: this turn's trace; ``history_calls``: all earlier+current calls of the case."""
    norm = normalize_status(turn)
    ops = turn_operations(turn, run_id)
    blockers = blocker_states(turn.get('task'), history_calls)
    status_checks, status_info = judge_status(norm, expect)
    answer_checks, answer_info = judge_answer(turn, norm, expect, blockers, calls)
    constraint_checks, constraint_info = judge_constraints(turn, expect, ops, calls, changes)
    req = set(expect.get('required') or required)
    dims = dict(files=_dim(judge_files(turn), 'files' in req),
                status=_dim(status_checks, 'status' in req, **status_info),
                answer=_dim(answer_checks, 'answer' in req, **answer_info),
                constraints=_dim(constraint_checks, 'constraints' in req, **constraint_info),
                efficiency=judge_efficiency(turn, expect.get('efficiency_warn')))
    accepted = all(d['passed'] for d in dims.values() if d['required'])
    v4_checks, v4_info = judge_status_v4(turn, expect)
    dims['status_v4'] = _dim(v4_checks, False, **v4_info)  # reported beside v3, not part of `accepted`
    accepted_v4 = all(d['passed'] for k, d in dims.items() if d['required'] and k != 'status') \
        and (dims['status_v4']['passed'] or 'status' not in req)
    files_ok, cons_ok = dims['files']['passed'], dims['constraints']['passed']
    diagnostics = dict(
        # correct files but reported unfinished: the "false partial" class
        false_incomplete=norm['klass'] in {'incomplete', 'failed'} and files_ok and cons_ok,
        review_pending=norm['klass'] == 'review',
        recovered_blockers_shown=len(answer_info['recovered_blockers_shown']),
        open_blockers=len([b for b in blockers if b['status'] == 'open']))
    return dict(accepted=accepted, accepted_v4=accepted_v4, status=norm['status'], dimensions=dims, diagnostics=diagnostics)


def judge_case(spec, row, traces):
    """``traces``: list of per-turn call lists (same order as row['turns'])."""
    expect = spec.get('expect') or {}
    turn_expect = expect.get('turns') or []
    turns, history = [], []
    single = len(row.get('turns', [])) == 1
    for index, turn in enumerate(row.get('turns', [])):
        calls = traces[index] if index < len(traces) else []
        history = history + calls
        texp = turn_expect[index] if index < len(turn_expect) else {}
        if 'snapshot_before' in turn and 'snapshot_after' in turn:
            changes = snapshot_changes(turn['snapshot_before'], turn['snapshot_after'])
        elif single and row.get('before') is not None and row.get('after') is not None:
            changes = snapshot_changes(row['before'], row['after'])
        else:
            changes = None
        result = judge_turn(turn, texp, calls=calls, history_calls=history,
                            run_id=f"{row['id']}_{index + 1}", changes=changes)
        result['turn'] = index + 1
        result['changes_source'] = 'snapshot' if changes is not None else 'operations'
        turns.append(result)
    case_checks = {k: bool(row['checks'][k]) for k in ('all_turns_completed', 'source_contents_preserved',
                                                     'undo_restores_fixture', 'no_exception')
                   if k in (row.get('checks') or {})}
    case_checks['expected_turns_run'] = len(row.get('turns', [])) == len(spec.get('turns', row.get('turns', [])))
    if row.get('error_type'): case_checks['no_exception'] = False
    case_files = _dim(case_checks)
    accepted = bool(turns) and all(t['accepted'] for t in turns) and case_files['passed']
    accepted_v4 = bool(turns) and all(t['accepted_v4'] for t in turns) and case_files['passed']
    failed_dims = sorted({f"turn_{t['turn']}:{name}" for t in turns for name, d in t['dimensions'].items()
                          if d['required'] and not d['passed']} | ({'case:files'} if not case_files['passed'] else set()))
    failed_v4 = sorted({f"turn_{t['turn']}:{name}" for t in turns for name, d in t['dimensions'].items()
                        if (d['required'] or name == 'status_v4') and name != 'status' and not d['passed']}
                       | ({'case:files'} if not case_files['passed'] else set()))
    return dict(id=row['id'], tags=spec.get('tags', []), overall_accepted=accepted, failed_dimensions=failed_dims,
                accepted_v4=accepted_v4, failed_dimensions_v4=failed_v4,
                case_files=case_files, turns=turns, passed_local=row.get('passed_local'),
                false_incomplete_turns=[t['turn'] for t in turns if t['diagnostics']['false_incomplete']],
                recovered_blocker_turns=[t['turn'] for t in turns if t['diagnostics']['recovered_blockers_shown']])


def judge_suite(specs, report, traces_for):
    """``specs``: case id -> spec; ``traces_for(case_id)`` -> per-turn call lists."""
    cases = []
    for row in report.get('cases', []):
        spec = specs.get(row['id'], dict(turns=row.get('turns', [])))
        cases.append(judge_case(spec, row, traces_for(row['id'])))
    expected_ids = list(specs)
    complete = bool(report.get('suite_complete', len(cases) == len(expected_ids)))
    integrity = dict(suite_complete=complete,
                     source_papers_unchanged=report.get('source_papers_unchanged', True) is not False,
                     product_code_unchanged=report.get('product_code_unchanged', True) is not False)
    accepted = complete and all(integrity.values()) and bool(cases) and all(c['overall_accepted'] for c in cases)
    turns = [t for c in cases for t in c['turns']]
    metrics = [t['dimensions']['efficiency']['metrics'] for t in turns]
    efficiency = dict(requests=sum(m['requests'] or 0 for m in metrics), tokens=sum(m['tokens'] or 0 for m in metrics),
                      cost=round(sum(m['cost'] or 0 for m in metrics), 6),
                      active_seconds=round(sum(m['active_seconds'] or 0 for m in metrics), 3),
                      warnings=[f"{c['id']}#{t['turn']}: {w}" for c in cases for t in c['turns']
                                for w in t['dimensions']['efficiency']['warnings']])
    dim_pass = {name: sum(t['dimensions'][name]['passed'] for t in turns) for name in REQUIRED}
    return dict(judge_version=JUDGE_VERSION, overall_accepted=accepted, integrity=integrity,
                cases_accepted=sum(c['overall_accepted'] for c in cases), cases_total=len(cases),
                cases_accepted_v4=sum(c['accepted_v4'] for c in cases),
                turns_total=len(turns), turn_dimension_passes=dim_pass,
                legacy_passed_local=sum(bool(c['passed_local']) for c in cases),
                false_incomplete=[f"{c['id']}#{n}" for c in cases for n in c['false_incomplete_turns']],
                recovered_blockers_shown=[f"{c['id']}#{n}" for c in cases for n in c['recovered_blocker_turns']],
                review_pending=[f"{c['id']}#{t['turn']}" for c in cases for t in c['turns'] if t['diagnostics']['review_pending']],
                efficiency=efficiency, cases=cases)


def exit_code(judgement, infrastructure_blocked=False):
    """0 accepted, 1 not accepted (any required dimension or integrity), 2 infrastructure."""
    if infrastructure_blocked: return 2
    return 0 if judgement['overall_accepted'] else 1
