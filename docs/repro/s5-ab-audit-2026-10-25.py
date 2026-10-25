"""Read Round 40 evidence only. No API calls, product imports, or input mutations.
Run: .venv/bin/python /tmp/filebutler-s5-ab-audit-20261025.py [eval/results]
"""
import collections
import math
import json
from pathlib import Path
import statistics
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / 'eval/results'
rows = {}
groups = {g: dict(tool_calls=0, failed_tool_calls=0, error_steps=0, error_turns=0,
                  trace_llm_attempts=0, trace_llm_failures=0, failed_tools=collections.Counter()) for g in ('A', 'B')}
manifests = []
suite_reports = []
for directory in sorted(root.glob('policy_live_20261025_40_r*_*_*')):
    _, _, _, _, rep, group, suite = directory.name.split('_')
    report = json.loads((directory / 'report.json').read_text())
    judgment = json.loads((directory / 'judgement.json').read_text())
    manifests.append(json.loads((directory / 'manifest.json').read_text()))
    suite_reports.append(dict(path=str(directory.relative_to(root)), complete=report['suite_complete'],
                              accepted=judgment['cases_accepted'], cases=judgment['cases_total']))
    judged = {case['id']: case for case in judgment['cases']}
    for case in report['cases']:
        key = rep, group, suite, case['id']
        errors = 0
        for trace in (directory / case['id']).glob('*.trace.jsonl'):
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            calls = [e for e in events if 'tool' in e and 'event' not in e]
            bad = [e for e in calls if not e.get('ok')]
            attempts = [e for e in events if e.get('event') == 'llm_request' and e.get('phase') == 'start']
            failed_attempts = [e for e in events if e.get('event') == 'llm_request' and e.get('phase') == 'end' and not e.get('ok')]
            errors += len(bad)
            metrics = groups[group]
            metrics['tool_calls'] += len(calls)
            metrics['failed_tool_calls'] += len(bad)
            metrics['error_steps'] += len({e['step'] for e in bad})
            metrics['error_turns'] += bool(bad)
            metrics['trace_llm_attempts'] += len(attempts)
            metrics['trace_llm_failures'] += len(failed_attempts)
            metrics['failed_tools'].update(e['tool'] for e in bad)
        rows[key] = dict(rep=rep, group=group, suite=suite, case=case['id'],
                         accepted=judged[case['id']]['overall_accepted'],
                         failed_dimensions=judged[case['id']]['failed_dimensions'],
                         error_type=case.get('error_type'), turns=len(case['turns']),
                         requests=sum(t.get('actual_requests', 0) or 0 for t in case['turns']),
                         cost=sum(t.get('cost', 0) or 0 for t in case['turns']),
                         active_seconds=sum(t.get('active_seconds', 0) or 0 for t in case['turns']),
                         failed_tool_calls=errors,
                         nonstatus_dimensions_pass=all(t['dimensions'][d]['passed'] for t in judged[case['id']]['turns'] for d in ('files', 'answer', 'constraints')),
                         evidence=str(directory.relative_to(root)))

def aggregate(selected):
    return dict(cases=len(selected), accepted=sum(r['accepted'] for r in selected),
                requests=sum(r['requests'] for r in selected), cost=sum(r['cost'] for r in selected),
                median_case_requests=statistics.median(r['requests'] for r in selected),
                median_case_cost=statistics.median(r['cost'] for r in selected),
                active_seconds=sum(r['active_seconds'] for r in selected),
                failed_tool_calls=sum(r['failed_tool_calls'] for r in selected))

a = {(rep, suite, cid): r for (rep, g, suite, cid), r in rows.items() if g == 'A'}
b = {(rep, suite, cid): r for (rep, g, suite, cid), r in rows.items() if g == 'B'}
paired = sorted(a.keys() & b.keys())
complete = [key for key in paired if not a[key]['error_type'] and not b[key]['error_type']]
discordant = [dict(rep=k[0], suite=k[1], case=k[2], A=a[k], B=b[k]) for k in complete if a[k]['accepted'] != b[k]['accepted']]
comparison = collections.Counter((bool(a[k]['accepted']), bool(b[k]['accepted'])) for k in complete)
n_discordant = len(discordant)
result = dict(source_root=str(root), planned_case_runs_per_group=51,
              groups={g: dict(**groups[g], report_metrics=aggregate([r for r in rows.values() if r['group'] == g]),
                              failures=[r for r in rows.values() if r['group'] == g and not r['accepted']]) for g in ('A', 'B')},
              matched_keys_including_infrastructure=len(paired), completed_pairs=len(complete),
              complete_pair_metrics={g: aggregate([(a if g == 'A' else b)[k] for k in complete]) for g in ('A', 'B')},
              complete_pair_table={str(k): v for k, v in comparison.items()}, discordant_pairs=discordant,
              missing_in_B=[a[k] for k in sorted(a.keys() - b.keys())],
              infrastructure_pairs=[dict(A=a[k], B=b[k]) for k in paired if k not in complete],
              exact_two_sided_discordance_probability=min(1.0, 2 * sum(math.comb(n_discordant, k) for k in range(min(comparison[(True, False)], comparison[(False, True)]) + 1)) / (2 ** n_discordant)),
              statistical_limit='3 discordant pairs all favor B gives p=0.25 under an independent-pair null; repeated fixed cases and infrastructure missingness limit inference; no equivalence is established.',
              distinct_scenarios=len({(r['suite'], r['case']) for r in rows.values()}),
              product_code_snapshot_count=len({json.dumps(m['code_sha256'], sort_keys=True) for m in manifests}),
              suite_reports=suite_reports)
print(json.dumps(result, ensure_ascii=False, indent=2))
