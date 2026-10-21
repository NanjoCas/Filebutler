"""Frozen intent checks plus isolated live task workflows. No user files used.

python -m eval.phase1 --live --split dev --out eval/results/phase1_dev_<id>
python -m eval.phase1 --live --split heldout --out eval/results/phase1_holdout_<id>
"""
import argparse
import json
import tempfile
import time
import hashlib
from pathlib import Path

import yaml
from rich.console import Console

from agent.artifacts import ArtifactStore
from agent.llm import LLM
from agent.loop import run_agent
from agent.prompts import SYSTEM_PROMPTS
from agent.task_runtime import TaskRuntime
from main import ROOT, load_config
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox


def check_intent(spec, expected):
    # This is a structural count, never an oracle for semantic goal coverage.
    checks = {'goal_count': len(spec['goals']) >= expected['min_goals']}
    effects = set(spec['allowed_effects'])
    forbidden = {e for c in spec['constraints'] for e in c['forbidden_effects']}
    rules = [r for c in spec['constraints'] for r in c.get('rules', [])]
    if expected.get('readonly'):
        checks['readonly'] = not effects or effects <= forbidden
    if expected.get('effects'):
        checks['required_effects'] = set(expected['effects']) <= effects - forbidden
    if expected.get('forbidden'):
        checks['excluded_effects'] = not (set(expected['forbidden']) & (effects - forbidden))
    if expected.get('scope'):
        checks['scope'] = expected['scope'] in spec['read_roots']
    required = expected.get('rules', []) + ([expected['rule']] if expected.get('rule') else [])
    for rule in required:
        checks['rule:'+rule] = any(r['predicate'] == rule for r in rules)
    for index, requirement in enumerate(expected.get('rule_contracts', [])):
        checks[f'rule_contract:{index}'] = any(all(r.get(key) == value for key, value in requirement.items()) for r in rules)
    if 'max_count' in expected:
        checks['count_limit'] = any(r['predicate']=='max_count' and r['limit']==expected['max_count'] for r in rules)
    if expected.get('clarify'):
        checks['clarification'] = bool(spec['unresolved_questions']) and not effects
    if expected.get('semantic'):
        checks['semantic_boundary'] = any(g['semantic_review'] for g in spec['goals'])
    return checks


def fixture(root):
    for name in ('Inbox','Archive','audio','receipts','receipts/保留','照片','文献','实验记录'):
        (root/name).mkdir(parents=True, exist_ok=True)
    (root/'Inbox/a.txt').write_text('alpha')
    (root/'Inbox/b.txt').write_text('beta')
    (root/'Inbox/keep.pdf').write_bytes(b'not parsed in filesystem workflow')
    (root/'Inbox/empty').mkdir()
    (root/'Inbox/nonempty').mkdir()
    (root/'Inbox/nonempty/keep.txt').write_text('preserve')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true', required=True)
    ap.add_argument('--split', choices=['dev','heldout'], required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--total-cap', type=float, default=1.5)
    args=ap.parse_args(); args.out.mkdir(parents=True, exist_ok=False)
    cfg=load_config(ROOT/'config.yaml')
    cfg.update(llm_retries=0, llm_timeout=60, max_steps=7, cost_budget=.15)
    manifest=yaml.safe_load((ROOT/'eval/tasks_phase1.yaml').read_text())
    report=dict(model=cfg['model'], split=args.split, cases=[], cost=0, limitations=[
        'Single sample per case; goal_count checks quantity only. Structural checks cannot establish semantic understanding.',
        'No latency/cost improvement claim without a paired baseline.'])
    report['manifest_sha256']=hashlib.sha256((ROOT/'eval/tasks_phase1.yaml').read_bytes()).hexdigest()
    report['code_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [ROOT/'agent/task.py',ROOT/'agent/task_runtime.py',ROOT/'agent/constraints.py',ROOT/'agent/prompts.py']}
    (args.out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    def save():
        report['passed']=sum(c['passed'] for c in report['cases'])
        report['attempted']=len(report['cases'])
        report['total']=sum(c.get('status')!='blocked' for c in report['cases'])
        report['suite_complete']=not report.get('infrastructure_blocked') and not report.get('budget_stopped') and len(report['cases'])==len(
            [c for c in manifest['cases'] if c['split']==args.split])+2
        for kind in ('intent','workflow'):
            rows=[c for c in report['cases'] if c['kind']==kind]
            report[kind+'_summary']=dict(passed=sum(c['passed'] for c in rows),
                evaluated=sum(c.get('status')!='blocked' for c in rows),attempted=len(rows))
        (args.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for case in [c for c in manifest['cases'] if c['split']==args.split]:
        if report['cost'] >= args.total_cap:
            report['budget_stopped']=True; break
        llm=LLM(cfg); started=time.perf_counter(); row=dict(id=case['id'],kind='intent',checks={})
        try:
            with tempfile.TemporaryDirectory(prefix='filebutler-phase1-') as tmp:
                sb=Sandbox(tmp); fixture(sb.root); sb.artifacts=ArtifactStore(sb)
                runtime=TaskRuntime(case['instruction'],cfg,sb,case['id'])
                schema=next(s for s in runtime.schemas([]) if s['function']['name']=='set_task')
                response=llm.chat([dict(role='system',content=SYSTEM_PROMPTS['v8_tasks']+
                    '\n本轮为需求解析验收：只调用 set_task 登记任务，不执行其他操作。只根据用户要求登记，不额外添加目标。'),
                    dict(role='user',content=case['instruction'])], [schema])
                calls=[c for c in (response.tool_calls or []) if c.function.name=='set_task']
                if len(calls)!=1: raise ValueError('没有唯一的 set_task 调用')
                result=runtime.dispatch('set_task',calls[0].function.arguments,sb,None,None)
                if not result.startswith('OK'): raise ValueError(result)
                row['task']=runtime.current
                row['checks']=check_intent(runtime.current,case['expect'])
        except Exception as exc:
            row['error']=f'{type(exc).__name__}: {exc}'
            if type(exc).__name__ in {'APIConnectionError','APITimeoutError'}:
                row['status']='blocked'
        row.update(passed=bool(row['checks']) and all(row['checks'].values()),
                   seconds=time.perf_counter()-started,cost=llm.cost,tokens=llm.total_tokens)
        report['cost']+=llm.cost; report['cases'].append(row); save()
        print(json.dumps({k:row[k] for k in ('id','passed','seconds','cost')},ensure_ascii=False),flush=True)
        if row.get('error') and llm.total_tokens == 0:
            report['infrastructure_blocked']=row['error']; break
    if not report.get('infrastructure_blocked') and report['cost'] < args.total_cap:
        for name,instruction,expect in [
            ('flow_move','统计 Inbox 根目录的 txt，再把这些 txt 移到现有 Archive，PDF 和子目录保持原样，不创建目录，不回收文件。',
             lambda r:(r/'Archive/a.txt').is_file() and (r/'Archive/b.txt').is_file() and (r/'Inbox/keep.pdf').is_file() and (r/'Inbox/nonempty/keep.txt').is_file()),
            ('flow_empty','只将 Inbox 中的空目录移入回收区，非空目录和所有文件保持原样。',
             lambda r:not (r/'Inbox/empty').exists() and (r/'Inbox/nonempty/keep.txt').is_file() and (r/'Inbox/a.txt').is_file())]:
            if report['cost'] >= args.total_cap: break
            llm=LLM(cfg); row=dict(id=name,kind='workflow',checks={}); started=time.perf_counter()
            try:
                with tempfile.TemporaryDirectory(prefix='filebutler-phase1-flow-') as tmp:
                    sb=Sandbox(tmp); fixture(sb.root)
                    result=run_agent(instruction,llm,sb,cfg,Console(quiet=True),oplog=OpLog(sb,name),
                        confirmer=Confirmer('always',ask=lambda _:'y'),trace_path=args.out/(name+'.trace.jsonl'))
                    row['checks']=dict(filesystem=expect(sb.root), task_registered=bool(result.runtime['task_id']),
                        no_false_done=result.stop_reason!='done' or result.runtime['task_status']=='completed')
                    row['runtime']=result.runtime; row['final']=result.final; row['requests']=result.steps
                    # Do not persist hidden model reasoning; keep task outcomes only.
            except Exception as exc:
                row['error']=f'{type(exc).__name__}: {exc}'
                if type(exc).__name__ in {'APIConnectionError','APITimeoutError'}:
                    row['status']='blocked'; report['infrastructure_blocked']=row['error']
            row.update(passed=bool(row['checks']) and all(row['checks'].values()),seconds=time.perf_counter()-started,cost=llm.cost,tokens=llm.total_tokens)
            report['cost']+=llm.cost; report['cases'].append(row);save()
            print(json.dumps({k:row[k] for k in ('id','passed','seconds','cost')},ensure_ascii=False),flush=True)
            if report.get('infrastructure_blocked'): break
    save()
    return 2 if report.get('infrastructure_blocked') else 0 if report['suite_complete'] and report['passed']==report['total'] else 1


if __name__=='__main__': raise SystemExit(main())
