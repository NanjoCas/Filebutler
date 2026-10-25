"""New code (S6-S7) on a copy of the real task library: every stored task is read, its
state projected, its report rebuilt; nothing is written back."""
import json, sqlite3, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from agent.task import normalized, task_state
from agent.states import project
from agent.reporting import build_report
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
version = db.execute('PRAGMA user_version').fetchone()[0]
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
t0 = time.perf_counter(); n = errs = 0; flows = {}
rows = db.execute('SELECT data_json FROM task_revisions').fetchall()
for r in rows:
    n += 1
    try:
        task = normalized(json.loads(r['data_json']))
        s = project(task.get('status'), task.get('report') or {'state': task.get('status')}, task)
        flows[(s['flow_state'], s['result_state'])] = flows.get((s['flow_state'], s['result_state']), 0) + 1
        build_report(dict(task, operations=task.get('operations', [])), task.get('model_summary', ''), [], reply='')
    except Exception as e:
        errs += 1; print('ERR', type(e).__name__, e)
outs = db.execute('SELECT COUNT(*) FROM run_outcomes').fetchone()[0] if 'run_outcomes' in tables else 0
print(dict(user_version=version, tasks=n, errors=errs, outcomes=outs, ms=round((time.perf_counter()-t0)*1000),
           projected={f'{k[0]}/{k[1]}': v for k, v in sorted(flows.items())}))
