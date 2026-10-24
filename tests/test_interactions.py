"""Message receipts, exact answers and the migration (routing redesign S2,
docs/agent-routing-redesign-2026-10-24.md 4.4, 11, 13)."""
import json
import sqlite3
import threading

import pytest

from agent.interactions import InteractionError, Interactions, SCHEMA_VERSION, recover
from agent.task import TaskStore

QUESTION = dict(question='b.txt 怎么处理？', reason='conflict', allow_other=True, subject=['Archive/b.txt'],
                choices=[dict(key='A', label='b.txt 不移动', actions=[]),
                         dict(key='B', label='覆盖', actions=[dict(effect='trash', path='Archive/b.txt')])])


def repo(tmp_path, session='s1'):
    return Interactions(TaskStore(tmp_path / 'tasks.sqlite3', tmp_path, session))


def pick(question, key):
    option = next(c for c in question['choices'] if c['key'] == key)
    return dict(question_id=question['question_id'], version=question['version'], option_id=option['option_id'])


def test_a_retry_with_the_same_id_gets_the_same_receipt_and_a_changed_payload_conflicts(tmp_path):
    r = repo(tmp_path)
    first = r.accept('m1', '把 a 移到 Archive')
    again = r.accept('m1', '把 a 移到 Archive')
    assert again['existing'] and again['receipt_id'] == first['receipt_id'] and again['run_request_id'] == first['run_request_id']
    with pytest.raises(InteractionError) as error:
        r.accept('m1', '把 b 移到 Archive')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    # The same text under a new id is a new message: the user may mean to send it twice.
    assert r.accept('m2', '把 a 移到 Archive')['receipt_id'] != first['receipt_id']


def test_retrying_q1s_answer_after_q2_was_asked_returns_q1s_receipt_and_leaves_q2_open(tmp_path):
    r = repo(tmp_path)
    q1 = r.record_question(QUESTION, run_id='run1')
    answer = r.accept('m1', '选择 A', pick(q1, 'A'))
    q2 = r.record_question(dict(QUESTION, question='a.txt 呢？'), run_id='run2')
    retried = r.accept('m1', '选择 A', pick(q1, 'A'))
    assert retried['existing'] and retried['answer_id'] == answer['answer_id']
    assert r.open_question()['question_id'] == q2['question_id']
    with pytest.raises(InteractionError) as error:  # a new message cannot answer Q1 again
        r.accept('m3', '选择 B', pick(q1, 'B'))
    assert error.value.code == 'QUESTION_ALREADY_ANSWERED'


def test_stale_foreign_and_unknown_answers_are_refused_without_saying_more(tmp_path):
    r = repo(tmp_path)
    q1 = r.record_question(QUESTION, run_id='run1')
    q2 = r.record_question(dict(QUESTION, question='另一问'), run_id='run2')  # supersedes q1
    with pytest.raises(InteractionError) as error:
        r.accept('m1', 'A', pick(q1, 'A'))
    assert error.value.code == 'QUESTION_STALE'
    other = repo(tmp_path, session='s2')
    with pytest.raises(InteractionError) as error:
        other.accept('m2', 'A', pick(q2, 'A'))
    assert error.value.code == 'QUESTION_STALE' and '不属于当前会话' in str(error.value)
    with pytest.raises(InteractionError) as error:
        r.accept('m3', 'A', dict(pick(q2, 'A'), option_id='Onot-an-option'))
    assert error.value.code == 'INVALID_ARGUMENTS'
    with pytest.raises(InteractionError) as error:
        r.accept('m4', 'A', dict(pick(q2, 'A'), version=2))
    assert error.value.code == 'QUESTION_STALE'
    assert r.open_question()['question_id'] == q2['question_id']  # nothing was consumed


def test_two_answers_racing_for_one_question_consume_it_once(tmp_path):
    r = repo(tmp_path)
    q = r.record_question(QUESTION, run_id='run1')
    results, barrier = [], threading.Barrier(2)

    def answer(message_id, key):
        barrier.wait()
        try: results.append(('ok', repo(tmp_path).accept(message_id, key, pick(q, key))['answer_id']))
        except InteractionError as error: results.append((error.code, None))

    threads = [threading.Thread(target=answer, args=(f'm{k}', k)) for k in 'AB']
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(code for code, _ in results) == ['QUESTION_ALREADY_ANSWERED', 'ok']
    with r.store.connect() as db:
        assert db.execute('SELECT count(*) FROM answers').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM run_requests').fetchone()[0] == 1


def test_typed_text_closes_the_open_question_without_choosing(tmp_path):
    r = repo(tmp_path)
    q = r.record_question(QUESTION, run_id='run1')
    receipt = r.accept('m1', '覆盖吧')
    assert receipt['answer_id'] is None and r.open_question() is None
    assert r.replied_to(receipt['receipt_id'])['question_id'] == q['question_id']


def test_a_restart_marks_running_requests_interrupted_and_returns_queued_ones(tmp_path):
    r = repo(tmp_path)
    running = r.accept('m1', '一')
    assert r.claim(running['run_request_id'], 'worker', 'run1') and not r.claim(running['run_request_id'], 'other', 'run1')
    queued = r.accept('m2', '二')
    pending = recover(r.store)
    assert [p['run_request_id'] for p in pending] == [queued['run_request_id']]
    assert r.run_request(running['run_request_id'])['status'] == 'interrupted'
    # A retry of the interrupted message returns its receipt; nothing runs again.
    assert r.accept('m1', '一')['status'] == 'interrupted'


def test_migration_backs_up_and_turns_the_waiting_question_into_one_open_question(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE run_outcomes (run_id TEXT NOT NULL, root TEXT NOT NULL, session TEXT NOT NULL, '
               'data_json TEXT NOT NULL, PRIMARY KEY(run_id,root,session))')
    root = str(tmp_path.resolve())
    db.execute('INSERT INTO run_outcomes VALUES(?,?,?,?)', ('r1', root, 's1', json.dumps(dict(stop_reason='done'))))
    db.execute('INSERT INTO run_outcomes VALUES(?,?,?,?)', ('r2', root, 's1', json.dumps(dict(
        stop_reason='waiting_input', task_id=None, pending_choice=QUESTION), ensure_ascii=False)))
    db.commit(); db.close()
    store = TaskStore(path, tmp_path, 's1')
    with store.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert dict(db.execute('SELECT key, value FROM protocol_meta').fetchall())['stage'] == 'authoritative'
    assert list(tmp_path.glob('tasks.sqlite3.bak-v0-*'))
    question = Interactions(store).open_question()
    assert json.loads(question['scope_json'])['level'] == 'legacy_scope_unknown'
    assert all(c['option_id'].startswith('O') for c in question['data']['choices'])
    TaskStore(path, tmp_path, 's1')  # opening again changes nothing
    assert len(list(tmp_path.glob('tasks.sqlite3.bak-*'))) == 1
