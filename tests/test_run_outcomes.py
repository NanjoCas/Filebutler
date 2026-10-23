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
from tests.helpers import run_script
from tests.test_week1 import FakeClient
from tests.test_web_ui import ui, files
from tools import registry
import webapp


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
        run_agent('统计资料',LLM({'model':'fake','llm_retries':0},client=client),sb,{},Console(quiet=True),
                  on_event=events.append,oplog=OpLog(sb,'failed'))
    failure=caught.value.filebutler_outcome
    assert '无法连接模型服务' in failure['final'] and 'SECRET' not in json.dumps(failure,ensure_ascii=False)
    assert failure['messages'][-1]['role']=='assistant'
    assert events[-1]['type']=='run_failed' and events[-1]['final']==failure['final']
    assert sb.runtime.store.outcome()['stop_reason']=='exception'


def test_inspection_without_history_reports_missing_evidence_without_guessing(tmp_path):
    result,_,client=run_script(tmp_path,'为什么停止',FakeClient([[('inspect_run',{})]]))
    assert result.stop_reason=='done' and '没有对应的运行记录' in result.final
    assert len(client.seen)==1


def test_missing_model_configuration_has_a_specific_safe_reason(tmp_path,monkeypatch):
    monkeypatch.delenv('FILEBUTLER_TEST_MISSING_KEY',raising=False)
    cfg={'model':'fake','api_key_env':'FILEBUTLER_TEST_MISSING_KEY'}
    with pytest.raises(RuntimeError) as caught:
        run_agent('统计',LLM(cfg),Sandbox(tmp_path),cfg,Console(quiet=True))
    assert 'FILEBUTLER_TEST_MISSING_KEY 未设置' in caught.value.filebutler_outcome['final']


def test_oversized_recent_turns_are_archived_while_retaining_constraints_and_stop_reason():
    previous='任务已中止。原因：费用超过预算。'
    history=[dict(role='system',content='rules'),dict(role='user',content='保留全部原件'),
        dict(role='assistant',content='',tool_calls=[dict(id='x',function=dict(name='paper_profiles',arguments='{}'))]),
        dict(role='tool',tool_call_id='x',content='OK '+('long evidence '*10000)),
        dict(role='assistant',content=previous),dict(role='user',content='为什么停止')]
    original=copy.deepcopy(history)
    projected,stats=prepare_history(history,{'conversation_history_max_chars':2000})
    assert stats['after_chars']<2000 and stats['archived_messages']==2
    assert previous in [m.get('content') for m in projected]
    assert history==original and [m['content'] for m in projected if m['role']=='user']==['保留全部原件','为什么停止']


def test_web_failure_is_a_saved_chat_reply_with_accurate_status(ui,files,monkeypatch):
    cfg=webapp._config();monkeypatch.setattr(webapp,'_config',lambda:dict(cfg))
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


def test_legacy_budget_stop_can_be_inspected_without_resuming(ui,files,monkeypatch):
    cfg=webapp._config();monkeypatch.setattr(webapp,'_config',lambda:dict(cfg))
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
