"""Replays for chain plan 2 (2026-10-23): does an answer reach the right question?

Each test states the behaviour the plan asks for (docs/agent-chain-optimization-
plan-2026-10-23.md, 3.5). A test marked xfail is a gap these replays found in
the current code; the others pin behaviour that already holds.

Live evidence (all eval/results histories up to round 35): 24 questions answered
in a later turn. 23 were asked before start_task; in all 24 every option declared
the whole request's actions ("只移动 a.pdf、b.pdf", "先不动" = nothing at all), never
a part of it. The two "choice about b" gaps below need a partial option and were
not seen live; the resume gap was seen once.

The web page itself already keeps only the latest question's buttons clickable
(web/app.js settleChoices) and refuses a second message while a turn runs (409),
so these replays go through the backend with the letters a page or a retry sends.
"""
import pytest

from agent.task_runtime import TaskRuntime

import helpers as H
from helpers import Session, step, call, files_fixture
from test_protocol_v3 import start, execute, finish, finish_with, errors, PLAN, TASK

ASK_B = dict(question='Archive 里已有 b.txt，b.txt 怎么处理？', reason='conflict', subject=['Archive/b.txt'], options=[
    dict(label='b.txt 不移动', actions=[]),
    dict(label='覆盖', actions=[dict(effect='trash', path='Archive/b.txt'),
                              dict(effect='move', path='Inbox/b.txt', destination='Archive/b.txt')])])
TWO_PARTS = [dict(id='a', kind='organize', description='把 Inbox/a.txt 移到 Archive'),
             dict(id='b', kind='organize', description='把 Inbox/b.txt 移到 Archive')]
MOVE_A = dict(description='移动 a', goal_ids=['a'], actions=[dict(effect='move', source='Inbox/a.txt', destination='Archive')])
ASK_SCOPE = dict(question='b.txt 也移吗？', reason='scope_beyond_request', subject=['Inbox'], options=[
    dict(label='只移动 a.txt', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive')]),
    dict(label='a.txt 和 b.txt 都移动', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive'),
                                             dict(effect='move', path='Inbox/b.txt', destination='Archive')])])


def clash_b(root):
    files_fixture(root)
    (root / 'Archive/b.txt').write_text('older archived b')


def checks(session):
    return {c['goal_id']: c for c in session.sb.runtime.current['checks']}


@pytest.mark.xfail(strict=True, reason='gap: an option about b bounds the whole next plan, so a cannot be moved')
def test_answering_bs_question_settles_b_by_the_choice_and_a_by_its_work(tmp_path):
    # As in 23 of 24 live asks: the question comes before the task is registered.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    result, client = session.run('A', [step(start(TWO_PARTS), call('prepare_file_plan', MOVE_A, alias=PLAN)),
                                       execute(), finish()])
    assert errors(client) == []
    part = checks(session)
    assert part['a']['outcome'] == 'done' and part['b']['outcome'] == 'by_choice'
    assert result.runtime['task_status'] == 'completed'
    assert (tmp_path / 'Inbox/b.txt').is_file() and (tmp_path / 'Archive/b.txt').read_text() == 'older archived b'


def test_skipping_b_leaves_an_untouched_part_a_unfinished(tmp_path):
    # Fixed 2026-10-23: empty actions cannot tell "do nothing at all" from "leave b
    # alone"; a write part is left out only when the model names it as not done.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    result, client = session.run('A', [step(start(TWO_PARTS)),
                                       finish_with([dict(part_id='b', reason='用户选择 b.txt 不移动')])])
    assert errors(client) == []
    # Only b was asked about; a was never moved, so this choice cannot settle it.
    assert (tmp_path / 'Inbox/a.txt').is_file()
    part = checks(session)
    assert part['b']['outcome'] == 'by_choice'
    assert part['a']['outcome'] == 'unfinished' and result.runtime['task_status'] != 'completed'


def test_an_answer_resumes_the_task_its_question_was_asked_in(tmp_path):
    # 1 of 24 live asks (round 30, blind3_choice_keep_folder): asked after start_task;
    # the next turn spent two rejected calls before revise_task reattached the task.
    # Fixed 2026-10-23: a chosen option resumes that task.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [
        step(start(TWO_PARTS), call('prepare_file_plan', MOVE_A, alias=PLAN)), execute(), step(call('ask_user', ASK_B))])
    result, client = session.run('A', [finish_with([dict(part_id='b', reason='用户选择 b.txt 不移动')])])
    assert errors(client) == []
    assert '已接回提问时的任务' in '\n'.join(m['content'] for m in result.messages if m['role'] == 'system')
    assert result.runtime['task_status'] == 'completed'
    part = checks(session)
    assert part['a']['outcome'] == 'done' and part['b']['outcome'] == 'by_choice'
    assert [o['src'] for o in session.sb.runtime.current['operation_history'] if o['op'] == 'move'] == ['Inbox/a.txt']


@pytest.mark.xfail(strict=True, reason='gap: a resent letter answers whichever question is pending now')
def test_a_resent_letter_does_not_answer_the_follow_up_question(tmp_path):
    # The page sent "A" and the request went through, but the reply was lost; the page
    # puts "A" back in the input box and the user sends it again after the turn ended.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_SCOPE))])
    session.run('A', [step(call('ask_user', ASK_B))])  # the answer to Q1 led to a follow-up Q2
    resent = TaskRuntime('A', dict(H.CFG), session.sb, 'resent')
    assert resent.answered_choice is None  # "A" was the answer to Q1, already used


def test_doing_nothing_at_all_settles_every_part_the_model_names(tmp_path):
    # As in every live "先不动": the option means the whole request.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', dict(ASK_B, options=[
        dict(label='先都不动', actions=[]), ASK_B['options'][1]])))])
    result, _ = session.run('A', [step(start(TWO_PARTS)), finish_with([
        dict(part_id='a', reason='用户选择先都不动'), dict(part_id='b', reason='用户选择先都不动')])])
    assert {g: c['outcome'] for g, c in checks(session).items()} == {'a': 'by_choice', 'b': 'by_choice'}
    assert result.runtime['task_status'] == 'completed'


def test_a_typed_reply_resumes_no_task(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [
        step(start(TWO_PARTS), call('prepare_file_plan', MOVE_A, alias=PLAN)), execute(), step(call('ask_user', ASK_B))])
    typed = TaskRuntime('Inbox 里还有什么？', dict(H.CFG), session.sb, 'typed')
    assert typed.answered_choice['chosen'] is None and typed.current is None


def test_a_new_request_after_a_question_carries_no_option(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    result, client = session.run('先不管这个，把 Inbox/a.txt 移到 Archive', [
        step(start([dict(kind='organize', description='移动 a')]),
             call('prepare_file_plan', dict(description='移动 a', actions=[
                 dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)), execute(), finish()])
    assert errors(client) == []
    runtime = session.sb.runtime
    assert runtime.chosen_option is None and not runtime.answered_choice['chosen']
    # Free text may be an answer: overwrite consent from text is only ever confirmed on the card.
    assert {r['source'] for r in runtime.decisions} <= {'text'}
    assert '不要沿用上一问的选项' in runtime._choice_text()
    # One turn later nothing from the question is left.
    later = TaskRuntime('A', dict(H.CFG), session.sb, 'later')
    assert later.answered_choice is None and later.decisions == []


def test_a_letter_after_an_unrelated_finished_turn_is_not_an_answer(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    session.run('Inbox 里有什么？', [step(call('list_dir', {'path': 'Inbox'}))], final='有 a.txt、b.txt')
    stale = TaskRuntime('A', dict(H.CFG), session.sb, 'stale')
    assert stale.answered_choice is None


def test_the_pending_question_survives_a_restart_and_stays_in_its_conversation(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    # A new process: a fresh sandbox and runtime over the same library and task store.
    from agent.interactions import receive
    sb = H.Sandbox(tmp_path)
    _, cfg = receive(dict(H.CFG), sb, '选择 B：覆盖', pressed='B')
    restarted = TaskRuntime('选择 B：覆盖', cfg, sb, 'after_restart')
    assert restarted.answered_choice['chosen']['label'] == '覆盖'
    assert restarted.decisions == [dict(kind='overwrite', target='Archive/b.txt', source='option', revision=None, note='')]
    _, cfg = receive(dict(H.CFG, task_session='another-conversation'), sb, 'B', pressed='B')
    assert cfg.get('answer_id') is None
    assert TaskRuntime('B', cfg, sb, 'other').answered_choice is None


def test_a_bare_letter_without_a_question_token_grants_no_option(tmp_path):
    # Routing redesign S2: a letter typed or sent without the question's ids is text.
    from agent.interactions import receive
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    _, cfg = receive(dict(H.CFG), session.sb, 'B')
    runtime = TaskRuntime('B', cfg, session.sb, 'typed')
    assert runtime.answered_choice['chosen'] is None and runtime.answered_choice['text'] == 'B'
    assert all(d['source'] == 'text' for d in runtime.decisions)  # the card still confirms text consent


def test_an_answer_whose_files_changed_meanwhile_writes_nothing_unchecked(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_SCOPE))])
    (tmp_path / 'Inbox/b.txt').write_text('changed after the question')
    (tmp_path / 'Inbox/b.txt').rename(tmp_path / 'b_moved_by_user.txt')
    result, client = session.run('B', [step(start([dict(kind='organize', description='都移动')]),
                                            call('prepare_file_plan', dict(description='都移动', actions=[
        dict(effect='move', source='Inbox/a.txt', destination='Archive'),
        dict(effect='move', source='Inbox/b.txt', destination='Archive')])))])
    assert client.calls('prepare_file_plan')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/a.txt').is_file() and not (tmp_path / 'Archive/b.txt').exists() and not session.approvals


# Routing redesign S0 (docs/agent-routing-redesign-2026-10-24.md 2): a choice written
# into the reply instead of ask_user. User log logs/20261024-213101 (names replaced):
# "删掉所有测试文件" ended in plain text with options A-D; the page showed no choice card,
# and the next message "A" had no local record of what A was. The fix is the strict
# terminal of slice S5 (finish_turn), not detecting words in the text.
TEXT_OPTIONS = ('根目录有 2 个测试文件夹——你说的“测试文件”指哪些？\n\n'
                'A. 只回收根目录的测试文件夹（推荐）\nB. 连代码里的测试脚本一起回收\n'
                'C. 先不动，只给我清单')


@pytest.mark.xfail(strict=True, reason='routing redesign S5 (finish_turn) not enabled: plain-text options end as answered')
def test_options_written_in_the_reply_do_not_end_as_an_answer(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('删掉所有测试文件', [step(call('list_dir', dict(path='.')))], final=TEXT_OPTIONS)
    assert result.stop_reason != 'answered'


def test_options_written_in_the_reply_leave_no_question_to_answer(tmp_path):
    # Pins today's behaviour of the gap above: nothing records what A meant.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('删掉所有测试文件', [step(call('list_dir', dict(path='.')))], final=TEXT_OPTIONS)
    assert result.stop_reason == 'answered' and result.final.startswith('根目录有 2 个测试文件夹')
    assert TaskRuntime('A', session.cfg, session.sb, 'next').answered_choice is None
