"""2.0 P1-B: one place for the plan-level goal fallback."""
from agent.completion import step_owners
from agent.file_plans import prepare
from tests.test_file_plans import proposal, runtime


def test_new_plans_give_every_step_its_own_goals(tmp_path):
    (tmp_path/'a.txt').write_text('a')
    r = runtime(tmp_path)
    plan = prepare(r, proposal(actions=[dict(effect='move', source='a.txt', destination='out/a.txt')]))
    assert [s['effect'] for s in plan['payload']['steps']] == ['create', 'move']
    assert all(s['goal_ids'] == ['files'] for s in plan['payload']['steps'])


def test_only_plans_from_before_strict_attribution_fall_back_to_the_plan_goals():
    legacy = dict(payload=dict(goal_ids=['a', 'b'], steps=[dict(id='X:0', effect='move')]))
    assert step_owners(legacy, legacy['payload']['steps'][0]) == ['a', 'b']
    assert step_owners(legacy, dict(id='X:1', effect='move', goal_ids=['a'])) == ['a']
