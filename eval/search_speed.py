"""用真实运行指令检查查找速度，始终复制沙盒到临时目录，调用配置中的真实 API。

python -m eval.search_speed logs/<run>.trace.jsonl [logs/<run2>.trace.jsonl] --repeats 2
历史日志只作观察性参考，不是随机化 A/B 实验；应人工检查最终回答的证据和覆盖范围。
"""
import argparse
import json
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from eval.checker import snapshot
from main import ROOT, load_config
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("records", type=Path, nargs="+")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--out", type=Path, default=ROOT / "eval/results/search_speed")
    args = ap.parse_args()
    if args.repeats < 1:
        ap.error("repeats 必须大于 0")
    cfg = load_config(ROOT / "config.yaml")
    source = Path(cfg["sandbox"])
    if not source.is_dir():
        ap.error(f"没有沙盒 {source}")
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for record in args.records:
        old = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        instruction = next(r["instruction"] for r in old if r.get("event") == "start")
        baseline = next(r for r in reversed(old) if r.get("event") == "end")
        for rep in range(1, args.repeats + 1):
            name = f"{record.stem}_{rep}"
            trace = args.out / f"{name}.jsonl"
            if trace.exists():
                raise FileExistsError(f"结果已存在，换 --out 避免混合实验：{trace}")
            with tempfile.TemporaryDirectory(prefix="filebutler-speed-") as tmp:
                root = Path(tmp) / "sandbox"
                shutil.copytree(source, root, symlinks=True,
                                ignore=shutil.ignore_patterns(".oplog", ".trash", ".DS_Store"))
                sb = Sandbox(root)
                before = snapshot(root, True)
                res = run_agent(instruction, LLM(cfg), sb, cfg, Console(quiet=True),
                                trace_path=trace, oplog=OpLog(sb, name), confirmer=Confirmer("never"))
                unchanged = snapshot(root, True) == before
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            row = dict(record=str(record), rep=rep, prompt=cfg.get("prompt", "v1"),
                       instruction=instruction, baseline=baseline, unchanged=unchanged,
                       tools=dict(Counter(r["tool"] for r in events if "tool" in r)), **asdict(res))
            results.append(row)
            (args.out / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: row[k] for k in ("instruction", "rep", "steps", "tool_calls", "seconds",
                                                   "llm_seconds", "cost", "stop_reason", "unchanged", "tools")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
