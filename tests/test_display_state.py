"""What the user is told to do next (user decision 2026-10-24); task states are unchanged.

Round 37: the same "which file mentions X" question ended as needs_review ("待核实")
when the model registered a find task with cited fragments, and as a plain answer
when it did not. A read-only task's review is its cited fragments, so it now reads
as answered; only a task that changed files asks the user to confirm.
"""
from agent.states import display_of, project

from helpers import CFG, Session, step, call
from test_protocol_v3 import start, finish, holdout_fixture, read_calls, finding


def shown(state, task=None, stop=None):
    return display_of(project(stop, dict(state=state), task))


def test_display_follows_what_the_user_does_next():
    goal = dict(goal_id='g', passed=True, needs_review=True, conditions=[])
    review = lambda **ops: dict(goals=[dict(id='g')], checks=[goal], operation_history=[], operations=[], **ops)
    assert shown('needs_review', review()) == 'answered'
    assert shown('needs_review', dict(review(), operation_history=[dict(op='move')])) == 'confirm'
    assert shown('needs_review', dict(review(), operations=[dict(op='trash')])) == 'confirm'
    assert [shown(s) for s in ('completed', 'answered', 'explained')] == ['done'] * 3
    assert shown('waiting_input') == 'waiting' and shown('cancelled') == 'cancelled'
    assert shown('partial', dict(goals=[dict(id='g')], checks=[dict(goal, passed=False)])) == 'unfinished'
    assert {shown(None, stop=s) for s in ('unverified_answer', 'cost_budget', 'repair_limit')} == {'unfinished'}
    assert display_of(project('answered', dict(state='answered', result='by_choice'))) == 'chosen'


def test_a_run_stopped_by_a_limit_also_gets_its_display(tmp_path):
    (tmp_path / 'a.txt').write_text('a')
    session = Session(tmp_path, dict(CFG, max_steps=1))
    result, _ = session.run('看看', [step(call('list_dir', dict(path='.'))), step(call('list_dir', dict(path='.')))])
    assert result.stop_reason == 'max_steps' and result.report['display'] == 'unfinished'


def test_a_find_task_with_cited_fragments_reads_as_answered(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    result, _ = session.run('哪些笔记讲光合作用？', [step(*read_calls(['note_01.txt', 'note_02.txt'])),
                                              step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                                              step(call('report_findings', dict(findings=[finding(0), finding(1)]))), finish()])
    assert session.sb.runtime.current['status'] == 'needs_review'  # unchanged
    assert result.report['state'] == 'needs_review' and result.report['display'] == 'answered'
    assert '待核实' not in result.final and '待复核' not in result.final and '模型判断：' in result.final
