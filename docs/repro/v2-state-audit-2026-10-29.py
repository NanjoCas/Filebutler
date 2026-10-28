"""Read-only old/new task-state comparison. Original records and judgements stay unchanged.

Historical reports have no question-table snapshot: their explicit waiting report
is used as the recorded question fact, never to assert a new model acceptance rate.
The real task DB is opened mode=ro and uses its actual question rows.
"""
import argparse
import ast
import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.task import task_state, is_legacy, normalized
from agent.states import project


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = subprocess.check_output(['git', 'show', '5e17c81:agent/task.py'], cwd=ROOT, text=True)
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'task_state')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<old-task-state>', 'exec'), namespace)
    old = namespace['task_state']
    old_states = {}
    exec(subprocess.check_output(['git', 'show', '5e17c81:agent/states.py'], cwd=ROOT, text=True), old_states)
    differences, errors, skipped = [], [], []
    counts = dict(historical_cases=0, historical_turns=0, real_tasks=0, real_outcomes=0,
                  real_outcome_projection_comparisons=0)

    def compare(task, waiting, location):
        task = normalized(copy.deepcopy(task))
        if is_legacy(task):
            skipped.append(dict(location=location, reason='legacy protocol'))
            return
        try:
            before, after = old(task), task_state(task, waiting=waiting)
            if before != after:
                expected = bool(task.get('unresolved_questions')) != waiting
                differences.append(dict(location=location, before=before, after=after, waiting=waiting,
                                        historical_notes=bool(task.get('unresolved_questions')), expected=expected))
        except Exception as exc:
            errors.append(dict(location=location, error=type(exc).__name__))

    for path in sorted((ROOT / 'eval/results').glob('policy_live_*/report.json')):
        report = json.loads(path.read_text())
        for case in report.get('cases', []):
            counts['historical_cases'] += 1
            for index, turn in enumerate(case.get('turns', []), 1):
                if not turn.get('task'): continue
                counts['historical_turns'] += 1
                waiting = turn.get('stop_reason') == 'waiting_input' and bool((turn.get('report') or {}).get('question'))
                compare(turn['task'], waiting, f'{path.parent.name}/{case["id"]}/{index}')

    path = ROOT / 'sandbox/.oplog/tasks.sqlite3'
    if path.exists():
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute('SELECT * FROM task_revisions'):
                task = json.loads(row['data_json'])
                waiting = db.execute("SELECT 1 FROM questions WHERE root=? AND session=? AND task_id=? "
                                     "AND status IN ('open','suspended')", (row['root'], row['session'], row['id'])).fetchone() is not None
                counts['real_tasks'] += 1
                compare(task, waiting, f'real/{row["id"]}/{row["revision"]}')
            for row in db.execute('SELECT * FROM run_outcomes'):
                counts['real_outcomes'] += 1
                outcome = json.loads(row['data_json'])
                stored = outcome.get('states') or {}
                report = dict(state=stored.get('old_state'), result=stored.get('business'))
                task_row = db.execute('SELECT data_json FROM task_revisions WHERE id=? AND root=? AND session=? '
                                      'ORDER BY revision DESC LIMIT 1', (outcome.get('task_id'), row['root'], row['session'])).fetchone()
                task = normalized(json.loads(task_row[0])) if task_row else None
                inputs = (outcome.get('stop_reason'), report, task)
                before, after = old_states['project'](*copy.deepcopy(inputs)), project(*copy.deepcopy(inputs))
                counts['real_outcome_projection_comparisons'] += 1
                if before != after:
                    differences.append(dict(location='outcome/' + row['run_id'], before=before, after=after, expected=False))
    result = dict(counts=counts, differences=differences, errors=errors, skipped=skipped,
                  unexpected=[d for d in differences if not d['expected']],
                  limitation='Historical waiting is reconstructed from the saved question report. Outcome projection compares both reducers on the same latest saved task snapshot; it is not a replay of historical model decisions.')
    (args.out / 'state-comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(dict(counts=counts, differences=len(differences), errors=len(errors), skipped=len(skipped),
                          unexpected=len(result['unexpected'])), ensure_ascii=False))
    return int(bool(errors or result['unexpected']))


if __name__ == '__main__':
    raise SystemExit(main())
