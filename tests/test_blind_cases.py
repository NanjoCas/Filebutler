"""The blind suite is well formed: fixtures build, checks run, untouched state fails the write cases."""
from types import SimpleNamespace as NS

import pytest

from eval.blind_cases import BLIND_CASES, blind_checks, blind_fixture
from eval.checker import snapshot
from eval.live_policy import SUITES

READ_ONLY = {'blind_count_only', 'blind_vague_criterion', 'blind_injection_in_file'}


def test_suite_is_registered_and_expectations_cover_every_turn():
    assert SUITES['blind'] is BLIND_CASES and len(BLIND_CASES) == 8
    assert len({c['id'] for c in BLIND_CASES}) == 8
    for case in BLIND_CASES:
        assert len(case['expect']['turns']) == len(case['turns'])


@pytest.mark.parametrize('case', BLIND_CASES, ids=lambda c: c['id'])
def test_checks_run_and_doing_nothing_only_passes_read_only_cases(tmp_path, case):
    blind_fixture(tmp_path, case['fixture'])
    before = snapshot(tmp_path, True)
    result = blind_checks(case['id'], 0, tmp_path, before, NS(current=None), None)
    assert result['contents_preserved']
    assert all(result.values()) == (case['id'] in READ_ONLY)


from eval.blind_cases import BLIND2_CASES, blind2_checks, blind2_fixture

READ_ONLY2 = {'blind2_largest_file', 'blind2_unsupported_copy'}


def test_second_set_is_registered_and_distinct():
    assert SUITES['blind2'] is BLIND2_CASES and len(BLIND2_CASES) == 8
    assert not {c['id'] for c in BLIND2_CASES} & {c['id'] for c in BLIND_CASES}
    for case in BLIND2_CASES:
        assert len(case['expect']['turns']) == len(case['turns'])


@pytest.mark.parametrize('case', BLIND2_CASES, ids=lambda c: c['id'])
def test_second_set_checks_run_and_doing_nothing_fails_write_cases(tmp_path, case):
    blind2_fixture(tmp_path, case['fixture'])
    before = snapshot(tmp_path, True)
    result = blind2_checks(case['id'], 0, tmp_path, before, NS(current=None), None)
    assert result['contents_preserved']
    assert all(result.values()) == (case['id'] in READ_ONLY2)
