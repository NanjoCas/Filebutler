"""Cross-domain policy contracts; local fixtures, no paid model or user files."""
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.artifacts import ArtifactStore, digest
from agent.policy import TaskPolicy
from agent.task import TaskPatch, TaskSubmission
from agent.task_runtime import TaskRuntime
from agent.verifier import verify_task
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tests.test_retrieval import pdf
from tests.test_task_runtime_v2 import CFG, spec, execute
from tools import registry


def runtime(root, run='first', cfg=None, data=None):
    sb = Sandbox(root)
    sb.artifacts = ArtifactStore(sb, data)
    sb.artifacts.run_id = run
    r = TaskRuntime('用户请求', {**CFG, **(cfg or {})}, sb, run)
    sb.runtime = r
    return r


def call(r, name, args=None):
    raw, blocked = r.prepare(name, json.dumps(args or {}, ensure_ascii=False), r.sb)
    result = blocked or r.dispatch(name, raw, r.sb, OpLog(r.sb, r.run_id), Confirmer('never'))
    r.after(name, result, r.sb)
    return result


def step(r, gid, sid=None):
    assert call(r, 'begin_step', dict(id=sid or gid, goal_ids=[gid], description='为该目标执行步骤')).startswith('OK')


def test_cleanup_and_dedup_rules_do_not_leak_between_goals(tmp_path):
    (tmp_path/'audio').mkdir()
    for name in ('a.wav', 'b.wav'): (tmp_path/'audio'/name).write_bytes(b'same')
    (tmp_path/'audio/empty').mkdir()
    r = runtime(tmp_path)
    task = spec(('清理相同录音', [dict(kind='receipt_verified')]),
                ('清理空文件夹', [dict(kind='empty_directories_removed', path='audio')]),
                write_roots=['audio'], allowed_effects=['trash'], constraints=[dict(
                    id='empty_only', description='空目录步骤只能回收空目录', goal_ids=['g1'],
                    rules=[dict(effects=['trash'], predicate='empty_directory')])])
    assert call(r, 'set_task', task).startswith('OK')
    assert 'begin_step' in call(r, 'trash', {'path':'audio/a.wav'})
    step(r, 'g0')
    assert call(r, 'deduplicate', dict(path='audio', mode='bytes')).startswith('OK')
    step(r, 'g1')
    assert 'ERROR' in call(r, 'trash', {'path':'audio/a.wav'})
    assert call(r, 'trash', {'path':'audio/empty'}).startswith('OK')
    assert all(row['passed'] for row in verify_task(r.current, r.sb))
    assert len(list((tmp_path/'audio').glob('*.wav'))) == 1


def test_step_never_overrides_global_protection(tmp_path):
    (tmp_path/'keep.txt').write_text('keep')
    r = runtime(tmp_path)
    task = spec(('目标甲', [dict(kind='manual_review')]), ('目标乙', [dict(kind='manual_review')]),
                write_roots=['.'], allowed_effects=['trash'], constraints=[dict(
                    id='keep', description='全局禁止回收', forbidden_effects=['trash'])])
    call(r, 'set_task', task)
    step(r, 'g1')
    assert call(r, 'trash', {'path':'keep.txt'}).startswith('ERROR')
    assert (tmp_path/'keep.txt').is_file()
    assert call(r, 'begin_step', dict(id='bad', goal_ids=['unknown'], description='错误步骤')).startswith('ERROR')


def test_references_are_goal_bound_and_plan_ambiguity_fails_closed(tmp_path):
    from agent.verifier import _reference
    task = dict(goals=[dict(id='g0'), dict(id='g1')], observations=[], references=[
        dict(collection='classification_plans', id='P_old', goal_ids=['g0']),
        dict(collection='classification_plans', id='P_new', goal_ids=['g1'])])
    assert _reference({}, task, 'classification_plans', 'g1') == ['P_new']
    task['references'].append(dict(collection='classification_plans', id='P_ambiguous', goal_ids=['g1']))
    with pytest.raises(ValueError, match='多个候选'):
        _reference({}, task, 'classification_plans', 'g1')
    assert _reference({'reference':'P_new'}, task, 'classification_plans', 'g1') == ['P_new']
    task['references'] = [dict(collection='classification_plans', id='legacy')]
    assert _reference({}, task, 'classification_plans', 'g1') == []


def test_task_read_budget_survives_continuation_and_policy_edit(tmp_path):
    (tmp_path/'notes.md').write_text('实验记录')
    r = runtime(tmp_path, cfg={'task_read_limit':1})
    task = spec(('读取记录', [dict(kind='manual_review')]))
    call(r, 'set_task', task)
    assert call(r, 'read_file', {'path':'notes.md'}).startswith('OK')
    previous = r.current
    newer = runtime(tmp_path, 'second', cfg={'task_read_limit':1}, data=r.sb.artifacts.export())
    task.update(task_id=previous['id'], expected_revision=1)
    assert call(newer, 'set_task', task).startswith('OK')
    assert newer.current['usage']['reads'] == 1
    assert '预算' in call(newer, 'read_file', {'path':'notes.md'})
    edit = newer.store.preview_edit(previous['id'], TaskPatch(expected_revision=2,
        policy=TaskPolicy(criteria=['按实验日期判断'], output={'format':'json'}), reason='改变展示策略'))
    changed = newer.store.apply_edit(edit['id'], '确认', 'third', actor='user')
    assert changed['usage']['reads'] == 1
    assert changed['policy']['criteria'] == ['按实验日期判断']


@pytest.mark.parametrize('count_scope,allowed', [('task',False), ('run',True)])
def test_operation_limit_has_explicit_time_scope(tmp_path, count_scope, allowed):
    for name in ('a.txt', 'b.txt'): (tmp_path/name).write_text(name)
    r = runtime(tmp_path)
    task = spec(('清理指定材料', [dict(kind='manual_review')]), write_roots=['.'], allowed_effects=['trash'],
        constraints=[dict(id='limit',description='最多一项',rules=[dict(effects=['trash'], predicate='max_count',
                        limit=1,count_scope=count_scope)])])
    call(r, 'set_task', task)
    assert call(r, 'trash', {'path':'a.txt'}).startswith('OK')
    next_r = runtime(tmp_path, 'second', data=r.sb.artifacts.export())
    task.update(task_id=r.current['id'], expected_revision=1)
    assert call(next_r, 'set_task', task).startswith('OK')
    result = call(next_r, 'trash', {'path':'b.txt'})
    assert result.startswith('OK') == allowed
    assert (tmp_path/'b.txt').exists() != allowed


def test_new_capability_scope_and_typed_status_need_no_controller_branch(tmp_path, monkeypatch):
    class Args(BaseModel):
        resource: str
    registered = dict(registry.TOOLS)
    monkeypatch.setattr(registry, 'TOOLS', registered)
    @registry.tool('inspect_dataset', '测试数据集能力', Args,
                   read_scope=lambda a, sb:[a['resource']], usage='reads')
    def inspect(a, sb):
        return registry.ToolResult(status='error', text='OK 字样不是状态，结果实际失败', error_code='DATASET_INVALID')
    (tmp_path/'inside').mkdir()
    (tmp_path/'outside.csv').write_text('data')
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('读取数据集', [dict(kind='manual_review')]), read_roots=['inside']))
    assert '超出' in call(r, 'inspect_dataset', {'resource':'outside.csv'})
    call(r, 'inspect_dataset', {'resource':'inside'})
    assert r.current['observations'][-1]['status'] == 'error'
    assert r.current['observations'][-1]['error_code'] == 'DATASET_INVALID'
    assert r.current['usage']['reads'] == 1


@pytest.mark.parametrize('layout', ['preserve_tree', 'mapping'])
def test_placement_verifies_hierarchy_or_rename_instead_of_forcing_flatten(tmp_path, layout):
    (tmp_path/'Inbox/sub').mkdir(parents=True)
    (tmp_path/'Inbox/sub/data.csv').write_text('data')
    destination = 'Archive/sub/data.csv' if layout == 'preserve_tree' else 'Archive/renamed.csv'
    c = dict(kind='files_placed', path='Inbox', destination='Archive', placement=layout)
    if layout == 'mapping': c['placements'] = {'Inbox/sub/data.csv':destination}
    r = runtime(tmp_path)
    assert call(r, 'set_task', spec(('归档数据集', [c]), write_roots=['.'],
                                      allowed_effects=['create','move','rename'])).startswith('OK')
    parent = str(Path(destination).parent)
    assert call(r, 'make_dir', {'path':parent}).startswith('OK')
    assert call(r, 'move', {'src':'Inbox/sub/data.csv', 'dst_dir':parent}).startswith('OK')
    if layout == 'mapping':
        assert call(r, 'rename', {'path':'Archive/data.csv', 'new_name':'renamed.csv'}).startswith('OK')
    assert verify_task(r.current, r.sb)[0]['passed']


def classification(r, *, dimension='应用领域', base_dir=None, category_paths=None):
    assert call(r, 'paper_profiles', {'evidence':{'pdf_pages':None}}).startswith('OK')
    cid = next(reversed(r.sb.artifacts.data['catalogs']))
    ids = r.sb.artifacts.data['catalogs'][cid]['members']
    args = dict(catalog_id=cid, dimension=dimension, categories=['医学','统计'], assignments=[
        dict(file_id=fid,category=1,secondary_categories=[2]) for fid in ids])
    if base_dir is not None: args['base_dir'] = base_dir
    if category_paths: args['category_paths'] = category_paths
    assert call(r, 'submit_classification', args).startswith('OK')
    return next(reversed(r.sb.artifacts.data['classification_plans']))


def test_dimension_hierarchy_labels_and_evidence_are_task_data(tmp_path):
    pdf(tmp_path/'a.pdf', ['Abstract short introduction. '*5, 'Methods. '*12, 'Clinical applications. '*15])
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('按应用领域分类并执行', [dict(kind='plan_applied')]),
                            write_roots=['.'], allowed_effects=['create','move']))
    pid = classification(r, category_paths={'1':'研究/医学'})
    plan = r.sb.artifacts.data['classification_plans'][pid]
    assert plan['dimension'] == '应用领域' and plan['base_dir'] == '.' and not plan['numbered']
    assert plan['evidence_policy']['pdf_pages'] is None
    assert call(r, 'apply_classification', {'plan_id':pid}).startswith('OK')
    assert (tmp_path/'研究/医学/a.pdf').is_file() and not (tmp_path/'按方法').exists()
    assert not (tmp_path/'统计/a.pdf').exists()  # Extra label is not a copy command.
    assert verify_task(r.current, r.sb)[0]['passed']


def test_execution_fact_does_not_silently_replace_final_location_contract(tmp_path):
    pdf(tmp_path/'a.pdf', ['Abstract clinical trials. '*20])
    r = runtime(tmp_path)
    task = spec(('执行分类步骤', [dict(kind='plan_applied', evaluation='execution_fact')]),
                ('最终文件位置', [dict(kind='path_exists',path='Final/医学/a.pdf')]),
                write_roots=['.'], allowed_effects=['create','move'])
    call(r, 'set_task', task)
    step(r, 'g0')
    pid = classification(r, base_dir='Staging')
    assert call(r, 'apply_classification', {'plan_id':pid}).startswith('OK')
    step(r, 'g1')
    assert call(r, 'make_dir', {'path':'Final'}).startswith('OK')
    assert call(r, 'move', {'src':'Staging/医学','dst_dir':'Final'}).startswith('OK')
    checks = verify_task(r.current, r.sb)
    assert all(c['passed'] for c in checks)
    r.current['goals'][0]['conditions'][0]['evaluation'] = 'current_state'
    assert not verify_task(r.current, r.sb)[0]['passed']
    (tmp_path/'Final/医学/a.pdf').unlink()
    assert not verify_task(r.current, r.sb)[1]['passed']


def test_full_text_search_rebuilds_index_and_evidence_paginates_beyond_five(tmp_path):
    for n in range(9):
        pdf(tmp_path/f'{n}.pdf', ['Introduction. '*15, 'Background. '*15, 'reinforcement trial. '*20])
    r = runtime(tmp_path)
    assert '候选 0 篇' in call(r, 'search_content', dict(query='reinforcement',top_k=9))
    results = []
    for page in range(1,4):
        result = call(r, 'search_content', dict(query='reinforcement', top_k=9, pdf_pages=None, view='evidence', page=page))
        assert result.startswith('OK') and '已截断' not in result
        results.append(result)
    assert sum(s.count('file:F') for s in results) == 9
    assert 'p=3' in results[-1] and '全部文字层页' in results[-1]


def test_plain_discussion_and_inspection_can_be_composed_without_authorizing_writes(tmp_path):
    result, sb, client = execute(tmp_path, '解释如何归档实验数据，先讨论', ['可以按照日期和实验编号归档。'])
    assert result.stop_reason == 'answered' and not sb.runtime.current and len(client.seen) == 1
    r = runtime(tmp_path)
    r.store.save_outcome(dict(run_id='old', summary='预算用尽', purpose='action'))
    assert '预算用尽' in call(r, 'inspect_run', {'finish':False})
    assert not r.finished and not r.current
    assert call(r, 'make_dir', {'path':'unauthorized'}).startswith('ERROR')
    assert not (tmp_path/'unauthorized').exists()


def test_json_and_full_output_preserve_local_failure_reason(tmp_path):
    task = spec(('目标必须存在', [dict(kind='path_exists',path='missing')]),
                policy={'output':{'format':'json'}})
    r = runtime(tmp_path)
    call(r, 'set_task', task)
    call(r, 'finish_task', {'summary':'模型不能覆盖本地失败'})
    output = json.loads(r.final_answer)
    assert output['state'] == 'partial' and 'missing' in output['summary']
    assert not output['checks'][0]['passed']


@pytest.mark.parametrize('reason', ['两篇论文的内容不一致。', 'These sources describe different settings.'])
def test_semantic_judgments_are_labels_not_language_regex_proofs(tmp_path, reason):
    (tmp_path/'notes.md').write_text('original evidence')
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('解释记录', [dict(kind='manual_review')])))
    fid = r.sb.artifacts.register('notes.md')
    eid = r.sb.artifacts.evidence(fid, 'fragment', excerpt='original evidence', scope='实验记录指定片段')
    assert call(r, 'report_findings', dict(findings=[dict(file_id=fid,evidence_ids=[eid],
                       relation='数据质量', reason=reason)])).startswith('OK')
    assert '模型判断（待复核）' in r.outputs[-1]
    assert '前两页' not in r.outputs[-1]
    assert call(r, 'report_findings', dict(findings=[dict(file_id=fid,evidence_ids=[eid],
                       relation='一致性', reason='Identical bytes', claim_type='byte_identity')])).startswith('ERROR')


def test_prompt_does_not_inherit_legacy_topic_rules():
    from agent.prompts import SYSTEM_PROMPTS
    prompt = SYSTEM_PROMPTS['v8_tasks']
    assert '初次 top_k=5' not in prompt and '仅检索 PDF 前两页' not in prompt
    assert '策略职责边界' in prompt and 'begin_step' in prompt


def test_placement_can_verify_name_swap_without_mistaking_final_files_for_old_sources(tmp_path):
    (tmp_path/'records').mkdir()
    (tmp_path/'records/a.txt').write_text('A')
    (tmp_path/'records/b.txt').write_text('B')
    r = runtime(tmp_path)
    c = dict(kind='files_placed',path='records',placement='mapping',placements={
        'records/a.txt':'records/b.txt','records/b.txt':'records/a.txt'})
    assert call(r,'set_task',spec(('交换记录名称',[c]),write_roots=['records'],allowed_effects=['rename'])).startswith('OK')
    for src,dst in [('a.txt','temporary.txt'),('b.txt','a.txt'),('temporary.txt','b.txt')]:
        assert call(r,'rename',dict(path='records/'+src,new_name=dst)).startswith('OK')
    assert verify_task(r.current,r.sb)[0]['passed']
    (tmp_path/'records/a.txt').write_text('wrong')
    assert not verify_task(r.current,r.sb)[0]['passed']


def test_legacy_manifest_survives_new_default_fields_on_resume(tmp_path):
    (tmp_path/'Inbox').mkdir(); (tmp_path/'Archive').mkdir()
    (tmp_path/'Inbox/a.txt').write_text('A')
    r=runtime(tmp_path)
    task=spec(('归档全部记录',[dict(kind='files_placed',path='Inbox',destination='Archive')]),
              write_roots=['.'],allowed_effects=['move'])
    call(r,'set_task',task)
    original=r.current['completion_snapshots']
    assert call(r,'move',dict(src='Inbox/a.txt',dst_dir='Archive')).startswith('OK')
    for c in r.current['goals'][0]['conditions']:
        for key in ('placement','placements','evaluation'): c.pop(key)
    r.store.checkpoint(r.current)
    task.update(task_id=r.current['id'],expected_revision=1)
    resumed=r.store.submit(TaskSubmission.model_validate(task),'继续','next')
    assert resumed['completion_snapshots']==original
    assert verify_task(resumed,r.sb)[0]['passed']


def test_same_category_directory_alias_is_rejected(tmp_path):
    pdf(tmp_path/'a.pdf',['Abstract clinical data. '*20])
    r=runtime(tmp_path)
    call(r,'paper_profiles')
    cid=next(iter(r.sb.artifacts.data['catalogs']))
    fid=r.sb.artifacts.data['catalogs'][cid]['members'][0]
    assert call(r,'submit_classification',dict(catalog_id=cid,categories=['x/y','x／y'],
        assignments=[dict(file_id=fid,category=1)])).startswith('ERROR')


def test_schema_default_is_not_misreported_as_explicit_null(tmp_path):
    pdf(tmp_path/'a.pdf',['Introduction. '*15,'Background. '*15,'instrument. '*20])
    r=runtime(tmp_path)
    call(r,'set_task',spec(('查仪器记录',[dict(kind='manual_review')]),policy={'evidence':{'pdf_pages':None}}))
    assert '候选 1 篇' in call(r,'search_content',dict(query='instrument'))
    assert 'pdf_pages' not in r.current['observations'][-1]['arguments']
    assert '候选 0 篇' in call(r,'search_content',dict(query='instrument',pdf_pages=2))
    assert r.current['observations'][-1]['arguments']['pdf_pages']==2


def test_structural_evaluation_does_not_call_goal_count_semantic_coverage():
    from eval.phase1 import check_intent
    task=TaskSubmission.model_validate(spec(('无关目标',[dict(kind='manual_review')]))).model_dump()
    checks=check_intent(task,dict(min_goals=1,rule_contracts=[dict(predicate='extensions',values=['csv'])]))
    assert checks['goal_count'] and not checks['rule_contract:0'] and 'goal_coverage' not in checks


def test_typed_date_arguments_remain_serializable_for_file_search(tmp_path):
    (tmp_path/'record.csv').write_text('data')
    r=runtime(tmp_path)
    call(r,'set_task',spec(('按日期筛选记录',[dict(kind='manual_review')])))
    assert call(r,'search_files',dict(modified_after='2000-01-01')).startswith('OK')
    assert r.current['observations'][-1]['arguments']['modified_after']=='2000-01-01'


def test_run_count_does_not_reset_on_revision_inside_same_run(tmp_path):
    for name in ('a.txt','b.txt'): (tmp_path/name).write_text(name)
    r=runtime(tmp_path)
    task=spec(('清理指定记录',[dict(kind='manual_review')]),write_roots=['.'],allowed_effects=['trash'],
        constraints=[dict(id='limit',description='本轮最多一项',rules=[dict(predicate='max_count',effects=['trash'],limit=1,count_scope='run')])])
    call(r,'set_task',task)
    assert call(r,'trash',{'path':'a.txt'}).startswith('OK')
    task.update(task_id=r.current['id'],expected_revision=1)
    assert call(r,'set_task',task).startswith('OK')
    assert call(r,'trash',{'path':'b.txt'}).startswith('ERROR')
    call(r,'finish_task')
    assert '已移入回收区 1 个文件' in r.final_answer
