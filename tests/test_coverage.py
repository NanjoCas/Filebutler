"""Search coverage and its ledger (routing redesign S7, 9 and 9.1). Temporary files only."""
import json

import pymupdf

from agent import coverage
from helpers import Session, step, call, Sandbox
from test_protocol_v3 import start, finish
from tools import registry
import tools.retrieval as retrieval


def library(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def scan(root, query, **args):
    sb = Sandbox(root)
    out = registry.dispatch_result('search_content', json.dumps(dict(query=query, **args)), sb)
    assert out.status == 'ok', out.text
    return sb, out


def test_a_search_states_what_it_covered_and_that_top_k_is_not_everything(tmp_path):
    library(tmp_path, {f'Notes/n{i}.txt': f'photosynthesis note {i}' for i in range(6)} | {'Notes/x.zip': 'zz'})
    _, out = scan(tmp_path, 'photosynthesis', top_k=2)
    c = out.data['coverage']
    assert (c['indexed_files'], c['unsupported_files'], c['matched_files'], c['returned_files']) == (6, 1, 6, 2)
    assert c['candidate_truncated'] and c['extract_complete'] and c['enumeration_complete']
    assert c['scan_id'].startswith('S') and c['index_revision'] == retrieval.INDEX_REVISION
    assert '覆盖缺口' in out.text and '只返回 2 个' in out.text


def test_pages_left_for_ocr_stay_pending_across_searches(tmp_path, monkeypatch):
    (tmp_path / 'Scans').mkdir()
    with pymupdf.open() as doc:  # a page with no text layer is recognised by OCR
        doc.new_page(); doc.save(tmp_path / 'Scans/blank.pdf')
    (tmp_path / 'Scans/note.txt').write_text('budget report')
    monkeypatch.setattr(retrieval, 'OCR_SECONDS', -1)  # no time left: the page is left for later
    sb, out = scan(tmp_path, 'budget')
    first = out.data['coverage']
    assert first['pending_ocr_units'] == 1 and not first['extract_complete']
    second = registry.dispatch_result('search_content', json.dumps(dict(query='budget')), sb).data['coverage']
    assert second['pending_ocr_units'] == 1, 'counted from the file state, not from this call'
    assert '1 个因时间上限未识别' in out.text


def test_a_changed_library_is_a_new_snapshot_and_old_reviews_are_not_reused_for_new_files():
    files = {f'n{i}.txt': dict(version=[1, i], state='indexed') for i in range(10)}
    first = dict(scope='Notes', snapshot_id='s1', index_revision='r', files=files, scan_id='S1', candidates=[],
                 candidate_truncated=False, matched_files=10, pending_ocr_units=0, ocr_failed_units=0, enumeration_complete=True)
    ledgers = coverage.record_scan(None, first, 'REQ')
    for rel in files: ledgers['Notes']['files'][rel].update(status='reviewed', reason='读过正文')
    assert coverage.summary(ledgers['Notes'])['reviewed'] == 10 and not coverage.gap_line(coverage.summary(ledgers['Notes']))
    changed = dict(files, **{'n3.txt': dict(version=[2, 3], state='indexed'), 'n10.txt': dict(version=[1, 10], state='indexed')})
    ledgers = coverage.record_scan(ledgers, dict(first, snapshot_id='s2', scan_id='S2', files=changed), 'REQ')
    s = coverage.summary(ledgers['Notes'])
    assert (s['total'], s['reviewed'], s['unreviewed'], s['inherited']) == (11, 9, 2, 9)
    assert sorted(s['changed']) == ['n10.txt', 'n3.txt']
    assert '资料库在检索之后有变化，2 个新增或变更的文件尚未核对' in coverage.gap_line(s)


def test_a_find_task_reports_its_coverage_and_a_known_gap_in_the_answer(tmp_path):
    library(tmp_path, {f'Notes/n{i}.txt': f'photosynthesis note {i} ' * 20 for i in range(6)})
    session = Session(tmp_path)
    result, _ = session.run('找讲光合作用的笔记', [
        step(start([dict(id='find', kind='find', description='光合作用笔记')])),
        step(call('search_content', dict(query='photosynthesis', top_k=2, view='evidence'))),
        step(call('read_file', dict(path='Notes/n0.txt'))), finish()])
    ledger = session.sb.runtime.current['coverage']['Notes' if 'Notes' in session.sb.runtime.current['coverage'] else '.']
    s = coverage.summary(ledger)
    assert (s['total'], s['reviewed'], s['truncated']) == (6, 1, True)
    assert '已核对 1 个，其余未核对' in result.final and '不代表已经找全' in result.final
    assert any(sec['title'] == '检索覆盖' for sec in result.report['sections'])


def test_an_excluded_file_says_why_and_is_not_counted_as_unreviewed():
    files = {'a.txt': dict(version=[1, 1], state='indexed'), 'b.pdf': dict(version=[1, 2], state='failed'),
             'c.zip': dict(version=None, state='unsupported')}
    scanned = dict(scope='.', snapshot_id='s', index_revision='r', files=files, scan_id='S', candidates=[],
                   candidate_truncated=False, matched_files=0, pending_ocr_units=0, ocr_failed_units=0, enumeration_complete=True)
    ledger = coverage.record_scan(None, scanned, 'REQ')['.']
    assert ledger['files']['b.pdf'] == dict(version=[1, 2], state='failed', status='excluded', reason='读取失败')
    s = coverage.summary(ledger)
    assert (s['total'], s['excluded'], s['unreviewed']) == (2, 1, 1)  # the zip is not readable at all
