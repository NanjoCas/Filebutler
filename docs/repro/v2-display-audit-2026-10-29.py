"""Read-only old/new comparison of the shown state (2.0 P1-D). Records stay unchanged.

Old: the report's saved ``display``, or for reports saved without one the
reducer of the base commit (reporting.display_state at 41b4586); task cards use
the old page rule (web/labels.js taskState). New: states.display_of over the
same saved facts. The real task DB is opened mode=ro.

    .venv/bin/python docs/repro/v2-display-audit-2026-10-29.py --root <repo with eval/results> --out <new dir>
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
from agent.states import display_of, project
from agent.task import normalized
from eval.judge import turn_states

BASE = '41b4586'
TASK_STATES = {'running': '待继续', 'completed': '已完成', 'partial': '没有完成', 'needs_review': '请确认结果',
               'waiting_input': '等你回答', 'interrupted': '已中断', 'cancelled': '已取消'}
DISPLAY_LABELS = {'done': '已完成', 'answered': '已回答', 'chosen': '已按你的选择处理', 'confirm': '请确认结果',
                  'waiting': '等你回答', 'unfinished': '没有完成', 'cancelled': '已取消'}


def old_display_state():
    source = subprocess.check_output(['git', 'show', f'{BASE}:agent/reporting.py'], cwd=ROOT, text=True)
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'display_state')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<old-display-state>', 'exec'), namespace)
    return namespace['display_state']


def old_task_label(task):
    changed = task.get('operation_history') or task.get('operations')
    return '已回答' if task.get('status') == 'needs_review' and not changed else TASK_STATES.get(task.get('status'), task.get('status'))


def new_task_label(task):
    if task.get('status') in {'running', 'interrupted'}:
        return TASK_STATES.get(task['status'])
    return DISPLAY_LABELS[display_of(project(None, dict(state=task.get('status')), task))]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=ROOT, help='checkout whose eval/results and sandbox are read')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    old = old_display_state()
    differences, errors = [], []
    counts = dict(historical_cases=0, turns_saved_display=0, turns_recomputed=0, turns_without_report_state=0,
                  real_tasks=0, real_outcomes=0, real_outcomes_without_state=0)

    def differ(location, before, after, **extra):
        if before != after: differences.append(dict(location=location, before=before, after=after, **extra))

    for path in sorted((args.root / 'eval/results').glob('policy_live_*/report.json')):
        for case in json.loads(path.read_text()).get('cases', []):
            counts['historical_cases'] += 1
            for index, turn in enumerate(case.get('turns', []), 1):
                report, location = turn.get('report') or {}, f'{path.parent.name}/{case["id"]}/{index}'
                try:
                    if report.get('display'):
                        before = report['display']; counts['turns_saved_display'] += 1
                    elif report.get('state'):
                        before = 'chosen' if report.get('result') == 'by_choice' else old(report['state'], turn.get('task'))
                        counts['turns_recomputed'] += 1
                    else:
                        counts['turns_without_report_state'] += 1; continue
                    differ(location, before, display_of(turn_states(turn)[0]), stop_reason=turn.get('stop_reason'))
                except Exception as exc:
                    errors.append(dict(location=location, error=type(exc).__name__))

    path = args.root / 'sandbox/.oplog/tasks.sqlite3'
    if path.exists():
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute('SELECT * FROM task_revisions'):
                counts['real_tasks'] += 1
                task = normalized(json.loads(row['data_json']))
                try: differ(f'task/{row["id"]}/{row["revision"]}', old_task_label(task), new_task_label(copy.deepcopy(task)))
                except Exception as exc: errors.append(dict(location=f'task/{row["id"]}', error=type(exc).__name__))
            for row in db.execute('SELECT * FROM run_outcomes'):
                counts['real_outcomes'] += 1
                outcome = json.loads(row['data_json'])
                states = outcome.get('states')
                if not states or not states.get('old_state'):
                    counts['real_outcomes_without_state'] += 1; continue
                # Outcomes keep no report: the old reducer runs on the saved old state and the
                # latest saved snapshot of the run's task, as the P0-A audit does.
                task_row = db.execute('SELECT data_json FROM task_revisions WHERE id=? AND root=? AND session=? '
                                      'ORDER BY revision DESC LIMIT 1', (outcome.get('task_id'), row['root'], row['session'])).fetchone()
                task = normalized(json.loads(task_row[0])) if task_row else None
                before = 'chosen' if states.get('business') == 'by_choice' else old(states['old_state'], task)
                differ('outcome/' + row['run_id'], before, display_of(states), stop_reason=outcome.get('stop_reason'))
    result = dict(counts=counts, differences=differences, errors=errors,
                  limitation='Saved facts are re-reduced; this is not a replay of model decisions. '
                             'Task cards compare page labels, so a renamed label shows as a difference.')
    (args.out / 'display-comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(dict(counts=counts, differences=len(differences), errors=len(errors)), ensure_ascii=False))
    return int(bool(errors or differences))


if __name__ == '__main__':
    raise SystemExit(main())
