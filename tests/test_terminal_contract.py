"""The finish_turn contract after the S5 audit (docs/s5-routing-audit-2026-10-25.md 4.3, 5).
Temporary synthetic libraries, scripted models; no API."""
import json

import helpers as H
from helpers import Session, step, call, files_fixture
from test_protocol_v3 import start, execute, finish_with, errors, PLAN, ARCHIVE_A
from agent.interactions import Interactions

ASK = dict(question='Archive 里已有 a.txt，怎么处理？', reason='conflict', subject=['Archive'],
           options=[dict(label='不移动', actions=[]),
                    dict(label='保留两份', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive/a (1).txt')])])


def test_a_registered_tasks_reply_is_in_the_main_answer_once(tmp_path):
    # P1-A: the reply was moved into the details when a task ended by mode=reply.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    told = '额外回答：a 属于项目红色，这是用户要求的说明。'
    result, _ = session.run('归档 a 并解释所属项目', [
        step(start([dict(kind='organize', description='归档 a')]),
             call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A), alias=PLAN)),
        execute(), step(call('finish_turn', dict(mode='reply', reply=told)))])
    assert result.stop_reason == 'done' and result.final.count(told) == 1
    assert result.final.index('已移动 1 个文件') < result.final.index(told)  # local facts lead


def test_invalid_terminal_calls_are_bounded_by_the_no_progress_guard(tmp_path):
    # P1-B: eight differently worded invalid calls used to need nine requests.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    invalid = [step(call('finish_turn', dict(mode='ask', reply=f'draft{i}'))) for i in range(8)]
    result, client = session.run('问一下', invalid, final='ok')
    assert result.stop_reason == 'repair_limit' and client.requests == H.CFG.get('task_repair_limit', 6)


def test_asking_refuses_settling_fields_and_shows_the_reply_first(tmp_path):
    # P2-A: an ask carrying unfinished / evidence claims silently dropped them.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    _, client = session.run('归档 a', [step(start([dict(id='g', kind='organize', description='归档 a')]),
                                           call('finish_turn', dict(mode='ask', **ASK, unfinished=[dict(part_id='g', reason='x')])))],
                            final='好的。')
    assert 'mode=ask 只提问并等待用户，不能带 unfinished' in client.calls('finish_turn')[0]['text']
    result, _ = session.run('归档 a', [step(call('finish_turn', dict(mode='ask', reply='Archive 里已有一个 a.txt。', **ASK)))])
    assert result.stop_reason == 'waiting_input' and result.final.startswith('Archive 里已有一个 a.txt。')


def test_a_rejected_calls_machine_code_matches_its_text(tmp_path):
    # P2-C: TURN_CLOSED in the text, CONTROL_REJECTED in the structured result.
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('你好', [step(call('finish_turn', dict(mode='reply', reply='你好。')), call('list_dir', dict(path='.')))])
    tool = [json.loads(m['content']) for m in result.messages if m['role'] == 'tool'][-1]
    assert tool['error_code'] == 'TURN_CLOSED' and tool['text'].startswith('ERROR: TURN_CLOSED')


def test_options_written_into_a_reply_still_end_as_an_answer(tmp_path):
    # P2-B, a known limit: the schema shows whether the turn ended properly, not whether the
    # model picked the right mode. Measured in live runs as "a choice without buttons".
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, _ = session.run('删掉测试文件', [step(call('finish_turn', dict(mode='reply', reply='你指哪些？\nA. 文件夹\nB. 脚本')))])
    assert result.stop_reason == 'answered' and not result.report.get('question_id')


def test_choosing_to_change_nothing_reads_the_same_with_or_without_a_task(tmp_path):
    # 4.3: the business result follows the user's choice, not whether the model registered a task.
    results = {}
    for path in ('without_task', 'with_task'):
        root = tmp_path / path; root.mkdir()
        files_fixture(root)
        (root / 'Archive/a.txt').write_text('older a')
        session = Session(root)
        first = [step(start([dict(id='a', kind='organize', description='把 a 移到 Archive')]))] if path == 'with_task' else []
        session.run('把 a 移到 Archive', first + [step(call('finish_turn', dict(mode='ask', **ASK)))])
        end = (finish_with([dict(part_id='a', reason='用户选择不移动')]) if path == 'with_task'
               else step(call('finish_turn', dict(mode='reply', reply='好的，不动。'))))
        result, client = session.run('A', [end])
        assert errors(client) == [], errors(client)
        assert (root / 'Inbox/a.txt').is_file() and (root / 'Archive/a.txt').read_text() == 'older a'
        assert result.final.startswith('已按你的选择保持原样（A（不移动）') and '符合要求' not in result.final
        rid = (session.sb.runtime.current or {}).get('request_id') or session.sb.runtime.cfg['request_id']
        request = Interactions(session.sb.runtime.store).request(rid)
        results[path] = (result.report.get('result'), result.report.get('display'), request['status'],
                         json.loads(request['decision_json'])['label'])
    assert results['without_task'] == results['with_task'] == ('by_choice', 'chosen', 'settled', '不移动')


def test_the_business_check_applies_only_to_cases_that_state_it():
    from eval.judge import judge_status
    norm = dict(status='answered', business='by_choice')
    assert judge_status(norm, dict(status=['answered'], business=['by_choice']))[0] == dict(
        status_in_expected=True, business_in_expected=True)
    assert judge_status(dict(norm, business='answered'), dict(status=['answered'], business=['by_choice']))[0][
        'business_in_expected'] is False
    assert judge_status(norm, dict(status=['done']))[0] == dict(status_in_expected=False)  # older cases unchanged


def test_every_s5_case_has_a_fixture_and_checks(tmp_path):
    from eval.live_policy import SUITES, fixture, checks
    from eval.checker import snapshot
    for case in SUITES['s5']:
        root = tmp_path / case['id']; root.mkdir()
        fixture(root, case['fixture'])
        before = snapshot(root, True)
        assert checks(case['id'], 0, root, before, None, None, None)['contents_preserved']
