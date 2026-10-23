"""File-plan contracts against real files, races, undo and persistent state."""
import json
import pytest

from agent.artifacts import ArtifactStore
from agent.file_plans import prepare, execute, PrepareFilePlan
from agent.task_runtime import TaskRuntime
from agent.task import TaskSubmission, OperationRule
from agent.verifier import verify_task
from safety.sandbox import Sandbox
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from tests.helpers import spec
from web_confirm import build_confirmation


def runtime(root, session='cli', run='one'):
    sb=Sandbox(root); sb.artifacts=ArtifactStore(sb)
    r=TaskRuntime('明确整理这些文件',dict(task_session=session),sb,run); sb.runtime=r
    return r


def proposal(**kwargs):
    return PrepareFilePlan(description='移动指定文件',**kwargs)


def test_local_selection_compiles_executes_once_and_undo(tmp_path):
    (tmp_path/'in/sub').mkdir(parents=True)
    for p in ('in/a.txt','in/sub/b.txt','in/sub/protected.pdf'):(tmp_path/p).write_text(p)
    r=runtime(tmp_path)
    plan=prepare(r,proposal(selection=dict(path='in',extensions=['txt'],destination='out/deep')))
    assert not (tmp_path/'out').exists()
    assert r.current['write_roots'] and '.' not in r.current['write_roots']
    asked=[]
    def confirm(req):
        preview=build_confirmation(req,r.sb)
        assert preview['counts']['affected']==4
        assert any(i['previewable'] for g in preview['groups'] for i in g['items'])
        asked.append(req);return 'y'
    log=OpLog(r.sb,'one')
    assert execute(r,plan['id'],log,Confirmer('always',ask_action=confirm)).startswith('OK')
    assert len(asked)==1
    assert all(c['passed'] for c in verify_task(r.current,r.sb))
    assert (tmp_path/'in/sub/protected.pdf').exists()
    assert execute(r,plan['id'],log,Confirmer('always',ask_action=confirm)).startswith('OK')
    assert len(asked)==1 and len(log.path.read_text().splitlines())==4
    undo(r.sb,'one')
    assert (tmp_path/'in/a.txt').exists() and (tmp_path/'in/sub/b.txt').exists()
    assert not (tmp_path/'out').exists()
    assert not verify_task(r.current,r.sb)[0]['passed']


@pytest.mark.parametrize('race',['source','target','revision','symlink'])
def test_confirmation_wait_rechecks_without_writes(tmp_path,race):
    (tmp_path/'a.txt').write_text('original'); (tmp_path/'elsewhere').mkdir()
    r=runtime(tmp_path)
    plan=prepare(r,proposal(actions=[dict(effect='move',source='a.txt',destination='out/a.txt')]))
    def confirm(_):
        if race=='source': (tmp_path/'a.txt').write_text('changed')
        if race=='target': (tmp_path/'out').mkdir();(tmp_path/'out/a.txt').write_text('occupied')
        if race=='symlink': (tmp_path/'out').symlink_to(tmp_path/'elsewhere',target_is_directory=True)
        if race=='revision':
            spec2=TaskSubmission.model_validate(spec(('更多目标',[dict(kind='manual_review')]),task_id=r.current['id'],expected_revision=1))
            # Different goal ID avoids changing the existing goal.
            spec2.goals[0].id='extra'
            r.store.submit(spec2,'补充目标','other')
        return 'y'
    with pytest.raises(ValueError):execute(r,plan['id'],OpLog(r.sb,'one'),Confirmer('always',ask_action=confirm))
    assert (tmp_path/'a.txt').exists()
    assert not (tmp_path/'elsewhere/a.txt').exists()


@pytest.mark.parametrize('bad',[
    dict(effect='move',source='a.txt',destination='taken.txt'),
    dict(effect='move',source='a.txt',destination='a.txt'),
    dict(effect='move',source='a.txt',destination='.hidden/a.txt'),
])
def test_preflight_blocks_conflicts_before_task_or_confirmation(tmp_path,bad):
    (tmp_path/'a.txt').write_text('A');(tmp_path/'taken.txt').write_text('B')
    r=runtime(tmp_path)
    with pytest.raises(ValueError):prepare(r,proposal(actions=[bad]))
    assert not r.store.recent()


def test_foreign_session_stale_plan_and_tampering(tmp_path):
    (tmp_path/'a.txt').write_text('A')
    r=runtime(tmp_path);plan=prepare(r,proposal(actions=[dict(effect='trash',source='a.txt')]))
    with pytest.raises(ValueError):runtime(tmp_path,'other').store.file_plan(plan['id'])
    p=r.store.file_plan(plan['id']);p['payload']['steps'][0]['source']='taken.txt';r.store.save_file_plan(p)
    with pytest.raises(ValueError,match='摘要'):execute(r,plan['id'],OpLog(r.sb,'one'),Confirmer('never'))
    assert (tmp_path/'a.txt').exists()


def test_restart_after_completed_plan_uses_saved_receipt_without_writes(tmp_path):
    (tmp_path/'a.txt').write_text('A')
    r=runtime(tmp_path);p=prepare(r,proposal(actions=[dict(effect='move',source='a.txt',destination='out/a.txt')]))
    execute(r,p['id'],OpLog(r.sb,'one'),Confirmer('never'))
    reloaded=runtime(tmp_path,run='two'); asked=[]
    execute(reloaded,p['id'],OpLog(reloaded.sb,'two'),Confirmer('always',ask=lambda _:asked.append(1) or 'y'))
    assert not asked and not (tmp_path/'.oplog/two.jsonl').exists()
    assert verify_task(reloaded.current,reloaded.sb)[0]['passed']


def test_interrupted_mutation_is_not_guessed_or_replayed(tmp_path):
    (tmp_path/'a.txt').write_text('A')
    r=runtime(tmp_path);p=prepare(r,proposal(actions=[dict(effect='trash',source='a.txt')]))
    p['inflight']=p['payload']['steps'][0]['id'];r.store.save_file_plan(p)
    (tmp_path/'.trash').mkdir();(tmp_path/p['payload']['steps'][0]['destination']).write_text('A')
    with pytest.raises(ValueError,match='禁止自动重放'):
        execute(runtime(tmp_path,run='two'),p['id'],OpLog(r.sb,'two'),Confirmer('never'))
    assert (tmp_path/'a.txt').exists()


@pytest.mark.parametrize('kwargs',[
    dict(effects=['create'],predicate='outside',values=['protected']),
    dict(effects=['trash'],selector='destination',predicate='within',values=['.']),
])
def test_impossible_predicates_rejected_at_schema_boundary(kwargs):
    with pytest.raises(ValueError):OperationRule.model_validate(kwargs)


def test_source_symlink_inside_library_rejected(tmp_path):
    (tmp_path/'actual.txt').write_text('A');(tmp_path/'alias.txt').symlink_to(tmp_path/'actual.txt')
    r=runtime(tmp_path)
    with pytest.raises(ValueError,match='符号链接'):
        prepare(r,proposal(actions=[dict(effect='move',source='alias.txt',destination='out.txt')]))


def test_no_replace_primitive_preserves_conflicting_target(tmp_path):
    from safety.atomic import rename_no_replace
    (tmp_path/'a').write_text('A');(tmp_path/'b').write_text('B')
    with pytest.raises(OSError):rename_no_replace(tmp_path/'a',tmp_path/'b')
    assert (tmp_path/'a').read_text()=='A' and (tmp_path/'b').read_text()=='B'


def test_resume_partial_plan_skips_completed_steps_and_keeps_old_run_ledger(tmp_path,monkeypatch):
    import safety.atomic as atomic
    (tmp_path/'out').mkdir()
    for name in ('a.txt','b.txt'):(tmp_path/name).write_text(name)
    r=runtime(tmp_path)
    p=prepare(r,proposal(actions=[dict(effect='move',source=name,destination='out/'+name) for name in ('a.txt','b.txt')]))
    original=atomic.rename_no_replace
    def fail_second(src,dst):
        if src.name=='b.txt':raise OSError('injected pre-mutation failure')
        return original(src,dst)
    monkeypatch.setattr(atomic,'rename_no_replace',fail_second)
    with pytest.raises(ValueError):execute(r,p['id'],OpLog(r.sb,'one'),Confirmer('never'))
    assert (tmp_path/'out/a.txt').exists() and (tmp_path/'b.txt').exists()
    monkeypatch.setattr(atomic,'rename_no_replace',original)
    new=runtime(tmp_path,run='two');execute(new,p['id'],OpLog(new.sb,'two'),Confirmer('never'))
    assert len((tmp_path/'.oplog/one.jsonl').read_text().splitlines())==1
    assert len((tmp_path/'.oplog/two.jsonl').read_text().splitlines())==1
    assert len(new.current['operation_history'])==1 and len(new.operations)==1
    assert verify_task(new.current,new.sb)[0]['passed']
    undo(new.sb,'two');undo(new.sb,'one')
    assert (tmp_path/'a.txt').exists() and (tmp_path/'b.txt').exists()


def test_reconcile_journalled_move_without_replaying_it(tmp_path,monkeypatch):
    r=runtime(tmp_path);(tmp_path/'a.txt').write_text('A');(tmp_path/'out').mkdir()
    p=prepare(r,proposal(actions=[dict(effect='move',source='a.txt',destination='out/a.txt')]))
    original=r.store.save_file_plan
    def crash_after_journal(plan):
        if plan['progress']:raise KeyboardInterrupt()
        original(plan)
    monkeypatch.setattr(r.store,'save_file_plan',crash_after_journal)
    with pytest.raises(KeyboardInterrupt):execute(r,p['id'],OpLog(r.sb,'one'),Confirmer('never'))
    new=runtime(tmp_path,run='two');execute(new,p['id'],OpLog(new.sb,'two'),Confirmer('never'))
    assert len((tmp_path/'.oplog/one.jsonl').read_text().splitlines())==1
    assert not (tmp_path/'.oplog/two.jsonl').exists()
    assert verify_task(new.current,new.sb)[0]['passed']


def test_existing_read_scope_checked_before_selection(tmp_path):
    (tmp_path/'allowed').mkdir();(tmp_path/'private').mkdir();(tmp_path/'private/a.txt').write_text('A')
    r=runtime(tmp_path)
    r.current=r.store.submit(TaskSubmission.model_validate(spec(('只读 allowed',[dict(kind='manual_review')]),read_roots=['allowed'],write_roots=['.'],allowed_effects=['move','create'])),'请求','one')
    with pytest.raises(ValueError,match='读取范围'):
        prepare(r,proposal(selection=dict(path='private',destination='out')))


def test_classification_bridge_compiles_paths_and_preserves_semantic_review(tmp_path):
    from tests.test_retrieval import pdf
    from tests.helpers import classification
    for name in ('a.pdf','b.pdf','c.pdf'):
        pdf(tmp_path/name,['Abstract. Clinical application study and method evidence. '*10])
    r=runtime(tmp_path)
    cid=classification(r,base_dir='organized',category_paths={'1':'应用/医疗'})
    assert r.current is None
    plan=prepare(r,proposal(classification_plan_id=cid))
    assert r.current['goals'][0]['semantic_review']
    assert len([s for s in plan['payload']['steps'] if s['effect']=='move'])==3
    execute(r,plan['id'],OpLog(r.sb,'one'),Confirmer('never'))
    checks=verify_task(r.current,r.sb)
    assert checks[0]['passed'] and checks[0]['needs_review']
    assert r.sb.artifacts.data['classification_plans'][cid]['status']=='applied'
    assert len(list((tmp_path/'organized/应用/医疗').glob('*.pdf')))==3
