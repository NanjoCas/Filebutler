"""Offline regressions for the 2026-10-21 holdout live run (second run of the day).

Recorded calls come from ``eval/results/policy_live_20261021_02_holdout/<case>/``
(git-ignored) and are frozen in ``tests/fixtures/live_20261021_02/*.json`` in the
same format as ``test_live_regressions_20261021``, whose replay harness is reused.
No model API is called.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.verifier import verify_task
from eval.holdout_cases import holdout_fixture

import test_live_regressions_20261021 as H
from test_live_regressions_20261021 import Session, step, call


FIXTURES = Path(__file__).parent / 'fixtures' / 'live_20261021_02'


def recorded(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


def replay(session, name, *, until=None, extra=(), final=''):
    data = recorded(name)
    steps = [s for s in data['steps'] if until is None or s['trace_step'] < until]
    return session.run(data['instruction'], steps + list(extra), final)


def check(task, goal_id):
    return next(c for c in task['checks'] if c['goal_id'] == goal_id)


# ============================================================================ search + dedup + move
# Live failure: holdout_search_dedup_move/turn_1.trace.jsonl. Files were all correct
# but the run ended partial: (a) read_file evidence was produced before any step,
# so in a four-goal task it was bound to no goal; (b) the dedup receipt checked the
# kept copy at its old path although a later logged step of the same task filed it.


def test_search_dedup_move_correct_files_are_not_partial(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    result, client = replay(session, 'holdout_search_dedup_move_turn1')
    # Since P1-4 two recorded rejections succeed earlier: apply_dedup without
    # begin_step (step 9, attributed by its receipt) and the step-12 plan. The
    # only call that then fails is the model's retry of the dedup it already did.
    changed = [c for c in client.log if c['status'] != c['recorded']]
    assert all(c['recorded'] == 'error' for c in changed if c['status'] == 'ok')
    assert [(c['tool'], c['trace_step']) for c in changed if c['status'] == 'error'] == [('apply_dedup', 11)]
    assert result.runtime['task_status'] in {'completed', 'needs_review'}
    assert result.stop_reason in {'done', 'needs_review'}
    assert '尚未全部完成' not in result.final
    task = session.sb.runtime.current
    assert check(task, 'find_photosynthesis_notes')['passed']
    assert check(task, 'dedup_photosynthesis_notes')['passed']
    assert (tmp_path / 'Topics/光合作用/note_01.txt').is_file()
    assert not (tmp_path / 'Notes/note_01_copy.txt').exists()


def test_receipt_keeper_moved_outside_the_log_still_fails(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    # Stop after apply_dedup (trace step 11), before the logged move to Topics.
    replay(session, 'holdout_search_dedup_move_turn1', until=12,
           extra=[step(call('finish_task', {'summary': '收尾'}))])
    task = session.sb.runtime.current
    assert check(task, 'dedup_photosynthesis_notes')['passed']
    (tmp_path / 'Elsewhere').mkdir()
    shutil.move(tmp_path / 'Notes/note_01.txt', tmp_path / 'Elsewhere/note_01.txt')
    assert not check(dict(task, checks=verify_task(task, session.sb)), 'dedup_photosynthesis_notes')['passed']


def test_evidence_from_file_recycled_outside_this_task_is_invalid(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    replay(session, 'holdout_search_dedup_move_turn1')
    task = session.sb.runtime.current
    assert check(task, 'find_photosynthesis_notes')['passed']
    # Same state, but the recycle is no longer this task's logged operation.
    strip = lambda ops: [op for op in ops if op.get('op') != 'trash']
    foreign = dict(task, operation_history=strip(task.get('operation_history', [])),
                   operations=strip(task.get('operations', [])))
    assert not check(dict(foreign, checks=verify_task(foreign, session.sb)), 'find_photosynthesis_notes')['passed']


# ============================================================================ ambiguous target
# Live failure: holdout_ambiguous_target/turn_1.trace.jsonl. set_task was correctly
# rejected (two report.txt), the model then inspected both files and replied in
# plain text; finish_plain replaced that reply with a fixed "unverified" sentence,
# so the user never saw the question. The recorded reply text was not kept, so the
# final message below is scripted.

QUESTION = '找到两个 report.txt：Projects/alpha/report.txt 与 Projects/beta/report.txt。请问要移动哪一个？'


def test_ambiguous_target_keeps_model_question(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    result, client = replay(session, 'holdout_ambiguous_target_turn1', final=QUESTION)
    assert client.calls('set_task')[0]['status'] == 'error'
    assert result.stop_reason == 'answered'
    assert QUESTION in result.final
    assert '未执行任何文件操作' in result.final and '同名' in result.final
    assert (tmp_path / 'Projects/alpha/report.txt').is_file()
    assert (tmp_path / 'Projects/beta/report.txt').is_file()
    assert not any((tmp_path / 'Archive').iterdir())


def test_blocked_reply_claiming_success_is_contradicted_locally(tmp_path):
    holdout_fixture(tmp_path, 'ambiguous')
    session = Session(tmp_path)
    result, _ = replay(session, 'holdout_ambiguous_target_turn1', final='已把 report.txt 移到 Archive。')
    assert result.stop_reason not in {'done', 'needs_review'}
    assert '未执行任何文件操作' in result.final
    assert not any((tmp_path / 'Archive').iterdir())


# ============================================================================ run 04: max_count double count
# Live failures: policy_live_20261021_04_regression/cross_turn_correction/turn_1 and
# policy_live_20261021_04_holdout/holdout_search_dedup_move/turn_1. A plan passed its
# pre-check, then the per-action re-check counted already executed actions twice
# (runtime ledger + stale journal offset), so max_count stopped the plan midway:
# a.txt moved, b.txt did not. The holdout run then re-prepared a plan whose source
# had already been recycled and crashed with FileNotFoundError.


def test_plan_within_max_count_executes_in_one_pass(tmp_path):
    H.files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'run04_cross_turn_correction_turn1')
    first = client.one('execute_file_plan', 4)
    assert first['status'] == 'ok'
    assert (tmp_path / 'Archive/a.txt').is_file() and (tmp_path / 'Archive/b.txt').is_file()
    # The model's files_placed also selected Inbox/nested/keep.txt; it is told
    # before executing instead of only at finish.
    prepared = next(c for c in client.calls('prepare_file_plan') if c['status'] == 'ok')
    assert 'Archive/keep.txt' in prepared['text'] and 'edit_task' in prepared['text']


def test_max_count_still_rejects_before_any_write(tmp_path):
    H.files_fixture(tmp_path)
    session = Session(tmp_path)
    data = recorded('run04_cross_turn_correction_turn1')
    steps = json.loads(json.dumps([s for s in data['steps'] if s['trace_step'] <= 4]))
    task_call = steps[0]['calls'][0]
    args = json.loads(task_call['arguments'])
    rules = [r for c in args['constraints'] for r in c.get('rules', []) if r['predicate'] == 'max_count']
    assert rules
    for rule in rules: rule['limit'] = 1
    task_call['arguments'] = json.dumps(args, ensure_ascii=False)
    result, client = session.run(data['instruction'], steps + [step(call('finish_task', {'summary': '收尾'}))])
    assert client.one('execute_file_plan', 4)['status'] == 'error'
    assert (tmp_path / 'Inbox/a.txt').is_file() and (tmp_path / 'Inbox/b.txt').is_file()
    assert result.runtime['task_status'] == 'partial'


def test_dedup_move_plan_executes_and_stale_source_is_rejected_not_raised(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    data = recorded('run04_holdout_search_dedup_move_turn1')
    prepare = [c for s in data['steps'] for c in s['calls'] if c['tool'] == 'prepare_file_plan'][-1]
    # Scripted: the crashed call was not traced; re-preparing the same plan
    # after it ran is the call that reaches the recycled source.
    result, client = session.run(data['instruction'], data['steps'] + [step(dict(prepare))])
    assert client.one('execute_file_plan', 11)['status'] == 'ok'
    assert (tmp_path / 'Topics/光合作用/note_02.txt').is_file()
    assert result.stop_reason != 'exception'
    again = client.calls('prepare_file_plan')[-1]
    assert again['status'] == 'error' and '来源不存在' in again['text']
    prepared = next(c for c in client.calls('prepare_file_plan') if c['status'] == 'ok')
    assert 'Notes/note_01.txt' in prepared['text'] and '移走' in prepared['text']


# ============================================================================ run 05: no-op write task
# Live: policy_live_20261021_05_regression/target_conflict/turn_1. The model explained
# the name conflict correctly and wrote nothing, but its goal carried a review flag,
# so the state was needs_review with "结果待核实" appended although nothing ran.

from agent.task import task_state


def target_conflict_fixture(root):
    H.files_fixture(root)
    (root / 'Archive/a.txt').write_text('existing archived file of 30 b')


def test_write_task_that_executed_nothing_waits_for_user(tmp_path):
    target_conflict_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'run05_target_conflict_turn1')
    assert [c for c in client.log if c['status'] != c['recorded']] == []
    assert result.runtime['task_status'] == 'waiting_input'
    assert '结果待核实' not in result.final
    assert '未执行任何文件操作' in result.final and '同名' in result.final
    assert result.final.count('Archive/a.txt（30B') == 1
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'alpha'


def review_task(**extra):
    check = dict(goal_id='g', passed=True, needs_review=True, conditions=[])
    return dict(dict(unresolved_questions=[], checks=[check], constraints=[], reviews=[], revision=1,
                     allowed_effects=['move'], operation_history=[], operations=[]), **extra)


def test_review_still_required_after_writes_or_for_read_only_tasks():
    assert task_state(review_task()) == 'waiting_input'
    assert task_state(review_task(operations=[dict(op='move', src='a', dst='b')])) == 'needs_review'
    assert task_state(review_task(operation_history=[dict(op='move', src='a', dst='b')])) == 'needs_review'
    assert task_state(review_task(allowed_effects=[])) == 'needs_review'
    failed = review_task()
    failed['checks'][0]['passed'] = False
    assert task_state(failed) == 'partial'
