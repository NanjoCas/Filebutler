"""2.0 P1-C: option matching v2, option_not_offered, repair requests, pass^k, and the
routing redesign 13 rows the earlier slices left without a test."""
import json

from agent.verifier import verify_task
from helpers import Session, call, step
from test_protocol_v3 import duplicates_fixture, finding, finish, holdout_fixture, read_calls, start


def test_a_rename_option_matches_a_move_rule_but_the_path_stays_exact():
    from eval.live_policy import choose
    rename = dict(key='A', label='改名放进 Archive', actions=[dict(effect='rename', path='Inbox/a.txt', destination='Archive/a (1).txt')])
    other = dict(key='B', label='改名另一份', actions=[dict(effect='rename', path='Inbox/b.txt', destination='Archive/b (1).txt')])
    turn = {'choose_actions': {'has': [dict(effect='move', path='Inbox/a.txt')], 'lacks': [dict(effect='trash')]}}
    assert choose(turn, dict(choices=[other, rename]))[0] == 'A'


def test_a_rule_with_a_precondition_is_tried_only_while_it_holds(tmp_path):
    from eval.live_policy import choose
    (tmp_path / 'Inbox').mkdir(); (tmp_path / 'Inbox/a.txt').write_text('a')
    move_a = dict(key='A', label='移 a', actions=[dict(effect='move', path='Inbox/a.txt', destination='Archive')])
    nothing = dict(key='B', label='都不动', actions=[])
    turn = {'choose_actions': [{'when': {'absent': ['Inbox/a.txt']}, 'has': [], 'lacks': [dict(effect='move')]},
                               {'has': [dict(effect='move', path='Inbox/a.txt')]}]}
    assert choose(turn, dict(choices=[nothing, move_a]), tmp_path)[0] == 'A'
    (tmp_path / 'Inbox/a.txt').unlink()
    assert choose(turn, dict(choices=[nothing, move_a]), tmp_path)[0] == 'B'


def test_a_missing_option_sends_the_frozen_text_and_is_recorded():
    from eval.live_policy import choose
    turn = {'choose_actions': {'has': [dict(effect='trash', path='x')]}, 'otherwise': '回收 x。'}
    text, record = choose(turn, dict(choices=[dict(key='A', label='不动', actions=[])]))
    assert text == '回收 x。' and record['key'] is None and record['option_not_offered']


def _turn(**extra):
    return dict(stop_reason='done', report=dict(state='completed'), task=None, runtime={}, checks=dict(ok=True),
                actual_requests=1, **extra)


def test_option_not_offered_never_passes_v4_and_v3_is_unchanged():
    from eval.judge import judge_turn
    expect = dict(status=['done', 'answered'])
    plain = judge_turn(_turn(), expect, calls=[], history_calls=[], run_id='r')
    missing = judge_turn(_turn(chosen_by_eval=dict(key=None)), expect, calls=[], history_calls=[], run_id='r')
    assert plain['accepted_v4'] and not missing['accepted_v4']
    assert missing['accepted'] == plain['accepted'] and missing['diagnostics']['option_not_offered']


def test_repair_requests_count_requests_whose_every_executed_call_failed():
    from eval.judge import repair_requests
    calls = [dict(step=1, ok=False), dict(step=1, ok=True), dict(step=2, ok=False), dict(step=2, ok=False),
             dict(step=3, ok=False, executed=False), dict(step=4, ok=True)]
    assert repair_requests(calls) == 1


def test_pass_k_counts_runs_uncovered_cases_and_environment_failures(tmp_path):
    from eval.pass_k import summarize
    turn = dict(status='done', diagnostics=dict(option_not_offered=False, repair_requests=0))
    def run(name, accepted, rows):
        d = tmp_path / name; d.mkdir()
        (d / 'manifest.json').write_text(json.dumps(dict(cases=[dict(id='a', turns=['x']), dict(id='b', turns=['x'])])))
        (d / 'report.json').write_text(json.dumps(dict(cases=rows)))
        (d / 'judgement.json').write_text(json.dumps(dict(cases=[
            dict(id=r['id'], overall_accepted=accepted[r['id']], accepted_v4=accepted[r['id']], turns=[turn]) for r in rows])))
        return d
    runs = [run('r1', dict(a=True, b=True), [dict(id='a'), dict(id='b')]),
            run('r2', dict(a=True, b=False), [dict(id='a'), dict(id='b')]),
            run('r3', dict(a=True), [dict(id='a')])]
    s = summarize(runs)
    assert s['k'] == 3 and s['executed'] == 5 and s['uncovered'] == ['r3/b']
    assert s['v4'] == dict(pass_1='4/5', pass_k='1/1')
    blocked = run('r4', dict(a=True, b=True), [dict(id='a'), dict(id='b', status='blocked')])
    assert summarize([blocked])['environment_failures'] == ['r4/b']


# ---------------------------------------------------------------- routing redesign 13 rows


def test_an_empty_dedup_proves_only_its_own_part(tmp_path):
    duplicates_fixture(tmp_path)
    (tmp_path / 'Other').mkdir(); (tmp_path / 'Other/only.txt').write_text('unique')
    session = Session(tmp_path)
    parts = [dict(id='inbox', kind='dedup', description='Inbox 去重'), dict(id='other', kind='dedup', description='Other 去重')]
    session.run('清理', [step(start(parts), call('deduplicate', dict(path='Other', mode='bytes', keep='shortest_name', part='other'))),
                       finish()])
    checks = {c['goal_id']: c['passed'] for c in session.sb.runtime.current['checks']}
    assert checks == {'inbox': False, 'other': True}  # the empty scan proves "other" only
    assert (tmp_path / 'Inbox/copy_of_a.txt').is_file()


def test_evidence_stale_after_the_file_changed_fails_the_mechanical_check(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    session.run('找笔记', [step(*read_calls(['note_01.txt'])), step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                         step(call('report_findings', dict(findings=[finding(0)]))), finish()])
    task = session.sb.runtime.current
    assert [c['passed'] for c in task['checks']] == [True]
    (tmp_path / 'Notes/note_01.txt').write_text('changed after the answer')
    assert [c['passed'] for c in verify_task(task, session.sb)] == [False]


def test_a_plain_reply_takes_one_model_request_and_registers_nothing(tmp_path):
    session = Session(tmp_path)
    result, client = session.run('你好', [step(call('finish_turn', dict(mode='reply', reply='你好。')))])
    assert result.stop_reason == 'answered' and client.requests == 1
    assert session.sb.runtime.current is None
