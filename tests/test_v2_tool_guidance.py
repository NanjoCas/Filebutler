import json

import pytest

from agent.intent import StartTask
from helpers import Session, call, step
from test_protocol_v3 import start
from tests.test_file_plans import runtime, proposal
from agent.file_plans import prepare
from tools import registry


def test_missing_read_path_suggests_a_registered_handle_without_reading(tmp_path):
    (tmp_path / 'Notes').mkdir()
    (tmp_path / 'Notes/report.txt').write_text('secret content')
    r = runtime(tmp_path, task=False)
    fid = r.sb.artifacts.register('Notes/report.txt')
    out = registry.dispatch('read_file', json.dumps({'path': 'report'}), r.sb)
    assert 'ERROR:' in out and f'file:{fid}' in out and 'Notes/report.txt' in out
    assert 'secret content' not in out and not r.sb.artifacts.data['evidence']


def test_ambiguous_names_are_not_automatically_resolved(tmp_path):
    r = runtime(tmp_path, task=False)
    for folder in ('a', 'b'):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'report.txt').write_text(folder)
        r.sb.artifacts.register(f'{folder}/report.txt')
    out = registry.dispatch('read_file', json.dumps({'path': 'report'}), r.sb)
    assert out.startswith('ERROR') and '可能是 file:' not in out


def test_evidence_results_preserve_the_full_relative_path(tmp_path):
    (tmp_path / 'Notes').mkdir()
    rel = 'Notes/' + 'long_name_' * 12 + '.txt'
    (tmp_path / rel).write_text('dengue prediction')
    r = runtime(tmp_path, task=False)
    out = registry.dispatch('search_content', json.dumps({'query': 'dengue', 'view': 'evidence'}), r.sb)
    assert json.dumps(rel, ensure_ascii=False) in out and 'evidence:' in out and len(out) <= 2000


@pytest.mark.parametrize('empty', [False, True])
def test_unknown_goal_ids_provide_copyable_candidates(tmp_path, empty):
    (tmp_path / 'a.txt').write_text('a')
    r = runtime(tmp_path)
    args = proposal(goal_ids=['missing'], **({'selection': dict(path='.', extensions=['pdf'], destination='out')}
                    if empty else {'actions': [dict(effect='rename', source='a.txt', destination='b.txt')]}))
    with pytest.raises(ValueError, match='当前部分：files'):
        prepare(r, args)


def test_normalized_part_ids_cannot_collide_at_registration():
    with pytest.raises(ValueError, match='ID'):
        StartTask(summary='s', parts=[dict(id=i, kind='organize', description='x')
                                     for i in ('organize_1', 'ＯＲＧＡＮＩＺＥ-１')])


def test_unique_part_normalization_keeps_the_exact_attribution_and_reports_it(tmp_path):
    (tmp_path / 'a.txt').write_text('a')
    session = Session(tmp_path)
    _, client = session.run('改名', [step(start([dict(id='organize_1', kind='organize', description='改名')]),
        call('prepare_file_plan', dict(description='改名', goal_ids=['ＯＲＧＡＮＩＺＥ-１'],
             actions=[dict(effect='rename', source='a.txt', destination='b.txt', part='Organize-1')])) )])
    result = client.calls('prepare_file_plan')[0]
    assert result['status'] == 'ok' and 'organize_1' in result['text'] and '统一' in result['text']


def test_control_bad_json_has_a_chinese_invalid_arguments_error(tmp_path):
    session = Session(tmp_path)
    malformed = dict(tool='start_task', arguments='{broken', status='error', result_head='', result_ids=[])
    result, _ = session.run('开始', [step(malformed)])
    message = next(json.loads(m['content']) for m in result.messages if m['role'] == 'tool')
    assert message['error_code'] == 'INVALID_ARGUMENTS' and 'JSON' in message['text'] and '请' in message['text']


def test_a_mistyped_classification_plan_id_gets_the_saved_plans_as_candidates(tmp_path):
    # Round 47 application_dimension r2: Pe778b3cecf copied as Pe778b3cef, three failed calls
    # and a second classification, because the error named no saved plan.
    from tests.test_classification import ready
    from tools.classification import _plan
    from tools.registry import ToolError
    sb, _ = ready(tmp_path)
    pid = next(reversed(sb.artifacts.data['classification_plans']))
    with pytest.raises(ToolError) as error:
        _plan(sb.artifacts, pid[:-2] + pid[-1])
    assert pid in str(error.value) and 'Transformer/其他' in str(error.value)
    assert _plan(sb.artifacts, pid)[0] == pid  # only candidates: the exact ID still works
