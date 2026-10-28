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


ASK_B_SCOPED = dict(ASK_B, drafts=[dict(id='d_b', description='把 Inbox/b.txt 移到 Archive', subjects=['Inbox/b.txt'])],
                    scope=dict(level='parts', part_ids=['d_b']),
                    options=[dict(ASK_B['options'][0], disposition='skip'), ASK_B['options'][1]])
TWO_PARTS_MAPPED = [TWO_PARTS[0], dict(TWO_PARTS[1], draft_id='d_b')]


def test_answering_bs_question_settles_b_by_the_choice_and_a_by_its_work(tmp_path):
    # Routing redesign S3: the question names its scope (a draft part, as in 23 of 24
    # live asks it comes before the task); the option about b bounds and settles b only.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B_SCOPED))])
    result, client = session.run('A', [step(start(TWO_PARTS_MAPPED), call('prepare_file_plan', MOVE_A, alias=PLAN)),
                                       execute(), finish()])
    assert errors(client) == []
    part = checks(session)
    assert part['a']['outcome'] == 'done' and part['b']['outcome'] == 'by_choice'
    assert result.runtime['task_status'] == 'completed'
    assert (tmp_path / 'Inbox/b.txt').is_file() and (tmp_path / 'Archive/b.txt').read_text() == 'older archived b'


def test_a_skipped_part_gets_no_plan_and_an_unmapped_draft_settles_nothing(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B_SCOPED))])
    _, client = session.run('A', [step(start(TWO_PARTS))])  # b not mapped to its draft
    assert 'UNRESOLVED_QUESTION_SCOPE' in client.calls('start_task')[0]['text']
    clash_b(tmp_path / 'again'); again = Session(tmp_path / 'again')
    again.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_B_SCOPED))])
    _, client = again.run('A', [step(start(TWO_PARTS_MAPPED), call('prepare_file_plan', dict(
        description='移动 b', actions=[dict(effect='move', source='Inbox/b.txt', destination='Archive/b (1).txt', part='b')])))])
    assert '跳过部分 b' in client.calls('prepare_file_plan')[0]['text']


def test_a_skip_option_needs_a_parts_scope_and_cancel_needs_the_request(tmp_path):
    clash_b(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('把 b.txt 移到 Archive', [step(call('ask_user', dict(ASK_B, options=[
        dict(ASK_B['options'][0], disposition='skip'), ASK_B['options'][1]])))], final='好的。')
    assert 'scope.level=parts' in client.calls('ask_user')[0]['text']
    _, client = session.run('把 b.txt 移到 Archive', [step(call('ask_user', dict(ASK_B, scope=dict(level='parts', part_ids=['d_x']),
        drafts=[dict(id='d_b', description='b')])))], final='好的。')
    assert 'scope.part_ids 引用了不存在的部分' in client.calls('ask_user')[0]['text']


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


def test_a_resent_letter_does_not_answer_the_follow_up_question(tmp_path):
    # The page sent "A" and the request went through, but the reply was lost; the page
    # sends it again after the turn ended, with the same message id (routing redesign S2-S3).
    from agent.interactions import Interactions, receive
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_SCOPE))])
    first, cfg = receive(session.cfg, session.sb, 'A', client_message_id='page-1', pressed='A')
    q1 = Interactions(TaskRuntime('A', cfg, session.sb, 'q1').store)
    session.run('B', [step(call('ask_user', ASK_B))])  # stands for the follow-up question Q2
    q2 = q1.open_question()
    import json
    import pytest as _pytest
    from agent.interactions import InteractionError
    sent = json.loads(q1.receipt(first['receipt_id'])['payload_json'])['response']  # what the page kept
    again, _ = receive(session.cfg, session.sb, 'A', client_message_id='page-1', response=sent)
    assert again['existing'] and again['receipt_id'] == first['receipt_id'] and again['answer_id'] == first['answer_id']
    # Pressing A on the question shown now, under the old id, is a different message: refused.
    with _pytest.raises(InteractionError) as error:
        receive(session.cfg, session.sb, 'A', client_message_id='page-1', pressed='A')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    assert q1.open_question()['question_id'] == q2['question_id']  # Q2 still waits for its own answer


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
    # Routing redesign S3: text after a question must say what it is before any write.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    move_a = call('prepare_file_plan', dict(description='移动 a', actions=[
        dict(effect='move', source='Inbox/a.txt', destination='Archive')]), alias=PLAN)
    _, client = session.run('先不管这个，把 Inbox/a.txt 移到 Archive', [
        step(start([dict(kind='organize', description='移动 a')]), move_a)], final='好的。')
    assert 'INPUT_RELATION_REQUIRED' in client.calls('prepare_file_plan')[0]['text'] and (tmp_path / 'Inbox/a.txt').is_file()
    session.run('把 b.txt 移到 Archive', [step(call('ask_user', ASK_B))])
    result, client = session.run('先不管这个，把 Inbox/a.txt 移到 Archive', [
        step(start([dict(kind='organize', description='移动 a')], input_resolution=dict(kind='new_request')), move_a),
        execute(), finish()])
    assert errors(client) == [] and result.stop_reason == 'done'
    runtime = session.sb.runtime
    # A new request: neither the option nor text consent to overwrite carries over.
    assert runtime.chosen_option is None and runtime.answered_choice is None and runtime.decisions == []
    from agent.interactions import Interactions
    assert Interactions(runtime.store).open_question() is None  # the question waits, suspended
    later = TaskRuntime('A', dict(H.CFG), session.sb, 'later')
    assert later.decisions == []


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


def test_options_written_in_the_reply_do_not_end_as_an_answer(tmp_path):
    # S5: a model that ends with bare text is corrected once; ending so again is not an answer.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = session.run('删掉所有测试文件', [step(call('list_dir', dict(path='.')))], final=TEXT_OPTIONS, bare=True)
    assert result.stop_reason == 'unstructured_terminal' and client.requests == 3
    assert result.final.startswith('根目录有 2 个测试文件夹') and '没有形成完整的交互' in result.final
    assert any(m['role'] == 'system' and 'TERMINAL_REQUIRED' in m['content'] for m in result.messages)


def test_a_model_corrected_once_asks_with_buttons(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    ask = dict(question='你说的测试文件指哪些？', reason='multiple_candidates', subject=['.'], options=[
        dict(label='只回收测试文件夹', actions=[]), dict(label='先不动', actions=[])])

    class Corrected(H.Replay):
        def create(self, **kw):
            if any(m['role'] == 'system' and 'TERMINAL_REQUIRED' in (m.get('content') or '') for m in kw['messages']):
                self.steps = [step(call('finish_turn', dict(mode='ask', **ask)))]
                self.bare = False
            return super().create(**kw)

    from agent.interactions import receive
    _, cfg = receive(session.cfg, session.sb, '删掉所有测试文件')
    client = Corrected([], session.ids, TEXT_OPTIONS, bare=True)
    from rich.console import Console
    import io
    from agent.llm import LLM
    from agent.loop import run_agent
    result = run_agent('删掉所有测试文件', LLM({'model': 'fake'}, client=client), session.sb, cfg,
                       Console(file=io.StringIO()), artifacts=session.store)
    assert result.stop_reason == 'waiting_input' and result.report['question_id'].startswith('Q')
    assert [c['label'] for c in result.report['choices']] == ['只回收测试文件夹', '先不动']


ASK_BOTH_SCOPED = dict(ASK_SCOPE, reason='conflict', drafts=[dict(id='d_a', description='移动 a'), dict(id='d_b', description='移动 b')],
                       scope=dict(level='parts', part_ids=['d_a', 'd_b']),
                       options=[dict(ASK_SCOPE['options'][0], disposition='continue'), dict(
                           label='两份都保留', disposition='continue', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive'),
                                                         dict(effect='move', path='Inbox/b.txt', destination='Archive/b (1).txt')])])


def test_a_continue_choice_about_parts_settles_the_untouched_ones_in_scope(tmp_path):
    # Round 42 r2 (s5_skip_b): "只移 a" marked continue; b untouched but left for review.
    clash_b(tmp_path)
    session = Session(tmp_path)
    session.run('把 a.txt 和 b.txt 移到 Archive', [step(call('ask_user', ASK_BOTH_SCOPED))])
    mapped = [dict(TWO_PARTS[0], draft_id='d_a'), dict(TWO_PARTS[1], draft_id='d_b')]
    result, client = session.run('A', [step(start(mapped), call('prepare_file_plan', MOVE_A, alias=PLAN)), execute(),
                                       step(call('finish_turn', dict(mode='task', not_needed=[dict(part_id='b', reason='按选择 b 不动')])))])
    assert errors(client) == []
    part = checks(session)
    assert part['a']['outcome'] == 'done' and part['b']['outcome'] == 'by_choice'
    assert result.runtime['task_status'] == 'completed'


def test_an_empty_continue_choice_about_its_part_is_by_choice(tmp_path):
    # Round 42 r2 (s5_nothing_by_choice): "先不移动" marked continue, then unfinished; was partial.
    clash_b(tmp_path)
    session = Session(tmp_path)
    one = [TWO_PARTS[1]]
    session.run('把 b.txt 移到 Archive', [step(start(one), call('ask_user', dict(
        ASK_B, scope=dict(level='parts', part_ids=['b']), options=[dict(o, disposition='continue') for o in ASK_B['options']])))])
    result, client = session.run('A', [finish_with([dict(part_id='b', reason='用户选择先不移动')])])
    assert errors(client) == []
    assert checks(session)['b']['outcome'] == 'by_choice' and result.report.get('result') == 'by_choice'
    assert (tmp_path / 'Inbox/b.txt').is_file()


def ask_b_with_question_noted(session):
    one = [TWO_PARTS[1]]
    session.run('把 b.txt 移到 Archive', [step(start(one), call('ask_user', dict(
        ASK_B, scope=dict(level='parts', part_ids=['b']))))])
    # 2.0 no longer accepts questions at registration. Simulate an old stored task
    # to keep covering the real round-45 compatibility case.
    runtime = session.sb.runtime
    runtime.current['unresolved_questions'] = ['Archive 里已有 b.txt，要怎么处理？']
    runtime.store.checkpoint(runtime.current)
    assert session.sb.runtime.current['unresolved_questions']


def test_an_exact_answer_resolves_the_question_the_task_noted(tmp_path):
    # Round 45 r1/r2 (s5_nothing_by_choice): the task noted the question as text when it
    # was registered; the button answer left it, so the task waited and asked it again.
    clash_b(tmp_path)
    session = Session(tmp_path)
    ask_b_with_question_noted(session)
    result, client = session.run('A', [finish_with([dict(part_id='b', reason='用户选择先不移动')])])
    assert errors(client) == []
    assert result.runtime['task_status'] == 'completed' and '待确认' not in result.final
    task = session.sb.runtime.current
    assert task['unresolved_questions'] == ['Archive 里已有 b.txt，要怎么处理？']
    assert 'answered_questions' not in task


def test_an_exact_answer_opens_the_writes_its_noted_question_held(tmp_path):
    # Round 43/45 (s5_skip_b, ans_skip_b): the leftover question refused the chosen overwrite once.
    clash_b(tmp_path)
    session = Session(tmp_path)
    ask_b_with_question_noted(session)
    overwrite = dict(description='覆盖 b', goal_ids=['b'], actions=[
        dict(effect='trash', source='Archive/b.txt'), dict(effect='move', source='Inbox/b.txt', destination='Archive')])
    result, client = session.run('B', [step(call('prepare_file_plan', overwrite, alias=PLAN)), execute(), finish()])
    assert errors(client) == []
    assert (tmp_path / 'Archive/b.txt').read_text() != 'older archived b' and result.runtime['task_status'] == 'completed'
