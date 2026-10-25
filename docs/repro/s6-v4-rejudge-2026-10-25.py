"""Re-judge saved live results with judgement v3 and v4 side by side (S6a). Read-only, no API.

    .venv/bin/python docs/repro/s6-v4-rejudge-2026-10-25.py [glob ...]

Specs come from each result's own manifest.json (the cases as frozen for that run);
manifests from before expectations were saved (round 20 and earlier) take the
case of the same id from eval.live_policy.SUITES, whose expectations are frozen;
v4 states are projected from the saved turns by agent/states.py. Prints totals and
every case where the two judgements disagree.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.judge import judge_suite, load_trace  # noqa: E402
from eval.live_policy import SUITES  # noqa: E402

FROZEN = {c['id']: c for cases in SUITES.values() for c in cases}


def rejudge(out):
    manifest = json.loads((out / 'manifest.json').read_text())
    report = json.loads((out / 'report.json').read_text())
    specs = {c['id']: c if 'expect' in c else FROZEN.get(c['id'], c) for c in manifest['cases']}
    return judge_suite(specs, report, lambda cid: [load_trace(out / cid / f'turn_{i}.trace.jsonl')
                                                   for i in range(1, len(specs.get(cid, {}).get('turns', [])) + 1)])


def main(patterns):
    dirs = sorted({d for p in patterns for d in (ROOT / 'eval/results').glob(p)
                   if (d / 'manifest.json').is_file() and (d / 'report.json').is_file()})
    total, v3, v4, diffs, errors = 0, 0, 0, [], Counter()
    for out in dirs:
        try:
            j = rejudge(out)
        except Exception as exc:  # old layouts the current judge cannot read
            errors[type(exc).__name__] += 1
            continue
        for c in j['cases']:
            total += 1; v3 += c['overall_accepted']; v4 += c['accepted_v4']
            if c['overall_accepted'] != c['accepted_v4']:
                states = [t['dimensions']['status_v4']['states'] for t in c['turns']]
                diffs.append((out.name, c['id'], c['overall_accepted'], c['accepted_v4'],
                              [t['status'] for t in c['turns']],
                              [(s['flow_state'], s['result_state'], s['verification']['mechanical'],
                                s['verification']['semantic'], s['next_action'], s.get('business')) for s in states],
                              c['failed_dimensions'], c['failed_dimensions_v4']))
    print(f'批次 {len(dirs)}，读不了 {dict(errors)}；场景次数 {total}：v3 通过 {v3}，v4 通过 {v4}，不一致 {len(diffs)}')
    for d in diffs:
        print(f'- {d[0]} / {d[1]}：v3 {"过" if d[2] else "不过"} → v4 {"过" if d[3] else "不过"}；'
              f'v3 状态 {d[4]}；v4 {d[5]}；v3 失败 {d[6]}；v4 失败 {d[7]}')


if __name__ == '__main__':
    main(sys.argv[1:] or ['policy_live_*'])
