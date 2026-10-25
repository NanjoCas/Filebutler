"""New code writes a find task (coverage ledger, typed results) and an organize task into a fresh library."""
import json, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
from pathlib import Path
sys.path[:0] = [str(REPO / 'tests'), str(REPO)]
import helpers as H
from helpers import Session, step, call, files_fixture
from test_protocol_v3 import start, execute, finish, PLAN, ARCHIVE_A
root = Path(sys.argv[1]); root.mkdir(parents=True)
files_fixture(root)
for i in range(4): (root / f'Notes/n{i}.txt').parent.mkdir(exist_ok=True); (root / f'Notes/n{i}.txt').write_text(f'photosynthesis note {i} ' * 20)
cfg = dict(H.CFG, task_db=str(root.parent / 'tasks.sqlite3'))
s = Session(root, cfg)
r1, _ = s.run('找光合作用笔记', [step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                           step(call('search_content', dict(query='photosynthesis', top_k=2, view='evidence'))),
                           step(call('read_file', dict(path='Notes/n0.txt'))), finish()])
t1 = s.sb.runtime.current
r2, _ = s.run('归档 a', [step(start([dict(kind='organize', description='归档 a')]),
                       call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A), alias=PLAN)), execute(), finish()])
print(json.dumps(dict(find_task=t1['id'], has_coverage=bool(t1.get('coverage')),
                      typed_obs=sum('result_id' in o for o in t1['observations']), states=r1.report['states']['flow_state'],
                      organize=s.sb.runtime.current['id'], organize_status=s.sb.runtime.current['status'])))
