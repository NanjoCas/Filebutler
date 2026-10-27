"""离线检索诊断：python -m eval.retrieval [--queries ...] [--top-k 5] [--out ...]。

无 API 调用。使用只读真实 PDF；结果与 gold/evidence 均写入报告，不读取 topic_ok。
比较同一文本/分词下的词项覆盖数基线和 BM25。Recall 对无相关查询记 null，Precision 分母固定为 k。
"""
import argparse
import json
import time
from pathlib import Path

import yaml

from eval.retrieval_library import normalize
from safety.sandbox import Sandbox
from tools.retrieval import PaperIndex, tokenize

ROOT = Path(__file__).resolve().parents[1]


def metrics(paths, gold, k):
    matched = set(paths).intersection(gold)
    first = next((i for i, p in enumerate(paths, 1) if p in gold), None)
    return dict(recall=len(matched) / len(gold) if gold else None,
                precision=len(matched) / k, returned=len(paths),
                reciprocal_rank=1 / first if first else 0.0,
                false_positive=sorted(set(paths) - gold), missed=sorted(gold - set(paths)))


def evaluate(root: Path, queries: list[dict], k=5):
    if not root.is_dir():
        raise FileNotFoundError(f"缺少真实语料：{root}")
    sb, index = Sandbox(root), PaperIndex('pdf', pdf_pages=2, chunk=None)  # the E12 setting
    start = time.perf_counter()
    papers, coverage = index.collect(sb)
    build_seconds = time.perf_counter() - start
    rows = []
    for q in queries:
        gold = {g["path"] for g in q["relevant"]}
        for g in q["relevant"]:
            p = papers.get(g["path"])
            if p is None or not 1 <= g["page"] <= len(p.pages):
                raise ValueError(f"{q['id']} 标注文件/页不可用：{g['path']}")
            if normalize(g["evidence"]) not in normalize(p.pages[g["page"] - 1]):
                raise ValueError(f"{q['id']} 证据不匹配：{g['path']} 第 {g['page']} 页")
        start = time.perf_counter()
        hits, stats = index.search(sb, q["query"], top_k=k)
        seconds = time.perf_counter() - start
        terms = set(tokenize(q["query"]))
        keyword = sorted(((len(terms.intersection(p.terms)), path) for path, p in papers.items()),
                         key=lambda x: (-x[0], x[1]))
        keyword_paths = [path for score, path in keyword if score > 0][:k]
        rows.append(dict(**q, bm25=metrics([h["path"] for h in hits], gold, k),
                         keyword=metrics(keyword_paths, gold, k), hits=hits,
                         query_seconds=seconds, extracted=stats["extracted"]))
    return dict(scope="PDF 前两页，文件名不参与评分", top_k=k, coverage=coverage,
                build_seconds=build_seconds, rows=rows)


def summary(report):
    k, rows = report["top_k"], report["rows"]
    lines = [f"# BM25 主题检索诊断（k={k}）", "", report["scope"], "",
             f"语料覆盖：{report['coverage']}；首次抽取/建索引 {report['build_seconds']:.3f}s。无 API 调用。", "",
             "诊断集的标注来自标题/摘要及证据核对，不是独立盲测；相关文件的不同版本分别计数。",
             "Precision 分母固定为 k；无相关查询 Recall 记 —，单独检查是否返回误候选。", "",
             "| 查询 | 类型 | BM25 Recall | 词项覆盖 Recall | BM25 Precision | 误候选数 |",
             "|---|---|---|---|---|---|"]
    fmt = lambda x: "—" if x is None else f"{x:.0%}"
    for r in rows:
        lines.append(f"| {r['id']} | {r['kind']} | {fmt(r['bm25']['recall'])} | "
                     f"{fmt(r['keyword']['recall'])} | {fmt(r['bm25']['precision'])} | {len(r['bm25']['false_positive'])} |")
    for method in ("bm25", "keyword"):
        recalls = [r[method]["recall"] for r in rows if r[method]["recall"] is not None]
        value = f"{sum(recalls) / len(recalls):.1%}" if recalls else "—"
        lines += ["", f"{method} 宏平均 Recall@{k}（排除无相关查询）：{value}。"]
        nonempty = [r for r in rows if r["relevant"]]
        if nonempty:
            mrr = sum(r[method]["reciprocal_rank"] for r in nonempty) / len(nonempty)
            lines += [f"{method} MRR@{k}（排除无相关查询）：{mrr:.3f}。"]
    lines += ["", "## 漏检与误候选", ""]
    for r in rows:
        if r["bm25"]["missed"] or r["bm25"]["false_positive"]:
            lines += [f"### {r['id']}：{r['query']}", "",
                      "漏检：" + json.dumps(r["bm25"]["missed"], ensure_ascii=False), "",
                      "误候选：" + json.dumps(r["bm25"]["false_positive"], ensure_ascii=False), ""]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queries", type=Path, default=Path(__file__).with_name("retrieval_queries.yaml"))
    ap.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    ap.add_argument("--out", type=Path, default=ROOT / "eval/results/retrieval_bm25")
    args = ap.parse_args()
    spec = yaml.safe_load(args.queries.read_text(encoding="utf-8"))
    report = evaluate(ROOT / spec["corpus"], spec["queries"], args.top_k)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = summary(report)
    (args.out / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
