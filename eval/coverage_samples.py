"""The seven frozen coverage samples of the routing redesign (S7, 9.1). Offline, no model.

    .venv/bin/python -m eval.coverage_samples --out eval/results/coverage_samples_<date>

Each sample builds a synthetic library in a temporary folder (never data/real),
runs search_content through the registry as the agent would, and records the facts
9.1 asks for: queries, reads, local seconds, OCR pending/failed, matched/returned,
reviewed/total, and whether the stated facts hold. Query expansion (CN->EN) is
the model's choice in a live run; here the original query is the measured
baseline and the translated query is run beside it, labelled ``offline_proxy``.
No cost is reported: no model is called. Frozen 2026-10-25 before its first run.
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

import pymupdf

from agent import coverage
from safety.sandbox import Sandbox
from tools import registry
import tools.retrieval as retrieval

TOPICS_EN = ['federated learning for hospital records', 'graph neural networks for traffic', 'contrastive image pretraining',
             'speech recognition with transformers', 'protein folding prediction', 'reinforcement learning for robots',
             'privacy in recommender systems', 'climate model downscaling', 'code generation benchmarks',
             'medical image segmentation', 'fake news detection', 'question answering over tables']
TOPICS_ZH = ['联邦学习与医院病历', '图神经网络交通预测', '对比学习图像预训练', '语音识别模型', '蛋白质结构预测', '机器人强化学习',
             '推荐系统隐私保护', '气候模型降尺度', '代码生成评测', '医学图像分割', '虚假新闻检测', '表格问答']


def _pdf(path, pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text: page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=11)
        doc.save(path)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _search(sb, query, **args):
    out = registry.dispatch_result('search_content', json.dumps(dict(query=query, **args), ensure_ascii=False), sb)
    if out.status != 'ok': raise RuntimeError(out.text)
    return out


def _review_all(ledgers, sb, paths):
    return coverage.record_review(ledgers, sb, paths, '读过正文')


def cn_to_en(root):
    for i, t in enumerate(TOPICS_EN): _write(root / f'Papers/p{i:02d}.txt', f'{t}. ' * 30)
    sb = Sandbox(root)
    base = _search(sb, '联邦学习').data['coverage']
    proxy = _search(sb, 'federated learning').data['coverage']
    return dict(queries=2, reads=0, original_matched=base['matched_files'], offline_proxy_matched=proxy['matched_files'],
                facts=dict(original_query_misses=base['matched_files'] == 0, translated_query_hits=proxy['matched_files'] >= 1,
                           extract_complete=base['extract_complete']))


def cn_to_cn(root):
    for i, t in enumerate(TOPICS_ZH): _write(root / f'Papers/p{i:02d}.txt', f'{t}。' * 30)
    sb = Sandbox(root)
    c = _search(sb, '联邦学习').data['coverage']
    return dict(queries=1, reads=0, matched=c['matched_files'], facts=dict(found_without_translation=c['matched_files'] >= 1))


def no_terms_complete(root):
    for i in range(8): _write(root / f'Notes/n{i}.txt', f'{TOPICS_EN[i]}. ' * 20)
    sb = Sandbox(root)
    out = _search(sb, 'volcano eruption')
    c = out.data['coverage']
    return dict(queries=1, reads=0, matched=c['matched_files'],
                facts=dict(no_match=c['matched_files'] == 0, extract_complete=c['extract_complete'],
                           does_not_claim_absence='不代表没有相关文件' in out.text or '无命中不代表' in out.text or '可更换关键词' in out.text))


def no_terms_ocr_pending(root):
    for i in range(5): _write(root / f'Notes/n{i}.txt', f'{TOPICS_EN[i]}. ' * 20)
    for i in range(3): _pdf(root / f'Notes/scan{i}.pdf', [''])
    sb = Sandbox(root)
    saved = retrieval.OCR_SECONDS
    retrieval.OCR_SECONDS = -1  # the time limit is used up: the scans are left for a later search
    try:
        out = _search(sb, 'volcano eruption')
    finally:
        retrieval.OCR_SECONDS = saved
    c = out.data['coverage']
    return dict(queries=1, reads=0, pending=c['pending_ocr_units'],
                facts=dict(pending_counted=c['pending_ocr_units'] == 3, extract_incomplete=not c['extract_complete'],
                           pending_stated='3 个因时间上限未识别' in out.text))


def later_page(root):
    pages = [f'Introduction page {n}. General background on learning systems.' for n in range(1, 6)]
    pages[3] = 'Appendix: the calibration constant kappa equals 0.3172 in all runs.'
    _pdf(root / 'Papers/long.pdf', pages)
    for i in range(3): _pdf(root / f'Papers/other{i}.pdf', ['Unrelated survey of datasets.'] * 2)
    sb = Sandbox(root)
    out = _search(sb, 'calibration constant kappa', view='detail')
    return dict(queries=1, reads=0, facts=dict(found_on_page_4='page=4' in out.text and 'long' in out.text))


def over_top_k(root):
    for i in range(30): _write(root / f'Papers/p{i:02d}.txt', f'fake news detection variant {i}. ' * 10)
    sb = Sandbox(root)
    c = _search(sb, 'fake news detection', top_k=5).data['coverage']
    ledgers = coverage.record_scan(None, c, 'sample')
    ledgers = _review_all(ledgers, sb, c['candidates'])
    s = coverage.summary(ledgers[c['scope']])
    return dict(queries=1, reads=5, matched=c['matched_files'], returned=c['returned_files'], reviewed=s['reviewed'], total=s['total'],
                facts=dict(truncated=c['candidate_truncated'], reviewed_5_of_30=(s['reviewed'], s['total']) == (5, 30),
                           gap_stated='只看了前 5 个候选' in coverage.gap_line(s)))


def library_changed(root):
    for i in range(10): _write(root / f'Notes/n{i}.txt', f'budget note {i}. ' * 10)
    sb = Sandbox(root)
    c = _search(sb, 'budget').data['coverage']
    ledgers = _review_all(coverage.record_scan(None, c, 'sample'), sb, [f'Notes/n{i}.txt' for i in range(10)])
    before = coverage.summary(ledgers[c['scope']])
    time.sleep(0.01)
    (root / 'Notes/n3.txt').write_text('budget note changed. ' * 12)
    (root / 'Notes/n10.txt').write_text('budget note new. ' * 10)
    c2 = _search(sb, 'budget').data['coverage']
    ledgers = coverage.record_scan(ledgers, c2, 'sample')
    after = coverage.summary(ledgers[c2['scope']])
    return dict(queries=2, reads=10, before=f"{before['reviewed']}/{before['total']}", after=f"{after['reviewed']}/{after['total']}",
                facts=dict(complete_before=(before['reviewed'], before['total']) == (10, 10),
                           new_denominator_11=after['total'] == 11, two_unreviewed=sorted(after['changed']) == ['Notes/n10.txt', 'Notes/n3.txt'],
                           old_reviews_inherited=after['inherited'] == 9, snapshot_changed=c['snapshot_id'] != c2['snapshot_id']))


SAMPLES = [('CN→EN', cn_to_en), ('CN→CN', cn_to_cn), ('无词项且完整', no_terms_complete), ('无词项且OCR未完', no_terms_ocr_pending),
           ('后几页才相关', later_page), ('超过top_k', over_top_k), ('扫描间库改变', library_changed)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, sample in SAMPLES:
        with tempfile.TemporaryDirectory(prefix='filebutler-coverage-') as tmp:
            root = Path(tmp)
            t = time.perf_counter()
            row = sample(root)
            row.update(sample=name, seconds=round(time.perf_counter() - t, 3), passed=all(row['facts'].values()))
        rows.append(row)
        print(f"{'✓' if row['passed'] else '✗'} {name}: " + json.dumps({k: v for k, v in row.items() if k not in ('sample', 'passed')},
                                                                     ensure_ascii=False))
    (args.out / 'samples.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"{sum(r['passed'] for r in rows)}/{len(rows)} 个样例的事实成立")
    return 0 if all(r['passed'] for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
