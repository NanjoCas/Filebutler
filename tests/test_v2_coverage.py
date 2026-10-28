from types import SimpleNamespace

from agent import coverage
from tools.retrieval import coverage as search_coverage


def scan(state='indexed', pending=0):
    return dict(scope='.', snapshot_id='same', index_revision='r', scan_id='S',
                candidates=['scan.pdf'], candidate_truncated=False, matched_files=1,
                pending_ocr_units=pending, ocr_failed_units=0, enumeration_complete=True,
                files={'scan.pdf': dict(version=[100, 1], state=state)})


def test_ocr_completion_refreshes_the_file_even_if_its_content_version_is_unchanged():
    ledger = coverage.record_scan(None, scan('pending_ocr', 1), 'req1')
    ledger = coverage.record_scan(ledger, scan(), 'req1')['.']
    assert ledger['files']['scan.pdf']['state'] == 'indexed'
    assert ledger['files']['scan.pdf']['status'] == 'unreviewed'
    assert coverage.summary(ledger)['excluded'] == 0


def test_a_previous_requests_read_is_recorded_but_not_counted_for_this_request():
    ledgers = coverage.record_scan(None, scan(), 'req1')
    ledgers['.']['files']['scan.pdf'].update(status='reviewed', reason='topic A')
    ledgers = coverage.record_scan(ledgers, scan(), 'req2')
    assert coverage.summary(ledgers['.'])['reviewed'] == 0
    assert coverage.summary(ledgers['.'])['previously_read'] == 1
    assert '以前读过' in coverage.table(ledgers)
    again = coverage.record_scan(ledgers, scan(), 'req2')
    assert coverage.summary(again['.'])['previously_read'] == 1


def test_a_same_request_rescan_keeps_its_valid_review():
    ledgers = coverage.record_scan(None, scan(), 'req1')
    ledgers['.']['files']['scan.pdf'].update(status='reviewed', reason='read')
    again = coverage.record_scan(ledgers, scan(), 'req1')
    assert coverage.summary(again['.'])['reviewed'] == 1


def test_query_parameters_are_part_of_the_scan_identity():
    index = SimpleNamespace(states={'a.txt': ((5, 1), 'indexed', 0, 0)})
    def identity(query, k=5):
        return search_coverage(index, '.', {'matched': 1}, [{'path': 'a.txt'}], k, query)['scan_id']
    assert len({identity('alpha'), identity('beta'), identity('alpha', 2)}) == 3


def test_only_the_displayed_page_is_counted_as_returned_candidates(tmp_path):
    import json
    from tests.test_file_plans import runtime
    from tools import registry
    for i in range(6):
        (tmp_path / f'paper{i}.txt').write_text('dengue prediction')
    r = runtime(tmp_path, task=False)
    result = registry.dispatch_result('search_content', json.dumps(dict(query='dengue', top_k=6, view='evidence')), r.sb)
    covered = result.data['coverage']
    assert covered['returned_files'] == len(covered['candidates']) == len(result.references)
    assert covered['returned_files'] < 6 and covered['retained_files'] == 6
