"""Read-only protocol budget prototype; creates its TaskStore only in a temporary directory."""
import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import agent.loop  # registers the current tool surface; does not run the agent
from agent.artifacts import ArtifactStore
from agent.prompts import SYSTEM_PROMPT
from agent.task_runtime import TaskRuntime
from safety.sandbox import Sandbox
from tools import registry

CAP = 31311
CHANGES = [
    ('file_info.desc', 'file_info', None,
     '图片不能用 read_file，要用它。', ''),
    ('Action.part', 'prepare_file_plan', ('$defs', 'Action', 'properties', 'part', 'description'),
     '此动作完成的部分 ID；一个计划完成多个同类部分时填写', '动作的部分ID；多个候选部分时必填'),
    ('PrepareFilePlan.goal_ids', 'prepare_file_plan', ('properties', 'goal_ids', 'description'),
     '此计划完成的部分；任务只有一个会改文件的部分时可省略，有多个同类部分时必填', '计划涉及的部分ID；多目标动作仍须part'),
]


def serialized_size(schemas):
    return sum(len(json.dumps(s, ensure_ascii=False)) for s in schemas)


with tempfile.TemporaryDirectory(prefix='filebutler-schema-prototype-') as temporary:
    for sensitive in (False, True):
        root = Path(temporary) / str(sensitive)
        root.mkdir()
        sandbox = Sandbox(root)
        sandbox.artifacts = ArtifactStore(sandbox)
        runtime = TaskRuntime('x', {'task_db': str(root / 'tasks.sqlite3'),
                                   'sensitive_files': sensitive}, sandbox, 'prototype')
        surface = runtime.schemas(registry.schemas())
        fixed = len(SYSTEM_PROMPT) + len(runtime.guidance())
        baseline = fixed + serialized_size(surface)
        print(json.dumps({'sensitive': sensitive, 'baseline': baseline, 'headroom': CAP-baseline}, ensure_ascii=False))
        candidate = copy.deepcopy(surface)
        for name, tool, path, old, new in CHANGES:
            item = next(s for s in candidate if s['function']['name'] == tool)['function']
            before = serialized_size(candidate)
            if path is None:
                if old not in item['description']:  # applied in S1 (2026-10-24)
                    print(json.dumps({'change': name, 'already_applied': True}, ensure_ascii=False)); continue
                item['description'] = item['description'].replace(old, new)
            else:
                owner = item['parameters']
                for part in path[:-1]:
                    owner = owner[part]
                if owner[path[-1]] == new:
                    print(json.dumps({'change': name, 'already_applied': True}, ensure_ascii=False)); continue
                assert owner[path[-1]] == old, (name, owner[path[-1]])
                owner[path[-1]] = new
            print(json.dumps({'change': name, 'delta': serialized_size(candidate)-before}, ensure_ascii=False))
        total = fixed + serialized_size(candidate)
        print(json.dumps({'candidate': 'S1-existing-fields-only', 'total': total, 'headroom': CAP-total,
                          'within_cap': total <= CAP}, ensure_ascii=False))
        assert total <= CAP
