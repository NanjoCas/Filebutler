"""Task protocol, composite goals and failure handling; no paid model calls."""
import json

import pytest
from rich.console import Console

from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.task import TaskStore, TaskSubmission
from agent.task_runtime import TaskRuntime
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tests.test_retrieval import pdf
from tests.test_runtime import Client
from tests.test_week1 import FakeClient
from tools import registry


CFG = dict(local_runtime=True, task_runtime=True, prompt='v8_tasks', max_steps=8)


def spec(*goals, **kwargs):
    return dict(goals=[dict(id=f'g{i}', description=description, conditions=conditions)
                       for i, (description, conditions) in enumerate(goals)], **kwargs)


def execute(tmp_path, instruction, script, *, confirmer=None, **cfg):
    sb = Sandbox(tmp_path)
    client = script if isinstance(script, Client) else FakeClient(script)
    result = run_agent(instruction, LLM({'model': 'fake'}, client=client), sb, {**CFG, **cfg},
                       Console(quiet=True), confirmer=confirmer,
                       oplog=OpLog(sb, 'v2-test'))
    return result, sb, client


@pytest.mark.parametrize('instruction', [
    '统计 PDF 后列出类型，不要整理',
    'Please count documents; keep all originals.',
    '帮忙盘点这些资料，原件保持原样',
    'REAL.FND 只是文件名的一部分，请统计目录',
])
def test_language_does_not_select_capabilities_or_add_model_round(tmp_path, instruction):
    (tmp_path/'a.pdf').write_bytes(b'count only')
    task = spec(('统计文件', [dict(kind='tool_success', tool='inventory_files', arguments={'ext': 'pdf'})]))
    result, sb, client = execute(tmp_path, instruction, [
        [('set_task', task), ('inventory_files', {'ext': 'pdf'})], [('finish_task', {})]])
    assert len(client.seen) == 2 and result.runtime['route'] == 'task_controller'
    assert result.stop_reason == 'needs_review'  # Tool success alone is not semantic proof.
    assert result.runtime['checks'][0]['passed']
    assert '共 1 个PDF 文件' in result.final
    offered = {s['function']['name'] for s in sb.runtime.schemas(registry.schemas())}
    assert {'apply_classification', 'deduplicate', 'report_findings', 'set_task', 'finish_task'} <= offered
    outcome = json.loads(next(m['content'] for m in client.seen[1]
                             if m['role'] == 'tool' and '共 1' in m['content']))
    assert outcome['status'] == 'ok' and outcome['error_code'] is None


def test_local_path_verification_and_risky_mode_require_actual_confirmation(tmp_path):
    requests = []
    task = spec(('建立资料目录', [dict(kind='path_exists', path='资料')]),
                write_roots=['.'], allowed_effects=['create'])
    result, sb, _ = execute(tmp_path, '建立资料目录', [
        [('set_task', task), ('make_dir', {'path': '资料'})], [('finish_task', {})]],
        confirmer=Confirmer('risky', ask=lambda prompt: requests.append(prompt) or 'y'))
    assert len(requests) == 1 and (tmp_path/'资料').is_dir()
    assert result.stop_reason == 'done' and result.runtime['task_status'] == 'completed'
    restored = TaskStore(sb.runtime.store.path, sb.root, 'cli').get(result.runtime['task_id'])
    assert restored['status'] == 'completed' and restored['source_messages'][0]['content'] == '建立资料目录'
    undo(sb, 'v2-test')
    assert not (tmp_path/'资料').exists()


def test_plain_final_cannot_hide_unfinished_subgoal(tmp_path):
    task = spec(('目录甲', [dict(kind='path_exists', path='甲')]),
                ('目录乙', [dict(kind='path_exists', path='乙')]),
                write_roots=['.'], allowed_effects=['create'])
    result, sb, _ = execute(tmp_path, '建立甲和乙', [
        [('set_task', task), ('begin_step', dict(id='create-a', goal_ids=['g0'], description='建立目录甲')), ('make_dir', {'path': '甲'})], '已经全部完成'],
        confirmer=Confirmer('never'))
    assert result.stop_reason == 'partial' and '尚未全部完成' in result.final
    assert [c['passed'] for c in result.runtime['checks']] == [True, False]
    assert result.messages[-1]['content'] == result.final
    assert sb.runtime.store.get(result.runtime['task_id'])['model_summary'] == '已经全部完成'


@pytest.mark.parametrize('variant', ['no_task', 'forbidden', 'scope', 'missing_confirmer', 'questions'])
def test_write_cannot_be_authorized_by_model_flag_alone(tmp_path, variant):
    task = spec(('建目录', [dict(kind='path_exists', path='output')]),
                write_roots=['.'], allowed_effects=['create'])
    if variant == 'forbidden':
        task['constraints'] = [dict(id='keep', description='不能新建目录', forbidden_effects=['create'])]
    if variant == 'scope': task['write_roots'] = ['allowed']
    if variant == 'questions': task['unresolved_questions'] = ['目标位置尚不明确']
    calls = [] if variant == 'no_task' else [('set_task', task)]
    calls.append(('make_dir', {'path': 'output'}))
    result, _, _ = execute(tmp_path, '测试范围与约束', [calls, '完成'],
                           confirmer=None if variant == 'missing_confirmer' else Confirmer('never'))
    assert not (tmp_path/'output').exists()
    assert result.stop_reason != 'done'
    errors = [json.loads(m['content']) for m in result.messages if m['role'] == 'tool'
              and json.loads(m['content'])['status'] == 'error']
    assert errors and errors[-1]['error_code'] == 'CONTROL_REJECTED'


def test_read_scope_checks_defaults_and_snapshot_handles(tmp_path):
    (tmp_path/'inside').mkdir()
    (tmp_path/'outside.txt').write_text('private')
    task = spec(('仅查看 inside', [dict(kind='tool_success', tool='list_dir')]), read_roots=['inside'])
    result, _, _ = execute(tmp_path, '只查看 inside', [
        [('set_task', task), ('list_dir', {}), ('read_file', {'path': 'outside.txt'})], [('finish_task', {})]])
    assert not result.runtime['checks'][0]['passed']
    assert all(json.loads(m['content'])['status'] == 'error' for m in result.messages
               if m['role'] == 'tool' and m['tool_call_id'].endswith(('_1', '_2')))


def test_versioned_patch_preserves_goals_constraints_and_original_user_sources(tmp_path):
    store = TaskStore(tmp_path/'tasks.sqlite3', tmp_path, 'conversation-a')
    first = TaskSubmission.model_validate(spec(('保留资料', [dict(kind='manual_review')]),
        read_roots=['资料'], write_roots=['资料'], allowed_effects=['move'],
        constraints=[dict(id='keep', description='不可回收', forbidden_effects=['trash'])]))
    a = store.submit(first, '保留全部资料', 'run-1')
    patch = TaskSubmission.model_validate(spec(('补充统计', [dict(kind='tool_success', tool='inventory_files')]),
        task_id=a['id'], expected_revision=1,
        constraints=[dict(id='keep', description='模型试图放宽', forbidden_effects=[])]))
    # Distinct goal ID represents addition; same ID explicitly updates a goal.
    patch.goals[0].id = 'additional'
    with pytest.raises(ValueError, match='修改已有约束'):
        store.submit(patch, '不能静默放宽', 'attempt')
    patch.constraints = []
    b = store.submit(patch, '再统计数量', 'run-2')
    assert b['revision'] == 2 and len(b['goals']) == 2
    assert b['constraints'][0]['forbidden_effects'] == ['trash']
    assert b['read_roots'] == b['write_roots'] == ['资料'] and b['allowed_effects'] == ['move']
    assert [s['content'] for s in b['source_messages']] == ['保留全部资料', '再统计数量']
    with pytest.raises(ValueError, match='版本冲突'): store.submit(patch, '过期更新', 'run-3')
    with pytest.raises(ValueError, match='不属于'):
        TaskStore(store.path, tmp_path, 'conversation-b').get(a['id'])
    with pytest.raises(ValueError, match='不属于'):
        TaskStore(store.path, tmp_path/'other-root', 'conversation-a').get(a['id'])
    with pytest.raises(ValueError, match='版本已被'):
        store.checkpoint(a)


def test_classification_and_cleanup_do_not_end_after_saving_plan(tmp_path):
    pdf(tmp_path/'a.pdf', ['Abstract We propose a CNN method for detection. '*20])
    (tmp_path/'old-empty').mkdir()
    task = spec(('方法分类并执行', [dict(kind='plan_applied')]),
                ('清理旧空目录', [dict(kind='path_absent', path='old-empty')]),
                write_roots=['.'], allowed_effects=['create', 'move', 'trash'])
    task['goals'][0]['semantic_review'] = True
    sb_holder = {}
    def model(kw, n):
        if n == 1:
            return [('set_task', task), ('begin_step',dict(id='classify',goal_ids=['g0'],description='分类并执行')), ('paper_profiles', {})]
        sb = sb_holder['sb']
        if n == 2:
            cid = next(iter(sb.artifacts.data['catalogs']))
            return [('submit_classification', dict(catalog_id=cid, categories=['CNN'],
                    assignments=[dict(file_id=f, category=1) for f in sb.artifacts.data['catalogs'][cid]['members']]))]
        assert n == 3
        pid = next(iter(sb.artifacts.data['classification_plans']))
        return [('apply_classification', {'plan_id': pid}), ('begin_step',dict(id='cleanup',goal_ids=['g1'],description='清理旧空目录')), ('trash', {'path': 'old-empty'}), ('finish_task', {})]
    sb = Sandbox(tmp_path)
    sb_holder['sb'] = sb
    client = Client(model)
    result = run_agent('按核心方法整理文献并清理空目录', LLM({'model': 'fake'}, client=client), sb,
                       CFG, Console(quiet=True), confirmer=Confirmer('never'), oplog=OpLog(sb, 'compound'))
    assert client.calls == 3 and result.stop_reason == 'needs_review'
    assert all(c['passed'] for c in result.runtime['checks'])
    assert (tmp_path/'CNN/a.pdf').is_file() and not (tmp_path/'old-empty').exists()
    details = '\n'.join(s['markdown'] for s in result.report['sections'])
    assert '分类计划执行完成' in details and '分类计划' in details
    assert '已移动 1 个文件' in result.final and len(result.final)<160
    assert '尚未移动文件' not in result.final
    undo(sb, 'compound')
    assert (tmp_path/'a.pdf').is_file() and (tmp_path/'old-empty').is_dir()


def test_finish_blocks_remaining_queued_writes(tmp_path):
    task = spec(('已存在根目录', [dict(kind='path_exists', path='.')]),
                write_roots=['.'], allowed_effects=['create'])
    result, _, _ = execute(tmp_path, '核实目录', [[('set_task', task), ('finish_task', {}),
                            ('make_dir', {'path': 'unexpected'})]], confirmer=Confirmer('never'))
    assert result.stop_reason == 'done' and not (tmp_path/'unexpected').exists()


def test_budget_checked_before_first_request_and_interruption_checkpoint(tmp_path):
    result, _, client = execute(tmp_path, '测试', [], token_budget=-1)
    assert result.stop_reason == 'token_budget' and not client.seen
    task = spec(('尚未执行', [dict(kind='path_exists', path='missing')]))
    result, sb, _ = execute(tmp_path, '稍后继续', [[('set_task', task)]], max_steps=1)
    assert result.stop_reason == 'max_steps'
    assert sb.runtime.store.get(result.runtime['task_id'])['status'] == 'interrupted'


def test_task_snapshot_is_not_repeated_in_model_history(tmp_path):
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    runtime = TaskRuntime('测试', CFG, sb, 'test')
    old = {'role': 'system', 'content': runtime.guidance()}
    history = [{'role': 'user', 'content': '保留原件'}, old, {'role': 'user', 'content': '继续'}]
    projected, _ = prepare_history(history, CFG, sb.artifacts)
    assert old not in projected and old in history
    assert projected[0]['content'] == '保留原件'


def test_contracts_distinguish_effects_from_result_verification(tmp_path):
    contract = registry.TOOLS['apply_classification'].contract()
    assert contract['effects'] == ['create', 'move']
    assert contract['verification'] == 'execution_receipt'
    sb = Sandbox(tmp_path)
    outcome = registry.dispatch_result('missing_tool', '{}', sb)
    assert outcome.status == 'error' and outcome.error_code == 'TOOL_ERROR' and not outcome.retryable


def test_receipt_is_revalidated_after_external_change(tmp_path):
    for name in ('a.txt', 'b.txt'):
        (tmp_path/name).write_text('same')
    task = spec(('清理相同副本', [dict(kind='receipt_verified')]),
                write_roots=['.'], allowed_effects=['trash'])
    def model(kw, n):
        if n == 1:
            return [('set_task', task), ('deduplicate', {'mode': 'bytes'})]
        (tmp_path/'a.txt').write_text('externally changed keeper')
        return [('finish_task', {})]
    result, _, _ = execute(tmp_path, '清理字节相同副本', Client(model), confirmer=Confirmer('never'))
    assert result.stop_reason == 'partial'
    assert not result.runtime['checks'][0]['passed']


def test_old_catalog_cannot_bypass_new_task_read_scope(tmp_path):
    (tmp_path/'inside').mkdir(); (tmp_path/'outside').mkdir()
    pdf(tmp_path/'outside'/'a.pdf', ['Abstract CNN method '*30])
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    assert registry.dispatch('paper_profiles', json.dumps({'path': 'outside'}), sb).startswith('OK')
    cid = next(iter(sb.artifacts.data['catalogs']))
    runtime = TaskRuntime('只处理 inside', CFG, sb, 'test')
    task = spec(('只处理 inside', [dict(kind='manual_review')]), read_roots=['inside'])
    assert runtime.dispatch('set_task', json.dumps(task), sb, None, None).startswith('OK')
    assert runtime.prepare('paper_profiles', json.dumps({'catalog_id': cid}), sb)[1]
    assert runtime.prepare('submit_classification', json.dumps(dict(catalog_id=cid,
        categories=['CNN'], assignments=[])), sb)[1]
