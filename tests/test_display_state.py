"""What the user is told to do next (user decision 2026-10-24); task states are unchanged.

Round 37: the same "which file mentions X" question ended as needs_review ("待核实")
when the model registered a find task with cited fragments, and as a plain answer
when it did not. A read-only task's review is its cited fragments, so it now reads
as answered; only a task that changed files asks the user to confirm.
"""
from agent.reporting import display_state

from helpers import Session, step, call
from test_protocol_v3 import start, finish, holdout_fixture, read_calls, finding


def test_display_follows_what_the_user_does_next():
    assert display_state('needs_review', dict(operation_history=[], operations=[])) == 'answered'
    assert display_state('needs_review', dict(operation_history=[dict(op='move')])) == 'confirm'
    assert display_state('needs_review', dict(operations=[dict(op='trash')])) == 'confirm'
    assert [display_state(s) for s in ('completed', 'answered', 'explained')] == ['done'] * 3
    assert display_state('waiting_input') == 'waiting' and display_state('cancelled') == 'cancelled'
    assert {display_state(s) for s in ('partial', 'unverified_answer', 'cost_budget', 'repair_limit')} == {'unfinished'}


def test_a_find_task_with_cited_fragments_reads_as_answered(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    result, _ = session.run('哪些笔记讲光合作用？', [step(*read_calls(['note_01.txt', 'note_02.txt'])),
                                              step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                                              step(call('report_findings', dict(findings=[finding(0), finding(1)]))), finish()])
    assert session.sb.runtime.current['status'] == 'needs_review'  # unchanged
    assert result.report['state'] == 'needs_review' and result.report['display'] == 'answered'
    assert '待核实' not in result.final and '待复核' not in result.final and '模型判断：' in result.final
