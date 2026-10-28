"""2.0 P2 experiments (off by default): escalating a repeated error, and executing and
finishing in one response with the ending refused when the execution did not succeed."""
from helpers import CFG, Session, call, files_fixture, step
from test_protocol_v3 import ARCHIVE_A, PLAN, start


def _bad_plan_calls(session):
    _, client = session.run('归档 a', [step(start([dict(kind='organize', description='归档 a')])),
                                      step(call('execute_file_plan', dict(plan_id='Xnot0000real'))),
                                      step(call('execute_file_plan', dict(plan_id='Xnot0001real'))),
                                      step(call('finish_turn', dict(mode='task')))])
    return [c['text'] for c in client.calls('execute_file_plan')]


def test_a_repeated_error_gets_one_escalation_line_only_when_switched_on(tmp_path):
    files_fixture(tmp_path)
    off = _bad_plan_calls(Session(tmp_path))
    assert not any('[连续出错]' in t for t in off)
    on = _bad_plan_calls(Session(tmp_path, dict(CFG, escalate_repeated_errors=2)))
    assert '[连续出错]' not in on[0] and '[连续出错] 同一错误已连续出现 2 次' in on[1]


def test_execute_and_finish_in_one_response_saves_a_request(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path, dict(CFG, finish_with_execute=True))
    result, client = session.run('归档 a', [
        step(start([dict(kind='organize', description='归档 a')]),
             call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A), alias=PLAN)),
        step(call('execute_file_plan', {'plan_id': PLAN}), call('finish_turn', dict(mode='task', summary='已归档')))])
    assert '同一响应中依次调用 execute_file_plan 和 finish_turn' in client.calls('prepare_file_plan')[0]['text']
    assert result.stop_reason == 'done' and client.requests == 2


def test_an_ending_written_before_a_failed_execution_is_refused(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path, dict(CFG, finish_with_execute=True))
    result, client = session.run('归档 a', [
        step(start([dict(kind='organize', description='归档 a')])),
        step(call('execute_file_plan', dict(plan_id='Xnot0000real')), call('finish_turn', dict(mode='task', summary='已归档'))),
        step(call('finish_turn', dict(mode='task')))])
    first, second = client.calls('finish_turn')
    assert first['text'].startswith('ERROR: EXECUTION_NOT_SEEN') and not second['text'].startswith('ERROR')
    assert result.stop_reason != 'done' and (tmp_path / 'Inbox/a.txt').is_file()
