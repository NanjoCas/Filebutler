"""51 篇真实论文的本地查重诊断，所有应用和 undo 都在临时副本，无 API。

python -m eval.dedup
按标题/版本信息整理的已知重复论文家族；这是开发诊断集，不是独立盲测。
"""
import argparse
import itertools
import json
import shutil
import tempfile
from pathlib import Path

from eval.checker import files_preserved, snapshot
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tools import registry
from tools.dedup import scan_duplicates

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = [
    ["1912.12520v2.pdf", "Wang.pdf", "强化学习/Weak Supervision Reinforcement Learning.pdf"],
    ["2202.08159v1.pdf", "强化学习/Domain Adaptive Fake News Detection via Reinforcement Learning.pdf"],
    ["Hu.pdf", "review/Deep learning for fake_news_detection_A_comprehensive_survey.pdf"],
    ["review/A systematic literature review of machine learning algorithms and datasets.pdf",
     "review/Fake news detection a systematic literature review of machine learning algorithms and datasets.pdf"],
]


def pairs(groups):
    return {tuple(sorted(p)) for g in groups for p in itertools.combinations(g, 2)}


def evaluate(source):
    if not source.is_dir():
        raise FileNotFoundError(f"缺少真实语料：{source}")
    with tempfile.TemporaryDirectory(prefix="filebutler-dedup-") as tmp:
        root = Path(tmp) / "sandbox"
        shutil.copytree(source, root, symlinks=True)
        before = snapshot(root, True)
        gold = pairs(FAMILIES)
        missing = {p for pair in gold for p in pair if p not in before}
        if missing:
            raise ValueError(f"缺少诊断集文件：{sorted(missing)}")
        sb = Sandbox(root)
        scan = scan_duplicates(sb)
        predicted = pairs([g["members"] for g in scan["groups"]])
        report = dict(scan=scan, expected_families=FAMILIES,
                      pair_precision=len(predicted & gold) / len(predicted) if predicted else None,
                      pair_recall=len(predicted & gold) / len(gold),
                      false_pairs=sorted(predicted - gold), missed_pairs=sorted(gold - predicted), executions=[])
        for include in (False, True):
            run_id = f"dedup-{'reviewed' if include else 'safe'}"
            result = registry.dispatch("apply_dedup", json.dumps(dict(scan_id=scan["id"], include_similar=include)),
                                       sb, OpLog(sb, run_id), Confirmer("never"))
            after = snapshot(root, True)
            operations = root / ".oplog" / f"{run_id}.jsonl"
            rows = [json.loads(line) for line in operations.read_text().splitlines()] if operations.exists() else []
            undo_report = undo(sb, run_id)
            report["executions"].append(dict(include_similar=include, result=result, operations=len(rows),
                                             preserved=files_preserved(before, after),
                                             restored=snapshot(root, True) == before, undo=undo_report))
        return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=ROOT / "data/real/papers")
    ap.add_argument("--out", type=Path, default=ROOT / "eval/results/dedup_local")
    args = ap.parse_args()
    report = evaluate(args.source)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("scan", "expected_families")}, ensure_ascii=False, indent=2))
    print("扫描统计：", report["scan"]["stats"], "耗时：", round(report["scan"]["seconds"], 3), "秒")
    for g in report["scan"]["groups"]:
        print(g["kind"], "Jaccard", g["similarity"], "coverage", g["containment"], g["members"])
    if report["false_pairs"] or report["missed_pairs"] or any(
        not e["preserved"] or not e["restored"] or not e["result"].startswith("OK") for e in report["executions"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
