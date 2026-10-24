"""Retrieval set of 2026-10-24 (eval/retrieval_set_20261024.yaml): python -m eval.retrieval_set [--out ...].

No API calls. The real papers are copied to a temporary folder and the mixed
library is generated there, so nothing is written under data/real. Every
evidence snippet is checked against the full text of its page first.

Methods (same queries, same top k):
- keyword: count of query terms present in a document's first two PDF pages (E12 baseline);
- pdf_front2: search_content as shipped before stage 3 (BM25, PDFs only, first two pages).
"""
import argparse
import json
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path

import yaml

from eval import retrieval_mixed
from eval.retrieval import ROOT, normalize
from safety.sandbox import Sandbox
from tools.retrieval import PaperIndex, tokenize

SET = Path(__file__).with_name('retrieval_set_20261024.yaml')


def load(path=SET):
    spec = yaml.safe_load(path.read_text(encoding='utf-8'))
    base = yaml.safe_load(path.with_name(spec['base']).read_text(encoding='utf-8'))
    queries = [dict(q, corpus='papers') for q in base['queries']] + spec['queries']
    ids = [q['id'] for q in queries]
    assert len(ids) == len(set(ids)), 'duplicate query id'
    return spec, queries


def prepare(spec, folder):
    """Corpus name -> library root inside folder."""
    roots = {'papers': Path(folder) / 'papers', 'mixed': Path(folder) / 'mixed'}
    shutil.copytree(ROOT / spec['corpora']['papers'], roots['papers'],
                    ignore=shutil.ignore_patterns('.*'))
    retrieval_mixed.build(roots['mixed'])
    return roots


def check_evidence(roots, queries):
    from agent.extract import units
    sbs = {name: Sandbox(root) for name, root in roots.items()}
    for q in queries:
        sb = sbs[q['corpus']]
        for g in q['relevant']:
            path = sb.root / g['path']
            if not path.is_file():
                raise ValueError(f"{q['id']} 相关文件不存在：{g['path']}")
            pages = units(sb, path, pages=None)
            if not 1 <= g['page'] <= len(pages) or normalize(g['evidence']) not in normalize(pages[g['page'] - 1]):
                raise ValueError(f"{q['id']} 证据不匹配：{g['path']} 第 {g['page']} 页")


def metrics(paths, gold, k):
    matched = set(paths) & gold
    first = next((i for i, p in enumerate(paths, 1) if p in gold), None)
    return dict(recall=len(matched) / min(k, len(gold)) if gold else None,
                reciprocal_rank=1 / first if first else 0.0, returned=len(paths),
                false_positive=sorted(set(paths) - gold), missed=sorted(gold - set(paths)))


class Keyword:
    name = 'keyword'

    def __init__(self, sb):
        self.sb, self.index = sb, PaperIndex()

    def build(self):
        self.papers, self.coverage = self.index.collect(self.sb)

    def search(self, query, k):
        terms = set(tokenize(query))
        ranked = sorted(((len(terms.intersection(p.terms)), path) for path, p in self.papers.items()), key=lambda x: (-x[0], x[1]))
        return [path for score, path in ranked if score > 0][:k]


class PdfFront2(Keyword):
    name = 'pdf_front2'

    def search(self, query, k):
        if not tokenize(query):
            return []
        return [h['path'] for h in self.index.search(self.sb, query, top_k=k)[0]]


METHODS = [Keyword, PdfFront2]


def evaluate(roots, queries, k=5, methods=METHODS):
    report = dict(top_k=k, methods={}, rows=[dict(q) for q in queries])
    for method in methods:
        builds = {}
        for name, root in roots.items():
            engine = method(Sandbox(root))
            tracemalloc.start()
            start = time.perf_counter()
            engine.build()
            seconds = time.perf_counter() - start
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            builds[name] = dict(engine=engine, seconds=seconds, peak_mb=peak / 2**20,
                                coverage=getattr(engine, 'coverage', None))
        times = []
        for row in report['rows']:
            start = time.perf_counter()
            paths = builds[row['corpus']]['engine'].search(row['query'], k)
            times.append(time.perf_counter() - start)
            row[method.name] = dict(paths=paths, **metrics(paths, {g['path'] for g in row['relevant']}, k))
        report['methods'][method.name] = dict(
            build={n: {key: v for key, v in b.items() if key != 'engine'} for n, b in builds.items()},
            query_ms_mean=1000 * sum(times) / len(times), query_ms_max=1000 * max(times))
    return report


def scores(report, method, rows=None):
    rows = [r for r in (rows or report['rows']) if r['relevant']]
    if not rows:
        return None, None, 0
    recall = sum(r[method]['recall'] for r in rows) / len(rows)
    mrr = sum(r[method]['reciprocal_rank'] for r in rows) / len(rows)
    return recall, mrr, sum(r[method]['recall'] > 0 for r in rows)


GROUPS = [
    ('E12 原题 Q01–Q12', lambda r: r['id'][0] == 'Q' and int(r['id'][1:]) <= 12),
    ('新增主题 Q13–Q30', lambda r: r['id'][0] == 'Q' and int(r['id'][1:]) > 12),
    ('细节 F01–F19', lambda r: r['id'][0] == 'F'),
    ('混合格式 M01–M12', lambda r: r['id'][0] == 'M'),
    ('跨语言（全部）', lambda r: 'cross_language' in r['kind']),
    ('全部', lambda r: True),
]


def summary(report):
    k, names = report['top_k'], list(report['methods'])
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    lines = [f'# 检索评测（2026-10-24 冻结集，k={k}）', '',
             '无 API 调用。题目和标注由同一人写成，标注时看过全文，不是独立盲测。', '',
             '| 分组 | 题数（有相关） | ' + ' | '.join(f'{n} Recall@{k} / MRR / 命中题数' for n in names) + ' |',
             '|---|---|' + '---|' * len(names)]
    for label, keep in GROUPS:
        rows = [r for r in report['rows'] if keep(r)]
        cells = []
        for n in names:
            recall, mrr, hit = scores(report, n, rows)
            cells.append(f'{pct(recall)} / {"—" if mrr is None else f"{mrr:.3f}"} / {hit}')
        lines.append(f"| {label} | {len(rows)}（{sum(bool(r['relevant']) for r in rows)}） | " + ' | '.join(cells) + ' |')
    lines += ['', '无相关题返回的误候选数：' + '；'.join(
        f"{r['id']} " + ' / '.join(f"{n}={len(r[n]['false_positive'])}" for n in names)
        for r in report['rows'] if not r['relevant'])]
    lines += ['', '| 方法 | 建索引（papers / mixed） | Python 内存峰值（papers / mixed） | 单次查询平均 / 最长 |', '|---|---|---|---|']
    for n, m in report['methods'].items():
        b = m['build']
        lines.append(f"| {n} | {b['papers']['seconds']:.2f}s / {b['mixed']['seconds']:.2f}s | "
                     f"{b['papers']['peak_mb']:.0f}MB / {b['mixed']['peak_mb']:.0f}MB | "
                     f"{m['query_ms_mean']:.1f}ms / {m['query_ms_max']:.1f}ms |")
    lines += ['', '## 逐题', '', '| 题 | 类型 | ' + ' | '.join(names) + ' |', '|---|---|' + '---|' * len(names)]
    for r in report['rows']:
        cell = lambda n: (f"{len(r[n]['false_positive'])} 误" if not r['relevant']
                          else f"{pct(r[n]['recall'])} @{round(1 / r[n]['reciprocal_rank']) if r[n]['reciprocal_rank'] else '-'}")
        lines.append(f"| {r['id']} | {r['kind']} | " + ' | '.join(cell(n) for n in names) + ' |')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--top-k', type=int, default=5)
    ap.add_argument('--out', type=Path, required=True, help='新的结果目录（不覆盖已有目录）')
    args = ap.parse_args()
    if args.out.exists():
        raise SystemExit(f'{args.out} 已存在，请换一个目录')
    spec, queries = load()
    with tempfile.TemporaryDirectory(prefix='filebutler-retrieval-') as folder:
        roots = prepare(spec, folder)
        check_evidence(roots, queries)
        report = evaluate(roots, queries, args.top_k)
    args.out.mkdir(parents=True)
    (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    text = summary(report)
    (args.out / 'summary.md').write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
