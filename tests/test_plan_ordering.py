"""Plans whose steps depend on each other: each source names an object as it is
now, and the steps run in an order where every target is free when written."""
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
    write(tmp_path, {'report.txt': 'draft', 'final.txt': 'final'})
    r = runtime(tmp_path)
    run(r, [dict(effect='rename', source='final.txt', destination='report.txt'),
            dict(effect='trash', source='report.txt')])
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
    ({'旧/keep.pdf': 'k'}, '不在本计划的移动中'),
    ({'旧/.env': 'TOKEN=1'}, '隐藏文件'),
    ({'旧/~$doc.docx': 'lock'}, '锁文件'),
])
def test_folder_move_refused_while_it_still_holds_files(tmp_path, extra, why):
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
    from tests.test_task_runtime_v2 import spec
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
