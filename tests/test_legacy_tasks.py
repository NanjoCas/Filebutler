"""Tasks and chat history left from protocol v2 (retired 2026-10-23, tag archive-v2-legacy-20261023).

The user's library holds tasks registered under v2. They must load without error,
show as read-only and never be verified or continued; v2 tool calls in an old
conversation reach the model only as notes.
"""
import json

import pytest

from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.intent import ReviseTask, compile_revision
from agent.task import LEGACY_NOTE, TaskStore, is_legacy
from agent.task_runtime import TaskRuntime
from agent.verifier import verify_task
from safety.sandbox import Sandbox

# Every field a v2 revision stored for a condition (model_dump of the old schema).
V2_FIELDS = dict(path='', reference='', sha256='', tool='', arguments={}, destination='', extensions=[],
                 exclude_paths=[], recursive=True, placement='flatten', placements={}, evaluation='current_state')


def stored(store, task_id, goals, constraints=()):
    data = dict(id=task_id, revision=1, goals=goals, constraints=list(constraints), read_roots=['.'],
                write_roots=['.'], allowed_effects=['move'], unresolved_questions=[], status='partial',
                observations=[], outputs=[], checks=[], reviews=[], references=[], source_messages=[],
                operation_history=[], operations=[], file_plan_receipts={}, file_plan_bindings={})
    with store.connect() as db:
        db.execute('INSERT INTO task_revisions(id,revision,root,session,data_json) VALUES(?,?,?,?,?)',
                   (task_id, 1, store.root, store.session, json.dumps(data, ensure_ascii=False)))


def goal(kind, **fields):
    return dict(id='g0', description='旧目标', semantic_review=False, conditions=[dict(V2_FIELDS, kind=kind, **fields)])


@pytest.fixture
def runtime(tmp_path):
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    r = TaskRuntime('续做', dict(task_session='s'), sb, 'run'); sb.runtime = r
    return r


def test_a_v2_condition_is_shown_read_only_and_never_verified(runtime):
    stored(runtime.store, 'Told00000001', [goal('path_exists', path='a.txt')])
    task = runtime.store.get('Told00000001')
    assert is_legacy(task) and task['goals'][0]['conditions'][0]['path'] == 'a.txt'  # kept as stored
    [check] = verify_task(task, runtime.sb)
    assert not check['passed'] and not check['needs_review'] and check['legacy']
    assert check['conditions'][0]['detail'] == LEGACY_NOTE


@pytest.mark.parametrize('legacy', [
    [goal('files_placed', path='in', destination='out')],
    [goal('plan_applied', evaluation='execution_fact')],
])
def test_retired_kinds_and_evaluation_modes_are_legacy(runtime, legacy):
    stored(runtime.store, 'Told00000002', legacy)
    assert is_legacy(runtime.store.get('Told00000002'))


def test_a_retired_rule_predicate_is_legacy(runtime):
    rule = dict(effects=['rename'], selector='source', predicate='same_extension', values=[], limit=None, count_scope='task')
    stored(runtime.store, 'Told00000003', [goal('file_plan_verified')],
           [dict(id='c', description='后缀不变', forbidden_effects=[], rules=[rule], goal_ids=[])])
    assert is_legacy(runtime.store.get('Told00000003'))


def test_a_legacy_task_cannot_be_revised_or_continued(runtime):
    stored(runtime.store, 'Told00000004', [goal('path_absent', path='a.txt')])
    task = runtime.store.get('Told00000004')
    with pytest.raises(ValueError, match='只读'):
        runtime.store.preview_edit('Told00000004', compile_revision(ReviseTask(
            task_id='Told00000004', expected_revision=1, reason='更正', remove_part_ids=['g0']), task))
    text = runtime.dispatch('prepare_file_plan', json.dumps(dict(description='续做', task_id='Told00000004', actions=[
        dict(effect='move', source='a.txt', destination='b/a.txt')])), runtime.sb, None, None)
    assert text.startswith('ERROR') and '只读' in text


def test_a_v3_task_stored_with_the_old_empty_fields_still_revises(runtime):
    stored(runtime.store, 'Tv3000000001', [goal('file_plan_verified')])
    task = runtime.store.get('Tv3000000001')
    assert not is_legacy(task) and task['goals'][0]['conditions'] == [dict(kind='file_plan_verified', reference='')]
    edit = runtime.store.preview_edit('Tv3000000001', compile_revision(ReviseTask(
        task_id='Tv3000000001', expected_revision=1, reason='补充', limits=dict(no_effects=['trash'])), task))
    assert [c['field'] for c in edit['changes']] == ['constraints']  # the unchanged goal is not a change


def test_v2_tool_calls_in_an_old_conversation_reach_the_model_as_notes():
    call = lambda cid, name, args: dict(role='assistant', content='', tool_calls=[
        dict(id=cid, type='function', function=dict(name=name, arguments=json.dumps(args)))])
    history = [dict(role='system', content='rules'), dict(role='user', content='整理'),
               call('a', 'set_task', dict(goals=[])), dict(role='tool', tool_call_id='a', content='OK: task_id=T1'),
               call('b', 'move', dict(src='x.txt', dst_dir='归档')), dict(role='tool', tool_call_id='b', content='OK: x.txt → 归档/x.txt'),
               call('c', 'list_dir', {}), dict(role='tool', tool_call_id='c', content='OK: 共 0 项'),
               dict(role='assistant', content='已移动'), dict(role='user', content='再看看')]
    projected, stats = prepare_history(history, {})
    names = [tc['function']['name'] for m in projected for tc in m.get('tool_calls', [])]
    assert names == ['list_dir'] and stats['legacy_folded'] == 2
    notes = [m['content'] for m in projected if m['content'].startswith('[旧版本工具记录]')]
    assert notes == ['[旧版本工具记录] set_task {"goals": []}\n成功（历史结果；当前位置需核实）',
                     '[旧版本工具记录] move {"src": "x.txt", "dst_dir": "\\u5f52\\u6863"}\n成功（历史结果；当前位置需核实）']
    assert projected[-1] == dict(role='user', content='再看看')


def test_decisions_saved_before_the_table_are_migrated_on_load(runtime):
    stored(runtime.store, 'Tv3000000002', [goal('file_plan_verified')])
    with runtime.store.connect() as db:
        row = db.execute('SELECT data_json FROM task_revisions WHERE id=?', ('Tv3000000002',)).fetchone()
        data = json.loads(row['data_json'])
        data.update(sensitive_grants=['合同.txt'], chosen_scope=dict(revision=1, key='B', label='只移 a', excluded=['g0']))
        db.execute('UPDATE task_revisions SET data_json=? WHERE id=?', (json.dumps(data, ensure_ascii=False), 'Tv3000000002'))
    task = runtime.store.get('Tv3000000002')
    assert 'chosen_scope' not in task and 'sensitive_grants' not in task
    [check] = verify_task(task, runtime.sb)
    assert check['outcome'] == 'by_choice' and check['notes'][0]['text'] == '按你选择的选项 B（只移 a），此部分不执行'
    runtime.current = task
    assert runtime.sensitive_grants() == {'合同.txt'}
