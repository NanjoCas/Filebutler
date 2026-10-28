"""Acceptance-oracle fault injection; these tests never call a model."""
from types import SimpleNamespace

from agent.artifacts import ArtifactStore
from eval.checker import snapshot
from eval.live_policy import checks, fixture
from safety.sandbox import Sandbox


def test_root_only_oracle_rejects_nested_changes_and_content_swaps(tmp_path):
    fixture(tmp_path,'files')
    before=snapshot(tmp_path,True)
    store=ArtifactStore(Sandbox(tmp_path)); runtime=SimpleNamespace(current=None)
    for name in ('a.txt','b.txt'): (tmp_path/'Inbox'/name).rename(tmp_path/'Archive'/name)
    assert all(checks('root_only',0,tmp_path,before,store,runtime,None).values())
    (tmp_path/'Archive/a.txt').write_text('beta'); (tmp_path/'Archive/b.txt').write_text('alpha')
    outcome=checks('root_only',0,tmp_path,before,store,runtime,None)
    assert outcome['contents_preserved'] and not outcome['a_contents_correct'] and not outcome['b_contents_correct']
    (tmp_path/'Inbox/nested/keep.txt').rename(tmp_path/'Archive/keep.txt')
    assert not checks('root_only',0,tmp_path,before,store,runtime,None)['others_unchanged']


def test_application_oracle_compares_original_content_at_each_target(tmp_path):
    fixture(tmp_path,'applications'); before=snapshot(tmp_path,True)
    for i,category in enumerate(('农业','医疗','能源'),1):
        dest=tmp_path/'资料/按领域'/category;dest.mkdir(parents=True)
        (tmp_path/'资料'/f'CNN_{i:02d}.pdf').rename(dest/f'CNN_{i:02d}.pdf')
    runtime=SimpleNamespace(current=None)
    assert all(checks('application_dimension',0,tmp_path,before,None,runtime,None).values())
    (tmp_path/'资料/按领域/农业/CNN_01.pdf').write_bytes(b'wrong')
    assert not checks('application_dimension',0,tmp_path,before,None,runtime,None)['correct_final_locations']


def test_cross_turn_oracle_requires_original_task_revision(tmp_path):
    fixture(tmp_path,'files');before=snapshot(tmp_path,True)
    (tmp_path/'Inbox/a.txt').rename(tmp_path/'Archive/a.txt')
    runtime=SimpleNamespace(current=dict(id='new-task',revision=1))
    assert not checks('cross_turn_correction',1,tmp_path,before,None,runtime,'original-task')['same_task_updated']
    runtime.current=dict(id='original-task',revision=2)
    assert all(checks('cross_turn_correction',1,tmp_path,before,None,runtime,'original-task').values())


# ============================================================================ multi-dimension judge
# Fault injection on eval/judge.py. Turns are built in the shape eval/live_policy.py
# saves (report.json turns + trace calls). No model, no network.
import json
from pathlib import Path

import pytest

from eval import judge
from eval.dryrun import BAD_SCRIPTS, SCRIPTS, scripted_llm_factory
from eval.live_policy import CASE_SPECS, CASES, SUITES, run_suite
from eval.rejudge import rejudge


def goal(gid, passed=True, review=False):
    return dict(goal_id=gid, description=gid, passed=passed, needs_review=review,
                conditions=[dict(kind='files_placed', passed=passed, needs_review=review, detail='')])


def make_turn(stop='done', task_status='completed', summary='已移动 2 个文件。\n\n本地核验通过，可撤销本轮文件操作。',
              goals=None, files_ok=True, blockers=None, ops=None, run_id='case_1', report_state=None, task=True):
    goals = [goal('g')] if goals is None else goals
    data = dict(stop_reason=stop, final=summary, actual_requests=4, tokens=1000, cost=0.01, active_seconds=3.0,
                tool_calls=3, violations=0, checks=dict(a_moved=files_ok, contents_preserved=True),
                runtime=dict(task_status=task_status if task else 'not_registered', checks=goals if task else [], blocked_calls=0))
    data['report'] = dict(state=report_state or (task_status if task else 'answered'), summary=summary, sections=[])
    if task:
        data['task'] = dict(blockers=blockers or [], operations=[dict(run_id=run_id, **o) for o in (ops or [])],
                            operation_history=[], constraint_failures=[])
    else:
        data['task'] = None
    return data


def call(tool, ok=True, result='OK', args='{}'):
    return dict(tool=tool, ok=ok, executed=True, args=args, result=result if ok else 'ERROR: ' + result, step=1)


MOVE_EXPECT = dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']))
MOVES = [dict(op='move', src='Inbox/a.txt', dst='Archive/a.txt'), dict(op='move', src='Inbox/b.txt', dst='Archive/b.txt')]


def run_judge(turn, expect, calls=(), history=None, changes=None):
    calls = list(calls)
    return judge.judge_turn(turn, expect, calls=calls, history_calls=list(history or calls), run_id='case_1', changes=changes)


def test_judge_accepts_clean_turn():
    out = run_judge(make_turn(ops=MOVES), MOVE_EXPECT)
    assert out['accepted'] and out['status'] == 'done'
    assert all(d['passed'] for d in out['dimensions'].values())


def test_judge_rejects_correct_files_reported_partial():
    """Live failure class root_only/real_classification#2/cross_turn#1: files right, status partial."""
    turn = make_turn(stop='partial', task_status='partial', goals=[goal('move'), goal('report', passed=False, review=True)],
                     summary='已移动 2 个文件。\n\n尚未全部完成。原因：未找到满足参数的成功调用：list_dir。', ops=MOVES)
    out = run_judge(turn, MOVE_EXPECT)
    assert out['dimensions']['files']['passed'] and out['dimensions']['constraints']['passed']
    assert not out['dimensions']['status']['passed'] and out['status'] == 'partial'
    assert out['diagnostics']['false_incomplete'] and not out['accepted']
    # The legacy physical oracle alone would have passed this turn.
    assert all(turn['checks'].values())


def test_judge_flags_recovered_legacy_blocker_from_trace():
    """Legacy {tool, message} blockers: resolution is inferred from the cumulative trace."""
    msg = '操作包含任务写入范围外的对象：资料/CNN_01.pdf；effect=move'
    summary = '已移动 3 个文件。\n\n文件操作已执行，部分结果待核实；请查看核验详情。\n\n阻塞原因：' + msg
    turn = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True)], summary=summary,
                     blockers=[dict(tool='prepare_file_plan', message=msg)])
    expect = dict(status=['done', 'needs_review'])
    calls = [call('prepare_file_plan', ok=False, result=msg), call('edit_task'), call('apply_task_edit'),
             call('prepare_file_plan', result='OK: plan_id=Xabc'), call('execute_file_plan')]
    out = run_judge(turn, expect, calls)
    assert out['dimensions']['status']['passed']
    assert not out['dimensions']['answer']['passed'] and not out['accepted']
    shown = out['dimensions']['answer']['recovered_blockers_shown']
    assert shown and shown[0]['inferred'] and 'CNN_01' in shown[0]['message']
    # Still failing, never fixed: the same blocker is a legitimate current reason.
    open_calls = [call('prepare_file_plan', ok=False, result=msg)]
    still = run_judge(make_turn(stop='partial', task_status='partial', goals=[goal('g', passed=False)],
                                summary='尚未全部完成。\n\n阻塞原因：' + msg,
                                blockers=[dict(tool='prepare_file_plan', message=msg)]),
                      dict(status=['partial']), open_calls)
    assert still['dimensions']['answer']['passed']


def test_judge_flags_structured_resolved_blocker_and_carried_over_turns():
    msg = '1 validation error for ExecutionStep\ngoal_ids\n  Field required'
    blockers = [dict(source='begin_step', object='scan', message=msg, status='resolved', resolved_by='同一对象后续成功')]
    turn = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True)],
                     summary='已形成分类计划。\n\n阻塞原因：' + msg, blockers=blockers)
    out = run_judge(turn, dict(status=['needs_review']))
    assert not out['dimensions']['answer']['passed'] and not out['dimensions']['answer']['recovered_blockers_shown'][0]['inferred']
    # Legacy blocker carried into turn 2: its failure and recovery live in turn 1's trace.
    legacy = [dict(tool='begin_step', message=msg)]
    turn2 = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True)],
                      summary='已移动 6 个文件。\n\n阻塞原因：' + msg, blockers=legacy)
    turn1_calls = [call('begin_step', ok=False, result=msg), call('begin_step')]
    out2 = run_judge(turn2, dict(status=['needs_review']), calls=[call('execute_file_plan')],
                     history=turn1_calls + [call('execute_file_plan')])
    assert not out2['dimensions']['answer']['passed']


def test_judge_rejects_discussion_only_turn_that_scans_or_writes():
    expect = CASE_SPECS['explanation_not_execution']['expect']['turns'][0]
    clean = make_turn(stop='answered', task=False, summary='“删除重复文件”指按字节或文本判定重复后移入回收区。')
    assert run_judge(clean, expect)['accepted']
    scanned = run_judge(clean, expect, [call('find_duplicates'), call('list_dir')])
    assert not scanned['dimensions']['constraints']['passed'] and 'no_scan_tools' in scanned['dimensions']['constraints']['failed']
    wrote = make_turn(stop='done', task_status='completed', summary='已移入回收区 1 个文件。',
                      ops=[dict(op='trash', src='Inbox/copy_of_a.txt', dst='.trash/x')])
    out = run_judge(wrote, expect, [call('execute_file_plan')], changes=['Inbox/copy_of_a.txt'])
    failed = out['dimensions']['constraints']['failed']
    assert {'effects_allowed', 'no_write_tools', 'no_filesystem_change'} <= set(failed)
    assert not out['dimensions']['status']['passed'] and not out['dimensions']['answer']['passed']
    # A rejected write attempt changed nothing and is not counted as a write.
    rejected = run_judge(clean, dict(status=['answered'], constraints=dict(effects=[], forbid_tools=['write'])),
                         [call('move', ok=False, result='路径超出资料库')])
    assert rejected['dimensions']['constraints']['passed']


def test_judge_needs_review_allowed_only_where_declared():
    turn = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True)],
                     summary='已移动 3 个文件。\n\n文件操作已执行，部分结果待核实；请查看核验详情。')
    allowed = run_judge(turn, dict(status=['done', 'needs_review']))
    assert allowed['accepted'] and allowed['diagnostics']['review_pending'] and not allowed['diagnostics']['false_incomplete']
    strict = run_judge(turn, dict(status=['done']))
    assert not strict['accepted'] and strict['dimensions']['status']['failed'] == ['status_in_expected']
    # needs_review can never hide an unmet machine goal.
    hidden = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True), goal('h', passed=False)])
    assert run_judge(hidden, dict(status=['needs_review']))['status'] == 'partial'


def test_judge_answer_fragments_scope_and_efficiency_is_observation_only():
    expect = dict(status=['done'], answer=dict(include_any=[['2 个', '两个']], exclude=['已回收']),
                  constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                  efficiency_warn=dict(requests=1, answer_chars=5))
    out = run_judge(make_turn(ops=MOVES), expect)
    assert out['accepted'] and out['dimensions']['efficiency']['warnings']  # warnings never fail
    assert not out['dimensions']['efficiency']['required']
    missing = run_judge(make_turn(ops=MOVES, summary='本地核验通过。'), expect)
    assert 'include_any[0]' in missing['dimensions']['answer']['failed']
    escaped = run_judge(make_turn(ops=MOVES + [dict(op='move', src='Inbox/nested/keep.txt', dst='Other/keep.txt')]), expect)
    assert 'writes_within_scope' in escaped['dimensions']['constraints']['failed']
    finished_wording = run_judge(make_turn(summary='已移动 2 个文件。尚未全部完成。', ops=MOVES), expect)
    assert 'no_unfinished_wording' in finished_wording['dimensions']['answer']['failed']
    mismatch = run_judge(make_turn(ops=MOVES, report_state='partial'), expect)
    assert 'report_state_matches_runtime' in mismatch['dimensions']['answer']['failed']


def test_judge_rejects_positive_findings_for_no_answer_case():
    expect = CASE_SPECS['no_answer']['expect']['turns'][0]
    findings = lambda rel: call('report_findings', args=json.dumps(dict(findings=[dict(file_id='F1', relation=rel)])))
    turn = make_turn(stop='needs_review', task_status='needs_review', goals=[goal('g', review=True)], summary='没有正文支持的结果。')
    assert run_judge(turn, expect, [findings('not_related')])['accepted']
    assert not run_judge(turn, expect, [findings('method')])['dimensions']['answer']['passed']


def test_case_and_suite_overall_and_exit_code():
    spec = dict(turns=['t1'], expect=dict(turns=[MOVE_EXPECT]))
    good = dict(id='case', turns=[make_turn(ops=MOVES)], passed_local=True,
                checks=dict(all_turns_completed=True, source_contents_preserved=True, undo_restores_fixture=True))
    bad_undo = json.loads(json.dumps(good)); bad_undo['checks']['undo_restores_fixture'] = False
    report = lambda row: dict(cases=[row], suite_complete=True)
    ok = judge.judge_suite(dict(case=spec), report(good), lambda cid: [[]])
    assert ok['overall_accepted'] and judge.exit_code(ok) == 0
    undo_fail = judge.judge_suite(dict(case=spec), report(bad_undo), lambda cid: [[]])
    assert not undo_fail['overall_accepted'] and judge.exit_code(undo_fail) == 1
    incomplete = judge.judge_suite(dict(case=spec, other=spec), dict(cases=[good], suite_complete=False), lambda cid: [[]])
    assert not incomplete['overall_accepted']
    assert judge.exit_code(ok, infrastructure_blocked=True) == 2
    # 2.0 Q5: the exit code follows v4; a judgement saved before v4 had a suite verdict falls back to v3.
    assert judge.exit_code(dict(overall_accepted=False, overall_accepted_v4=True)) == 0
    assert judge.exit_code(dict(overall_accepted=True, overall_accepted_v4=False)) == 1
    assert judge.exit_code(dict(overall_accepted=True)) == 0


def test_every_case_declares_turn_expectations_and_holdout_is_separate():
    for case in SUITES['all']:
        turns = case['expect']['turns']
        assert len(turns) == len(case['turns']), case['id']
        assert all(t.get('status') for t in turns), case['id']
    holdout = {c['id'] for c in SUITES['holdout']}
    assert holdout and not holdout & {c['id'] for c in CASES}
    assert all('holdout' in c['tags'] for c in SUITES['holdout']) and all('regression' in c['tags'] for c in CASES)
    assert set(SCRIPTS) == holdout


# ---------------------------------------------------------------- offline re-judge and dry run

def _synthetic_result(base):
    base.mkdir()
    turn = make_turn(stop='partial', task_status='partial', goals=[goal('g'), goal('h', passed=False)], ops=MOVES,
                     run_id='root_only_1', summary='已移动 2 个文件。\n\n尚未全部完成。')
    row = dict(id='root_only', turns=[turn], passed_local=True,
               checks=dict(all_turns_completed=True, source_contents_preserved=True, undo_restores_fixture=True))
    (base / 'report.json').write_text(json.dumps(dict(cases=[row], passed_local=1, suite_complete=True), ensure_ascii=False))
    (base / 'manifest.json').write_text(json.dumps(dict(cases=[dict(id='root_only', turns=CASE_SPECS['root_only']['turns'])]),
                                                   ensure_ascii=False))
    (base / 'root_only').mkdir()
    (base / 'root_only/turn_1.trace.jsonl').write_text('')


def test_rejudge_writes_elsewhere_and_leaves_source_untouched(tmp_path):
    source = tmp_path / 'run'; _synthetic_result(source)
    before = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit):
        rejudge(source, source / 'rejudge', CASE_SPECS)
    out = rejudge(source, tmp_path / 'out', CASE_SPECS)
    assert out['source_unchanged'] and not out['overall_accepted']
    assert out['legacy_passed_local'] == 1 and out['false_incomplete'] == ['root_only#1']
    assert {p: p.read_bytes() for p in source.rglob('*') if p.is_file()} == before
    with pytest.raises(SystemExit):  # never overwrite an existing output
        rejudge(source, tmp_path / 'out', CASE_SPECS)


HISTORICAL = Path(__file__).resolve().parents[1] / 'eval/results/policy_live_20261021_01'


@pytest.mark.skipif(not (HISTORICAL / 'report.json').is_file(), reason='frozen live results are git-ignored')
def test_rejudge_frozen_20261021_matches_post_hoc_review(tmp_path):
    out = rejudge(HISTORICAL, tmp_path / 'rejudge', CASE_SPECS)
    assert out['source_unchanged'] and out['legacy_passed_local'] == 8
    assert out['overall_accepted'] is False
    assert sorted(out['false_incomplete']) == ['cross_turn_correction#1', 'real_classification#2', 'root_only#1']
    assert 'application_dimension#1' in out['recovered_blockers_shown']
    review = json.loads((tmp_path / 'rejudge/review_comparison.json').read_text())
    assert review['overall_accepted']['agree'] and review['false_partial_turns']['agree']


def test_dry_run_holdout_pipeline_accepts_good_script_and_rejects_bad(tmp_path):
    from eval.live_policy import load_config, ROOT
    cfg = load_config(ROOT / 'config.yaml'); cfg.update(llm_retries=0, task_session='dry-run-test', model='scripted-dry-run')
    report, judgement, code = run_suite(SUITES['holdout'], tmp_path / 'good', cfg, 1.0, scripted_llm_factory(SCRIPTS), 'dry_run')
    assert code == 0 and judgement['overall_accepted'] and report['cost'] == 0
    assert all(c['checks']['undo_restores_fixture'] for c in report['cases'])
    ambiguous = [c for c in SUITES['holdout'] if c['id'] == 'holdout_ambiguous_target']
    report, judgement, code = run_suite(ambiguous, tmp_path / 'bad', cfg, 1.0, scripted_llm_factory(BAD_SCRIPTS), 'dry_run')
    case = judgement['cases'][0]
    assert code == 1 and not judgement['overall_accepted']
    assert {'turn_1:files', 'turn_1:status', 'turn_1:constraints'} <= set(case['failed_dimensions'])
    assert report['cases'][0]['checks']['undo_restores_fixture']


def test_choose_presses_the_one_matching_option_or_records_none():
    from eval.live_policy import choose, NO_MATCH
    report = dict(choices=[dict(key='A', label='不覆盖，保留两份'), dict(key='B', label='覆盖旧文件'), dict(key='C', label='不移动')])
    assert choose({'choose': ['覆盖'], 'avoid': ['不覆盖']}, report)[0] == 'B'
    text, record = choose({'choose': ['覆盖']}, report)  # two labels match: never guess
    assert text == NO_MATCH and record['key'] is None and len(record['matched']) == 2
    assert choose('保留', report) == ('保留', None)


def test_choose_by_declared_actions_ignores_wording():
    from eval.live_policy import choose, NO_MATCH
    keep = dict(key='A', label='暂不删除「资料」', actions=[dict(effect='move', path='资料/a.pdf', destination='归档'),
                                                         dict(effect='move', path='./资料/计划.docx', destination='归档')])
    two = dict(key='B', label='只移 a', actions=[dict(effect='move', path='资料/a.pdf', destination='归档')])
    wait = dict(key='C', label='先等等', actions=[])
    turn = {'choose_actions': {'has': [dict(effect='move', path='资料/a.pdf'), dict(effect='move', path='资料/计划.docx')],
                               'lacks': [dict(effect='trash')]}}
    assert choose(turn, dict(choices=[wait, two, keep]))[0] == 'A'
    trash = dict(keep, key='D', actions=keep['actions'] + [dict(effect='trash', path='资料')])
    assert choose(turn, dict(choices=[trash, two]))[0] == NO_MATCH


def test_evaluation_keeps_reasoning_in_its_own_file_only(tmp_path, monkeypatch):
    # User decision A4 (2026-10-29): evaluation runs keep the model's reasoning; the judged history does not.
    from eval import dryrun
    from eval.live_policy import load_config, ROOT
    real = dryrun.ScriptedClient.create
    def thinking(self, **kw):
        response = real(self, **kw)
        response.choices[0].message.reasoning_content = f'思考 {self.requests}'
        return response
    monkeypatch.setattr(dryrun.ScriptedClient, 'create', thinking)
    cfg = load_config(ROOT / 'config.yaml'); cfg.update(llm_retries=0, task_session='dry-run-test', model='scripted-dry-run')
    case = [c for c in SUITES['holdout'] if c['id'] == 'holdout_ambiguous_target']
    run_suite(case, tmp_path / 'run', cfg, 1.0, scripted_llm_factory(SCRIPTS), 'dry_run')
    turn = tmp_path / 'run/holdout_ambiguous_target'
    saved = json.loads((turn / 'turn_1.reasoning.json').read_text())
    assert saved and saved[0]['reasoning'] == '思考 1' and 'tools' in saved[0]
    assert 'reasoning_content' not in (turn / 'turn_1.history.json').read_text()
