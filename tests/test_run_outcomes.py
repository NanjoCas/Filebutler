"""Stop reports, immutable run evidence and diagnostic-only turns; no network."""
import copy
import json
import threading

try:
    import httpx
except ImportError:
    import httpx2 as httpx
import openai
import pytest
from rich.console import Console

from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.task import TaskStore, TaskSubmission
from agent.task_runtime import TaskRuntime
from agent.verifier import verify_task
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tests.test_runtime import Client
from tests.test_task_runtime_v2 import CFG, execute, spec
from tests.test_week1 import FakeClient
from tests.test_web_ui import ui, files
from tools import registry
import webapp


def test_formed_and_applied_conditions_are_compatible_and_rechecked(tmp_path):
    from tests.test_retrieval import pdf
    pdf(tmp_path/'a.pdf',['Abstract We propose a CNN method for detection. '*20])
    sb=Sandbox(tmp_path)
    task=spec(('形成方案',[dict(kind='plan_ready')]),('应用方案',[dict(kind='plan_applied')]),
              write_roots=['.'],allowed_effects=['create','move'])
    task['goals'][0]['semantic_review']=True
    def model(kw,n):
        if n==1:return [('set_task',task),('begin_step',dict(id='plan',goal_ids=['g0'],description='形成方案')),('paper_profiles',{})]
        if n==2:
            cid=next(iter(sb.artifacts.data['catalogs']))
            members=sb.artifacts.data['catalogs'][cid]['members']
            return [('submit_classification',dict(catalog_id=cid,categories=['CNN'],
                    assignments=[dict(file_id=f,category=1) for f in members]))]
        pid=next(iter(sb.artifacts.data['classification_plans']))
        return [('begin_step',dict(id='apply',goal_ids=['g1'],description='应用方案')),('apply_classification',{'plan_id':pid}),('finish_task',{})]
    result=run_agent('形成方案并执行',LLM({'model':'fake'},client=Client(model)),sb,CFG,Console(quiet=True),
                     confirmer=Confirmer('never'),oplog=OpLog(sb,'classification'))
    assert result.stop_reason=='needs_review'
    assert all(c['passed'] for c in result.runtime['checks'])
    runtime=sb.runtime;old=runtime.current
    addition=TaskSubmission.model_validate(spec(('附加核对',[dict(kind='path_exists',path='.')]),
        task_id=old['id'],expected_revision=1))
    addition.goals[0].id='extra'
    resumed=runtime.store.submit(addition,'继续核对','next')
    assert all(c['passed'] for c in verify_task(resumed,sb))  # References survive a new revision.
    refreshed=runtime.store.recheck(resumed['id'],2,sb)
    assert refreshed['status']=='needs_review' and all(c['passed'] for c in refreshed['checks'])
    plan=next(iter(sb.artifacts.data['classification_plans'].values()))
    plan['status']='partial'
    partial_checks=verify_task(old,sb)
    assert partial_checks[0]['passed'] and not partial_checks[1]['passed']
    plan['status']='applied'
    (tmp_path/'CNN/a.pdf').write_bytes(b'external change')
    assert not any(c['passed'] for c in verify_task(old,sb))


@pytest.mark.parametrize('reason',['cost_budget','token_budget','max_steps','repeated_call'])
def test_stops_return_actual_reason_and_persist_it_for_next_turn(tmp_path,reason):
    task=spec(('建立目标目录',[dict(kind='path_exists',path='out')]),write_roots=['.'],allowed_effects=['create'])
    script=[[('set_task',task),('make_dir',{'path':'out'})]]
    cfg={'max_steps':1};llm_cfg={'model':'fake'}
    if reason=='cost_budget':
        cfg.update(cost_budget=.0001,max_steps=8)
        llm_cfg['prices']={'fake':dict(cache_hit=2,cache_miss=2,output=8)}
    if reason=='token_budget':cfg.update(token_budget=100,max_steps=8)
    if reason=='repeated_call':
        script=[[('set_task',task),('make_dir',{'path':'out'})], [('list_dir',{})], [('list_dir',{})], [('list_dir',{})]]
        cfg['max_steps']=8
    client=FakeClient(script);sb=Sandbox(tmp_path)
    result=run_agent('建立目录并继续核验',LLM(llm_cfg,client=client),sb,{**CFG,**cfg},Console(quiet=True),
        confirmer=Confirmer('never'),oplog=OpLog(sb,'stopped'))
    assert result.stop_reason==reason and '原因：' in result.final
    assert '已记录创建目录 1 项' in result.final and (tmp_path/'out').is_dir()
    assert result.messages[-1]['content']==result.final
    recorded=sb.runtime.store.outcome()
    assert recorded['stop_reason']==reason and recorded['summary']==result.final
    future=TaskRuntime('解释原因',CFG,sb,'new')
    assert reason in future.guidance()
    assert len(client.seen)==(4 if reason=='repeated_call' else 1)


def test_partial_reply_gives_concrete_cause_in_main_text(tmp_path):
    task=spec(('创建甲乙',[dict(kind='path_exists',path='甲'),dict(kind='path_exists',path='乙')]),
        write_roots=['.'],allowed_effects=['create'])
    result,_,_=execute(tmp_path,'建立甲乙',[[('set_task',task),('make_dir',{'path':'甲'}),('finish_task',{})]],
                          confirmer=Confirmer('never'))
    assert result.stop_reason=='partial' and '原因：' in result.final and '路径不存在：乙' in result.final


@pytest.mark.parametrize('instruction',['上个任务为什么停止','Why did the previous run stop?','解释一下刚才的运行结果'])
def test_inspection_ends_turn_and_blocks_all_queued_mutations(tmp_path,instruction):
    sb=Sandbox(tmp_path);sb.artifacts=ArtifactStore(sb)
    store=TaskStore(tmp_path/'.oplog/tasks.sqlite3',tmp_path,'cli')
    store.save_outcome(dict(run_id='old',stop_reason='cost_budget',summary='任务已中止。原因：费用超过预算。',purpose='action'))
    script=[[('inspect_run',{}),('set_task',spec(('新建',[dict(kind='path_exists',path='bad')]),
        write_roots=['.'],allowed_effects=['create'])),('make_dir',{'path':'bad'})]]
    result,_,client=execute(tmp_path,instruction,script,confirmer=Confirmer('never'))
    assert result.stop_reason=='done' and '费用超过预算' in result.final
    assert not (tmp_path/'bad').exists() and store.recent()==[] and len(client.seen)==1
    assert store.outcome()['run_id']=='old'  # Repeated diagnostics still refer to the action run.
    assert store.outcome('v2-test')['purpose']=='inspection'


def test_run_evidence_is_isolated_by_session(tmp_path):
    a=TaskStore(tmp_path/'tasks.sqlite3',tmp_path,'a');b=TaskStore(a.path,tmp_path,'b')
    a.save_outcome(dict(run_id='secret',summary='private',stop_reason='exception'))
    with pytest.raises(ValueError):b.outcome('secret')


def test_exception_exposes_safe_cause_and_closes_history_without_swallowing_error(tmp_path):
    client=FakeClient([])
    def fail(**kw):raise openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid?key=SECRET'))
    client.chat.completions.create=fail
    sb=Sandbox(tmp_path);events=[]
    with pytest.raises(openai.APIConnectionError) as caught:
        run_agent('统计资料',LLM({'model':'fake','llm_retries':0},client=client),sb,CFG,Console(quiet=True),
                  on_event=events.append,oplog=OpLog(sb,'failed'))
    failure=caught.value.filebutler_outcome
    assert '无法连接模型服务' in failure['final'] and 'SECRET' not in json.dumps(failure,ensure_ascii=False)
    assert failure['messages'][-1]['role']=='assistant'
    assert events[-1]['type']=='run_failed' and events[-1]['final']==failure['final']
    assert sb.runtime.store.outcome()['stop_reason']=='exception'


def test_unexpected_tool_failure_closes_pending_calls_and_explains_the_step(tmp_path,monkeypatch):
    def fail(*a,**kw):raise RuntimeError('internal failure')
    monkeypatch.setattr(registry,'dispatch_result',fail)
    task=spec(('创建目标',[dict(kind='path_exists',path='out')]),write_roots=['.'],allowed_effects=['create'])
    client=FakeClient([[('set_task',task),('read_file',{'path':'a.txt'}),('make_dir',{'path':'out'})]])
    sb=Sandbox(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        run_agent('建立目标',LLM({'model':'fake'},client=client),sb,CFG,Console(quiet=True),
                  confirmer=Confirmer('never'),oplog=OpLog(sb,'tool-failure'))
    result=caught.value.filebutler_outcome
    assert '工具 read_file失败' in result['final'] and not (tmp_path/'out').exists()
    calls={tc['id'] for m in result['messages'] for tc in m.get('tool_calls',[])}
    replies={m['tool_call_id'] for m in result['messages'] if m['role']=='tool'}
    assert calls==replies and result['messages'][-1]['content']==result['final']


def test_inspection_without_history_reports_missing_evidence_without_guessing(tmp_path):
    result,_,client=execute(tmp_path,'为什么停止',[[('inspect_run',{})]])
    assert result.stop_reason=='done' and '没有对应的运行记录' in result.final
    assert len(client.seen)==1


def test_missing_model_configuration_has_a_specific_safe_reason(tmp_path,monkeypatch):
    monkeypatch.delenv('FILEBUTLER_TEST_MISSING_KEY',raising=False)
    cfg={**CFG,'model':'fake','api_key_env':'FILEBUTLER_TEST_MISSING_KEY'}
    with pytest.raises(RuntimeError) as caught:
        run_agent('统计',LLM(cfg),Sandbox(tmp_path),cfg,Console(quiet=True))
    assert 'FILEBUTLER_TEST_MISSING_KEY 未设置' in caught.value.filebutler_outcome['final']


def test_v2_archives_oversized_recent_turns_while_retaining_constraints_and_stop_reason():
    previous='任务已中止。原因：费用超过预算。'
    history=[dict(role='system',content='rules'),dict(role='user',content='保留全部原件'),
        dict(role='assistant',content='',tool_calls=[dict(id='x',function=dict(name='paper_profiles',arguments='{}'))]),
        dict(role='tool',tool_call_id='x',content='OK '+('long evidence '*10000)),
        dict(role='assistant',content=previous),dict(role='user',content='为什么停止')]
    original=copy.deepcopy(history)
    projected,stats=prepare_history(history,{**CFG,'conversation_history_max_chars':2000,'conversation_keep_full_turns':2})
    assert stats['after_chars']<2000 and stats['archived_messages']==2
    assert previous in [m.get('content') for m in projected]
    assert history==original and [m['content'] for m in projected if m['role']=='user']==['保留全部原件','为什么停止']


def test_web_failure_is_a_saved_chat_reply_with_accurate_status(ui,files,monkeypatch):
    cfg={**webapp._config(),**CFG};monkeypatch.setattr(webapp,'_config',lambda:dict(cfg))
    class Broken:
        def chat(self,*args,**kwargs):raise RuntimeError('private exception payload SECRET')
        model='fake';total_tokens=0;cost=0;prices=None
    monkeypatch.setattr(webapp,'LLM',lambda cfg:Broken())
    finished=threading.Event();original=webapp._event
    def event(tid,ev):
        original(tid,ev)
        if ev['type']=='failed':finished.set()
    monkeypatch.setattr(webapp,'_event',event)
    cid=ui.post('/api/conversations').json()['id']
    tid=ui.post(f'/api/conversations/{cid}/messages',json={'content':'核对资料'}).json()['turn_id']
    assert finished.wait(5)
    conv=ui.get(f'/api/conversations/{cid}').json();turn=ui.get(f'/api/turns/{tid}').json()
    assert turn['status']=='failed' and turn['result']['stop_reason']=='exception'
    message = conv['messages'][-1]
    # The exception class is developer detail, not part of the user's reply (2026-10-22).
    assert '原因：' in message['content'] and 'RuntimeError' not in message['content']
    assert any(s.get('audience') == 'dev' and 'RuntimeError' in s['markdown'] for s in message['report']['sections'])
    assert 'SECRET' not in json.dumps(turn,ensure_ascii=False)
    assert turn['events'][-1]['message']!='本轮已完成'


def test_recheck_api_updates_derived_status_without_reexecuting_files(ui,files):
    cid=ui.post('/api/conversations').json()['id'];store=webapp._tasks(cid)
    task=store.submit(TaskSubmission.model_validate(spec(('核对文件',[dict(kind='path_exists',path='new.txt')]))),'核对','r')
    url=f'/api/conversations/{cid}/tasks/{task["id"]}/verify'
    assert ui.post(url,json={'expected_revision':1}).json()['status']=='partial'
    (files.root/'new.txt').write_text('exists')
    assert ui.post(url,json={'expected_revision':1}).json()['status']=='completed'
    assert ui.post(url,json={'expected_revision':2}).status_code==409
    other=ui.post('/api/conversations').json()['id']
    assert ui.post(f'/api/conversations/{other}/tasks/{task["id"]}/verify',json={'expected_revision':1}).status_code==409
    assert (files.root/'new.txt').read_text()=='exists'


def test_legacy_budget_stop_can_be_inspected_without_resuming(ui,files,monkeypatch):
    cfg={**webapp._config(),**CFG};monkeypatch.setattr(webapp,'_config',lambda:dict(cfg))
    fake=FakeClient([[('inspect_run',{})]])
    monkeypatch.setattr(webapp,'LLM',lambda cfg:LLM({'model':'fake'},client=fake))
    cid=ui.post('/api/conversations').json()['id']
    with webapp._db() as db:
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at,final,result_json) VALUES(?,?,?,?,?,?,?,?)',
            ('old',cid,'old-budget','completed',webapp._now(),webapp._now(),'（未完成：触发终止条件 cost_budget）',
             json.dumps(dict(stop_reason='cost_budget',cost=.31))))
    finished=threading.Event();original=webapp._event
    def event(tid,ev):
        original(tid,ev)
        if ev['type'] in {'completed','failed'}:finished.set()
    monkeypatch.setattr(webapp,'_event',event)
    tid=ui.post(f'/api/conversations/{cid}/messages',json={'content':'上次为何停止'}).json()['turn_id']
    assert finished.wait(5)
    turn=ui.get(f'/api/turns/{tid}').json()
    assert turn['result']['stop_reason']=='done' and '费用预算上限' in turn['final']
    assert len(fake.seen)==1 and webapp._tasks(cid).recent()==[]


def test_stopped_web_turn_does_not_claim_completion(ui,monkeypatch):
    cfg={**webapp._config(),**CFG,'max_steps':1};monkeypatch.setattr(webapp,'_config',lambda:dict(cfg))
    fake=FakeClient([[('set_task',spec(('尚未完成',[dict(kind='path_exists',path='missing')])))]])
    monkeypatch.setattr(webapp,'LLM',lambda cfg:LLM({'model':'fake'},client=fake))
    finished=threading.Event();original=webapp._event
    def event(tid,ev):
        original(tid,ev)
        if ev['type'] in {'completed','failed'}:finished.set()
    monkeypatch.setattr(webapp,'_event',event)
    cid=ui.post('/api/conversations').json()['id']
    tid=ui.post(f'/api/conversations/{cid}/messages',json={'content':'执行任务'}).json()['turn_id']
    assert finished.wait(5)
    turn=ui.get(f'/api/turns/{tid}').json()
    assert turn['result']['stop_reason']=='max_steps'
    assert '原因：已达到本轮执行轮数上限 1' in turn['final']
    assert turn['events'][-1]['message']!='本轮已完成'
    assert ui.get(f'/api/conversations/{cid}').json()['messages'][-1]['content']==turn['final']
