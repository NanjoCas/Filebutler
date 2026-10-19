"""Complete classification, exact plan execution, refusal, changes and undo."""
import copy
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console
from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.runtime import LocalRuntime,classification_followup
from eval.checker import snapshot,files_preserved
from safety.confirm import Confirmer
from safety.oplog import OpLog,undo
from safety.sandbox import Sandbox
from tests.test_retrieval import pdf
from tests.test_week1 import FakeClient
from tools import registry
import tools.classification as classification
from web_confirm import build_confirmation

CFG={'local_runtime':True,'prompt':'v7_classification'}


def dispatch(sb,name,**args):
    return registry.dispatch(name,json.dumps(args),sb)


def ready(tmp_path,count=3):
    sb=Sandbox(tmp_path)
    for i in range(count):pdf(tmp_path/f'paper {i}.pdf',[f'Paper {i}. Abstract We propose a transformer network for fake news detection. '*8])
    sb.artifacts=ArtifactStore(sb)
    sb.runtime=LocalRuntime('把论文按核心方法分类',CFG)
    result=dispatch(sb,'paper_profiles')
    assert result.startswith('OK') and '[已截断' not in result
    cid=next(reversed(sb.artifacts.data['catalogs']))
    for page in range(2,(count+15)//16+1):
        assert dispatch(sb,'paper_profiles',catalog_id=cid,page=page).startswith('OK')
    members=sb.artifacts.data['catalogs'][cid]['members']
    args=dict(catalog_id=cid,categories=['Transformer','其他'],assignments=[dict(file_id=f,category=1 if i%2 else 2,uncertain=i==0) for i,f in enumerate(members)],note='验收标记：松柏')
    assert dispatch(sb,'submit_classification',**args).startswith('OK')
    return sb,args


def execute(sb,answer='y',client=None,ask=None,runid='apply'):
    client=client or FakeClient([])
    result=run_agent('把这 2 类落成实际文件夹',LLM({'model':'fake'},client=client),sb,CFG,Console(quiet=True),
                     artifacts=sb.artifacts,confirmer=Confirmer('always',ask=ask or (lambda _:answer)),oplog=OpLog(sb,runid))
    return result,client


def test_plan_is_complete_persistent_local_table_and_reuses_cards(tmp_path,monkeypatch):
    sb,args=ready(tmp_path,19)
    assert len(list(tmp_path.glob('*.pdf')))==19 and not (tmp_path/'按方法').exists()
    assert sb.runtime.final_answer.count('(file:F')==19 and '**待核实**' in sb.runtime.final_answer
    data=json.loads(json.dumps(sb.artifacts.export()))
    sb.artifacts=ArtifactStore(sb,data)
    monkeypatch.setattr('tools.fs_read._pdf_pages',lambda *a,**kw:pytest.fail('cached cards must not reparse'))
    result=dispatch(sb,'paper_profiles',catalog_id=args['catalog_id'],page=1)
    assert '缓存复用=16' in result and len(result)<16000
    args['assignments'].pop()
    assert dispatch(sb,'submit_classification',**args).startswith('ERROR')


def test_zero_model_execution_single_confirmation_valid_refs_and_exact_undo(tmp_path):
    sb,_=ready(tmp_path,19);before=snapshot(tmp_path,True)
    requests=[]
    result,client=execute(sb,ask=lambda prompt:requests.append(prompt) or 'y')
    assert len(client.seen)==0 and result.llm_seconds==0 and result.tokens==0
    assert result.tool_calls==1 and result.stop_reason=='done' and len(requests)==1
    assert '已移动 19 篇' in result.final and '验收标记：松柏' in result.final
    assert not list(tmp_path.glob('*.pdf'))
    assert files_preserved(before,snapshot(tmp_path,True))
    for item in sb.artifacts.data['files']:
        assert sb.artifacts.resolve(item)[0].is_file()
    undo(sb,'apply',on_restore=sb.artifacts.relocate)
    assert snapshot(tmp_path,True)==before
    # Reapplying after undo works against the original version, not stale paths.
    again,_=execute(sb,runid='again')
    assert again.stop_reason=='done' and '已移动 19 篇' in again.final


def test_rejection_has_no_directories_or_moves(tmp_path):
    sb,_=ready(tmp_path);before=snapshot(tmp_path,True)
    result,_=execute(sb,'n')
    assert result.rejections==1 and '未创建目录' in result.final
    assert snapshot(tmp_path,True)==before and not (tmp_path/'按方法').exists()


def test_change_during_confirmation_prevents_all_mutations(tmp_path):
    sb,_=ready(tmp_path)
    def ask(_):
        (tmp_path/'paper 0.pdf').write_bytes(b'changed')
        return 'y'
    result,client=execute(sb,ask=ask)
    assert result.stop_reason=='operation_error' and not client.seen
    assert not (tmp_path/'按方法').exists() and len(list(tmp_path.glob('*.pdf')))==3


def test_target_conflict_does_not_overwrite_or_partially_move(tmp_path):
    sb,_=ready(tmp_path)
    target=tmp_path/'按方法/02_其他/paper 0.pdf';target.parent.mkdir(parents=True);target.write_text('external')
    before=snapshot(tmp_path,True)
    result,_=execute(sb)
    assert result.stop_reason=='operation_error' and target.read_text()=='external'
    assert snapshot(tmp_path,True)==before


def test_partial_journal_failure_keeps_prior_moves_and_undo(tmp_path,monkeypatch):
    sb,_=ready(tmp_path);before=snapshot(tmp_path,True)
    original=OpLog.record;seen=[]
    def fail(self,op,src,dst):
        if op=='move':
            seen.append(src)
            if len(seen)==2:raise OSError('disk failure')
        return original(self,op,src,dst)
    monkeypatch.setattr(OpLog,'record',fail)
    result,_=execute(sb)
    assert result.stop_reason=='operation_error' and '已移动 1 篇' in result.final
    assert files_preserved(before,snapshot(tmp_path,True))
    undo(sb,'apply',on_restore=sb.artifacts.relocate)
    assert snapshot(tmp_path,True)==before


def test_submit_needs_all_pages_and_no_hidden_category_or_foreign_evidence(tmp_path):
    sb,args=ready(tmp_path,17)
    catalog=sb.artifacts.data['catalogs'][args['catalog_id']]
    catalog['delivered'].pop()
    assert '尚未查看' in dispatch(sb,'submit_classification',**args)
    catalog['delivered']=list(catalog['members'])
    args['categories'][0]='../outside'
    assert dispatch(sb,'submit_classification',**args).startswith('ERROR')
    args['categories'][0]='Transformer'
    args['assignments'][0]['evidence_ids']=[sb.artifacts.data['paper_profiles'][catalog['members'][1]]['evidence_id']]
    assert dispatch(sb,'submit_classification',**args).startswith('ERROR')


def test_unreadable_pdf_must_be_uncertain_and_may_use_explicit_reread(tmp_path):
    sb=Sandbox(tmp_path);(tmp_path/'bad.pdf').write_bytes(b'not pdf');sb.artifacts=ArtifactStore(sb)
    sb.runtime=LocalRuntime('文献按照核心方法分类',CFG)
    assert dispatch(sb,'paper_profiles').startswith('OK')
    cid=next(iter(sb.artifacts.data['catalogs']));fid=sb.artifacts.data['catalogs'][cid]['members'][0]
    args=dict(catalog_id=cid,categories=['待核实'],assignments=[dict(file_id=fid,category=1)])
    assert '必须标 uncertain' in dispatch(sb,'submit_classification',**args)
    args['assignments'][0]['uncertain']=True
    assert dispatch(sb,'submit_classification',**args).startswith('OK')


def test_exact_followup_only_and_wrong_category_count_never_executes(tmp_path):
    assert classification_followup('把这 9 类落成实际文件夹')
    assert classification_followup('执行这个分类计划')
    for value in ['不要执行这个分类计划','执行这个计划','执行这个分类计划并删除图片','执行这个分类计划，但移动到其他目录']:
        assert not classification_followup(value)
    assert not LocalRuntime('不执行这个分类计划',CFG).allow_classification_apply
    sb,_=ready(tmp_path)
    runtime=LocalRuntime('文献按核心方法分类',CFG)
    assert not runtime.allow_classification_apply and runtime.prepare('move','{}',sb)[1]
    sb.runtime=LocalRuntime('把这 9 类落成实际文件夹',CFG)
    assert dispatch(sb,'apply_classification').startswith('ERROR')
    assert not (tmp_path/'按方法').exists()


def test_execute_history_projection_keeps_constraints_full_history_unchanged(tmp_path):
    sb,_=ready(tmp_path)
    history=[{'role':'system','content':'用中文'}, {'role':'user','content':'不要删除图片'},
             {'role':'assistant','content':'long evidence '*10000},{'role':'user','content':'执行这个分类计划'}]
    before=copy.deepcopy(history)
    projected,stats=prepare_history(history,CFG,sb.artifacts)
    assert history==before and stats['projection']=='classification_plan'
    assert stats['after_chars']<stats['before_chars']/10
    assert any(m['content']=='不要删除图片' for m in projected)
    assert projected[-1]==history[-1]


def test_batch_confirmation_shows_every_source_and_destination(tmp_path):
    sb,_=ready(tmp_path)
    review=build_confirmation({'tool':'move_many','args':{'moves':[
        dict(src='paper 0.pdf',dst='按方法/02_其他/paper 0.pdf',dst_dir='按方法/02_其他',uncertain=True),
        dict(src='paper 1.pdf',dst='按方法/01_Transformer/paper 1.pdf',dst_dir='按方法/01_Transformer',uncertain=False)]}},sb)
    assert review['counts']['affected']==2 and len(review['groups'])==2
    item=review['groups'][0]['items'][0]
    assert item['previewable'] and item['destination'].endswith('paper 0.pdf') and '待核实' in item['message']


def test_classification_finishes_structured_without_a_final_model_request(tmp_path):
    sb=Sandbox(tmp_path)
    for i in range(3):pdf(tmp_path/f'p{i}.pdf',['Abstract We use CNN for detection. '*20])
    calls=[]
    class Client:
        def __init__(self):self.chat=NS(completions=NS(create=self.create))
        def create(self,**kw):
            calls.append(kw)
            if len(calls)==1:name,args='paper_profiles',{}
            else:
                store=sb.artifacts;cid=next(iter(store.data['catalogs']))
                name,args='submit_classification',dict(catalog_id=cid,categories=['CNN'],assignments=[dict(file_id=f,category=1) for f in store.data['catalogs'][cid]['members']])
            tc=NS(id=str(len(calls)),function=NS(name=name,arguments=json.dumps(args)))
            return NS(usage=None,choices=[NS(message=NS(content='',tool_calls=[tc]))])
    result=run_agent('帮我把文献按照核心方法分类',LLM({'model':'fake'},client=Client()),sb,CFG,Console(quiet=True))
    assert len(calls)==2 and result.tool_calls==2 and result.stop_reason=='done'
    assert result.final.count('(file:F')==3 and '尚未移动' in result.final


def test_bare_file_id_is_resolved_exactly_and_read_budget_preserves_catalog_access(tmp_path):
    sb,args=ready(tmp_path);fid=sb.artifacts.data['catalogs'][args['catalog_id']]['members'][0]
    assert dispatch(sb,'read_file',path=fid,page=1).startswith('OK')
    r=LocalRuntime('文献按核心方法分类',{'classification_read_limit':1})
    r.after('read_file','OK: evidence',sb)
    assert r.prepare('read_file',json.dumps({'path':fid}),sb)[1]
    names={s['function']['name'] for s in r.schemas(registry.schemas())}
    assert names=={'paper_profiles','submit_classification'}
    assert LocalRuntime('文献按核心方法分类，逐篇核实方法章节',CFG).task=='general'


def test_symlink_destination_is_rejected_without_moves(tmp_path):
    sb,_=ready(tmp_path)
    (tmp_path/'target').mkdir();(tmp_path/'按方法').symlink_to(tmp_path/'target',target_is_directory=True)
    result,_=execute(sb)
    assert result.stop_reason=='operation_error' and len(list(tmp_path.glob('*.pdf')))==3
    assert not list((tmp_path/'target').iterdir())


from tests.test_web_ui import files,ui
import threading
import webapp


@pytest.mark.parametrize('answer',['y','n'])
def test_persisted_web_plan_confirmation_preview_and_undo_without_model(ui,files,monkeypatch,answer):
    path=files.root/'文献/paper #1 (初稿).pdf'
    pdf(path,['Abstract We propose a transformer for detection. '*20])
    cfg=webapp._config();cfg.update(CFG)
    monkeypatch.setattr(webapp,'_config',lambda:cfg)
    client=FakeClient([])
    monkeypatch.setattr(webapp,'LLM',lambda config:LLM({'model':'fake'},client=client))
    cid=ui.post('/api/conversations').json()['id']
    files.artifacts=webapp._artifacts(cid,files,persist=True)
    files.runtime=LocalRuntime('文献按核心方法分类',CFG)
    assert dispatch(files,'paper_profiles',path='文献').startswith('OK')
    catalog=next(iter(files.artifacts.data['catalogs']))
    fid=files.artifacts.data['catalogs'][catalog]['members'][0]
    assert dispatch(files,'submit_classification',catalog_id=catalog,categories=['Transformer'],assignments=[dict(file_id=fid,category=1)]).startswith('OK')
    before=snapshot(files.root,True)
    with webapp._db() as db:
        history=[dict(role='system',content='用中文'),dict(role='assistant',content='旧原文 '*10000)]
        db.execute('UPDATE conversations SET history_json=? WHERE id=?',(json.dumps(history),cid))
    waiting,finished=threading.Event(),threading.Event();original=webapp._event
    def event(tid,value):
        original(tid,value)
        if value['type']=='confirmation_required':waiting.set()
        if value['type'] in {'completed','failed'}:finished.set()
    monkeypatch.setattr(webapp,'_event',event)
    tid=ui.post(f'/api/conversations/{cid}/messages',json={'content':'执行这个分类计划'}).json()['turn_id']
    assert waiting.wait(5)
    pending=ui.get(f'/api/turns/{tid}').json()['pending_confirmation']
    assert pending['action']['operation']=='move_many' and pending['action']['counts']['affected']==1
    item=pending['action']['groups'][0]['items'][0]
    assert item['previewable'] and item['destination']=='文献/按方法/01_Transformer/paper #1 (初稿).pdf'
    assert ui.get('/api/files/preview',params={'path':item['path']}).status_code==200
    assert ui.post(f'/api/turns/{tid}/confirm',json={'answer':answer,'confirmation_id':pending['id']}).status_code==200
    assert finished.wait(5)
    turn=ui.get(f'/api/turns/{tid}').json()
    assert turn['status']=='completed' and turn['result']['llm_seconds']==0 and not client.seen
    assert turn['result']['context']['projection']=='classification_plan'
    if answer=='y':
        assert not path.exists()
        assert ui.get(f'/api/conversations/{cid}/files/{fid}/raw').content.startswith(b'%PDF')
        assert ui.post(f'/api/turns/{tid}/undo').status_code==200
    assert snapshot(files.root,True)==before


def test_category_slash_is_a_local_display_conversion_not_a_resubmission(tmp_path):
    sb,args=ready(tmp_path)
    args['categories'][0]='自监督/半监督'
    assert dispatch(sb,'submit_classification',**args).startswith('OK')
    result,_=execute(sb)
    assert result.stop_reason=='done' and (tmp_path/'按方法/01_自监督／半监督').is_dir()
    assert '自监督/半监督' in result.final
