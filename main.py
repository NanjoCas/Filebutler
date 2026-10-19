"""命令行入口。
  python main.py ping                 测试模型连通性
  python main.py init-demo            生成演示沙盒（= gen demo）
  python main.py gen invoices --seed 42 [--manifest]   生成评测场景
  python main.py run "把 PDF 移到报告文件夹"
  python main.py search "semi supervised fake news detection"  本地检索，不调用 API
  python main.py undo [run_id]        撤销一次运行（默认最近一次）
  python main.py eval --exp baseline [--repeats 3] [--ids R01] [--tasks real|synthetic|all] [--set prompt=v2]
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
from eval.generator import SCENARIOS, NotGeneratedError, generate  # noqa: E402
from safety.confirm import Confirmer  # noqa: E402
from safety.oplog import OpLog, undo  # noqa: E402
from safety.sandbox import Sandbox, SandboxError  # noqa: E402


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
    gen_p.add_argument("--force", action="store_true", help="沙盒里有非生成的文件也清空")
    demo_p = sub.add_parser("init-demo", help="重建演示沙盒（= gen demo，会清空沙盒）")
    demo_p.add_argument("--force", action="store_true", help="沙盒里有非生成的文件也清空")
    run_p = sub.add_parser("run", help="执行一条整理指令")
    run_p.add_argument("instruction")
    run_p.add_argument("--confirm", choices=["always", "risky", "never"], help="临时覆盖 config 里的确认模式")
    search_p = sub.add_parser("search", help="本地 BM25 论文主题检索，不调用模型 API")
    search_p.add_argument("query", help="主题关键词，英文论文优先用英文")
    search_p.add_argument("--path", default=".", help="沙盒内子目录")
    search_p.add_argument("--top-k", type=int, default=5)
    search_p.add_argument("--page", type=int, default=1)
    search_p.add_argument("--detail", action="store_true", help="额外显示命中的原文片段")
    search_p.add_argument("--match", choices=["any", "all"], default="any", help="all 要求所有查询词出现，适合完整姓名")
    dedup_p = sub.add_parser("dedup", help="本地批量查重，默认只报告，不调用模型 API")
    dedup_p.add_argument("--path", default=".", help="沙盒内递归扫描目录")
    dedup_p.add_argument("--mode", choices=["bytes", "text", "near"], default="near")
    dedup_p.add_argument("--threshold", type=float, default=0.85, help="Jaccard 阈值 0.7–1；另有覆盖率>=max(0.93,阈值)且长度比>=0.8 的版本规则")
    dedup_p.add_argument("--keep", choices=["longest_text", "shortest_name"], default="longest_text")
    dedup_p.add_argument("--apply", action="store_true", help="显示计划并确认后，批量移入回收区，可 undo")
    dedup_p.add_argument("--include-similar", action="store_true", help="明确同意处理近似版本；默认只回收确定重复")
    dedup_p.add_argument("--confirm", choices=["always", "risky", "never"], help="覆盖确认模式")
    undo_p = sub.add_parser("undo", help="撤销一次运行的所有写操作")
    undo_p.add_argument("run_id", nargs="?", help="不填则撤销最近一次")
    ev = sub.add_parser("eval", help="跑评测任务集")
    ev.add_argument("--exp", required=True, help="实验名，结果存到 eval/results/<实验名>/")
    ev.add_argument("--repeats", type=int, default=3)
    ev.add_argument("--workers", type=int, default=4)
    ev.add_argument("--ids", nargs="*", help="只跑这些任务")
    ev.add_argument("--category", nargs="*", help="只跑这些类别")
    ev.add_argument("--tasks", choices=["real", "synthetic", "retrieval", "all"], default="real",
                    help="任务集：real 真实数据（默认）/ synthetic 生成数据 / retrieval E12 主题查找 / all")
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
        try:
            manifest = generate(scenario, Path(cfg["sandbox"]), seed, force=args.force)
        except NotGeneratedError as e:
            console.print(f"[red]{e}")
            sys.exit(1)
        console.print(f"[green]已生成场景 {scenario}（seed={seed}）：{cfg['sandbox']}")
        if getattr(args, "manifest", False):
            console.print_json(data=manifest)
    elif args.cmd == "dedup":
        import json
        from tools.dedup import ApplyDedupArgs, FindDuplicatesArgs, scan_duplicates
        from tools import registry
        from pydantic import ValidationError
        try:
            parsed = FindDuplicatesArgs(path=args.path, mode=args.mode, threshold=args.threshold, keep=args.keep)
        except ValidationError as e:
            ap.error(str(e))
        sb = Sandbox(cfg["sandbox"])
        try:
            scan = scan_duplicates(sb, parsed.path, parsed.mode, parsed.threshold, parsed.keep)
        except (registry.ToolError, SandboxError, OSError) as e:
            console.print(Text(f"ERROR: {e}"))
            sys.exit(1)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        report = logs / f"{run_id}.dedup.json"
        report.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
        counts = {kind: sum(g["kind"] == kind for g in scan["groups"]) for kind in ("bytes", "text", "similar")}
        console.print(Text(f"本地查重：{scan['stats']}，耗时 {scan['seconds']:.2f}s\n分组：{counts}\n完整报告：{report}"))
        for g in scan["groups"]:
            console.print(Text(f"{g['id']} {g['kind']} Jaccard={g['similarity']:.3f} coverage={g['containment']:.3f}，保留建议：{g['keep']}\n" +
                               "\n".join(f"  {p}" for p in g["members"])))
        if args.apply:
            confirmer = Confirmer(args.confirm or cfg.get("confirm", "risky"), cfg.get("confirm_threshold", 10),
                                  ask=lambda q: console.input(Text(q, style="bold yellow")))
            action = ApplyDedupArgs(scan_id=scan["id"], include_similar=args.include_similar)
            result = registry.dispatch("apply_dedup", action.model_dump_json(), sb, OpLog(sb, run_id), confirmer)
            console.print(Text(result))
            console.print(Text(f"撤销：python main.py undo {run_id}"))
            if result.startswith("ERROR"):
                sys.exit(1)
    elif args.cmd == "search":
        from tools import registry
        import json
        result = registry.dispatch("search_content", json.dumps(dict(query=args.query, path=args.path,
                                    top_k=args.top_k, page=args.page, match=args.match,
                                    view="detail" if args.detail else "compact")), Sandbox(cfg["sandbox"]))
        console.print(Text(result))
        if result.startswith("ERROR"):
            sys.exit(1)
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
        run_eval(cfg, args.exp, args.repeats, args.workers, args.ids, args.category, args.tasks)
    elif args.cmd == "undo":
        for line in undo(Sandbox(cfg["sandbox"]), args.run_id):
            console.print(("[yellow]" if line.startswith("跳过") else "") + line)


if __name__ == "__main__":
    main()
