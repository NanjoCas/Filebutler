"""Isolated classification + zero-model apply acceptance. Never mutates user data.

python -m eval.classification --live --out eval/results/classification_<unique>
Uses a temporary copy of current sandbox/文献 and the existing authorized model.
"""
import argparse
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from eval.checker import snapshot,files_preserved
from main import ROOT,load_config
from safety.confirm import Confirmer
from safety.oplog import OpLog,undo
from safety.sandbox import Sandbox
from web_confirm import build_confirmation
from web_render import FileCatalog,render_markdown
from tools.classification import category_directory


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true',required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--price-cap',type=float,default=.15)
    args=ap.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    cfg=load_config(ROOT/'config.yaml');cfg.update(cost_budget=args.price_cap, task_runtime=False, local_runtime=True, prompt='v7_classification')
    source=Path(cfg['sandbox'])/'文献';original=snapshot(source,True)
    report=dict(model=cfg['model'],prompt=cfg['prompt'],checks=[],cases=[],estimated_cost=0,
                limitations=['Single run on a temporary copy; not a controlled A/B latency experiment.',
                             'Legacy v7 acceptance only; does not validate the current task controller or semantic accuracy.'])
    def check(name,ok):report['checks'].append(dict(name=name,ok=bool(ok)))
    def record(name,result,requests):
        row=asdict(result);row.pop('messages')
        report['estimated_cost']+=result.cost
        report['cases'].append(dict(name=name,result=row,requests=requests))
    with tempfile.TemporaryDirectory(prefix='filebutler-classification-') as tmp:
        sb=Sandbox(Path(tmp)/'sandbox');shutil.copytree(source,sb.root/'文献',symlinks=True)
        initial=snapshot(sb.root,True);data=out/'artifacts.json'
        store=ArtifactStore(sb,on_change=lambda value:data.write_text(json.dumps(value,ensure_ascii=False,indent=2)))
        llm=LLM(cfg);requests=[];old_chat=llm.chat
        def chat(*a,**kw):
            observer=kw.get('on_request')
            def event(e):
                requests.append(e)
                if observer:observer(e)
            kw['on_request']=event
            return old_chat(*a,**kw)
        llm.chat=chat
        first=run_agent('帮我把文献按照核心方法分类',llm,sb,cfg,Console(quiet=True),artifacts=store,
                        trace_path=out/'classification.trace.jsonl',oplog=OpLog(sb,'classify'),confirmer=Confirmer('always',ask=lambda _: 'n'))
        record('classification',first,requests)
        check('classification_completed',first.stop_reason=='done')
        plans=store.data['classification_plans'];check('plan_saved',bool(plans))
        if plans:
            pid=next(reversed(plans));plan=plans[pid]
            pdfs=[p for p in sb.root.rglob('*.pdf') if not any(x.startswith('.') for x in p.relative_to(sb.root).parts)]
            check('plan_complete',len(plan['assignments'])==len(pdfs))
            check('classification_readonly',snapshot(sb.root,True)==initial)
            check('classification_all_references', 'unavailable-reference' not in render_markdown(first.final,FileCatalog(sb,store,'test')))
            instruction=f"把这 {len(plan['categories'])} 类落成实际文件夹"
            history=first.messages+[dict(role='user',content=instruction)]
            projected,context=prepare_history(history,cfg,store);report['context']=context
            # Simulate persisted-store reload before the follow-up.
            store=ArtifactStore(sb,json.loads(data.read_text()),on_change=lambda value:data.write_text(json.dumps(value,ensure_ascii=False,indent=2)))
            approvals=[]
            def approve(request):
                approvals.append(build_confirmation(request,sb));return 'y'
            class NoModel:
                model=cfg['model'];total_tokens=0;prices=llm.prices;cost=0;cache_hit_tokens=0
                def chat(self,*a,**kw):raise AssertionError('execution must not call the model')
            second=run_agent(instruction,NoModel(),sb,cfg,Console(quiet=True),artifacts=store,history=projected,
                             trace_path=out/'apply.trace.jsonl',oplog=OpLog(sb,'apply'),confirmer=Confirmer('always',ask_action=approve))
            record('apply',second,[])
            (out/'confirmation.json').write_text(json.dumps(approvals,ensure_ascii=False,indent=2))
            check('apply_completed',second.stop_reason=='done')
            check('apply_zero_model',second.tokens==0 and second.llm_seconds==0)
            check('one_confirmation',len(approvals)==1)
            check('confirmation_complete',len(approvals)==1 and approvals[0]['counts']['affected']==len(pdfs))
            check('files_preserved',files_preserved(initial,snapshot(sb.root,True)))
            check('applied_references_valid','unavailable-reference' not in render_markdown(second.final,FileCatalog(sb,store,'test')))
            for item in plan['assignments']:
                p,row=store.resolve(item['file_id']);assert row['version']==plan['versions'][item['file_id']]
                assert p.parent.name==category_directory(item['category'],plan['categories'][item['category']-1])
            check('every_destination_correct',True)
            undo(sb,'apply',on_restore=store.relocate)
            check('undo_exact_restore',snapshot(sb.root,True)==initial)
        check('source_unchanged',snapshot(source,True)==original)
    report['passed']=all(c['ok'] for c in report['checks'])
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
