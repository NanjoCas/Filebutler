"""批量跑任务、判定、undo 还原检查、汇总指标。

每次运行：从缓存复制场景 → 快照 → 跑 Agent（确认模式 never）→ 判定 → 不变量 → undo → 还原检查 → 存 json。
结果在 eval/results/<实验名>/：每次运行一个 <任务>_<第几次>.json，外加 summary.md。
"""
import json
import shutil
import statistics
import unicodedata
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from agent.llm import LLM
from agent.loop import run_agent
from eval.checker import check, files_preserved, snapshot
from eval.generator import generate
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox

EVAL_DIR = Path(__file__).parent
CACHE = EVAL_DIR / ".cache"
REAL_DATA = EVAL_DIR.parent / "data" / "real"
TASK_FILES = {"real": EVAL_DIR / "tasks_real.yaml", "synthetic": EVAL_DIR / "tasks_synthetic.yaml"}
_cache_lock = threading.Lock()


def load_tasks(which: str = "all", ids=None, categories=None) -> list[dict]:
    """which：real（真实数据，主评测）/ synthetic（生成数据，离线自检和回归）/ all。"""
    names = list(TASK_FILES) if which == "all" else [which]
    tasks = [t for n in names for t in yaml.safe_load(TASK_FILES[n].read_text(encoding="utf-8")) or []]
    if ids:
        tasks = [t for t in tasks if t["id"] in ids]
    if categories:
        tasks = [t for t in tasks if t["category"] in categories]
    return tasks


def prepare(scenario: str, seed: int, dst: Path) -> dict:
    """生成场景只生成一次（生成发票要几秒），之后从缓存复制。copytree 默认 copy2，保留修改时间。
    real:<名字> 是真实数据：复制 data/real/<名字>/，标准答案读 data/real/<名字>.labels.yaml。
    real:<名字>/flat 是打散变体：所有文件移到根目录、子文件夹删掉，用于“从头重新分类”的任务。"""
    if scenario.startswith("real:"):
        return prepare_real(scenario.split(":", 1)[1], dst)
    src = CACHE / f"{scenario}-{seed}"
    with _cache_lock:
        if not (src / "manifest.json").exists():
            manifest = generate(scenario, src / "root", seed, force=True)  # 缓存目录归 runner 管
            (src / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src / "root", dst)
    return json.loads((src / "manifest.json").read_text(encoding="utf-8"))


def prepare_real(name: str, dst: Path) -> dict:
    name, _, variant = name.partition("/")
    src, labels_file = REAL_DATA / name, REAL_DATA / f"{name}.labels.yaml"
    if not src.is_dir():
        raise FileNotFoundError(f"没有真实数据 {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store"))
    labels = yaml.safe_load(labels_file.read_text(encoding="utf-8")) if labels_file.exists() else {}
    files = {unicodedata.normalize("NFC", k): v for k, v in (labels or {}).items()}  # 和快照的键一致
    if variant == "flat":
        moved = {}
        for p in sorted(x for x in dst.rglob("*") if x.is_file() and x.parent != dst):
            if (dst / p.name).exists():
                raise ValueError(f"打散后重名：{p.name}")
            moved[unicodedata.normalize("NFC", p.relative_to(dst).as_posix())] = p.name
            p.rename(dst / p.name)
        for d in sorted((x for x in dst.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
            d.rmdir()
        files = {unicodedata.normalize("NFC", moved.get(k, k)): v for k, v in files.items()}
    elif variant:
        raise ValueError(f"不认识的真实数据变体 {variant}，可选：flat")
    return {"scenario": f"real:{name}" + (f"/{variant}" if variant else ""), "seed": None, "files": files}


def run_one(task: dict, rep: int, cfg: dict, out_dir: Path, make_llm=LLM) -> dict:
    """make_llm(cfg, task, manifest) 可替换成假模型，用于离线验证任务集和 checker。"""
    run_id = f"{task['id']}_{rep}"
    root = out_dir / "_sandboxes" / run_id
    manifest = prepare(task["scenario"], task["seed"], root)
    before = snapshot(root, include_trash=True)
    sb = Sandbox(root)
    record = {"task": task["id"], "category": task["category"], "rep": rep, "instruction": task["instruction"]}
    try:
        llm = LLM(cfg) if make_llm is LLM else make_llm(cfg, task, manifest)
        res = run_agent(task["instruction"], llm, sb, cfg, Console(quiet=True),
                        trace_path=out_dir / f"{run_id}.trace.jsonl",
                        oplog=OpLog(sb, run_id), confirmer=Confirmer("never"))
        record.update(final=res.final, stop_reason=res.stop_reason, steps=res.steps, tokens=res.tokens,
                      seconds=round(res.seconds, 1), tool_calls=res.tool_calls, violations=res.violations,
                      cost=res.cost, cache_hit_tokens=res.cache_hit_tokens, reflection=res.reflection,
                      reflection_cost=res.reflection_cost, compressions=res.compressions,
                      compressed_msgs=res.compressed_msgs, rereads=res.rereads, messages=res.messages)
    except Exception as e:  # API 重试耗尽等：记为失败，继续跑别的任务
        record.update(error=f"{type(e).__name__}: {e}", final="", stop_reason="error",
                      steps=0, tokens=0, seconds=0, tool_calls=0, violations=sb.violations,
                      cost=0.0, cache_hit_tokens=0, reflection="", reflection_cost=0.0,
                      compressions=0, compressed_msgs=0, rereads=0)

    after = snapshot(root, include_trash=True)
    checks = check(task["expect"], before, after, manifest, record["final"])
    preserved = files_preserved(before, after)
    checks.append({"assert": "全局不变量：文件一个不少", "ok": preserved, "detail": ""})
    undo(sb, run_id)
    restored = snapshot(root, include_trash=True) == before
    passed = sum(c["ok"] for c in checks)
    record.update(checks=checks, success=passed == len(checks), partial=passed / len(checks),
                  restored=restored)
    (out_dir / f"{run_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(root)
    return record


def summarize(records: list[dict], exp: str, cfg: dict) -> str:
    """按类别汇总，返回 Markdown。"""
    by_cat = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)
    rows = []
    for cat in sorted(by_cat) + ["全部"]:
        rs = records if cat == "全部" else by_cat[cat]
        rows.append([cat, len(rs),
                     f"{sum(r['success'] for r in rs) / len(rs):.0%}",
                     f"{statistics.mean(r['partial'] for r in rs):.2f}",
                     f"{statistics.mean(r['steps'] for r in rs):.1f}",
                     f"{statistics.mean(r['tokens'] for r in rs):,.0f}",
                     f"{statistics.mean(r['seconds'] for r in rs):.1f}",
                     f"{statistics.mean(r['cost'] for r in rs) * 100:.3f}",
                     f"{sum(r['cache_hit_tokens'] for r in rs) / max(1, sum(r['tokens'] for r in rs)):.0%}",
                     f"{sum(r['reflection'] == 'fixed' for r in rs)}/{sum(bool(r['reflection']) for r in rs)}",
                     f"{statistics.mean(r.get('compressions', 0) for r in rs):.1f}/{statistics.mean(r.get('rereads', 0) for r in rs):.1f}",
                     sum(r["violations"] for r in rs),
                     f"{sum(r['restored'] for r in rs) / len(rs):.0%}"])
    by_task = defaultdict(set)
    for r in records:
        by_task[r["task"]].add(r["success"])
    unstable = sorted(t for t, s in by_task.items() if len(s) > 1)
    failed = sorted({r["task"] for r in records if not r["success"]})

    head = ["类别", "运行数", "严格成功率", "部分得分", "平均步数", "平均 Token", "平均耗时(s)",
            "平均成本(分)", "缓存命中", "自检修正/触发", "压缩/重读", "越权尝试", "还原率"]
    md = [f"## 实验 {exp}", "",
          f"模型 `{cfg['model']}` · temperature {cfg.get('temperature', 0)} · prompt `{cfg.get('prompt', 'v1')}`"
          f" · 自检 {'开' if cfg.get('reflect') else '关'}"
          f" · 压缩 {'开（水位 ' + str(cfg.get('context_high_water', 12000)) + '）' if cfg.get('context_compress') else '关'}"
          f" · 预算 ¥{cfg.get('cost_budget')}", "",
          "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    md += ["| " + " | ".join(map(str, row)) + " |" for row in rows]
    total_cost = sum(r["cost"] for r in records)
    refl_cost = sum(r["reflection_cost"] for r in records)
    md += ["", f"总成本 ¥{total_cost:.3f}" + (f"，其中自检 ¥{refl_cost:.3f}（{refl_cost / total_cost:.0%}）"
                                              if refl_cost and total_cost else ""),
           f"不稳定任务（多次运行结果不一致）：{', '.join(unstable) or '无'}",
           f"有失败的任务：{', '.join(failed) or '无'}"]
    return "\n".join(md)


def run_eval(cfg: dict, exp: str, repeats: int = 3, workers: int = 4, ids=None, categories=None,
             which: str = "real") -> list[dict]:
    console = Console()
    tasks = load_tasks(which, ids=ids, categories=categories)
    if not tasks:
        raise SystemExit(f"没有匹配的任务（任务集 {which}，ids={ids}，类别={categories}）")
    out_dir = EVAL_DIR / "results" / exp
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(t, rep) for t in tasks for rep in range(1, repeats + 1)]
    console.print(f"[bold]实验 {exp}[/]：{len(tasks)} 个任务 × {repeats} 次 = {len(jobs)} 次运行，"
                  f"模型 {cfg['model']}，并发 {workers}")
    t0 = time.time()
    records = []
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run_one, t, rep, cfg, out_dir): (t, rep) for t, rep in jobs}
        for f in as_completed(futures):
            t, rep = futures[f]
            try:
                r = f.result()
            except Exception as e:  # 判定或任务配置本身出错：直接报出来
                console.print(f"[red]✗ {t['id']}#{rep} 评测程序出错：{type(e).__name__}: {e}")
                raise
            records.append(r)
            mark = "[green]✓" if r["success"] else "[red]✗"
            fails = "; ".join(c["assert"] for c in r["checks"] if not c["ok"])[:120]
            console.print(f"{mark}[/] {r['task']}#{rep}  步数 {r['steps']}  tokens {r['tokens']}  "
                          f"{r['seconds']}s  [dim]{r.get('error') or fails}")
    records.sort(key=lambda r: (r["task"], r["rep"]))
    md = summarize(records, exp, cfg)
    (out_dir / "summary.md").write_text(md + "\n", encoding="utf-8")
    shutil.rmtree(out_dir / "_sandboxes", ignore_errors=True)

    table = Table(title=f"实验 {exp}（{time.time() - t0:.0f}s）")
    lines = [l for l in md.splitlines() if l.startswith("|")]
    for h in lines[0].strip("|").split("|"):
        table.add_column(h.strip())
    for l in lines[2:]:
        table.add_row(*[c.strip() for c in l.strip("|").split("|")])
    console.print(table)
    console.print("\n".join(md.splitlines()[-3:]))
    console.print(f"[dim]结果：{out_dir}")
    return records
