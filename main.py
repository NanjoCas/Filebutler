"""命令行入口。
  python main.py ping                 测试模型连通性
  python main.py init-demo            生成演示沙盒（= gen demo）
  python main.py gen invoices --seed 42 [--manifest]   生成评测场景
  python main.py run "把 PDF 移到报告文件夹"
  python main.py undo [run_id]        撤销一次运行（默认最近一次）
  python main.py eval --exp baseline [--repeats 3] [--ids A01 B02] [--set prompt=v2]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.llm import LLM  # noqa: E402
from agent.loop import run_agent  # noqa: E402
from eval.generator import SCENARIOS, generate  # noqa: E402
from safety.confirm import Confirmer  # noqa: E402
from safety.oplog import OpLog, undo  # noqa: E402
from safety.sandbox import Sandbox  # noqa: E402


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["sandbox"] = str((path.parent / cfg["sandbox"]).resolve())
    return cfg


def main():
    ap = argparse.ArgumentParser(prog="filebutler")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--model", help="临时覆盖 config 里的 model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping", help="发一句话测试 API key 和模型")
    gen_p = sub.add_parser("gen", help="生成测试场景到沙盒（会清空沙盒）")
    gen_p.add_argument("scenario", choices=list(SCENARIOS))
    gen_p.add_argument("--seed", type=int, default=42)
    gen_p.add_argument("--manifest", action="store_true", help="打印每个文件的标准答案")
    sub.add_parser("init-demo", help="重建演示沙盒（= gen demo，会清空沙盒）")
    run_p = sub.add_parser("run", help="执行一条整理指令")
    run_p.add_argument("instruction")
    run_p.add_argument("--confirm", choices=["always", "risky", "never"], help="临时覆盖 config 里的确认模式")
    undo_p = sub.add_parser("undo", help="撤销一次运行的所有写操作")
    undo_p.add_argument("run_id", nargs="?", help="不填则撤销最近一次")
    ev = sub.add_parser("eval", help="跑评测任务集")
    ev.add_argument("--exp", required=True, help="实验名，结果存到 eval/results/<实验名>/")
    ev.add_argument("--repeats", type=int, default=3)
    ev.add_argument("--workers", type=int, default=4)
    ev.add_argument("--ids", nargs="*", help="只跑这些任务")
    ev.add_argument("--category", nargs="*", help="只跑这些类别")
    ev.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE", help="临时覆盖 config，如 prompt=v2")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if args.model:
        cfg["model"] = args.model
    for kv in getattr(args, "set", []):
        k, v = kv.split("=", 1)
        cfg[k] = yaml.safe_load(v)  # 让 15、true 这类值保持类型
    console = Console()

    if args.cmd == "ping":
        llm = LLM(cfg)
        msg = llm.chat([{"role": "user", "content": "用一句话介绍你自己"}])
        console.print(f"[green]{cfg['model']}[/]: {msg.content}\n[dim]tokens {llm.total_tokens}")
    elif args.cmd in ("gen", "init-demo"):
        scenario, seed = (args.scenario, args.seed) if args.cmd == "gen" else ("demo", 0)
        manifest = generate(scenario, Path(cfg["sandbox"]), seed)
        console.print(f"[green]已生成场景 {scenario}（seed={seed}）：{cfg['sandbox']}")
        if getattr(args, "manifest", False):
            console.print_json(data=manifest)
    elif args.cmd == "run":
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        console.print(f"[bold]指令：[/]{args.instruction}  [dim](run {run_id}, 模型 {cfg['model']})")
        sb = Sandbox(cfg["sandbox"])
        # 用 Text 输出提示：内容里的 [ ] 不会被 rich 当成样式标记吞掉
        confirmer = Confirmer(args.confirm or cfg.get("confirm", "risky"), cfg.get("confirm_threshold", 10),
                              ask=lambda q: console.input(Text(q, style="bold yellow")))
        run_agent(args.instruction, LLM(cfg), sb, cfg, console,
                  trace_path=logs / f"{run_id}.trace.jsonl", oplog=OpLog(sb, run_id), confirmer=confirmer)
        console.print(f"[dim]撤销本次运行：python main.py undo {run_id}")
    elif args.cmd == "eval":
        from eval.runner import run_eval
        run_eval(cfg, args.exp, args.repeats, args.workers, args.ids, args.category)
    elif args.cmd == "undo":
        for line in undo(Sandbox(cfg["sandbox"]), args.run_id):
            console.print(("[yellow]" if line.startswith("跳过") else "") + line)


if __name__ == "__main__":
    main()
