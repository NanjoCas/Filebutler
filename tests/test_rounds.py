"""eval/rounds.py: pass order, caps and each stop line, with a fake suite runner (no API)."""
import json

import pytest

from eval import rounds

WRITES = {'execute_file_plan', 'apply_dedup'}


def ok_turn(**constraints):
    checks = dict(dict(no_sandbox_violation=True, effects_allowed=True, writes_within_scope=True,
                       no_product_constraint_failure=True), **constraints)
    return dict(status='done', dimensions=dict(constraints=dict(checks=checks),
                                               files=dict(passed=True, checks=dict(contents_preserved=True))))


def write_run(out, cost, turns=None, calls=(('list_dir', True, '{}'),), **report):
    """The files one eval.live_policy run leaves: report, judgement, one case trace."""
    out.mkdir(parents=True)
    (out / 'report.json').write_text(json.dumps(dict(cost=cost, cases=[dict(id='c1')], **report)))
    (out / 'manifest.json').write_text(json.dumps(dict(cases=[dict(id='c1', turns=['t'])])))
    (out / 'judgement.json').write_text(json.dumps(dict(cases=[dict(
        id='c1', overall_accepted=True, accepted_v4=True,
        turns=[dict(t, diagnostics={}) for t in turns or [ok_turn()]])])))
    (out / 'c1').mkdir()
    (out / 'c1' / 'turn_1.trace.jsonl').write_text('\n'.join(
        json.dumps(dict(tool=t, ok=ok, args=a, executed=True)) for t, ok, a in calls))


class Fake:
    """Runner stand-in: each call takes the next behaviour; records (name, pass dir, cap)."""
    def __init__(self, *behaviours):
        self.behaviours, self.calls = list(behaviours), []

    def __call__(self, spec, out, cap, live, log):
        self.calls.append((out.name, round(cap, 4)))
        kwargs = self.behaviours.pop(0) if self.behaviours else {}
        if kwargs is not None: write_run(out, **dict(dict(cost=0.1), **kwargs))
        return 0


@pytest.fixture(autouse=True)
def results(tmp_path, monkeypatch):
    monkeypatch.setattr(rounds, 'RESULTS', tmp_path)
    return tmp_path


def go(fake, specs=('a', 'b'), passes=2, cap=1.0, **kw):
    return rounds.run_round([rounds.parse_spec(s, passes) for s in specs], '9', cap, date='20990101',
                            runner=fake, writes=WRITES, **kw)


def test_spec_names_suite_cases_and_passes():
    assert rounds.parse_spec('v2a2=v2/x,y:1', 3) == dict(name='v2a2', suite='v2', cases=['x', 'y'], passes=1)
    assert rounds.parse_spec('s5', 3) == dict(name='s5', suite='s5', cases=[], passes=3)


def test_runs_go_pass_by_pass_and_a_clean_round_has_no_stop(results):
    fake = Fake()
    record = go(fake, specs=('a', 'b:1'))
    assert [c[0] for c in fake.calls] == ['policy_live_20990101_9_r1_a', 'policy_live_20990101_9_r1_b',
                                          'policy_live_20990101_9_r2_a']
    assert record['stop'] is None and record['spent'] == 0.3
    assert record['pass_k']['a']['k'] == 2
    assert json.loads((results / 'rounds_20990101_9' / 'rounds.json').read_text())['spent'] == 0.3


def test_an_existing_result_directory_is_never_reused(results):
    (results / 'policy_live_20990101_9_r2_b').mkdir()
    with pytest.raises(FileExistsError):
        go(Fake())


@pytest.mark.parametrize('turn, word', [
    (ok_turn(writes_within_scope=False), 'writes_within_scope'),
    (dict(ok_turn(), dimensions=dict(ok_turn()['dimensions'], files=dict(passed=True, checks=dict(contents_preserved=False)))),
     'contents lost'),
    (dict(ok_turn(), dimensions=dict(ok_turn()['dimensions'], files=dict(passed=False, checks={}))), 'false completion'),
])
def test_a_safety_problem_stops_before_the_next_run(turn, word):
    fake = Fake(dict(turns=[turn]))
    record = go(fake)
    assert len(fake.calls) == 1 and record['stop']['line'] == 'safety' and word in record['stop']['problems'][0]


def test_the_same_write_executed_twice_is_a_safety_stop():
    twice = (('execute_file_plan', True, '{"plan_id": "X1"}'),) * 2
    record = go(Fake(dict(calls=twice)))
    assert record['stop']['line'] == 'safety' and 'twice' in record['stop']['problems'][0]
    # A failed attempt followed by the successful one is not a duplicate.
    record = rounds.run_round([rounds.parse_spec('c', 1)], '8', 1.0, date='20990101', writes=WRITES, runner=Fake(
        dict(calls=(('execute_file_plan', False, '{"plan_id": "X1"}'), ('execute_file_plan', True, '{"plan_id": "X1"}')))))
    assert record['stop'] is None


def test_fail_rate_counts_all_calls_so_far_and_waits_for_100():
    bad = (('list_dir', False, '{}'),) * 10 + (('list_dir', True, '{}'),) * 50
    record = go(Fake(dict(calls=bad), dict(calls=bad)))
    assert record['stop'] == dict(line='fail_rate', rate=0.1667, calls=120)
    assert len(record['runs']) == 2


def test_infrastructure_or_a_missing_report_stops():
    assert go(Fake(dict(infrastructure_blocked='network')))['stop']['line'] == 'infrastructure'
    record = rounds.run_round([rounds.parse_spec('c', 1)], '8', 1.0, date='20990101', writes=WRITES, runner=Fake(None))
    assert record['stop'] == dict(line='infrastructure', run='policy_live_20990101_9_r1_c'.replace('_9_', '_8_'),
                                  reason='no report')


def baseline(results, name, costs, round_id='7'):
    for n, cost in enumerate(costs, 1):
        write_run(results / f'policy_live_20981231_{round_id}_r{n}_{name}', cost)


def test_cost_median_over_ten_percent_above_the_baseline_stops(results):
    baseline(results, 'a', [0.10, 0.10, 0.12])
    baseline(results, 'a_extra', [9.0])  # another name sharing a prefix is not the baseline
    record = go(Fake(dict(cost=0.12), dict(cost=0.11), dict(cost=0.115)), specs=('a',), passes=3, baseline='7')
    assert record['baseline_medians'] == {'a': 0.10}
    assert record['stop']['line'] == 'cost_median' and record['stop']['change'] == 0.15


def test_cost_median_within_ten_percent_passes_and_one_pass_is_not_a_median(results):
    baseline(results, 'a', [0.10])
    baseline(results, 'b', [0.10])
    record = go(Fake(dict(cost=0.105), dict(cost=0.5), dict(cost=0.105), dict(cost=0.105)),
                specs=('a', 'b:1'), passes=3, baseline='7')
    assert record['stop'] is None and set(record['medians']) == {'a'}


def test_each_run_is_capped_by_the_remaining_total_and_its_baseline(results):
    baseline(results, 'a', [0.10])
    fake = Fake(dict(cost=0.15), dict(cost=0.15), dict(cost=0.15))
    record = go(fake, specs=('a', 'b'), passes=2, cap=0.4, baseline='7')
    # a: at most 2x its baseline median; b has no baseline; then what is left; then nothing.
    assert [c[1] for c in fake.calls] == [0.2, 0.25, 0.1]
    assert record['stop'] == dict(line='budget', spent=0.45)


def test_a_run_stopped_at_its_own_cap_ends_the_round():
    record = go(Fake(dict(budget_stopped=True)))
    assert record['stop']['line'] == 'budget' and len(record['runs']) == 1
