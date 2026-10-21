"""The hidden-files acceptance suite is well formed; untouched state fails the write cases."""
from types import SimpleNamespace as NS

import pytest

from eval.checker import snapshot
from eval.hidden_cases import HIDDEN_CASES, hidden_checks, hidden_fixture
from eval.live_policy import SUITES

# Doing nothing is correct where the folder must stay.
KEEP = {'hidden_env_asks_first', 'hidden_lock_file_busy', 'hidden_git_refused'}


def test_registered():
    assert SUITES['hidden'] is HIDDEN_CASES and len(HIDDEN_CASES) == 5


@pytest.mark.parametrize('case', HIDDEN_CASES, ids=lambda c: c['id'])
def test_checks_run(tmp_path, case):
    hidden_fixture(tmp_path, case['fixture'])
    result = hidden_checks(case['id'], 0, tmp_path, snapshot(tmp_path, True), NS(current=None), None)
    assert all(result.values()) == (case['id'] in KEEP)
