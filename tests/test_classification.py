"""Classification plans: complete coverage, cached cards, evidence and uncertain items.

The plan is executed through a file plan (prepare_file_plan classification_plan_id);
see tests/test_file_plans.py. The old executor apply_classification is retired.
"""
import json

import pytest
from agent.artifacts import ArtifactStore
from agent.task_runtime import TaskRuntime
from safety.sandbox import Sandbox
from tests.test_retrieval import pdf
from tools import registry


def dispatch(sb,name,**args):
    return registry.dispatch(name,json.dumps(args),sb)


def ready(tmp_path,count=3):
    sb=Sandbox(tmp_path)
    for i in range(count):pdf(tmp_path/f'paper {i}.pdf',[f'Paper {i}. Abstract We propose a transformer network for fake news detection. '*8])
    sb.artifacts=ArtifactStore(sb)
    sb.runtime=TaskRuntime('把论文按核心方法分类',{},sb,'r')
    result=dispatch(sb,'paper_profiles')
    assert result.startswith('OK') and '[已截断' not in result
    cid=next(reversed(sb.artifacts.data['catalogs']))
    for page in range(2,(count+15)//16+1):
        assert dispatch(sb,'paper_profiles',catalog_id=cid,page=page).startswith('OK')
    members=sb.artifacts.data['catalogs'][cid]['members']
    args=dict(catalog_id=cid,categories=['Transformer','其他'],assignments=[dict(file_id=f,category=1 if i%2 else 2,uncertain=i==0) for i,f in enumerate(members)],note='验收标记：松柏')
    assert dispatch(sb,'submit_classification',**args).startswith('OK')
    return sb,args


def test_plan_is_complete_persistent_local_table_and_reuses_cards(tmp_path,monkeypatch):
    sb,args=ready(tmp_path,19)
    assert len(list(tmp_path.glob('*.pdf')))==19 and not (tmp_path/'按方法').exists()
    assert sb.runtime.outputs[-1].count('(file:F')==19 and '**待核实**' in sb.runtime.outputs[-1]
    data=json.loads(json.dumps(sb.artifacts.export()))
    sb.artifacts=ArtifactStore(sb,data)
    monkeypatch.setattr('tools.fs_read._pdf_pages',lambda *a,**kw:pytest.fail('cached cards must not reparse'))
    result=dispatch(sb,'paper_profiles',catalog_id=args['catalog_id'],page=1)
    assert '缓存复用=16' in result and len(result)<16000
    args['assignments'].pop()
    assert dispatch(sb,'submit_classification',**args).startswith('ERROR')


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
    sb.runtime=TaskRuntime('文献按照核心方法分类',{},sb,'r')
    assert dispatch(sb,'paper_profiles').startswith('OK')
    cid=next(iter(sb.artifacts.data['catalogs']));fid=sb.artifacts.data['catalogs'][cid]['members'][0]
    args=dict(catalog_id=cid,categories=['待核实'],assignments=[dict(file_id=fid,category=1)])
    assert '必须标 uncertain' in dispatch(sb,'submit_classification',**args)
    args['assignments'][0]['uncertain']=True
    assert dispatch(sb,'submit_classification',**args).startswith('OK')
