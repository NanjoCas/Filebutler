"""Run three paired, alternating-order real-model acceptance comparisons.

python -m eval.compare_runtime --out eval/results/runtime_comparison --price-cap .75
External API access and charges require the user's authorization. Each child
uses a fresh temporary corpus and browser. Original data is never mutated.
"""
import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--price-cap',type=float,default=.75)
    args=ap.parse_args();base=args.out.resolve();base.mkdir(parents=True,exist_ok=False)
    report=dict(price_cap=args.price_cap,estimated_cost=0.0,runs=[],limitations=[
        'Three paired runs; descriptive medians and ranges, not a statistical speed guarantee.',
        'Baseline is current code with local_runtime=False and prompt=v5_latency, not a checkout of old source.',
        'Each run starts a new process; shared OS/provider cache warming is reported, not eliminated.'])
    order=[('baseline',1),('runtime',1),('runtime',2),('baseline',2),('baseline',3),('runtime',3)]
    for variant,rep in order:
        remaining=args.price_cap-report['estimated_cost']
        if remaining<=0:break
        out=base/f'{variant}_{rep}'
        cmd=[sys.executable,'-m','eval.acceptance','--live','--out',str(out),'--price-cap',str(min(.30,remaining))]
        if variant=='baseline':cmd+=['--baseline']
        print(json.dumps(dict(start=variant,rep=rep,remaining=remaining)),flush=True)
        completed=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=700)
        (base/f'{variant}_{rep}.log').write_text(completed.stdout,encoding='utf-8')
        if not (out/'report.json').exists():
            report['error']=f'{variant}_{rep} did not produce a report';break
        result=json.loads((out/'report.json').read_text())
        report['estimated_cost']+=result['estimated_cost']
        report['runs'].append(dict(variant=variant,rep=rep,result=str(out/'report.json'),exit_code=completed.returncode,
                                   passed=result.get('passed'),cost=result['estimated_cost'],checks=result['checks'],cases=result['cases']))
        (base/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(finished=variant,rep=rep,cost=result['estimated_cost'],
                              cases=[dict(name=c['name'],active=c['result'].get('active_seconds'),steps=c['result'].get('steps'),tools=c['tool_counts']) for c in result['cases']]),ensure_ascii=False),flush=True)
        if result.get('error') or len(result['cases'])!=4:
            report['error']=result.get('error','incomplete acceptance');break
    summary={}
    for variant in ('baseline','runtime'):
        runs=[r for r in report['runs'] if r['variant']==variant]
        rows={}
        for name in ('topic_search','content_dedup','dedup_after_undo','long_conversation'):
            cases=[c for r in runs for c in r['cases'] if c['name']==name]
            if not cases:continue
            def distribution(fn):
                values=[fn(c) for c in cases]
                return dict(median=statistics.median(values),min=min(values),max=max(values))
            rows[name]=dict(n=len(cases),active_seconds=distribution(lambda c:c['result']['active_seconds']),
                            steps=distribution(lambda c:c['result']['steps']),
                            tool_calls=distribution(lambda c:c['result']['tool_calls']),
                            prompt_tokens=distribution(lambda c:c['prompt_tokens']),
                            output_tokens=distribution(lambda c:c['completion_tokens']),
                            cost=distribution(lambda c:c['result']['cost']))
        summary[variant]=rows
    report['summary']=summary
    (base/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(estimated_cost=report['estimated_cost'],summary=summary,error=report.get('error')),ensure_ascii=False),flush=True)
    if report.get('error') or len(report['runs'])!=6:raise SystemExit(1)
    if not all(r['passed'] for r in report['runs'] if r['variant']=='runtime'):raise SystemExit(1)


if __name__=='__main__':main()
