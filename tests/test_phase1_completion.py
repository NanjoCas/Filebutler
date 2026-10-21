"""Task editing, local constraint compiler and human review boundaries."""
import json

import pytest

from agent.artifacts import ArtifactStore
from agent.task import TaskPatch, TaskStore, TaskSubmission, task_state
from agent.task_runtime import TaskRuntime
from agent.verifier import verify_task
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tests.test_task_runtime_v2 import execute, spec, CFG


def task_runtime(tmp_path, *, constraints=None, goals=None):
    sb=Sandbox(tmp_path); sb.artifacts=ArtifactStore(sb)
    r=TaskRuntime('用户请求', CFG, sb, 'r1'); sb.runtime=r
    data=spec(*(goals or [('检查目录',[dict(kind='path_exists',path='.')])]),
              constraints=constraints or [], write_roots=['.'], allowed_effects=['create','move','rename','trash'])
    assert r.dispatch('set_task',json.dumps(data),sb,None,None).startswith('OK')
    return r,sb


def rule(predicate, *, effects=None, **kwargs):
    return dict(id='c',description='用户约束',rules=[dict(predicate=predicate,
        effects=effects or ['move','rename','trash'],**kwargs)])


def test_edit_preview_is_immutable_versioned_and_preserves_history(tmp_path):
    r,sb=task_runtime(tmp_path,goals=[('目标甲',[dict(kind='path_exists',path='.')]),('目标乙',[dict(kind='manual_review')])],
                      constraints=[dict(id='keep',description='禁止回收',forbidden_effects=['trash'])])
    original=r.current
    edit=r.store.preview_edit(original['id'],TaskPatch(expected_revision=1,remove_goal_ids=['g1'],
        remove_constraint_ids=['keep'],write_roots=['other'],allowed_effects=['move'],reason='用户改变目标'))
    assert r.store.get(original['id'])==original
    assert {x['field'] for x in edit['changes']}=={'goals','constraints','write_roots','allowed_effects'}
    reloaded=TaskStore(r.store.path,sb.root,'cli')
    applied=reloaded.apply_edit(edit['id'],'用户确认修改','r2',actor='user')
    assert applied['revision']==2 and len(applied['goals'])==1 and not applied['constraints']
    assert applied['last_edit']['actor']=='user' and len(applied['source_messages'])==2
    with pytest.raises(ValueError): reloaded.apply_edit(edit['id'],'重复提交','r3',actor='user')
    with pytest.raises(ValueError): TaskStore(r.store.path,sb.root,'other').edit(edit['id'])


def test_stale_and_foreign_task_edits_never_apply(tmp_path):
    r,sb=task_runtime(tmp_path)
    a=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,write_roots=[],reason='收紧范围'))
    b=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,allowed_effects=[],reason='只读'))
    r.store.apply_edit(a['id'],'确认','r2',actor='user')
    with pytest.raises(ValueError,match='版本改变'): r.store.apply_edit(b['id'],'旧确认','r3',actor='user')
    with pytest.raises(ValueError):
        r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=2,remove_goal_ids=['missing'],reason='错误对象'))
    with pytest.raises(ValueError):
        r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=2,remove_goal_ids=['g0'],reason='删光目标'))


@pytest.mark.parametrize('answer',['n','y'])
def test_model_edit_requires_concrete_confirmation(tmp_path,answer):
    r,sb=task_runtime(tmp_path)
    edit=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,write_roots=[],reason='改为只读'))
    asked=[]
    result=r.dispatch('apply_task_edit',json.dumps({'edit_id':edit['id']}),sb,None,
        Confirmer('risky',ask_action=lambda req:asked.append(req) or answer))
    assert len(asked)==1 and asked[0]['tool']=='task_edit' and asked[0]['args']['changes']
    assert r.store.get(r.current['id'])['revision']==(2 if answer=='y' else 1)
    assert result.startswith('OK' if answer=='y' else 'ERROR')


def test_confirmation_race_invalidates_task_edit(tmp_path):
    r,sb=task_runtime(tmp_path)
    a=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,write_roots=[],reason='编辑甲'))
    b=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,allowed_effects=[],reason='编辑乙'))
    def ask(_):
        r.store.apply_edit(b['id'],'另一次用户确认','other',actor='user'); return 'y'
    result=r.dispatch('apply_task_edit',json.dumps({'edit_id':a['id']}),sb,None,Confirmer('always',ask=ask))
    assert result.startswith('ERROR') and r.store.get(a['task_id'])['revision']==2


def test_unreviewed_set_task_cannot_change_completion_or_expand_scope(tmp_path):
    r,sb=task_runtime(tmp_path)
    changed=TaskSubmission.model_validate(spec(('伪装已完成',[dict(kind='manual_review')]),
                task_id=r.current['id'],expected_revision=1))
    with pytest.raises(ValueError,match='修改已有目标'):
        r.store.submit(changed,'修改','r2')


@pytest.mark.parametrize('predicate,source,destination,kwargs',[
    ('extensions','a.pdf','out',dict(values=['txt'])),
    ('outside','protected/a.txt','out',dict(values=['protected'])),
    ('within','a.txt','out',dict(values=['only'])),
    ('name_glob','a.txt','out',dict(values=['*.csv'])),
])
def test_rule_rejects_before_move_and_confirmation(tmp_path,predicate,source,destination,kwargs):
    path=tmp_path/source;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('keep')
    (tmp_path/destination).mkdir(exist_ok=True)
    task=spec(('移动',[dict(kind='manual_review')]),write_roots=['.'],allowed_effects=['move'],
              constraints=[rule(predicate,effects=['move'],**kwargs)])
    asked=[]
    result,_,_=execute(tmp_path,'按约束移动',[[('set_task',task),('move',{'src':source,'dst_dir':destination})],
                                            [('finish_task',{})]],
                        confirmer=Confirmer('always',ask=lambda _:asked.append(1) or 'y'))
    assert path.is_file() and not (tmp_path/destination/path.name).exists() and not asked
    assert result.runtime['blocked_calls']>=1


def test_protected_descendant_blocks_parent_directory_move(tmp_path):
    (tmp_path/'folder/protected').mkdir(parents=True);(tmp_path/'out').mkdir()
    (tmp_path/'folder/protected/a.txt').write_text('keep')
    r,sb=task_runtime(tmp_path,constraints=[rule('outside',effects=['move'],values=['folder/protected'])])
    assert r.prepare('move',json.dumps(dict(src='folder',dst_dir='out')),sb)[1]
    assert (tmp_path/'folder/protected/a.txt').exists()


def test_empty_directory_and_cumulative_count_are_enforced(tmp_path):
    for name in ('one','two','three','full'):(tmp_path/name).mkdir()
    (tmp_path/'full/a.txt').write_text('keep')
    rules=[rule('empty_directory',effects=['trash'])]
    rules[0]['rules'].append(dict(predicate='max_count',effects=['trash'],limit=2))
    task=spec(('清理空目录',[dict(kind='path_absent',path='one'),dict(kind='path_absent',path='two')]),
              write_roots=['.'],allowed_effects=['trash'],constraints=rules)
    result,_,_=execute(tmp_path,'最多清理两个空目录',[[('set_task',task),('trash',{'path':'full'}),
        ('trash',{'path':'one'}),('trash',{'path':'two'}),('trash',{'path':'three'})],[('finish_task',{})]],
        confirmer=Confirmer('never'))
    assert (tmp_path/'full/a.txt').is_file() and (tmp_path/'three').is_dir()
    assert not (tmp_path/'one').exists() and not (tmp_path/'two').exists()
    assert result.runtime['checks'][0]['passed']


def test_extension_and_content_rules_recheck_after_confirmation(tmp_path):
    (tmp_path/'a.txt').write_text('original')
    r,sb=task_runtime(tmp_path,constraints=[rule('same_extension',effects=['rename'])])
    assert r.prepare('rename',json.dumps(dict(path='a.txt',new_name='a.csv')),sb)[1]
    task=spec(('移动',[dict(kind='manual_review')]),write_roots=['.'],allowed_effects=['move'],
              constraints=[rule('content_unchanged',effects=['move'])])
    (tmp_path/'out').mkdir()
    def ask(_): (tmp_path/'a.txt').write_text('changed while waiting'); return 'y'
    result,_,_=execute(tmp_path,'保持内容移动',[[('set_task',task),('move',dict(src='a.txt',dst_dir='out'))],
                                             [('finish_task',{})]],confirmer=Confirmer('always',ask=ask))
    assert (tmp_path/'a.txt').exists() and not (tmp_path/'out/a.txt').exists()
    assert any(o['status']=='error' for o in r.store.recent()[0]['observations'])


def test_batch_count_constraint_prevents_partial_cleanup(tmp_path):
    for name in ('a.txt','b.txt','c.txt'):(tmp_path/name).write_text('same')
    task=spec(('清理重复',[dict(kind='receipt_verified')]),write_roots=['.'],allowed_effects=['trash'],
              constraints=[rule('max_count',effects=['trash'],limit=1)])
    result,_,_=execute(tmp_path,'最多处理一个副本',[[('set_task',task),('deduplicate',dict(mode='bytes'))],
                                                 [('finish_task',{})]],confirmer=Confirmer('never'))
    assert len(list(tmp_path.glob('*.txt')))==3 and result.stop_reason=='partial'


def test_creation_checks_all_missing_ancestors_against_scope(tmp_path):
    task=spec(('创建',[dict(kind='path_exists',path='outside/allowed/new')]),write_roots=['outside/allowed'],
              allowed_effects=['create'])
    result,_,_=execute(tmp_path,'仅写入 allowed',[[('set_task',task),('make_dir',dict(path='outside/allowed/new'))],
                                               [('finish_task',{})]],confirmer=Confirmer('never'))
    assert not (tmp_path/'outside').exists() and result.stop_reason=='partial'


def test_semantic_review_is_user_only_and_cannot_override_failed_local_check(tmp_path):
    r,sb=task_runtime(tmp_path,goals=[('语义判断',[dict(kind='manual_review')])])
    r.current['checks']=verify_task(r.current,sb);r.store.checkpoint(r.current)
    result=r.store.review(r.current['id'],1,['g0'],[],'accept','人工核对通过',sb)
    assert result['status']=='completed' and result['reviews'][-1]['actor']=='user'
    rejected=r.store.review(r.current['id'],1,['g0'],[],'reject','需要调整',sb)
    assert rejected['status']=='needs_review'
    assert r.store.review(r.current['id'],1,['g0'],[],'accept','重新核对',sb)['status']=='completed'
    edit=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,
        upsert_goals=[dict(id='g0',description='文件必须存在',conditions=[dict(kind='path_exists',path='missing')])],reason='改变目标'))
    r.store.apply_edit(edit['id'],'确认修改','r2',actor='user')
    with pytest.raises(ValueError,match='版本冲突'):r.store.review(r.current['id'],1,['g0'],[],'accept','旧复核',sb)
    with pytest.raises(ValueError,match='未满足'):r.store.review(r.current['id'],2,['g0'],[],'accept','强行通过',sb)


def test_cancel_and_restore_require_versioned_edit(tmp_path):
    r,sb=task_runtime(tmp_path)
    edit=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=1,cancel=True,reason='取消任务'))
    r.current=r.store.apply_edit(edit['id'],'取消','r2',actor='user')
    assert task_state(r.current)=='cancelled'
    assert r.prepare('make_dir',json.dumps(dict(path='out')),sb)[1]
    with pytest.raises(ValueError,match='已取消'):
        r.store.submit(TaskSubmission.model_validate(spec(('检查目录',[dict(kind='path_exists',path='.')]),
            task_id=r.current['id'],expected_revision=2)),'尝试绕过','r3')
    restore=r.store.preview_edit(r.current['id'],TaskPatch(expected_revision=2,cancel=False,reason='恢复任务'))
    assert r.store.apply_edit(restore['id'],'恢复','r4',actor='user')['status']=='running'


def test_unknown_text_constraint_is_shown_on_confirmation_until_user_review(tmp_path):
    # Since P1-4 (user-confirmed): a text-only limit does not block the write; it
    # is listed on every confirmation, and the task stays in review.
    r,sb=task_runtime(tmp_path,constraints=[dict(id='meaning',description='遵循特殊整理规则')])
    assert not r.prepare('make_dir',json.dumps(dict(path='out')),sb)[1]
    assert any('遵循特殊整理规则' in n for n in r.text_limits())
    r.store.review(r.current['id'],1,[],['meaning'],'accept','我已核对这些条件',sb)
    r.current=r.store.get(r.current['id'])
    assert r.text_limits()==[]
    r.store.review(r.current['id'],1,[],['meaning'],'reject','重新检查',sb)
    r.current=r.store.get(r.current['id'])
    assert any('遵循特殊整理规则' in n for n in r.text_limits())


def test_live_runner_marks_connection_failure_as_blocked_not_quality_failure(tmp_path,monkeypatch):
    import sys
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx
    import openai
    import eval.phase1 as evaluation
    class NoNetwork:
        cost=0; total_tokens=0
        def __init__(self,cfg):pass
        def chat(self,*args,**kwargs):raise openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
    monkeypatch.setattr(evaluation,'LLM',NoNetwork)
    out=tmp_path/'blocked'
    monkeypatch.setattr(sys,'argv',['phase1','--live','--split','dev','--out',str(out)])
    assert evaluation.main()==2
    report=json.loads((out/'report.json').read_text())
    assert report['total']==0 and report['attempted']==1 and report['cost']==0
    assert not report['suite_complete'] and report['cases'][0]['status']=='blocked'
