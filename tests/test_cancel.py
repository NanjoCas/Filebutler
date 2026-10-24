"""Cancelling, pausing and resuming a request (routing redesign S3,
docs/agent-routing-redesign-2026-10-24.md 5.2-5.3, 13). Temporary files, scripted models."""
import pytest

import helpers as H
from helpers import Session, step, call, files_fixture
from test_protocol_v3 import start, execute, finish, errors, PLAN, ARCHIVE_A
from agent.interactions import Interactions, InteractionError, receive
from safety.confirm import Confirmer
from safety.oplog import OpLog

ASK_GO_ON = dict(question='b.txt 也要移动吗？', reason='scope_beyond_request', subject=['Inbox'],
                 scope=dict(level='request'), options=[
                     dict(label='都移动', actions=[dict(effect='move', path='Inbox/b.txt', destination='Archive')],
                          disposition='continue'),
                     dict(label='取消整个请求', actions=[], disposition='cancel_request')])


def repo(session):
    return Interactions(session.sb.runtime.store)


def test_the_cancel_button_ends_the_request_without_a_model_call(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动 Inbox', [step(call('finish_turn', dict(mode='ask', question=ASK_GO_ON)))])
    result, client = session.run('B', [step(call('list_dir', dict(path='.')))])  # B: the cancel button
    assert client.requests == 0 and result.stop_reason == 'cancelled'
    assert result.final == '已取消，没有执行任何文件操作。'
    request = repo(session).request(session.sb.runtime.cfg['request_id'])
    assert request['status'] == 'cancelled' and request['status_actor'] == 'user_option'
    assert repo(session).open_question() is None


def test_cancelling_after_part_of_the_work_keeps_it_and_says_what_is_left(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    parts = [dict(id='a', kind='organize', description='把 a.txt 移到 Archive'),
             dict(id='b', kind='organize', description='把 b.txt 移到 Archive')]
    session.run('移动 a 和 b', [step(start(parts), call('prepare_file_plan', dict(description='移动 a', actions=[
        dict(ARCHIVE_A[0], part='a')]), alias=PLAN)), execute(),
        step(call('finish_turn', dict(mode='ask', question=ASK_GO_ON)))])
    result, client = session.run('B', [])
    assert client.requests == 0 and result.stop_reason == 'cancelled'
    assert result.final.startswith('已取消；此前已移动 1 个文件，可撤销') and '把 b.txt 移到 Archive' in result.final
    task = session.sb.runtime.current
    assert task['cancelled'] and task['status'] == 'cancelled' and task['cancellation']['actor'] == 'user_option'
    assert (tmp_path / 'Archive/a.txt').is_file() and (tmp_path / 'Inbox/b.txt').is_file()  # not undone, not continued


def test_typed_cancel_is_declared_by_the_model_and_applied_locally(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动 Inbox', [step(call('finish_turn', dict(mode='ask', question=ASK_GO_ON)))])
    result, _ = session.run('算了，都别做了', [step(call('finish_turn', dict(
        mode='reply', reply='好的。', input_resolution=dict(kind='cancel_request'))))])
    assert result.stop_reason == 'cancelled' and result.final.startswith('已取消')
    assert repo(session).request(session.sb.runtime._target_request())['status_actor'] == 'user_text'


def test_after_a_cancel_old_buttons_and_retries_start_nothing(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动 Inbox', [step(call('finish_turn', dict(mode='ask', question=ASK_GO_ON)))])
    question = repo(session).open_question()
    session.run('B', [])
    keep = next(c for c in question['data']['choices'] if c['key'] == 'A')
    with pytest.raises(InteractionError) as error:  # the question was closed by the cancel
        repo(session).accept('late-click', '选择 A', dict(question_id=question['question_id'], version=1,
                                                         option_id=keep['option_id']))
    assert error.value.code in {'QUESTION_STALE', 'QUESTION_ALREADY_ANSWERED'} and (tmp_path / 'Inbox/b.txt').is_file()


def test_pause_then_an_explicit_resume(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动 a', [step(start([dict(kind='organize', description='归档 a')]))], final='稍等。')
    paused, _ = session.run('先停一下', [step(call('finish_turn', dict(mode='reply', reply='好',
                                                                      input_resolution=dict(kind='pause'))))])
    assert paused.stop_reason == 'paused' and paused.final.startswith('已暂停')
    rid = session.sb.runtime.current['request_id']
    task_id = session.sb.runtime.current['id']
    assert repo(session).request(rid)['status'] == 'paused'
    result, client = session.run('继续', [step(call('prepare_file_plan', dict(
        description='归档 a', actions=ARCHIVE_A, task_id=task_id, input_resolution=dict(kind='resume', request_id=rid)),
        alias=PLAN)), execute(), finish()])
    assert errors(client) == [] and (tmp_path / 'Archive/a.txt').is_file()
    assert repo(session).request(rid)['status'] == 'active'


def test_a_cancel_while_the_card_is_open_stops_before_any_step(tmp_path):
    from test_protocol_v3 import two_part_runtime
    from agent.file_plans import PrepareFilePlan, prepare, execute as run_plan
    r, sb = two_part_runtime(tmp_path)
    _, cfg = receive(r.cfg, sb, '归档 a')
    r.cfg.update(cfg)
    rid = cfg['request_id']
    r.current['request_id'] = rid
    r._epochs[rid] = 0
    plan = prepare(r, PrepareFilePlan(description='归档 a', actions=[dict(ARCHIVE_A[0], part='g1')]))
    def cancel_meanwhile(request):
        Interactions(r.store).set_status(rid, 'cancelled', actor='user_button')
        return 'y'  # the card was approved in the same moment
    with pytest.raises(ValueError, match='USER_CANCELLED'):
        run_plan(r, plan['id'], OpLog(sb, 'one'), Confirmer('always', ask_action=cancel_meanwhile))
    assert (tmp_path / 'Inbox/a.txt').is_file()


def test_a_cancel_between_steps_lets_the_started_step_finish_and_runs_no_more(tmp_path, monkeypatch):
    from test_protocol_v3 import two_part_runtime
    from agent import file_plans
    r, sb = two_part_runtime(tmp_path)
    _, cfg = receive(r.cfg, sb, '归档并改名')
    r.cfg.update(cfg); rid = cfg['request_id']; r.current['request_id'] = rid; r._epochs[rid] = 0
    from test_protocol_v3 import ARCHIVE_AND_RENAME
    plan = file_plans.prepare(r, file_plans.PrepareFilePlan(description='归档并改名', actions=ARCHIVE_AND_RENAME))
    original = file_plans._run_step
    def step_then_cancel(*args):
        original(*args)
        Interactions(r.store).set_status(rid, 'cancelled', actor='user_button')
    monkeypatch.setattr(file_plans, '_run_step', step_then_cancel)
    with pytest.raises(ValueError, match='USER_CANCELLED'):
        file_plans.execute(r, plan['id'], OpLog(sb, 'one'), Confirmer('never'))
    done = [(tmp_path / 'Archive/a.txt').is_file(), (tmp_path / 'Inbox/c.txt').is_file()]
    assert done.count(True) == 1  # one step ran and is journalled; the other did not start


def test_an_interrupted_card_reports_the_users_cancel_not_a_refusal():
    outcome = Confirmer('always', ask_action=lambda request: 'interrupted:user_cancelled', source='web').decide('file_plan', {})
    assert (outcome.decision, outcome.reason) == ('interrupted', 'user_cancelled')
