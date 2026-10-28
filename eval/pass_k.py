"""pass^k over repeated live runs (2.0 P1-C); read-only over saved results, no API.

    .venv/bin/python -m eval.pass_k eval/results/<run_r1> eval/results/<run_r2> ... [--rejudge] [--json out.json]

Each directory is one run of a suite (``report.json``, ``manifest.json``,
``judgement.json``). Cases are grouped by id across directories. With
``--rejudge`` every run is judged again by the current eval/judge.py (the saved
judgement is left as it is). Reported per judge (v3 and v4):

- pass^1   accepted case-runs / executed case-runs
- pass^k   cases accepted in every one of their k runs / cases executed k times
- executed case-runs that ran to the end without an infrastructure block
- uncovered  case-runs the manifest lists but the report lacks (budget stop, crash)
- environment failures  case-runs blocked by infrastructure (network, auth, config)
- option_not_offered  case-runs where the scripted choice found no matching option;
  never accepted under v4, still in the denominator
- repair_requests  model requests whose every executed call failed
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from eval.judge import judge_suite, load_trace


def _judgement(run, rejudge):
    report = json.loads((run / 'report.json').read_text())
    manifest = json.loads((run / 'manifest.json').read_text())
    specs = {c['id']: c for c in manifest.get('cases', [])}
    if rejudge or not (run / 'judgement.json').exists():
        judgement = judge_suite(specs, report, lambda cid: [load_trace(run / cid / f'turn_{i}.trace.jsonl')
                                                            for i in range(1, len(specs.get(cid, {}).get('turns', [])) + 1)])
    else:
        judgement = json.loads((run / 'judgement.json').read_text())
    return report, specs, judgement


def summarize(runs, rejudge=False):
    per_case = defaultdict(lambda: dict(runs=0, v3=0, v4=0, option_not_offered=0, repair_requests=0, statuses=[]))
    uncovered, environment = [], []
    for run in runs:
        report, specs, judgement = _judgement(Path(run), rejudge)
        rows = {r['id']: r for r in report.get('cases', [])}
        for cid in specs:
            row = rows.get(cid)
            if row is None:
                uncovered.append(f'{Path(run).name}/{cid}')
            elif row.get('status') == 'blocked':
                environment.append(f'{Path(run).name}/{cid}')
        for case in judgement['cases']:
            if rows.get(case['id'], {}).get('status') == 'blocked': continue
            entry = per_case[case['id']]
            entry['runs'] += 1
            entry['v3'] += bool(case['overall_accepted'])
            entry['v4'] += bool(case['accepted_v4'])
            entry['option_not_offered'] += any(t['diagnostics'].get('option_not_offered') for t in case['turns'])
            entry['repair_requests'] += sum(t['diagnostics'].get('repair_requests', 0) for t in case['turns'])
            entry['statuses'].append([t['status'] for t in case['turns']])
    executed = sum(e['runs'] for e in per_case.values())
    k = max((e['runs'] for e in per_case.values()), default=0)
    full = {cid: e for cid, e in per_case.items() if e['runs'] == k}

    def rate(judge):
        return dict(pass_1=f"{sum(e[judge] for e in per_case.values())}/{executed}",
                    pass_k=f"{sum(e[judge] == e['runs'] for e in full.values())}/{len(full)}")
    return dict(k=k, executed=executed, uncovered=uncovered, environment_failures=environment,
                v3=rate('v3'), v4=rate('v4'),
                v3_pass_v4_fail=sorted(cid for cid, e in per_case.items() if e['v3'] > e['v4']),
                option_not_offered=sum(e['option_not_offered'] for e in per_case.values()),
                repair_requests=sum(e['repair_requests'] for e in per_case.values()),
                cases=dict(sorted(per_case.items())))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', nargs='+', type=Path)
    ap.add_argument('--rejudge', action='store_true', help='judge again with the current eval/judge.py')
    ap.add_argument('--json', type=Path, help='write the full summary here (must not exist)')
    args = ap.parse_args(argv)
    summary = summarize(args.runs, args.rejudge)
    if args.json:
        if args.json.exists(): ap.error(f'{args.json} 已存在')
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != 'cases'}, ensure_ascii=False, indent=2))
    for cid, e in summary['cases'].items():
        print(f"{cid}: v3 {e['v3']}/{e['runs']} v4 {e['v4']}/{e['runs']}"
              + (f" option_not_offered={e['option_not_offered']}" if e['option_not_offered'] else ''))


if __name__ == '__main__':
    main()
