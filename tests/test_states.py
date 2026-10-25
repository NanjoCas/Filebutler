"""Flow, result and verification beside the old state (routing redesign S6a, 7).

One test per row of the 7 table that a scripted run can reach; the old state and
display are asserted unchanged next to the new dimensions. Temporary files only."""
from agent.states import project

from helpers import Session, step, call, files_fixture
from test_protocol_v3 import start, execute, finish, finish_with, holdout_fixture, read_calls, finding, PLAN, ARCHIVE_A
from test_cancel import ASK_GO_ON
from test_answer_replays import ASK_B_SCOPED, TWO_PARTS_MAPPED, MOVE_A, clash_b


def dims(result):
    s = result.report['states']
    return s['flow_state'], s['result_state'], s['verification']['mechanical'], s['verification']['semantic'], s['next_action']


def test_a_plain_explanation_is_answered_with_nothing_to_verify(tmp_path):
    files_fixture(tmp_path)
    result, _ = Session(tmp_path).run('什么是 BM25？', [step(call('finish_turn', dict(mode='reply', reply='一种检索打分。')))])
    assert dims(result) == ('ended', 'answered', 'not_applicable', 'not_required', 'none')
    assert result.report['state'] == 'answered'


def test_a_cited_read_only_answer_passes_mechanically_and_waits_for_no_one(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    result, _ = session.run('哪些笔记讲光合作用？', [step(*read_calls(['note_01.txt', 'note_02.txt'])),
                                              step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                                              step(call('report_findings', dict(findings=[finding(0), finding(1)]))), finish()])
    assert dims(result) == ('ended', 'answered', 'passed', 'unreviewed', 'none')
    assert result.report['state'] == 'needs_review' and result.report['display'] == 'answered'  # unchanged


def test_a_task_answer_without_its_evidence_is_partial_not_answered(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    result, _ = Session(tmp_path).run('哪些笔记讲光合作用？', [
        step(start([dict(id='find', kind='find', description='光合作用笔记')])), finish()])
    assert dims(result)[:3] == ('ended', 'partial', 'failed')


def test_an_executed_and_verified_plan_is_fulfilled(tmp_path):
    files_fixture(tmp_path)
    result, _ = Session(tmp_path).run('归档 a', [
        step(start([dict(kind='organize', description='归档 a')]),
             call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A), alias=PLAN)), execute(), finish()])
    assert dims(result) == ('ended', 'fulfilled', 'passed', 'not_required', 'none')
    assert result.report['state'] == 'completed'


def test_skipping_b_after_a_is_done_is_fulfilled_with_b_skipped_by_the_user(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B_SCOPED))])
    result, _ = session.run('A', [step(start(TWO_PARTS_MAPPED), call('prepare_file_plan', MOVE_A, alias=PLAN)),
                                  execute(), finish()])
    assert dims(result)[:3] == ('ended', 'fulfilled', 'passed')
    parts = {p['id']: p for p in result.report['states']['parts']}
    assert parts['b']['skipped_by_user'] and parts['b']['outcome'] == 'by_choice' and parts['a']['mechanical'] == 'passed'


def test_an_open_question_waits_for_an_answer(tmp_path):
    files_fixture(tmp_path)
    result, _ = Session(tmp_path).run('移动 Inbox', [step(call('finish_turn', dict(mode='ask', **ASK_GO_ON)))])
    assert dims(result)[:2] == ('waiting_input', 'no_result') and dims(result)[4] == 'answer_question'


def test_cancel_with_nothing_run_and_cancel_after_part_of_the_work(tmp_path):
    files_fixture(tmp_path / 'zero')
    session = Session(tmp_path / 'zero')
    session.run('移动 Inbox', [step(call('finish_turn', dict(mode='ask', **ASK_GO_ON)))])
    result, _ = session.run('B', [])
    assert dims(result)[:2] == ('cancelled', 'no_result') and dims(result)[4] == 'none'
    files_fixture(tmp_path / 'some')
    session = Session(tmp_path / 'some')
    parts = [dict(id='a', kind='organize', description='把 a.txt 移到 Archive'),
             dict(id='b', kind='organize', description='把 b.txt 移到 Archive')]
    session.run('移动 a 和 b', [step(start(parts), call('prepare_file_plan', dict(description='移动 a', actions=[
        dict(ARCHIVE_A[0], part='a')]), alias=PLAN)), execute(), step(call('finish_turn', dict(mode='ask', **ASK_GO_ON)))])
    result, _ = session.run('B', [])
    assert dims(result)[:2] == ('cancelled', 'partial')  # never fulfilled


def test_a_pause_is_interrupted_and_waits_for_an_explicit_resume(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动 a', [step(start([dict(kind='organize', description='归档 a')]))], final='稍等。')
    result, _ = session.run('先停一下', [step(call('finish_turn', dict(mode='reply', reply='好', input_resolution=dict(kind='pause'))))])
    assert dims(result)[:2] == ('interrupted', 'no_result') and dims(result)[4] == 'none'


def test_one_part_done_and_one_not_is_partial(tmp_path):
    files_fixture(tmp_path)
    parts = [dict(id='a', kind='organize', description='把 a.txt 移到 Archive'),
             dict(id='b', kind='organize', description='把 b.txt 改名')]
    result, _ = Session(tmp_path).run('a 和 b', [
        step(start(parts), call('prepare_file_plan', dict(description='移动 a', actions=[dict(ARCHIVE_A[0], part='a')]), alias=PLAN)),
        execute(), finish_with([dict(part_id='b', reason='没做')])])
    assert dims(result)[:3] == ('ended', 'partial', 'failed')
    assert {p['id']: p['mechanical'] for p in result.report['states']['parts']} == dict(a='passed', b='failed')


def test_an_inflight_step_is_unknown_and_asks_for_recovery():
    task = dict(goals=[dict(id='a')], allowed_effects=['move'], checks=[dict(goal_id='a', passed=False, needs_review=False,
                conditions=[dict(kind='file_plan_verified', passed=False)])], operation_history=[dict(op='move')])
    s = project('max_steps', dict(state='interrupted'), task, plans=[dict(inflight='X:0')])
    assert (s['flow_state'], s['result_state'], s['verification']['mechanical'], s['next_action']) == \
        ('interrupted', 'partial', 'unknown', 'recover')


def test_a_write_waiting_for_semantic_review_asks_to_confirm_the_result():
    task = dict(goals=[dict(id='c')], allowed_effects=['move'], revision=1, operation_history=[dict(op='move')],
                checks=[dict(goal_id='c', passed=True, needs_review=True, conditions=[dict(kind='plan_applied', passed=True)])])
    s = project('needs_review', dict(state='needs_review'), task)
    assert (s['result_state'], s['verification']['semantic'], s['next_action']) == ('fulfilled', 'unreviewed', 'confirm_result')
    accepted = dict(task, reviews=[dict(revision=1, decision='accept', goal_ids=['c'])])
    assert project('done', dict(state='completed'), accepted)['verification']['semantic'] == 'accepted'


def test_nothing_by_choice_is_fulfilled_with_or_without_a_task():
    no_task = project('answered', dict(state='answered', result='by_choice'))
    assert (no_task['flow_state'], no_task['result_state'], no_task['business']) == ('ended', 'fulfilled', 'by_choice')


def test_a_stop_before_any_ending_is_retryable():
    s = project('repair_limit', dict(state='interrupted'))
    assert (s['flow_state'], s['result_state'], s['next_action']) == ('interrupted', 'no_result', 'retry')
    assert project('done', dict(state='failed'), error_type='RuntimeError')['flow_state'] == 'interrupted'


def _turn(stop, report, task=None):
    return dict(stop_reason=stop, report=report, task=task, runtime={}, checks=dict(ok=True))


def test_judgement_v4_keeps_what_v3_refused_for_a_reason():
    from eval.judge import judge_status_v4
    done = dict(status=['done'])
    # A user limit only a person can judge (round 39 dedup_cleanup): not done in either judgement.
    noted = dict(goals=[dict(id='d')], allowed_effects=['trash'], revision=1, operation_history=[dict(op='trash')],
                 constraints=[dict(id='note_1', forbidden_effects=[], rules=[])],
                 checks=[dict(goal_id='d', passed=True, needs_review=False, conditions=[dict(kind='receipt_verified', passed=True)])])
    assert not judge_status_v4(_turn('needs_review', dict(state='needs_review'), noted), done)[0]['states_in_expected']
    # A reply without a task is "answered", not a finished task.
    assert not judge_status_v4(_turn('answered', dict(state='answered')), done)[0]['states_in_expected']
    assert judge_status_v4(_turn('answered', dict(state='answered')), dict(status=['answered']))[0]['states_in_expected']


def test_judgement_v4_accepts_a_cited_read_only_answer_and_a_choice_as_done():
    from eval.judge import judge_status_v4
    found = dict(goals=[dict(id='f')], revision=1, constraints=[],
                 checks=[dict(goal_id='f', passed=True, needs_review=True, conditions=[dict(kind='evidence_valid', passed=True)])])
    assert judge_status_v4(_turn('needs_review', dict(state='needs_review'), found), dict(status=['done']))[0]['states_in_expected']
    chosen = _turn('answered', dict(state='answered', result='by_choice'))
    assert judge_status_v4(chosen, dict(status=['done']))[0]['states_in_expected']
    # Missing evidence stays a failure.
    missing = dict(found, checks=[dict(goal_id='f', passed=False, needs_review=True, conditions=[dict(kind='evidence_valid', passed=False)])])
    assert not judge_status_v4(_turn('partial', dict(state='partial'), missing), dict(status=['done']))[0]['states_in_expected']
