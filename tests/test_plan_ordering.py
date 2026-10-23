"""Plans whose steps depend on each other: each source names an object as it is
now, and the steps run in an order where every target is free when written."""
from agent import choices
import json
import pytest

from agent.file_plans import prepare, execute, verify_plan
from agent.verifier import verify_task
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from tests.test_file_plans import runtime, proposal
from tests.test_protocol_v3 import DS_STORE


def write(root, files):
    for rel, text in files.items():
        (root/rel).parent.mkdir(parents=True, exist_ok=True)
        (root/rel).write_text(text)


def run(r, actions, run_id='one'):
    plan = prepare(r, proposal(actions=actions))
    result = execute(r, plan['id'], OpLog(r.sb, run_id), Confirmer('never'))
    assert result.startswith('OK'), result
    return r.store.file_plan(plan['id'])


def names(folder):
    return sorted(p.name for p in folder.iterdir() if not p.name.startswith('.'))


def test_chained_renames_in_natural_order_run_backwards_and_undo(tmp_path):
    write(tmp_path, {'A.txt': 'a', 'B.txt': 'b'})
    r = runtime(tmp_path)
    plan = run(r, [dict(effect='rename', source='A.txt', destination='B.txt'),
                   dict(effect='rename', source='B.txt', destination='C.txt')])
    assert [s['source'] for s in plan['payload']['steps']] == ['B.txt', 'A.txt']
    assert (tmp_path/'B.txt').read_text() == 'a' and (tmp_path/'C.txt').read_text() == 'b'
    assert verify_plan(plan, r.sb)[0] and all(c['passed'] for c in verify_task(r.current, r.sb))
    undo(r.sb, 'one')
    assert (tmp_path/'A.txt').read_text() == 'a' and (tmp_path/'B.txt').read_text() == 'b'
    assert not (tmp_path/'C.txt').exists()


def test_vacate_a_name_then_fill_it(tmp_path):
    # Flatten with a clash at the root: the old x.pdf makes room first.
    write(tmp_path, {'x.pdf': 'old', '文献/a/x.pdf': 'new'})
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source='文献/a/x.pdf', destination='x.pdf'),
            dict(effect='rename', source='x.pdf', destination='x_old.pdf')])
    assert (tmp_path/'x.pdf').read_text() == 'new' and (tmp_path/'x_old.pdf').read_text() == 'old'


def test_recycle_the_occupant_then_take_its_name(tmp_path):
    # An overwrite: refused until the user chose it (decision 2026-10-22).
    write(tmp_path, {'report.txt': 'draft', 'final.txt': 'final'})
    r = runtime(tmp_path)
    actions = [dict(effect='rename', source='final.txt', destination='report.txt'), dict(effect='trash', source='report.txt')]
    with pytest.raises(ValueError, match='覆盖必须由用户选择'):
        prepare(r, proposal(actions=actions))
    assert (tmp_path/'report.txt').read_text() == 'draft'
    r.decisions = [choices.row('overwrite', 'report.txt', 'option')]
    plan = run(r, actions)
    assert plan['payload']['overwrites'] == [dict(target='report.txt', consent_source='option')]
    assert (tmp_path/'report.txt').read_text() == 'final' and not (tmp_path/'final.txt').exists()
    undo(r.sb, 'one')
    assert (tmp_path/'report.txt').read_text() == 'draft' and (tmp_path/'final.txt').read_text() == 'final'


@pytest.mark.parametrize('actions', [
    [dict(effect='rename', source='A.txt', destination='B.txt'), dict(effect='rename', source='B.txt', destination='A.txt')],
    [dict(effect='rename', source='A.txt', destination='B.txt'), dict(effect='rename', source='B.txt', destination='C.txt'),
     dict(effect='rename', source='C.txt', destination='A.txt')],
])
def test_swap_or_cycle_is_refused_before_any_task_or_write(tmp_path, actions):
    write(tmp_path, {'A.txt': 'a', 'B.txt': 'b', 'C.txt': 'c'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='互相依赖'):
        prepare(r, proposal(actions=actions))
    assert not r.store.recent() and names(tmp_path) == ['A.txt', 'B.txt', 'C.txt']


def test_occupied_target_not_vacated_is_still_refused(tmp_path):
    write(tmp_path, {'A.txt': 'a', 'B.txt': 'b', 'C.txt': 'c'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='目标已存在'):
        prepare(r, proposal(actions=[dict(effect='rename', source='A.txt', destination='B.txt'),
                                     dict(effect='rename', source='C.txt', destination='D.txt')]))


def test_empty_a_folder_then_rename_it(tmp_path):
    write(tmp_path, {'旧/a.pdf': 'a', '旧/sub/b.pdf': 'b'})
    (tmp_path/'旧/.DS_Store').write_bytes(DS_STORE)  # system metadata travels with the folder
    r = runtime(tmp_path)
    plan = run(r, [dict(effect='rename', source='旧', destination='新'),
                   dict(effect='move', source='旧/a.pdf', destination='a.pdf'),
                   dict(effect='move', source='旧/sub/b.pdf', destination='b.pdf')])
    assert plan['payload']['steps'][-1]['source'] == '旧'
    assert not (tmp_path/'旧').exists() and (tmp_path/'新/sub').is_dir() and (tmp_path/'新/.DS_Store').exists()
    assert names(tmp_path) == ['a.pdf', 'b.pdf', '新']
    undo(r.sb, 'one')
    assert (tmp_path/'旧/a.pdf').read_text() == 'a' and (tmp_path/'旧/sub/b.pdf').read_text() == 'b'


@pytest.mark.parametrize('extra,why', [
    ({'旧/.env': 'TOKEN=1'}, '隐藏文件'),
    ({'旧/~$doc.docx': 'lock'}, '锁文件'),
])
def test_folder_move_refused_with_hidden_or_open_contents(tmp_path, extra, why):
    write(tmp_path, {'旧/a.pdf': 'a', **extra})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match=why):
        prepare(r, proposal(actions=[dict(effect='move', source='旧/a.pdf', destination='a.pdf'),
                                     dict(effect='rename', source='旧', destination='新')]))
    assert (tmp_path/'旧/a.pdf').exists()


def test_target_inside_a_moved_folder_is_refused(tmp_path):
    write(tmp_path, {'旧/a.pdf': 'a', 'b.pdf': 'b'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='拆分阶段'):
        prepare(r, proposal(actions=[dict(effect='move', source='旧/a.pdf', destination='a.pdf'),
                                     dict(effect='rename', source='旧', destination='新'),
                                     dict(effect='move', source='b.pdf', destination='新/b.pdf')]))


def test_case_only_rename_of_file_and_folder(tmp_path):
    write(tmp_path, {'photo.JPG': 'img', 'docs/.keep_out': ''})
    (tmp_path/'docs/.keep_out').unlink()
    r = runtime(tmp_path)
    plan = run(r, [dict(effect='rename', source='photo.JPG', destination='photo.jpg'),
                   dict(effect='rename', source='docs', destination='Docs')])
    assert names(tmp_path) == ['Docs', 'photo.jpg']
    assert verify_plan(plan, r.sb)[0]
    undo(r.sb, 'one')
    assert names(tmp_path) == ['docs', 'photo.JPG']
    assert not verify_plan(plan, r.sb)[0]


def test_case_only_move_into_same_named_folder_is_a_rename(tmp_path):
    (tmp_path/'notes').mkdir()
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source='notes', destination='Notes')])
    assert names(tmp_path) == ['Notes']


def test_interrupted_case_only_rename_is_reconciled_from_the_journal(tmp_path, monkeypatch):
    write(tmp_path, {'a.JPG': 'img'})
    r = runtime(tmp_path)
    plan = prepare(r, proposal(actions=[dict(effect='rename', source='a.JPG', destination='a.jpg')]))
    original = r.store.save_file_plan
    def crash_after_journal(p):
        if p['progress']: raise KeyboardInterrupt()
        original(p)
    monkeypatch.setattr(r.store, 'save_file_plan', crash_after_journal)
    with pytest.raises(KeyboardInterrupt):
        execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('never'))
    new = runtime(tmp_path, run='two')
    assert execute(new, plan['id'], OpLog(new.sb, 'two'), Confirmer('never')).startswith('OK')
    assert names(tmp_path) == ['a.jpg'] and not (tmp_path/'.oplog/two.jsonl').exists()
    assert len([json.loads(x) for x in (tmp_path/'.oplog/one.jsonl').read_text().splitlines()]) == 1


def test_resume_after_first_step_of_a_chain(tmp_path, monkeypatch):
    import safety.atomic as atomic
    write(tmp_path, {'A.txt': 'a', 'B.txt': 'b'})
    r = runtime(tmp_path)
    plan = prepare(r, proposal(actions=[dict(effect='rename', source='A.txt', destination='B.txt'),
                                        dict(effect='rename', source='B.txt', destination='C.txt')]))
    original = atomic.rename_no_replace
    def fail_second(src, dst):
        if src.name == 'A.txt': raise OSError('injected pre-mutation failure')
        return original(src, dst)
    monkeypatch.setattr(atomic, 'rename_no_replace', fail_second)
    with pytest.raises(ValueError):
        execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('never'))
    assert names(tmp_path) == ['A.txt', 'C.txt']
    monkeypatch.setattr(atomic, 'rename_no_replace', original)
    new = runtime(tmp_path, run='two')
    assert execute(new, plan['id'], OpLog(new.sb, 'two'), Confirmer('never')).startswith('OK')
    assert (tmp_path/'B.txt').read_text() == 'a' and (tmp_path/'C.txt').read_text() == 'b'


def _two_plan_swap(tmp_path):
    from agent.task import TaskSubmission
    from tests.helpers import spec
    write(tmp_path, {'a.txt': 'a', 'b.txt': 'b'})
    r = runtime(tmp_path)
    r.current = r.store.submit(TaskSubmission.model_validate(spec(('互换', [dict(kind='file_plan_verified')]),
        read_roots=['.'], write_roots=['.'], allowed_effects=['rename'])), '互换', 'one')
    run(r, [dict(effect='rename', source='a.txt', destination='a.txt.swap-tmp')])
    run(r, [dict(effect='rename', source='b.txt', destination='a.txt'),
            dict(effect='rename', source='a.txt.swap-tmp', destination='b.txt')])
    return r


def test_later_plan_of_the_task_carries_an_earlier_result_on(tmp_path):
    # Live round 17: a swap in two plans was judged unfinished at the temporary name.
    r = _two_plan_swap(tmp_path)
    assert (tmp_path/'a.txt').read_text() == 'b' and (tmp_path/'b.txt').read_text() == 'a'
    assert all(c['passed'] for c in verify_task(r.current, r.sb))


def test_handoff_does_not_excuse_a_move_outside_the_plans(tmp_path):
    r = _two_plan_swap(tmp_path)
    (tmp_path/'b.txt').rename(tmp_path/'elsewhere.txt')
    assert not verify_task(r.current, r.sb)[0]['passed']


# ---------------------------------------------------------------- whole folders
# Live round 21: "rename the folder" had no direct form; the model rebuilt it by hand.

def test_a_folder_with_files_is_renamed_whole_and_undone(tmp_path):
    write(tmp_path, {'照片/a.jpg': 'a', '照片/2024/b.jpg': 'b'})
    (tmp_path/'照片/.DS_Store').write_bytes(DS_STORE)
    r = runtime(tmp_path)
    plan = run(r, [dict(effect='rename', source='照片', destination='Photos')])
    [step] = plan['payload']['steps']
    assert step['snapshot']['kind'] == 'folder' and sorted(step['snapshot']['after']) == ['2024/b.jpg', 'a.jpg']
    assert (tmp_path/'Photos/2024/b.jpg').read_text() == 'b' and not (tmp_path/'照片').exists()
    assert all(c['passed'] for c in verify_task(r.current, r.sb))
    undo(r.sb, 'one')
    assert (tmp_path/'照片/2024/b.jpg').read_text() == 'b' and not (tmp_path/'Photos').exists()


def test_files_inside_are_renamed_first_then_the_folder_in_one_plan(tmp_path):
    write(tmp_path, {'照片/IMG_01.JPEG': '1', '照片/IMG_02.JPEG': '2', '照片/说明.txt': 'n'})
    r = runtime(tmp_path)
    plan = run(r, [dict(effect='rename', source='照片', destination='Photos'),
                   dict(effect='rename', source='照片/IMG_01.JPEG', destination='照片/IMG_01.jpg'),
                   dict(effect='rename', source='照片/IMG_02.JPEG', destination='照片/IMG_02.jpg')])
    assert plan['payload']['steps'][-1]['source'] == '照片'
    assert names(tmp_path/'Photos') == ['IMG_01.jpg', 'IMG_02.jpg', '说明.txt']
    assert verify_plan(plan, r.sb)[0] and all(c['passed'] for c in verify_task(r.current, r.sb))
    undo(r.sb, 'one')
    assert names(tmp_path/'照片') == ['IMG_01.JPEG', 'IMG_02.JPEG', '说明.txt']


def test_a_file_moved_in_travels_with_the_folder(tmp_path):
    write(tmp_path, {'项目/a.txt': 'a', 'b.txt': 'b'})
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source='项目', destination='归档/项目'), dict(effect='move', source='b.txt', destination='项目/b.txt')])
    assert names(tmp_path/'归档/项目') == ['a.txt', 'b.txt']


def test_a_target_at_the_folders_new_place_needs_a_second_plan(tmp_path):
    write(tmp_path, {'项目/a.txt': 'a', 'b.txt': 'b'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='拆分阶段'):
        prepare(r, proposal(actions=[dict(effect='rename', source='项目', destination='新项目'),
                                     dict(effect='move', source='b.txt', destination='新项目/b.txt')]))


@pytest.mark.parametrize('change', ['add', 'edit'])
def test_a_folder_changed_while_waiting_for_confirmation_is_not_moved(tmp_path, change):
    write(tmp_path, {'照片/a.jpg': 'a'})
    r = runtime(tmp_path)
    plan = prepare(r, proposal(actions=[dict(effect='rename', source='照片', destination='Photos')]))
    def confirm(_):
        if change == 'add': (tmp_path/'照片/new.jpg').write_text('new')
        else: (tmp_path/'照片/a.jpg').write_text('edited')
        return 'y'
    with pytest.raises(ValueError):
        execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('always', ask_action=confirm))
    assert (tmp_path/'照片').is_dir() and not (tmp_path/'Photos').exists()


def test_the_card_states_the_whole_folder_and_its_files(tmp_path):
    from web_confirm import build_confirmation
    write(tmp_path, {'照片/a.jpg': 'a', '照片/b.jpg': 'b'})
    r = runtime(tmp_path)
    plan = prepare(r, proposal(actions=[dict(effect='rename', source='照片', destination='Photos')]))
    seen = []
    execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('always', ask_action=lambda req: seen.append(build_confirmation(req, r.sb)) or 'y'))
    assert any(n.startswith('整体移动文件夹 照片 → Photos：含 2 个文件（a.jpg、b.jpg）') for n in seen[0]['notes'])


# ---------------------------------------------------------------- folder or new name?
# Live round 28: "照片/2024" for a single c.jpg became a file named 2024, and verified.

def test_a_move_that_would_drop_the_extension_onto_a_missing_path_is_refused(tmp_path):
    write(tmp_path, {'照片/c.jpg': 'c'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='照片/2024/c.jpg'):
        prepare(r, proposal(actions=[dict(effect='move', source='照片/c.jpg', destination='照片/2024')]))
    assert names(tmp_path/'照片') == ['c.jpg']


@pytest.mark.parametrize('destination', ['照片/2024/', '照片/2024/c.jpg'])
def test_either_spelling_puts_the_file_in_a_new_folder(tmp_path, destination):
    write(tmp_path, {'照片/c.jpg': 'c'})
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source='照片/c.jpg', destination=destination)])
    assert (tmp_path/'照片/2024/c.jpg').read_text() == 'c'


def test_a_rename_with_a_new_extension_or_an_existing_target_folder_still_works(tmp_path):
    write(tmp_path, {'a.txt': 'a', 'notes': 'n', 'Archive/x.md': 'x'})
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source='a.txt', destination='Archive'),     # existing folder: into it
            dict(effect='move', source='notes', destination='Archive/notes2')])  # no extension to lose
    assert names(tmp_path/'Archive') == ['a.txt', 'notes2', 'x.md']


# ---------------------------------------------------------------- file-system edges (P2, 2026-10-22)

@pytest.mark.parametrize('on_disk,given', [(' paper.pdf', 'paper.pdf'), ('报告（终稿）.pdf', '报告(终稿).pdf'), ('Report.PDF', 'report.pdf')])
def test_a_slightly_misspelt_source_finds_the_one_file_and_keeps_its_real_name(tmp_path, on_disk, given):
    write(tmp_path, {on_disk: 'x', 'out/keep.txt': 'k'})
    r = runtime(tmp_path)
    run(r, [dict(effect='move', source=given, destination='out')])
    assert (tmp_path/'out'/on_disk).read_text() == 'x'


def test_two_files_matching_a_misspelling_are_not_guessed_between(tmp_path):
    write(tmp_path, {'报告（终稿）.pdf': '1', '报告(终稿).pdf ': '2', 'out/keep.txt': 'k'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='来源不存在'):
        prepare(r, proposal(actions=[dict(effect='move', source='报告 (终稿).pdf', destination='out')]))


def test_a_package_moves_whole_and_a_selection_never_takes_it_apart(tmp_path):
    write(tmp_path, {'资料/a.pdf': 'a', '资料/报告.pages/index.xml': 'i', '资料/报告.pages/Data/img.png': 'p'})
    r = runtime(tmp_path)
    plan = prepare(r, proposal(selection=dict(path='资料', destination='平铺')))
    assert sorted(s['source'] for s in plan['payload']['steps'] if s['effect'] == 'move') == ['资料/a.pdf', '资料/报告.pages']
    execute(r, plan['id'], OpLog(r.sb, 'one'), Confirmer('never'))
    assert (tmp_path/'平铺/报告.pages/Data/img.png').read_text() == 'p' and not (tmp_path/'平铺/img.png').exists()
    assert all(c['passed'] for c in verify_task(r.current, r.sb))
    undo(r.sb, 'one')
    assert (tmp_path/'资料/报告.pages/index.xml').read_text() == 'i'


def test_a_package_with_links_inside_is_recycled_whole(tmp_path):
    write(tmp_path, {'Tool.app/Contents/MacOS/tool': 'bin', 'Tool.app/Contents/Info.plist': 'p'})
    (tmp_path/'Tool.app/Contents/Current').symlink_to('MacOS')
    r = runtime(tmp_path)
    run(r, [dict(effect='trash', source='Tool.app')])
    assert not (tmp_path/'Tool.app').exists()


def test_locked_or_read_only_sources_are_refused_before_confirmation_in_plain_words(tmp_path):
    import os, subprocess
    write(tmp_path, {'a.pdf': 'a', 'ro/b.pdf': 'b', 'out/keep.txt': 'k'})
    subprocess.run(['chflags', 'uchg', str(tmp_path/'a.pdf')], check=True)
    os.chmod(tmp_path/'ro', 0o555)
    try:
        r = runtime(tmp_path)
        with pytest.raises(ValueError, match='已被锁定'):
            prepare(r, proposal(actions=[dict(effect='move', source='a.pdf', destination='out')]))
        with pytest.raises(ValueError, match='只读'):
            prepare(r, proposal(actions=[dict(effect='move', source='ro/b.pdf', destination='out')]))
    finally:
        subprocess.run(['chflags', 'nouchg', str(tmp_path/'a.pdf')]); os.chmod(tmp_path/'ro', 0o755)


def test_a_system_error_during_execution_reads_in_plain_words(tmp_path):
    import errno
    from agent.file_plans import os_reason
    from safety.sandbox import Sandbox
    sb = Sandbox(tmp_path)
    text = os_reason(PermissionError(errno.EACCES, 'Permission denied', str(tmp_path/'out/a.pdf')), sb)
    assert text == '没有权限（文件夹可能是只读的）：out/a.pdf' and str(tmp_path) not in text


# iCloud "dataless" files (content only in the cloud): reading would download them.
# A real one cannot be made here, so the flag check is replaced for one name.

@pytest.fixture
def cloud(monkeypatch):
    import agent.artifacts as artifacts
    real = artifacts.cloud_only
    monkeypatch.setattr(artifacts, 'cloud_only', lambda path: path.name.startswith('cloud') or real(path))


def test_cloud_only_files_are_not_hashed_for_a_plan(tmp_path, cloud):
    write(tmp_path, {'cloud.pdf': 'x', 'out/keep.txt': 'k'})
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match='只在 iCloud 云端'):
        prepare(r, proposal(actions=[dict(effect='move', source='cloud.pdf', destination='out')]))


def test_dedup_skips_cloud_only_files_and_says_so(tmp_path, cloud):
    from tools.dedup import scan_duplicates
    from safety.sandbox import Sandbox
    write(tmp_path, {'a.txt': 'same', 'b.txt': 'same', 'cloud_a.txt': 'same'})
    scan = scan_duplicates(Sandbox(tmp_path), mode='bytes')
    assert scan['stats']['cloud_only'] == 1
    assert all('cloud_a.txt' not in g['members'] for g in scan['groups'])


def test_read_file_refuses_a_cloud_only_file(tmp_path, cloud):
    import json
    from tools import registry
    from safety.sandbox import Sandbox
    write(tmp_path, {'cloud.txt': 'secret text'})
    text = registry.dispatch('read_file', json.dumps(dict(path='cloud.txt')), Sandbox(tmp_path))
    assert '只在 iCloud 云端' in text and 'secret text' not in text


def test_a_hard_link_is_not_a_duplicate_copy(tmp_path):
    import os
    from tools.dedup import scan_duplicates
    from safety.sandbox import Sandbox
    write(tmp_path, {'a.txt': 'same', 'b.txt': 'same'})
    os.link(tmp_path/'a.txt', tmp_path/'a_link.txt')
    scan = scan_duplicates(Sandbox(tmp_path), mode='bytes')
    assert scan['stats']['hard_links'] == 1
    assert [sorted(g['members']) for g in scan['groups']] == [['a.txt', 'b.txt']]


def test_dedup_batch_waits_for_no_other_writer(tmp_path):
    from agent.file_plans import library_lock
    from tools.registry import _trash_batch, ToolError, TrashBatch
    from safety.sandbox import Sandbox
    write(tmp_path, {'a.txt': 'same', 'b.txt': 'same'})
    sb = Sandbox(tmp_path)
    with library_lock(sb):  # another conversation is executing a plan
        with pytest.raises(ToolError, match='另一项整理'):
            _trash_batch(TrashBatch(paths=['b.txt'], keeps=['a.txt'], validate=lambda: None), sb, OpLog(sb, 'one'), Confirmer('never'))
    assert (tmp_path/'b.txt').exists()
