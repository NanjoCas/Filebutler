"""User decision A2 (2026-10-29): a non-empty folder is recycled by one trash action. The
files it takes with it are recorded by digest, listed on the confirmation card, checked
again before execution, counted by user limits, and restored by undo. Hidden files, lock
files and special objects keep their own rules."""
import json

import pytest

from agent.file_plans import execute, prepare
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from tests.test_file_plans import proposal, runtime
from web_confirm import build_confirmation

TREE = {'测试/a.txt': 'a', '测试/sub/b.md': 'b', '测试/sub/deep/c.pdf': 'c'}


def fixture(root, files=TREE):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def recycle(r, path='测试'):
    return prepare(r, proposal(actions=[dict(effect='trash', source=path)]))


def test_one_trash_recycles_a_non_empty_folder_and_undo_restores_it(tmp_path):
    fixture(tmp_path)
    r = runtime(tmp_path)
    plan = recycle(r)
    assert sorted(plan['payload']['steps'][0]['snapshot']['visible']) == ['a.txt', 'sub/b.md', 'sub/deep/c.pdf']
    cards = []
    def approve(request):
        cards.append(build_confirmation(request, r.sb)); return 'y'
    assert execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('always', ask_action=approve)).startswith('OK')
    assert not (tmp_path / '测试').exists() and len(cards) == 1
    listed = [i['path'] for g in cards[0]['groups'] for i in g['items']]
    assert {'测试/a.txt', '测试/sub/b.md', '测试/sub/deep/c.pdf'} <= set(listed)
    assert any('整体回收文件夹 测试：其中 3 个文件' in n for n in cards[0]['notes'])
    assert cards[0]['headline'] == '把「测试」文件夹（含 3 个文件）移入回收区（可撤销）'
    undo(r.sb, 'one')
    assert all((tmp_path / rel).read_text() == text for rel, text in TREE.items())


@pytest.mark.parametrize('change', ['edited', 'added', 'removed'])
def test_a_folder_that_changed_after_the_plan_is_not_recycled(tmp_path, change):
    fixture(tmp_path)
    r = runtime(tmp_path)
    plan = recycle(r)
    if change == 'edited': (tmp_path / '测试/sub/b.md').write_text('changed')
    if change == 'added': (tmp_path / '测试/new.txt').write_text('new')
    if change == 'removed': (tmp_path / '测试/a.txt').unlink()
    with pytest.raises(ValueError):
        execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('always', ask_action=lambda request: 'y'))
    assert (tmp_path / '测试/sub/deep/c.pdf').is_file()


def test_a_count_limit_counts_the_files_a_folder_takes_with_it(tmp_path):
    fixture(tmp_path)
    r = runtime(tmp_path, task=False)
    text = r.dispatch('start_task', json.dumps(dict(summary='回收', parts=[dict(id='files', kind='organize', description='回收测试')],
                                                     limits=dict(max_files=2))), r.sb, None, None)
    assert text.startswith('OK'), text
    with pytest.raises(ValueError, match='本次新增 3 项'):
        recycle(r)


def test_hidden_files_still_need_the_users_consent(tmp_path):
    fixture(tmp_path, {**TREE, '测试/.env': 'TOKEN=x'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='非系统隐藏文件'):
        recycle(r)


def test_more_than_500_files_are_refused(tmp_path):
    fixture(tmp_path, {f'测试/f{i:03d}.txt': str(i) for i in range(501)})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='超过单个计划 500 个的上限'):
        recycle(r)
