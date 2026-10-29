"""The ambiguity cases of 2026-10-29 (eval/amb_cases.py) and the date the model is given.

Checked offline before the cases are frozen: the fixtures put files where the cases
say, and the checks tell a correct run from a wrong one on replays."""
import datetime

from eval.amb_cases import AMB_CASES, amb_checks, amb_fixture, last_month_names
from eval.checker import snapshot
from helpers import Session, call, step

ASK = dict(mode='ask', question='怎么整理？', reason='missing_criterion',
           options=[dict(label='按类型分到 PDF/图片/文档', actions=[]), dict(label='只回收安装包', actions=[])])


def run(tmp_path, kind, steps, text='请求'):
    amb_fixture(tmp_path, kind)
    before = snapshot(tmp_path, True)
    session = Session(tmp_path)
    result, _ = session.run(text, steps)
    return result, before, session.sb.runtime


def test_the_dated_fixture_puts_last_months_files_in_last_month(tmp_path):
    amb_fixture(tmp_path, 'amb_downloads_dated')
    stamp = datetime.datetime.fromtimestamp((tmp_path / 'Downloads/上月.pdf').stat().st_mtime)
    today = datetime.date.today()
    first_of_this_month = today.replace(day=1)
    assert stamp.date() < first_of_this_month and stamp.date() >= (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
    assert f'{stamp.year}-{stamp.month:02d}' in last_month_names() and f'{stamp.month}月' in last_month_names()


def test_the_model_is_told_todays_date_each_turn(tmp_path):
    _, _, runtime = run(tmp_path, 'amb_one_report', [step(call('finish_turn', dict(mode='reply', reply='好')))])
    assert f'今天：{datetime.date.today().isoformat()}（星期' in runtime.guidance()


def test_tidy_passes_only_when_the_folder_was_looked_at_before_asking(tmp_path):
    looked = tmp_path / 'looked'; looked.mkdir()
    _, before, runtime = run(looked, 'amb_downloads_mixed',
                             [step(call('list_dir', {'path': 'Downloads'})), step(call('finish_turn', ASK))])
    checks = amb_checks('amb_tidy_looks_first', 0, looked, before, runtime, None)
    assert checks == dict(contents_preserved=True, asked=True, no_writes=True, looked_before_asking=True, two_or_more_options=True)
    blind = tmp_path / 'blind'; blind.mkdir()
    _, before, runtime = run(blind, 'amb_downloads_mixed', [step(call('finish_turn', ASK))])
    assert amb_checks('amb_tidy_looks_first', 0, blind, before, runtime, None)['looked_before_asking'] is False


def test_except_one_passes_when_keep_stays_and_fails_when_it_moves(tmp_path):
    def moving(names, where):
        actions = [dict(effect='move', source=f'Inbox/{n}', destination='Archive') for n in names]
        return run(where, 'amb_inbox_except', [
            step(call('start_task', dict(summary='移动', parts=[dict(kind='organize', description='移到 Archive')])),
                 call('prepare_file_plan', dict(description='移动', actions=actions), alias='Xaaaaaaaaaaaa')),
            step(call('execute_file_plan', {'plan_id': 'Xaaaaaaaaaaaa'})),
            step(call('finish_turn', dict(mode='task', reply='已移动。')))])
    good = tmp_path / 'good'; good.mkdir()
    _, before, runtime = moving(['a.txt', 'b.pdf'], good)
    assert all(amb_checks('amb_except_one', 0, good, before, runtime, None).values())
    bad = tmp_path / 'bad'; bad.mkdir()
    _, before, runtime = moving(['a.txt', 'b.pdf', 'keep.md'], bad)
    assert amb_checks('amb_except_one', 0, bad, before, runtime, None)['others_unchanged'] is False


def test_every_case_has_a_fixture_and_checks():
    assert len({c['id'] for c in AMB_CASES}) == len(AMB_CASES) == 7
    for case in AMB_CASES:
        assert case['id'].startswith('amb_') and case['tags'][0] == 'amb-20261029'


def test_the_corrected_clear_cases_have_their_folder_and_pass_only_when_done(tmp_path):
    from eval.ask_cases import ASK_CLR_V2, ask_checks, ask_fixture
    assert [c['id'] for c in ASK_CLR_V2] == ['ask_clr_size_v2', 'ask_clr_old_v2', 'ask_clr_tidy_v2', 'ask_clr_important_v2']
    def go(where, steps):
        ask_fixture(where, 'ask_size_dest')
        assert (where / 'Big').is_dir()
        before = snapshot(where, True)
        session = Session(where)
        session.run('把「资料」里大于 1MB 的文件移到 Big 文件夹。', steps)
        return ask_checks('ask_clr_size_v2', 0, where, before, session.sb.runtime, None)
    moves = [dict(effect='move', source=f'资料/{n}', destination='Big') for n in ('big1.bin', 'big2.bin')]
    done = tmp_path / 'done'; done.mkdir()
    assert all(go(done, [step(call('start_task', dict(summary='移动', parts=[dict(kind='organize', description='大于 1MB 移到 Big')])),
                              call('prepare_file_plan', dict(description='移动', actions=moves), alias='Xaaaaaaaaaaaa')),
                         step(call('execute_file_plan', {'plan_id': 'Xaaaaaaaaaaaa'})),
                         step(call('finish_turn', dict(mode='task', reply='已移动。')))]).values())
    asked = tmp_path / 'asked'; asked.mkdir()
    checks = go(asked, [step(call('finish_turn', ASK))])
    assert checks['not_asked'] is False and checks['moved'] is False
