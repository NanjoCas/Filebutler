"""2.0: question rows are authoritative; task notes cannot block or authorize writes."""
import json

import pytest

from agent.interactions import Interactions
from agent.intent import StartTask, ReviseTask
from agent.task import TaskPatch, task_state
from helpers import Session, call, step, files_fixture
from test_protocol_v3 import start, errors, execute, finish, finish_with, PLAN
from test_answer_replays import ASK_B, TWO_PARTS, clash_b


def test_task_schemas_cannot_create_a_second_copy_of_questions():
    assert 'questions' not in StartTask.model_json_schema()['properties']
    assert 'questions' not in ReviseTask.model_json_schema()['properties']
    with pytest.raises(ValueError, match='历史备注'):
        TaskPatch(expected_revision=1, reason='x', unresolved_questions=[])


def test_task_notes_are_read_only_history_and_do_not_mean_waiting():
    task = dict(unresolved_questions=['old note'], checks=[], constraint_failures=[])
    assert task_state(task, waiting=False) == 'partial'
    assert task_state(task, waiting=True) == 'waiting_input'


def test_waiting_questions_are_scoped_to_the_task_including_suspended(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('处理 b', [step(start(TWO_PARTS), call('ask_user', dict(ASK_B,
                 scope=dict(level='parts', part_ids=['b']))))])
    store = session.sb.runtime.store
    repo = Interactions(store)
    task_id = session.sb.runtime.current['id']
    q = repo.open_question(task_id)
    assert q and repo.open_question('other-task') is None
    repo.suspend_question(q['question_id'])
    assert repo.open_question() is None
    assert repo.open_question(task_id, include_suspended=True)['status'] == 'suspended'
    assert store.get(task_id)['status'] == 'waiting_input'


def test_a_button_answer_preserves_unrelated_historical_notes(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('处理 b', [step(start([TWO_PARTS[1]]), call('ask_user', dict(ASK_B,
                scope=dict(level='parts', part_ids=['b']))))])
    runtime = session.sb.runtime
    runtime.current['unresolved_questions'] = ['historical note about a']
    runtime.store.checkpoint(runtime.current)
    result, client = session.run('A', [finish_with([dict(part_id='b', reason='用户先不移动')])])
    assert errors(client) == []
    assert session.sb.runtime.current['unresolved_questions'] == ['historical note about a']
    assert 'answered_questions' not in session.sb.runtime.current


def test_a_typed_answer_can_continue_a_task_without_editing_its_questions(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    ask = dict(question='a 移到哪里？', reason='missing_criterion', subject=['Inbox/a.txt'],
               options=[dict(label='Archive', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive/a.txt')]),
                        dict(label='先不动', actions=[])])
    session.run('移动 a', [step(start([dict(id='a', kind='organize', description='移动 a')]), call('ask_user', ask))])
    tid = session.sb.runtime.current['id']
    result, client = session.run('放到 Archive', [step(call('prepare_file_plan', dict(task_id=tid,
        input_resolution=dict(kind='answer'), description='移动 a',
        actions=[dict(effect='move', source='Inbox/a.txt', destination='Archive/a.txt')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [] and result.runtime['task_status'] == 'completed'
    assert not any(a.get('kind') == 'task_edit' for a in session.approvals)
    assert (tmp_path / 'Archive/a.txt').read_text() == 'alpha'


def test_questions_asked_before_registration_still_need_an_input_relation_without_receipts(tmp_path):
    from agent.task_runtime import TaskRuntime
    from safety.oplog import OpLog
    from safety.confirm import Confirmer
    from tests.test_file_plans import runtime
    r = runtime(tmp_path, task=False)
    (tmp_path / 'a.txt').write_text('a')
    asked = dict(question='a 移到哪里？', reason='missing_criterion', subject=['a.txt'],
                 options=[dict(label='out', actions=[dict(effect='move', path='a.txt', destination='out/a.txt')]),
                          dict(label='先不动', actions=[])])
    assert r.dispatch('ask_user', json.dumps(asked), r.sb, None, None).startswith('OK')
    r.store.save_outcome(dict(run_id='one', pending_choice=r.pending_choice, task_id=None))
    later = TaskRuntime('放到 out', r.cfg, r.sb, 'two')
    assert later.relation_needed()
    question_id = later.answered_choice['question_id']
    assert later.dispatch('start_task', json.dumps(dict(summary='移动', parts=[dict(kind='organize', description='移动')],
                           input_resolution=dict(kind='answer'))), r.sb, None, None).startswith('OK')
    with later.store.connect() as db:
        row = db.execute('SELECT * FROM questions WHERE question_id=?', (question_id,)).fetchone()
    assert row['status'] == 'replied' and row['task_id'] == later.current['id']
