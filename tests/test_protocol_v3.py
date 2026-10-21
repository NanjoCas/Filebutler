"""Protocol v3 (P1-4): the model states intent and objects; contracts are compiled locally.

Two kinds of tests. Unit tests pin the compiler and the authorization boundary
the user confirmed on 2026-10-21 (write scope derived from the plan and listed on
its confirmation; text-only limits shown on the confirmation and kept in review;
user limits never widened). Scripted scenarios replay the regression/holdout
requests with v3 calls through the real loop (no model API) and record the
number of model requests each needs, the lower bound reported in
docs/protocol-v3-2026-10-21.md. Calls reused from the recorded live runs keep
their arguments verbatim; IDs are remapped by the replay harness.
"""
from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from agent.history import prepare_history
from agent.intent import StartTask, ReviseTask, compile_start, compile_revision, project
from agent.llm import LLM
from agent.loop import run_agent
from agent.prompts import SYSTEM_PROMPTS
from agent.task_runtime import TaskRuntime
from eval.holdout_cases import holdout_fixture
from safety.confirm import Confirmer
from safety.oplog import OpLog
from tools import registry

import test_live_regressions_20261021 as H
from test_live_regressions_20261021 import Replay, step, call, recorded, files_fixture, duplicates_fixture, \
    applications_fixture, assert_root_only_files

CFG = dict(H.CFG, protocol='v3', prompt='v9_intent')
TASK, PLAN, PLAN2, SCAN = 'Taaaaaaaaaaaa', 'Xaaaaaaaaaaaa', 'Xbbbbbbbbbbbb', 'cccccccccccc'


class Session(H.Session):
    """H.Session with the v3 configuration."""

    def run(self, instruction, steps, final=''):
        self.turn += 1
        if self.history:
            history, _ = prepare_history(self.history + [dict(role='user', content=instruction)], CFG, self.store)
        else:
            history = [dict(role='system', content=SYSTEM_PROMPTS[CFG['prompt']]),
                       dict(role='user', content=instruction)]
        client = Replay(steps, self.ids, final)
        approve = lambda request: self.approvals.append(request) or self.answer
        result = run_agent(instruction, LLM({'model': 'fake'}, client=client), self.sb, CFG,
                           Console(file=io.StringIO()), artifacts=self.store, history=history,
                           oplog=OpLog(self.sb, f'v3_{self.turn}'),
                           confirmer=Confirmer('always', ask_action=approve))
        client.absorb(result.messages)
        self.history = result.messages
        return result, client


def errors(client):
    return [(c['tool'], c['text'][:160]) for c in client.log if c['status'] != 'ok']


def start(parts, **extra):
    return call('start_task', dict(summary='用户要求', parts=parts, **extra), alias=TASK)


def execute(alias=PLAN):
    return step(call('execute_file_plan', {'plan_id': alias}))


def finish(summary=''):
    return step(call('finish_task', {'summary': summary}))


# ============================================================================ compiler


def test_each_kind_compiles_to_its_local_condition_with_empty_references():
    spec = compile_start(StartTask(summary='s', parts=[
        dict(kind=k, description=k) for k in ('find', 'dedup', 'organize', 'classify', 'answer')]))
    kinds = {g.id: [c.kind for c in g.conditions] for g in spec.goals}
    assert kinds == {'find_1': ['evidence_valid'], 'dedup_2': ['receipt_verified'],
                     'organize_3': ['file_plan_verified'], 'classify_4': ['plan_ready'], 'answer_5': ['manual_review']}
    assert all(not c.reference for g in spec.goals for c in g.conditions)
    assert {g.id for g in spec.goals if g.semantic_review} == {'find_1', 'classify_4', 'answer_5'}
    assert spec.allowed_effects == ['create', 'move', 'rename', 'trash'] and spec.write_roots == ['.']
    executed = compile_start(StartTask(summary='s', parts=[dict(kind='classify', description='c', execute=True)]))
    assert [c.kind for c in executed.goals[0].conditions] == ['plan_applied']
    assert executed.allowed_effects == ['create', 'move']


def test_read_only_parts_open_no_write_scope():
    spec = compile_start(StartTask(summary='s', parts=[dict(kind='find', description='f'), dict(kind='answer', description='a')]))
    assert spec.allowed_effects == [] and spec.write_roots == []


def test_limits_compile_to_rules_and_only_ever_narrow():
    spec = compile_start(StartTask(summary='s', parts=[dict(kind='organize', description='o', scope=['Inbox', 'Archive'])],
                                   limits=dict(no_effects=['create', 'trash'], keep_unchanged=['Inbox/nested'],
                                               only_within=['Inbox', 'Archive'], only_extensions=['txt'], max_files=2,
                                               notes=['按项目整理'])))
    assert spec.allowed_effects == ['move', 'rename'] and spec.write_roots == ['Inbox', 'Archive']
    limits, note = spec.constraints
    assert limits.forbidden_effects == ['create', 'trash']
    rules = {(r.predicate, r.selector): r for r in limits.rules}
    assert rules['max_count', 'source'].effects == ['move', 'rename', 'trash']  # objects, not created folders
    assert rules['max_count', 'source'].limit == 2 and rules['max_count', 'source'].count_scope == 'task'
    assert rules['outside', 'source'].values == ['Inbox/nested'] and rules['outside', 'destination'].values == ['Inbox/nested']
    assert rules['within', 'destination'].values == ['Inbox', 'Archive']
    assert rules['extensions', 'source'].values == ['txt']
    assert note.description == '按项目整理' and not note.rules and not note.forbidden_effects


def test_revision_compiles_to_a_reviewed_patch():
    task = dict(goals=[dict(id='organize_1', description='o', conditions=[dict(kind='file_plan_verified')], semantic_review=False)],
                constraints=[dict(id='user_limits', description='x', forbidden_effects=['trash'], rules=[]),
                             dict(id='note_1', description='旧备注', forbidden_effects=[], rules=[])],
                allowed_effects=['move'], write_roots=['Inbox'])
    patch = compile_revision(ReviseTask(task_id='T', expected_revision=3, reason='更正',
                                        parts=[dict(id='organize_1', kind='organize', description='新要求')]), task)
    assert patch.expected_revision == 3 and [g.id for g in patch.upsert_goals] == ['organize_1']
    # A new organize part may move/create/rename, but the user's "no trash" stays.
    assert patch.allowed_effects == ['create', 'move', 'rename'] and patch.write_roots == ['Inbox', '.']
    relax = compile_revision(ReviseTask(task_id='T', expected_revision=3, reason='用户允许回收',
                                        limits=dict()), task)
    assert relax.remove_constraint_ids == ['user_limits', 'note_1'] and relax.allowed_effects is None
    with pytest.raises(ValueError, match='不存在'):
        compile_revision(ReviseTask(task_id='T', expected_revision=3, reason='r', remove_part_ids=['nope']), task)


def test_v3_tool_surface_is_smaller_and_hides_contract_tools():
    base = registry.schemas()
    protocol = lambda name: type('R', (), dict(protocol=name, V3_HIDDEN=TaskRuntime.V3_HIDDEN))()
    v2, v3 = TaskRuntime.schemas(protocol('v2'), base), TaskRuntime.schemas(protocol('v3'), base)
    names = {s['function']['name'] for s in v3}
    assert {'set_task', 'edit_task', 'apply_task_edit', 'begin_step', 'move', 'rename', 'trash', 'make_dir'}.isdisjoint(names)
    assert {'start_task', 'revise_task', 'prepare_file_plan', 'execute_file_plan', 'finish_task', 'apply_dedup'} <= names
    size = lambda schemas: sum(len(json.dumps(s, ensure_ascii=False)) for s in schemas)
    # Guard against regrowth on what each request actually sends: system prompt,
    # protocol guidance and tool definitions together (schemas alone grew with
    # reply/unfinished/ask_user and field descriptions, by design).
    import tempfile
    from pathlib import Path
    from agent.artifacts import ArtifactStore
    from safety.sandbox import Sandbox
    root = Path(tempfile.mkdtemp()); sb = Sandbox(root); sb.artifacts = ArtifactStore(sb)
    def total(protocol, prompt, schemas):
        runtime = TaskRuntime('x', dict(protocol=protocol, task_db=str(root / f'{protocol}.db')), sb, 'r')
        return size(schemas) + len(runtime.guidance()) + len(SYSTEM_PROMPTS[prompt])
    v2_total, v3_total = total('v2', 'v8_tasks', v2), total('v3', 'v9_intent', v3)
    assert v3_total < 0.8 * v2_total, (v3_total, v2_total)


# ============================================================================ boundary


def test_second_start_task_in_one_run_is_refused(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    parts = [dict(kind='organize', description='移动 txt')]
    result, client = session.run('移动', [step(start(parts, limits=dict(no_effects=['trash']))),
                                         step(call('start_task', dict(summary='另起', parts=parts))), finish()])
    assert '本轮已登记任务' in client.calls('start_task')[1]['text']
    assert result.runtime['task_revision'] == 1


def test_v3_plan_needs_a_registered_task(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移动', [step(call('prepare_file_plan', dict(
        description='m', actions=[dict(effect='move', source='Inbox/a.txt', destination='Archive')])))])
    assert 'start_task' in client.calls('prepare_file_plan')[0]['text']
    assert (tmp_path / 'Inbox/a.txt').is_file()


def test_user_limits_reject_before_confirmation_and_any_write(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    moves = [dict(effect='move', source=f'Inbox/{n}', destination='Archive') for n in ('a.txt', 'b.txt', 'keep.pdf')]
    _, client = session.run('移动', [step(start([dict(kind='organize', description='m')],
                                               limits=dict(max_files=2, keep_unchanged=['Inbox/keep.pdf'])),
                                         call('prepare_file_plan', dict(description='m', actions=moves))), finish()])
    assert client.calls('prepare_file_plan')[0]['status'] == 'error'
    assert not session.approvals and (tmp_path / 'Inbox/a.txt').is_file()


def test_max_files_does_not_count_created_folders(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    moves = [dict(effect='move', source=f'Inbox/{n}', destination='New/Deep') for n in ('a.txt', 'b.txt')]
    result, client = session.run('移动', [step(start([dict(kind='organize', description='m')], limits=dict(max_files=2)),
                                              call('prepare_file_plan', dict(description='m', actions=moves), alias=PLAN)),
                                         execute(), finish()])
    assert errors(client) == []
    assert (tmp_path / 'New/Deep/a.txt').is_file() and (tmp_path / 'New/Deep/b.txt').is_file()
    assert result.runtime['task_status'] == 'completed'


def test_text_limit_is_on_the_confirmation_and_keeps_review(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('移动', [step(start([dict(kind='organize', description='m')],
                                                    limits=dict(notes=['按项目而不是按日期整理'])),
                                              call('prepare_file_plan', dict(description='m', actions=[
                                                  dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)),
                                         execute(), finish()])
    assert errors(client) == []
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    assert any('按项目而不是按日期整理' in n for n in request['notes'])
    assert result.runtime['task_status'] == 'needs_review'


def test_scope_beyond_parts_is_listed_and_refusal_writes_and_grants_nothing(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.answer = 'n'
    result, client = session.run('移动', [step(start([dict(kind='organize', description='m', scope=['Archive'])]),
                                              call('prepare_file_plan', dict(description='m', actions=[
                                                  dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)),
                                         execute(), finish()])
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    assert request['args']['scope_additions'] == {'write_roots': ['Inbox/a.txt']}
    assert client.calls('execute_file_plan')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/a.txt').is_file()
    task = session.sb.runtime.current
    assert task['write_roots'] == ['Archive'] and not task.get('scope_grants')


def test_read_only_task_cannot_gain_write_effects_from_a_plan(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('看看', [step(start([dict(kind='answer', description='说明')]),
                                         call('prepare_file_plan', dict(description='m', actions=[
                                             dict(effect='move', source='Inbox/a.txt', destination='Archive')]))), finish()])
    assert client.calls('prepare_file_plan')[0]['status'] == 'error'
    assert not session.approvals and (tmp_path / 'Inbox/a.txt').is_file()


def test_rejected_revision_keeps_the_task(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动', [step(start([dict(id='organize', kind='organize', description='m')], limits=dict(no_effects=['trash']))),
                        finish()])
    session.answer = 'n'
    _, client = session.run('允许回收', [step(call('revise_task', dict(task_id=TASK, expected_revision=1, reason='放宽',
                                                                       limits=dict()))), finish()])
    assert '拒绝' in client.calls('revise_task')[0]['text']
    stored = session.sb.runtime.store.get(session.ids[TASK])
    assert stored['revision'] == 1 and stored['constraints'][0]['forbidden_effects'] == ['trash']


def test_unstarted_plan_is_rebased_after_a_revision(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('移动', [
        step(start([dict(id='organize', kind='organize', description='移动 a')]),
             call('prepare_file_plan', dict(description='m', actions=[
                 dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)),
        step(call('revise_task', dict(task_id=TASK, expected_revision=1, reason='用户补充：不能回收',
                                      limits=dict(no_effects=['trash'])))),
        execute(), finish()])
    assert errors(client) == []
    assert '重新生成' in client.calls('execute_file_plan')[0]['text']
    assert (tmp_path / 'Archive/a.txt').is_file()
    assert result.runtime['task_revision'] == 2 and result.runtime['task_status'] == 'completed'


def test_rebased_plan_still_obeys_the_revised_limits(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移动', [
        step(start([dict(id='organize', kind='organize', description='移动 a')]),
             call('prepare_file_plan', dict(description='m', actions=[
                 dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)),
        step(call('revise_task', dict(task_id=TASK, expected_revision=1, reason='a.txt 不要动',
                                      limits=dict(keep_unchanged=['Inbox/a.txt'])))),
        execute(), finish()])
    assert client.calls('execute_file_plan')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/a.txt').is_file() and not (tmp_path / 'Archive/a.txt').exists()


def test_dedup_part_is_attributed_by_its_receipt_without_begin_step(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('清理', [
        step(start([dict(kind='dedup', description='字节副本保留最短名'), dict(kind='organize', description='回收空文件夹')],
                   limits=dict(only_within=['Inbox'], no_effects=['move', 'rename'])),
             call('deduplicate', dict(path='Inbox', mode='bytes', keep='shortest_name')),
             call('prepare_file_plan', dict(description='回收空文件夹', actions=[dict(effect='trash', source='Inbox/empty')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == []
    assert result.runtime['task_status'] == 'completed' and result.stop_reason == 'done'
    assert client.requests == 3


def test_two_parts_verifying_receipts_still_need_an_explicit_step(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('清理', [
        step(start([dict(kind='dedup', description='a'), dict(kind='dedup', description='b')]),
             call('deduplicate', dict(path='Inbox', mode='bytes', keep='shortest_name'))), finish()])
    text = client.calls('deduplicate')[0]['text']
    assert '无法确定归属' in text and 'revise_task' in text and 'begin_step' not in text
    assert (tmp_path / 'Inbox/copy_of_a.txt').is_file()


def test_projection_shows_parts_not_raw_conditions(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移动', [step(start([dict(id='organize', kind='organize', description='移动 txt')],
                                    limits=dict(no_effects=['trash']))), finish()])
    view = project(session.sb.runtime.current)
    assert view['parts'] == [dict(id='organize', kind='organize', description='移动 txt')]
    assert view['limits'] == ['不回收'] and 'conditions' not in json.dumps(view)


# ============================================================================ scenarios (lower bound of requests)
# Each replays one regression/holdout request with v3 calls. ``client.requests``
# counts model requests; the live v2 runs of 2026-10-21 (rounds 04/05) needed
# the numbers in the comments.


def test_scenario_root_only(tmp_path):
    # live v2: 5 and 5 requests
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run(H.recorded('root_only_turn1')['instruction'], [
        step(call('list_dir', {'path': 'Inbox'}),
             start([dict(id='count', kind='answer', description='统计 Inbox 根目录 txt 数量'),
                    dict(id='organize', kind='organize', description='根目录 txt 移入现有 Archive', scope=['Inbox', 'Archive'])],
                   limits=dict(no_effects=['create', 'trash', 'rename'], keep_unchanged=['Inbox/nested', 'Inbox/keep.pdf'])),
             call('prepare_file_plan', dict(description='txt 移入 Archive', selection=dict(
                 path='Inbox', extensions=['txt'], recursive=False, destination='Archive')), alias=PLAN)),
        execute(), finish('Inbox 根目录共 2 个 txt，已移入 Archive。')])
    assert errors(client) == []
    assert_root_only_files(tmp_path)
    assert result.stop_reason == 'needs_review' and '2 个' in result.final  # the count is a model statement
    assert client.requests == 3


def test_scenario_cross_turn_correction(tmp_path):
    # live v2: 9 + 6 (round 05), 14 + 5 (round 04)
    files_fixture(tmp_path)
    session = Session(tmp_path)
    first, client = session.run('仅将 Inbox 根目录的 a.txt、b.txt 移到现有 Archive，保留原名，其他内容保持原样，不创建目录也不回收。', [
        step(start([dict(id='organize', kind='organize', description='a.txt、b.txt 移到 Archive')],
                   limits=dict(no_effects=['create', 'trash', 'rename'], keep_unchanged=['Inbox/nested', 'Inbox/keep.pdf'])),
             call('prepare_file_plan', dict(description='移动', actions=[
                 dict(effect='move', source='Inbox/a.txt', destination='Archive'),
                 dict(effect='move', source='Inbox/b.txt', destination='Archive')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [] and first.stop_reason == 'done' and client.requests == 3
    second, client = session.run('更正刚才的要求：b.txt 应留在 Inbox，请只把 b.txt 移回；a.txt 继续放在 Archive。', [
        step(call('revise_task', dict(task_id=TASK, expected_revision=1, reason='用户更正：b.txt 应留在 Inbox',
                                      parts=[dict(id='organize', kind='organize', description='a.txt 在 Archive，b.txt 移回 Inbox')])),
             call('prepare_file_plan', dict(description='b.txt 移回', actions=[
                 dict(effect='move', source='Archive/b.txt', destination='Inbox')]), alias=PLAN2)),
        execute(PLAN2), finish()])
    assert errors(client) == [], errors(client)
    assert (tmp_path / 'Archive/a.txt').read_text() == 'alpha' and (tmp_path / 'Inbox/b.txt').read_text() == 'beta'
    assert second.stop_reason == 'done' and second.runtime['task_revision'] == 2
    assert client.requests == 3
    # The user's original limits survived the correction.
    assert session.sb.runtime.current['constraints'][0]['forbidden_effects'] == ['create', 'trash', 'rename']


def test_scenario_dedup_cleanup(tmp_path):
    # live v2: 9 and 12 requests
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('清理', [
        step(start([dict(kind='dedup', description='字节副本保留最短名'), dict(kind='organize', description='回收空文件夹')],
                   limits=dict(only_within=['Inbox'], no_effects=['move', 'rename'])),
             call('deduplicate', dict(path='Inbox', mode='bytes', keep='shortest_name')),
             call('prepare_file_plan', dict(description='回收空文件夹', actions=[dict(effect='trash', source='Inbox/empty')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == []
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'same exact bytes' and not (tmp_path / 'Inbox/copy_of_a.txt').exists()
    assert not (tmp_path / 'Inbox/empty').exists() and (tmp_path / 'Inbox/nonempty/keep.txt').is_file()
    assert (tmp_path / 'Inbox/unique.txt').read_text() == 'different'
    assert result.stop_reason == 'done' and client.requests == 3


def test_scenario_application_dimension(tmp_path):
    # live v2: 13 and 7 requests
    applications_fixture(tmp_path)
    session = Session(tmp_path)
    data = recorded('application_dimension_turn1')
    recorded_call = lambda trace_step, tool: next(c for s in data['steps'] if s['trace_step'] == trace_step
                                                  for c in s['calls'] if c['tool'] == tool)
    result, client = session.run(data['instruction'], [
        step(start([dict(id='classify', kind='classify', execute=True, description='按应用领域分三类并移入 资料/按领域', scope=['资料'])],
                   limits=dict(no_effects=['rename', 'trash'], notes=['不新增按方法划分的目录']),
                   criteria=['按应用领域（农业/医疗/能源），不按 CNN 等方法']),
             recorded_call(4, 'paper_profiles')),
        step(recorded_call(6, 'submit_classification')),
        step(call('prepare_file_plan', dict(description='执行分类方案', classification_plan_id='P5e2e89178f'), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [], errors(client)
    for i, category in enumerate(('农业', '医疗', '能源'), 1):
        assert (tmp_path / '资料/按领域' / category / f'CNN_{i:02d}.pdf').is_file()
    assert result.stop_reason == 'needs_review'  # classification semantics and the text limit
    assert client.requests == 5


def test_scenario_holdout_search_dedup_move(tmp_path):
    # live v2: 15 and 17 requests
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    notes = ['note_01.txt', 'note_01_copy.txt', 'note_02.txt', 'note_03.txt', 'note_04.txt']
    reads = [dict(tool='read_file', arguments=json.dumps({'path': f'Notes/{n}'}), status='ok', result_head='',
                  result_ids=[f'Faaaaaaaaa{i:02d}', f'Eaaaaaaaaa{i:02d}']) for i, n in enumerate(notes)]
    finding = lambda i: dict(file_id=f'Faaaaaaaaa{i:02d}', evidence_ids=[f'Eaaaaaaaaa{i:02d}'], relation='topic',
                             reason='片段讲光合作用')
    result, client = session.run('在 Notes 里找出光合作用笔记，去重后移到 Topics/光合作用', [
        step(call('search_files', {'pattern': '*.txt', 'path': 'Notes'}),
             start([dict(id='find', kind='find', description='找出与光合作用有关的笔记'),
                    dict(id='dedup', kind='dedup', description='字节副本只保留最短文件名'),
                    dict(id='organize', kind='organize', description='保留的光合作用笔记移到 Topics/光合作用')],
                   limits=dict(no_effects=['rename'], keep_unchanged=['Notes/misc']))),
        step(*reads),
        step(call('report_findings', dict(findings=[finding(0), finding(2)])),
             call('deduplicate', dict(path='Notes', mode='bytes', keep='shortest_name')),
             call('prepare_file_plan', dict(description='移到 Topics/光合作用', actions=[
                 dict(effect='move', source='Notes/note_01.txt', destination='Topics/光合作用'),
                 dict(effect='move', source='Notes/note_02.txt', destination='Topics/光合作用')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [], errors(client)
    assert (tmp_path / 'Topics/光合作用/note_01.txt').is_file() and (tmp_path / 'Topics/光合作用/note_02.txt').is_file()
    assert not (tmp_path / 'Notes/note_01_copy.txt').exists() and (tmp_path / 'Notes/misc/readme.txt').is_file()
    task = session.sb.runtime.current
    assert all(c['passed'] for c in task['checks']), task['checks']
    assert result.stop_reason == 'needs_review'  # topical relevance needs review
    assert client.requests == 5


def test_scenario_holdout_partial_resume(tmp_path):
    # live v2: 9 + 6 (round 05), 15 + 5 (round 04)
    holdout_fixture(tmp_path, 'batch')
    session = Session(tmp_path)
    first, client = session.run('先只移 a 开头的 txt 到 Done', [
        step(start([dict(id='organize', kind='organize', description='Inbox 根目录 txt 移到 Done；本轮只移 a 开头')],
                   limits=dict(no_effects=['create', 'trash', 'rename'], keep_unchanged=['Inbox/keep.md'])),
             call('prepare_file_plan', dict(description='a 开头', actions=[
                 dict(effect='move', source=f'Inbox/{n}', destination='Done') for n in ('a1.txt', 'a2.txt')]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [] and client.requests == 3
    second, client = session.run('继续把 b 开头的也移到 Done，沿用原任务', [
        step(call('prepare_file_plan', dict(task_id=TASK, description='b 开头', actions=[
            dict(effect='move', source=f'Inbox/{n}', destination='Done') for n in ('b1.txt', 'b2.txt')]), alias=PLAN2)),
        execute(PLAN2), finish()])
    assert errors(client) == [], errors(client)
    assert all((tmp_path / 'Done' / n).is_file() for n in ('a1.txt', 'a2.txt', 'b1.txt', 'b2.txt'))
    assert (tmp_path / 'Inbox/keep.md').read_text() == 'markdown stays'
    assert second.runtime['task_id'] == first.runtime['task_id'] and second.stop_reason == 'done'
    assert client.requests == 3


def test_scenario_target_conflict_reports_without_writing(tmp_path):
    # live v2: 2 and 6 requests
    files_fixture(tmp_path)
    (tmp_path / 'Archive/a.txt').write_text('existing archived file of 30 b')
    session = Session(tmp_path)
    result, client = session.run('把 Inbox/a.txt 移到现有 Archive，目标若有同名文件不覆盖，告诉我原因', [
        step(start([dict(id='organize', kind='organize', description='a.txt 移到 Archive')],
                   limits=dict(no_effects=['create', 'trash', 'rename'])),
             call('prepare_file_plan', dict(description='移动', actions=[
                 dict(effect='move', source='Inbox/a.txt', destination='Archive')])))],
        final='Archive 里已有同名的 a.txt，按你的要求没有覆盖，也没有移动。')
    assert '目标已存在' in client.calls('prepare_file_plan')[0]['text']
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'alpha'
    assert result.runtime['task_status'] in {'partial', 'waiting_input'} and '已移动' not in result.final
    assert '同名' in result.final or '已存在' in result.final
    assert client.requests == 2


# ============================================================================ rejection messages
# Live rounds 04/05: "超过task操作数量上限 3" carried no count, and the model
# invented a cause ("目录创建也被计入") and raised its own limit to 10. A
# rejection now states the facts the model would otherwise guess.


def test_count_rejection_states_what_was_counted(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    moves = [dict(effect='move', source=f'Inbox/{n}', destination='Archive') for n in ('a.txt', 'b.txt')]
    _, client = session.run('移动', [step(start([dict(kind='organize', description='m')], limits=dict(max_files=1)),
                                         call('prepare_file_plan', dict(description='m', actions=moves))), finish()])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '上限 1' in text and '移动/改名/回收' in text and '此前已计 0 项' in text
    assert '本次新增 2 项' in text and 'Inbox/a.txt' in text and '不要自行放宽' in text


def test_rule_rejection_states_the_rule_meaning(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移动', [step(start([dict(kind='organize', description='m')],
                                               limits=dict(keep_unchanged=['Inbox/keep.pdf'])),
                                         call('prepare_file_plan', dict(description='m', actions=[
                                             dict(effect='move', source='Inbox/keep.pdf', destination='Archive')]))), finish()])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '规则含义：来源（移动/改名/回收）不得位于 Inbox/keep.pdf 之内' in text


def test_stale_partly_executed_plan_says_what_to_do_next(tmp_path):
    from agent.file_plans import PrepareFilePlan, prepare, execute
    from agent.artifacts import ArtifactStore
    from safety.sandbox import Sandbox
    files_fixture(tmp_path)
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    r = TaskRuntime('移动', dict(protocol='v3'), sb, 'one'); sb.runtime = r
    r.dispatch('start_task', json.dumps(dict(summary='s', parts=[dict(id='o', kind='organize', description='m')])), sb, None, None)
    plan = prepare(r, PrepareFilePlan(description='m', actions=[
        dict(effect='move', source=f'Inbox/{n}', destination='Archive') for n in ('a.txt', 'b.txt')]))
    # As if the first action had run before the task was revised.
    plan['progress'][plan['payload']['steps'][0]['id']] = dict(run_id='earlier', log_path='', stat=[0, 0])
    r.store.save_file_plan(plan)
    edit = r.store.preview_edit(r.current['id'], compile_revision(ReviseTask(
        task_id=r.current['id'], expected_revision=1, reason='补充', limits=dict(no_effects=['trash'])), r.current))
    r.store.apply_edit(edit['id'], '补充', 'one', actor='user')
    with pytest.raises(ValueError) as exc:
        execute(r, plan['id'], OpLog(sb, 'one'), Confirmer('never'))
    assert '第 1 版，当前第 2 版' in str(exc.value) and '已执行 1/2 项' in str(exc.value)
    assert 'inspect_file_plan' in str(exc.value) and (tmp_path / 'Inbox/b.txt').is_file()


# ============================================================================ tool definitions


def visible_schemas(name):
    import agent.loop  # noqa: F401 - registers every tool module, as at runtime
    protocol = type('R', (), dict(protocol=name, V3_HIDDEN=TaskRuntime.V3_HIDDEN))()
    return TaskRuntime.schemas(protocol, registry.schemas())


def test_every_field_the_v3_model_sees_is_described():
    missing = []
    for s in visible_schemas('v3'):
        schema = s['function']['parameters']
        for owner, node in [('', schema), *schema.get('$defs', {}).items()]:
            for field, spec in node.get('properties', {}).items():
                if 'description' not in spec and field != 'page':  # paging needs no explanation
                    missing.append(f"{s['function']['name']}: {owner + '.' if owner else ''}{field}")
    assert missing == []


def test_v2_sees_exactly_the_definitions_of_the_live_baseline():
    # Exported from the commit the 2026-10-21 v2 live runs used (HEAD 0078210).
    frozen = json.loads((H.REPO / 'tests/fixtures/v2_tool_schemas_20261021.json').read_text(encoding='utf-8'))
    # Registration order depends on which test modules imported tools first; compare by name.
    by_name = lambda rows: {r['function']['name']: r for r in rows}
    assert by_name(json.loads(json.dumps(visible_schemas('v2'), sort_keys=True))) == by_name(frozen)


def test_v3_task_id_and_recursive_are_explained():
    plan = next(s for s in visible_schemas('v3') if s['function']['name'] == 'prepare_file_plan')['function']['parameters']
    assert '本层' in plan['$defs']['Selection']['properties']['recursive']['description']
    assert 'task_id' in plan['properties']


# ============================================================================ round 06 follow-ups
# Live: policy_live_20261021_06_v3_all/holdout_search_dedup_move. The model
# recycled the duplicate inside a file plan; no dedup receipt existed, so the
# dedup part ended partial although every file was right.


def test_plan_recycling_files_in_a_dedup_task_is_flagged_before_execution(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('清理', [
        step(start([dict(id='dedup', kind='dedup', description='字节副本保留最短名'),
                    dict(id='organize', kind='organize', description='回收空文件夹')]),
             call('prepare_file_plan', dict(description='回收副本和空文件夹', actions=[
                 dict(effect='trash', source='Inbox/copy_of_a.txt'), dict(effect='trash', source='Inbox/empty')])))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert 'deduplicate' in text and '不会产生回执' in text and 'Inbox/copy_of_a.txt' in text
    assert 'revise_task' in text and 'edit_task' not in text  # v3 names its own tools
    assert (tmp_path / 'Inbox/copy_of_a.txt').is_file()


def test_only_dedup_part_rejects_plan_trash_naming_v3_tools(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('清理', [
        step(start([dict(id='dedup', kind='dedup', description='字节副本保留最短名')]),
             call('prepare_file_plan', dict(description='回收副本', actions=[dict(effect='trash', source='Inbox/copy_of_a.txt')])))])
    result = client.calls('prepare_file_plan')[0]
    assert result['status'] == 'error' and 'deduplicate' in result['text']
    assert 'revise_task' in result['text'] and 'edit_task' not in result['text']


def test_plan_recycling_an_empty_folder_is_not_flagged(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('清理', [
        step(start([dict(kind='dedup', description='去重'), dict(kind='organize', description='回收空文件夹')]),
             call('prepare_file_plan', dict(description='回收空文件夹', actions=[dict(effect='trash', source='Inbox/empty')])))])
    assert '不会产生回执' not in client.calls('prepare_file_plan')[0]['text']


def test_notes_description_states_its_cost():
    notes = StartTask.model_json_schema()['$defs']['Limits']['properties']['notes']['description']
    assert '通常留空' in notes and '人工复核' in notes and '不要复述' in notes


# ============================================================================ round 07 follow-ups
# Live: policy_live_20261021_07_v3_all. (a) cross_turn_correction turn 2: the
# revision's part had no id, so it was added beside the old part, whose plan
# (b.txt into Archive) then failed after b.txt moved back. (b)
# holdout_search_dedup_move: notes were read before start_task, and that
# evidence was not kept, so the find part ended partial.


def test_revision_part_without_id_is_rejected_and_names_the_parts(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移动', [step(start([dict(kind='organize', description='a、b 移到 Archive')])), finish()])
    assert 'organize_1（organize）' in client.calls('start_task')[0]['text']
    _, client = session.run('更正', [step(call('revise_task', dict(task_id=TASK, expected_revision=1, reason='更正',
                                                                  parts=[dict(kind='organize', description='b 移回')]))), finish()])
    text = client.calls('revise_task')[0]['text']
    assert '每个部分都要写 id' in text and 'organize_1（organize）' in text
    assert session.sb.runtime.store.get(session.ids[TASK])['revision'] == 1 and not session.approvals


def read_calls(names):
    return [dict(tool='read_file', arguments=json.dumps({'path': f'Notes/{n}'}), status='ok', result_head='',
                 result_ids=[f'Faaaaaaaaa{i:02d}', f'Eaaaaaaaaa{i:02d}']) for i, n in enumerate(names)]


def finding(i):
    return dict(file_id=f'Faaaaaaaaa{i:02d}', evidence_ids=[f'Eaaaaaaaaa{i:02d}'], relation='topic', reason='光合作用')


def test_evidence_read_before_start_task_binds_to_the_find_part(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    session.run('找笔记', [step(*read_calls(['note_01.txt', 'note_02.txt'])),
                         step(start([dict(id='find', kind='find', description='光合作用笔记'),
                                     dict(id='organize', kind='organize', description='移动')])),
                         step(call('report_findings', dict(findings=[finding(0), finding(1)]))), finish()])
    checks = {c['goal_id']: c for c in session.sb.runtime.current['checks']}
    assert checks['find']['passed'] and checks['find']['needs_review']


def test_pre_task_evidence_is_not_guessed_between_two_find_parts(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    session.run('找笔记', [step(*read_calls(['note_01.txt'])),
                         step(start([dict(id='a', kind='find', description='光合作用'),
                                     dict(id='b', kind='find', description='税务')])), finish()])
    assert not any(c['passed'] for c in session.sb.runtime.current['checks'])


# ============================================================================ round 09 follow-up
# Live (blind): policy_live_20261021_09_v3_blind/blind_flatten_with_name_clash.
# The model left the clashing report.pdf files alone and named them in its
# finish summary, but a completed write task's main answer showed only the
# local counts, so the user's question ("告诉我是哪些") went unanswered.


def test_reply_answers_the_users_question_in_the_main_answer(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    reply = '重名未动：Inbox/a.txt 与 Archive/a.txt。'
    result, client = session.run('移动并告诉我哪些没动', [
        step(start([dict(kind='organize', description='移动 b.txt')]),
             call('prepare_file_plan', dict(description='m', actions=[
                 dict(effect='move', source='Inbox/b.txt', destination='Archive')]), alias=PLAN)),
        execute(), step(call('finish_task', dict(summary='过程说明', reply=reply)))])
    assert errors(client) == [] and result.stop_reason == 'done'
    assert result.final.startswith('已移动 1 个文件') and reply in result.final
    assert '过程说明' not in result.final  # the summary stays in details


def test_without_reply_the_main_answer_stays_compact(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('移动', [
        step(start([dict(kind='organize', description='移动 b.txt')]),
             call('prepare_file_plan', dict(description='m', actions=[
                 dict(effect='move', source='Inbox/b.txt', destination='Archive')]), alias=PLAN)),
        execute(), step(call('finish_task', dict(summary='很长的过程复述')))])
    assert '很长的过程复述' not in result.final and len(result.final) < 60


# ============================================================================ user run 2026-10-21 22:40
# logs/20261021-224026-606418: "把文献文件夹里的所有内容平铺到根目录并删除空文件夹".
# (1) A plan that moved files out and recycled their folders was rejected with
# a generic message (twice). (2) 文献 held only a hidden .DS_Store, so it was
# "not empty" though listings showed it empty; the model retried paths and then
# guessed a wrong cause. (3) 文献 stayed, yet the answer said "本地核验通过".

from agent.verifier import verify_task


# A Finder .DS_Store begins with this header; names alone are not trusted.
DS_STORE = bytes.fromhex('0000000142756431') + b'\x00' * 64


def flatten_fixture(root):
    for rel in ('文献/01/x.pdf', '文献/02/y.pdf'):
        (root / rel).parent.mkdir(parents=True, exist_ok=True); (root / rel).write_text(rel)
    (root / '文献/.DS_Store').write_bytes(DS_STORE)


PARTS = [dict(kind='organize', description='文献内所有文件平铺到根目录'),
         dict(kind='organize', description='回收清空后的子文件夹和文献文件夹本身')]
MOVE_ALL = dict(path='文献', recursive=True, effect='move', destination='.')


def test_flatten_and_recycle_folder_with_metadata_in_one_plan(tmp_path):
    # Risk tier M: metadata goes with the folder only when the plan says so, and
    # the card lists it. Moving out and recycling now fit one plan; the nested
    # folders are merged into the folder recycled whole.
    flatten_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('平铺并删除空文件夹', [
        step(start(PARTS), call('prepare_file_plan', dict(description='平铺并回收', selection=MOVE_ALL, actions=[
            dict(effect='trash', source='文献/01'), dict(effect='trash', source='文献', include_metadata=True)]), alias=PLAN)),
        execute(), finish()])
    assert errors(client) == [], errors(client)
    assert (tmp_path / 'x.pdf').read_text() == '文献/01/x.pdf' and (tmp_path / 'y.pdf').is_file()
    assert not (tmp_path / '文献').exists()
    assert any(p.name == '.DS_Store' for p in (tmp_path / '.trash').rglob('*'))
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    trashed = [s['source'] for s in request['args']['steps'] if s['effect'] == 'trash']
    assert trashed == ['文献'] and request['args']['steps'][-1]['source'] == '文献'  # merged, and last
    assert request['args']['hidden_contents'] == [dict(folder='文献', metadata=['.DS_Store'], hidden=[], sensitive={}, notes={})]
    assert result.stop_reason == 'done' and client.requests == 3
    from safety.oplog import undo
    undo(session.sb, 'v3_1')
    assert (tmp_path / '文献/01/x.pdf').is_file() and (tmp_path / '文献/.DS_Store').read_bytes() == DS_STORE


def test_metadata_needs_the_explicit_flag(tmp_path):
    flatten_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('平铺', [step(start(PARTS), call('prepare_file_plan', dict(
        description='平铺并回收', selection=MOVE_ALL, actions=[dict(effect='trash', source='文献')])))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '系统元数据文件 .DS_Store' in text and 'include_metadata=true' in text
    assert (tmp_path / '文献/01/x.pdf').is_file() and not session.approvals


def test_folder_with_files_the_plan_does_not_move_is_not_recycled(tmp_path):
    flatten_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('回收', [step(start(PARTS), call('prepare_file_plan', dict(description='回收', actions=[
        dict(effect='move', source='文献/01/x.pdf', destination='.'),
        dict(effect='trash', source='文献', include_metadata=True)])))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '还有 1 个可见文件不在本计划的移动中' in text and '02/y.pdf' in text
    assert (tmp_path / '文献/01/x.pdf').is_file()


def secrets_fixture(root):
    (root / '项目').mkdir()
    (root / '项目/a.txt').write_text('a')
    (root / '项目/.env').write_text('TOKEN=secret')


ASK_HIDDEN = dict(question='项目 里还有隐藏文件 .env（可能含密钥），要怎么处理？', reason='scope_beyond_request', subject=['项目'],
                  options=[dict(label='连同 .env 一起回收', consent='include_hidden'), dict(label='保留文件夹，只移动 a.txt'),
                           dict(label='我自己处理')])
TRASH_PROJECT = dict(description='移出并回收', actions=[dict(effect='move', source='项目/a.txt', destination='.'),
                                                   dict(effect='trash', source='项目', include_hidden=['项目/.env'])])


def test_other_hidden_files_require_asking_first(tmp_path):
    # Risk tier H: without the user's recorded consent the plan is refused.
    secrets_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移出并删除项目文件夹', [step(start([dict(kind='organize', description='移出并回收')]),
                                                   call('prepare_file_plan', TRASH_PROJECT))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '非系统隐藏文件 .env' in text and 'ask_user' in text and 'consent=include_hidden' in text
    assert (tmp_path / '项目/.env').is_file() and not session.approvals


def test_hidden_files_recycled_after_the_user_agrees(tmp_path):
    secrets_fixture(tmp_path)
    session = Session(tmp_path)
    first, _ = session.run('移出并删除项目文件夹', [step(start([dict(kind='organize', description='移出并回收')]),
                                                  call('ask_user', ASK_HIDDEN))])
    assert first.stop_reason == 'waiting_input'
    result, client = session.run('A', [step(start([dict(kind='organize', description='移出并回收')]),
                                            call('prepare_file_plan', TRASH_PROJECT, alias=PLAN)), execute(), finish()])
    assert errors(client) == [], errors(client)
    assert (tmp_path / 'a.txt').is_file() and not (tmp_path / '项目').exists()
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    assert request['args']['hidden_contents'] == [dict(folder='项目', metadata=[], hidden=['.env'],
                                                       sensitive={'.env': dict(name_rule='环境变量文件', findings=[])}, notes={})]
    assert result.stop_reason == 'done'


def test_hidden_files_card_warns_before_execution(tmp_path):
    secrets_fixture(tmp_path)
    from web_confirm import build_confirmation
    review = dict(plan_id='X1', digest='d', steps=[dict(effect='trash', source='项目', destination='.trash/x')],
                  hidden_contents=[dict(folder='项目', metadata=[], hidden=['.env'])])
    notes = build_confirmation(dict(tool='file_plan', args=review), H.Sandbox(tmp_path))['notes']
    assert any(n.startswith('⚠ 项目 含非系统隐藏文件：.env（12B）') and '密钥' in n for n in notes)


def test_declining_or_mismatched_consent_keeps_hidden_files(tmp_path):
    secrets_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('移出并删除项目文件夹', [step(call('ask_user', ASK_HIDDEN))])
    _, client = session.run('B', [step(start([dict(kind='organize', description='移出')]), call('prepare_file_plan', TRASH_PROJECT))])
    assert 'ask_user' in client.calls('prepare_file_plan')[0]['text']  # B carried no consent
    session.run('再问一次', [step(call('ask_user', ASK_HIDDEN))])
    wrong = json.loads(json.dumps(TRASH_PROJECT)); wrong['actions'][1]['include_hidden'] = []
    _, client = session.run('A', [step(start([dict(kind='organize', description='移出')]), call('prepare_file_plan', wrong))])
    assert '逐个列出 项目 中的全部隐藏文件' in client.calls('prepare_file_plan')[0]['text']
    assert (tmp_path / '项目/.env').is_file()


def test_special_objects_are_never_recycled(tmp_path):
    # Risk tier D: a hidden folder (here a Git repository) stops the recycle.
    (tmp_path / '代码/.git').mkdir(parents=True)
    (tmp_path / '代码/.git/config').write_text('[core]')
    session = Session(tmp_path)
    _, client = session.run('删除代码文件夹', [step(start([dict(kind='organize', description='回收')]), call(
        'prepare_file_plan', dict(description='回收', actions=[dict(effect='trash', source='代码', include_metadata=True)])))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '特殊对象 .git' in text and '不会回收' in text
    assert (tmp_path / '代码/.git/config').is_file()


def tree_runtime(tmp_path):
    from agent.artifacts import ArtifactStore
    from safety.sandbox import Sandbox
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    r = TaskRuntime('平铺', dict(protocol='v3'), sb, 'one'); sb.runtime = r
    r.dispatch('start_task', json.dumps(dict(summary='s', parts=PARTS)), sb, None, None)
    return r, sb


def test_finder_metadata_appearing_before_execution_is_tolerated(tmp_path):
    from agent.file_plans import PrepareFilePlan, prepare, execute
    flatten_fixture(tmp_path)
    r, sb = tree_runtime(tmp_path)
    plan = prepare(r, PrepareFilePlan(description='m', selection=MOVE_ALL,
                                      actions=[dict(effect='trash', source='文献', include_metadata=True)]))
    (tmp_path / '文献/01/.DS_Store').write_bytes(DS_STORE + b'later')
    execute(r, plan['id'], OpLog(sb, 'one'), Confirmer('never'))
    assert not (tmp_path / '文献').exists() and (tmp_path / 'x.pdf').is_file()


def test_new_hidden_file_before_execution_stops_before_any_write(tmp_path):
    from agent.file_plans import PrepareFilePlan, prepare, execute
    flatten_fixture(tmp_path)
    r, sb = tree_runtime(tmp_path)
    plan = prepare(r, PrepareFilePlan(description='m', selection=MOVE_ALL,
                                      actions=[dict(effect='trash', source='文献', include_metadata=True)]))
    (tmp_path / '文献/.env').write_text('appeared after the plan was confirmed')
    with pytest.raises(ValueError, match='文献'):
        execute(r, plan['id'], OpLog(sb, 'one'), Confirmer('never'))
    assert (tmp_path / '文献/01/x.pdf').is_file() and (tmp_path / '文献/.env').is_file()


def test_hidden_items_are_announced_by_list_dir(tmp_path):
    flatten_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('看看', [step(call('list_dir', {'path': '文献'}))], final='好')
    assert '另有 1 个隐藏项未显示（.DS_Store）' in client.calls('list_dir')[0]['text']


def test_unfinished_must_name_an_existing_part(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('移动', [step(start([dict(kind='organize', description='m')])),
                                     step(call('finish_task', dict(unfinished=[dict(part_id='nope', reason='x')]))), finish()])
    text = client.calls('finish_task')[0]['text']
    assert '不存在的部分：nope' in text and 'organize_1（organize）' in text


def test_unfinished_cannot_turn_a_failed_part_into_a_passed_one(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('移动', [step(start([dict(kind='organize', description='m')])),
                                    step(call('finish_task', dict(unfinished=[])))])
    assert result.runtime['task_status'] == 'partial'  # no plan executed: still not done


# ============================================================================ ask_user (choice questions)


ASK = dict(question='有两个 report.txt，要移动哪一个？', reason='multiple_candidates',
           options=[dict(label='Projects/alpha/report.txt'), dict(label='Projects/beta/report.txt', detail='beta 项目的报告')])


def guidance_of(result):
    return '\n'.join(m['content'] for m in result.messages if m.get('role') == 'system')


def test_ask_user_ends_the_run_with_lettered_choices_and_no_writes(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    result, client = session.run('把 report.txt 移到 Archive', [
        step(start([dict(kind='organize', description='移动 report.txt')]), call('ask_user', ASK)),
        step(call('prepare_file_plan', dict(description='不应执行', actions=[
            dict(effect='move', source='Projects/alpha/report.txt', destination='Archive')])))])
    assert result.stop_reason == 'waiting_input' and client.requests == 1  # nothing after the question
    assert 'A. Projects/alpha/report.txt' in result.final and 'B. Projects/beta/report.txt——beta 项目的报告' in result.final
    assert [c['key'] for c in result.report['choices']] == ['A', 'B'] and result.report['allow_other']
    assert not any((tmp_path / 'Archive').iterdir())


def test_a_letter_reply_is_resolved_to_the_option_for_the_model(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    session.run('把 report.txt 移到 Archive', [step(call('ask_user', ASK))])
    result, _ = session.run('b', [step(call('list_dir', {'path': 'Projects/beta'}))], final='好的')
    assert '选择 B. Projects/beta/report.txt（beta 项目的报告）' in guidance_of(result)


def test_a_free_text_reply_is_marked_as_the_answer(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    session.run('把 report.txt 移到 Archive', [step(call('ask_user', ASK))])
    result, _ = session.run('两个都移，alpha 的改名为 alpha_report.txt', [], final='好的')
    text = guidance_of(result)
    assert '作出了补充回答' in text and '选择 ' not in text


def test_ask_user_needs_at_least_two_options_and_a_reason(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    _, client = session.run('x', [step(call('ask_user', dict(question='哪个？', reason='multiple_candidates',
                                                             options=[dict(label='alpha')]))),
                                  step(call('ask_user', dict(question='哪个？', options=[dict(label='a'), dict(label='b')])))],
                            final='')
    assert all(c['status'] == 'error' for c in client.calls('ask_user'))


def test_ask_user_is_not_shown_to_v2():
    assert 'ask_user' not in {s['function']['name'] for s in visible_schemas('v2')}
    assert 'ask_user' in {s['function']['name'] for s in visible_schemas('v3')}
