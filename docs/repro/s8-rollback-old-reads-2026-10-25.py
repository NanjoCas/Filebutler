"""Pre-S6 code (e3c7d7f) reads the library the new code wrote, then runs a new task in it."""
import json, sys
from pathlib import Path
old = sys.argv[1]
sys.path[:0] = [old + '/tests', old]
import helpers as H
from helpers import Session, step, call
from test_protocol_v3 import start, finish
from agent.task import TaskStore, task_state
from agent.reporting import build_report
root = Path(sys.argv[2]); ids = json.loads(sys.argv[3])
store = TaskStore(root.parent / 'tasks.sqlite3', root, H.CFG['task_session'])
out = {}
for key in ('find_task', 'organize'):
    task = store.get(ids[key])
    out[key] = dict(status=task['status'], recomputed=task_state(task),
                    report_ok=bool(build_report(dict(task, operations=task.get('operations', [])), '', [], reply='')['summary']))
out['outcome_readable'] = bool(store.outcome())
s = Session(root, dict(H.CFG, task_db=str(root.parent / 'tasks.sqlite3')))
r, _ = s.run('列一下', [step(call('list_dir', dict(path='.')))], final='好。')
out['old_code_runs'] = r.stop_reason
print(json.dumps(out, ensure_ascii=False))
