"""回放失败案例：把一次运行的对话截到第 cut 条消息，在同样的上下文下让模型继续跑多次，看错误能否复现。

截断点之前的写操作会在全新沙盒上原样重放，让沙盒状态和当时一致。
  python -m eval.replay eval/results/baseline/C05_3.json --times 20
不指定 --cut 时，默认截在第一次写操作之前（即“读完信息、准备动手”的决策点）。
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console  # noqa: E402

from agent.llm import LLM  # noqa: E402
from agent.loop import run_agent  # noqa: E402
from eval.checker import check, files_preserved, snapshot  # noqa: E402
from eval.runner import load_tasks, prepare  # noqa: E402
from safety.confirm import Confirmer  # noqa: E402
from safety.sandbox import Sandbox  # noqa: E402
from tools import registry  # noqa: E402

WRITE_TOOLS = {name for name, t in registry.TOOLS.items() if t.writes}


def first_write_index(messages: list[dict]) -> int:
    for i, m in enumerate(messages):
        if any(tc["function"]["name"] in WRITE_TOOLS for tc in m.get("tool_calls", [])):
            return i
    raise SystemExit("这次运行没有写操作，请用 --cut 指定截断位置")


def replay_once(task, prefix, cfg, root: Path) -> dict:
    manifest = prepare(task["scenario"], task["seed"], root)
    before = snapshot(root, include_trash=True)
    sb = Sandbox(root)
    for m in prefix:  # 重放截断点之前的写操作，还原当时的沙盒状态
        for tc in m.get("tool_calls", []):
            if tc["function"]["name"] in WRITE_TOOLS:
                registry.dispatch(tc["function"]["name"], tc["function"]["arguments"], sb)
    res = run_agent(task["instruction"], LLM(cfg), sb, cfg, Console(quiet=True),
                    confirmer=Confirmer("never"), history=prefix)
    after = snapshot(root, include_trash=True)
    checks = check(task["expect"], before, after, manifest, res.final)
    checks.append({"assert": "全局不变量：文件一个不少", "ok": files_preserved(before, after), "detail": ""})
    moves = [json.loads(tc["function"]["arguments"]) for m in res.messages[len(prefix):]
             for tc in m.get("tool_calls", []) if tc["function"]["name"] == "move"]
    return {"success": all(c["ok"] for c in checks), "failed": [c["assert"] for c in checks if not c["ok"]],
            "moved": [mv["src"] for mv in moves], "tokens": res.tokens, "cost": res.cost,
            "reflection": res.reflection, "reflection_cost": res.reflection_cost}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("record", help="eval/results/<实验>/<任务>_<次>.json")
    ap.add_argument("--cut", type=int, help="保留前多少条消息（默认：第一次写操作之前）")
    ap.add_argument("--times", type=int, default=10)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE", help="临时覆盖 config，如 reflect=true")
    args = ap.parse_args()

    import yaml
    root_dir = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root_dir / "config.yaml").read_text(encoding="utf-8"))
    for kv in args.set:
        k, v = kv.split("=", 1)
        cfg[k] = yaml.safe_load(v)
    record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    task = load_tasks(ids=[record["task"]])[0]
    cut = args.cut if args.cut is not None else first_write_index(record["messages"])
    prefix = record["messages"][:cut]
    console = Console()
    console.print(f"回放 {record['task']}#{record['rep']}：保留前 {cut} 条消息，继续跑 {args.times} 次")
    console.print(f"[dim]原运行在此之后的第一步：{json.dumps(record['messages'][cut].get('tool_calls', ''), ensure_ascii=False)[:200]}")

    work = root_dir / "eval" / "results" / "_replay"
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda i: replay_once(task, prefix, cfg, work / str(i)), range(args.times)))
    fails = 0
    for i, r in enumerate(results, 1):
        fails += not r["success"]
        mark = "[green]✓" if r["success"] else "[red]✗"
        refl = f"自检 {r['reflection']} ¥{r['reflection_cost']:.4f}  " if r["reflection"] else ""
        console.print(f"{mark}[/] #{i}  移动 {len(r['moved'])} 个  tokens {r['tokens']}  ¥{r['cost']:.4f}  "
                      f"{refl}[dim]{'; '.join(r['failed'])}")
    console.print(f"[bold]失败：{fails}/{args.times}[/]  平均成本 ¥{sum(r['cost'] for r in results) / len(results):.4f}")
    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
