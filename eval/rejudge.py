"""Offline re-judgement of a frozen live_policy result directory; no API, read-only.

python -m eval.rejudge eval/results/policy_live_20261021_01 --out <new dir>

Reads report.json + turn_N.trace.jsonl, applies the current multi-dimension
judge (eval/judge.py) with the expectations declared in eval/live_policy.py,
and writes rejudge.json (+ review_comparison.json when review.json exists) to a
NEW directory outside the source. Every source file is hashed before and after;
nothing in the source directory is opened for writing. Historical scores stay
frozen: the legacy passed_local is copied, never rewritten.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from eval.judge import exit_code, judge_suite, load_trace


def tree_digest(base):
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob('*')) if p.is_file()}


def compare_review(judgement, review):
    """Item-by-item comparison with a post-hoc review.json (not independent gold)."""
    ours_false = set(judgement['false_incomplete'])
    theirs_false = {f"{r['case']}#{r['turn']}" for r in review.get('false_partial_turns', [])}
    rows = dict(
        overall_accepted=dict(review=review.get('overall_accepted'), rejudge=judgement['overall_accepted'],
                              agree=review.get('overall_accepted') == judgement['overall_accepted']),
        physical_groups_passed=dict(review=review.get('physical_groups_passed'), rejudge=judgement['legacy_passed_local'],
                                    agree=review.get('physical_groups_passed') == judgement['legacy_passed_local']),
        false_partial_turns=dict(review=sorted(theirs_false), rejudge=sorted(ours_false),
                                 agree=ours_false == theirs_false,
                                 only_review=sorted(theirs_false - ours_false), only_rejudge=sorted(ours_false - theirs_false)),
        recovered_blockers_shown=dict(
            review='known_issues mentions resolved blockers shown as current (no per-turn list)',
            review_mentions=any('blocker' in k.lower() for k in review.get('known_issues', [])),
            rejudge=judgement['recovered_blockers_shown']),
    )
    undo = sum(c['case_files']['checks'].get('undo_restores_fixture', False) for c in judgement['cases'])
    preserved = sum(c['case_files']['checks'].get('source_contents_preserved', False) for c in judgement['cases'])
    rows['undo_restored_groups'] = dict(review=review.get('undo_restored_groups'), rejudge=undo,
                                        agree=review.get('undo_restored_groups') == undo)
    rows['source_contents_preserved_groups'] = dict(review=review.get('source_contents_preserved_groups'), rejudge=preserved,
                                                    agree=review.get('source_contents_preserved_groups') == preserved)
    lengths = {f"{r['case']}#{r['turn']}": r['characters'] for r in review.get('final_answer_lengths', [])}
    ours_len = {f"{c['id']}#{t['turn']}": t['dimensions']['efficiency']['metrics']['answer_chars']
                for c in judgement['cases'] for t in c['turns']}
    rows['final_answer_lengths'] = dict(agree=all(ours_len.get(k) == v for k, v in lengths.items()),
                                        mismatches={k: [v, ours_len.get(k)] for k, v in lengths.items() if ours_len.get(k) != v})
    return rows


def rejudge(source, out, specs):
    source, out = Path(source).resolve(), Path(out).resolve()
    if out == source or source in out.parents:
        raise SystemExit('output must be outside the source result directory')
    if out.exists():
        raise SystemExit(f'output already exists, refusing to overwrite: {out}')
    before = tree_digest(source)
    report = json.loads((source / 'report.json').read_text(encoding='utf-8'))
    manifest_path = source / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    recorded = {c['id']: c for c in manifest.get('cases', [])}
    used = {}
    for row in report['cases']:
        spec = specs.get(row['id'])
        if spec is None:
            raise SystemExit(f"no expectation declared for case {row['id']}")
        if row['id'] in recorded and recorded[row['id']]['turns'] != spec['turns']:
            raise SystemExit(f"instructions of {row['id']} differ from the recorded run; refusing to re-judge")
        used[row['id']] = spec
    # Judge exactly the recorded suite, so a missing case is an incomplete suite.
    expected = {cid: specs[cid] for cid in recorded} if recorded else used

    def traces_for(case_id):
        n = len(expected[case_id]['turns'])
        return [load_trace(source / case_id / f'turn_{i}.trace.jsonl') for i in range(1, n + 1)]

    judgement = judge_suite(expected, report, traces_for)
    judgement['source'] = str(source)
    judgement['expectations'] = {cid: spec.get('expect') for cid, spec in expected.items()}
    judgement['legacy'] = dict(passed_local=report.get('passed_local'), suite_complete=report.get('suite_complete'),
                               legacy_exit_code=0 if report.get('suite_complete') and report.get('passed_local') == len(expected) else 1)
    out.mkdir(parents=True, exist_ok=False)
    review_path = source / 'review.json'
    if review_path.is_file():
        comparison = compare_review(judgement, json.loads(review_path.read_text(encoding='utf-8')))
        (out / 'review_comparison.json').write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
    after = tree_digest(source)
    judgement['source_unchanged'] = before == after
    judgement['source_files_hashed'] = len(before)
    (out / 'rejudge.json').write_text(json.dumps(judgement, ensure_ascii=False, indent=2))
    if before != after:
        raise SystemExit('source directory changed during re-judgement')
    return judgement


def main(argv=None):
    from eval.live_policy import CASE_SPECS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    j = rejudge(args.source, args.out, CASE_SPECS)
    lines = [f"overall_accepted={j['overall_accepted']} cases_accepted={j['cases_accepted']}/{j['cases_total']} "
             f"legacy_passed_local={j['legacy_passed_local']} legacy_exit_code={j['legacy']['legacy_exit_code']}"]
    for c in j['cases']:
        turns = ' '.join(f"t{t['turn']}:{t['status']}" + ''.join(f"[{n}:FAIL]" for n, d in t['dimensions'].items()
                                                                if d['required'] and not d['passed']) for t in c['turns'])
        lines.append(f"  {'ACCEPT' if c['overall_accepted'] else 'REJECT'} {c['id']}: {turns}")
    lines.append(f"false_incomplete={j['false_incomplete']}")
    lines.append(f"recovered_blockers_shown={j['recovered_blockers_shown']}")
    sys.stdout.write('\n'.join(lines) + '\n')
    return exit_code(j)


if __name__ == '__main__': raise SystemExit(main())
