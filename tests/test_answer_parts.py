"""User decision A1 (2026-10-29): an answer part backed by this request's local facts is
done; without any local fact it still needs review. Answer parts registered earlier keep
manual_review and are reviewed as before."""
import copy

from agent.verifier import verify_task
from helpers import Session, call, files_fixture, step
from test_protocol_v3 import PLAN, execute, finish, start


def test_an_answer_without_any_local_fact_still_needs_review(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('Inbox 里有几个 txt？', [step(start([dict(id='count', kind='answer', description='数 txt')])), finish()])
    check = session.sb.runtime.current['checks'][0]
    assert result.stop_reason == 'needs_review' and check['outcome'] == 'review'
    assert '没有本地查看' in check['conditions'][0]['detail']


def test_a_write_with_an_answer_after_a_verified_move_is_done(tmp_path):
    # Rounds 45/47/48 s5_explain_after_write_v2: the move was verified and the count was
    # right, yet the answer part alone kept the task at needs_review (0/3 every round).
    files_fixture(tmp_path)
    session = Session(tmp_path)
    parts = [dict(id='move', kind='organize', description='归档 a'), dict(id='count', kind='answer', description='Inbox 还剩几个')]
    result, _ = session.run('把 a 移到 Archive，再告诉我 Inbox 还剩几个文件', [
        step(start(parts), call('prepare_file_plan', dict(description='归档', actions=[
            dict(effect='move', source='Inbox/a.txt', destination='Archive', part='move')]), alias=PLAN)),
        execute(), finish('还剩 3 个')])
    outcomes = {c['goal_id']: c['outcome'] for c in session.sb.runtime.current['checks']}
    assert outcomes == {'move': 'done', 'count': 'done'} and result.stop_reason == 'done'


def test_an_answer_part_registered_before_the_change_keeps_its_review(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('看看', [step(call('list_dir', dict(path='Inbox'))),
                       step(start([dict(id='count', kind='answer', description='数 txt')])), finish()])
    task = copy.deepcopy(session.sb.runtime.current)
    assert task['checks'][0]['outcome'] == 'done'  # list_dir in this request
    task['goals'][0]['conditions'] = [dict(kind='manual_review')]
    task['goals'][0]['semantic_review'] = True
    assert verify_task(task, session.sb)[0]['outcome'] == 'review'
