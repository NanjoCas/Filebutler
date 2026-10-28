"""操作日志与撤销。每次运行一个 .oplog/<run_id>.jsonl，只记可逆操作：make_dir / move / rename / trash。"""
import json
import os
from datetime import datetime
from pathlib import Path

from safety.sandbox import Sandbox, SandboxError
from safety.locking import library_lock
from safety import atomic

UNDONE_SUFFIX = ".undone.jsonl"


class OpLog:
    def __init__(self, sandbox: Sandbox, run_id: str):
        self.dir = sandbox.root / ".oplog"
        self.path = self.dir / f"{run_id}.jsonl"
        self.seq = 0

    def record(self, op: str, src: str, dst: str, *, action_id: str | None = None):
        self.dir.mkdir(exist_ok=True)
        self.seq += 1
        row = {"seq": self.seq, "op": op, "src": src, "dst": dst, "time": datetime.now().isoformat()}
        if action_id: row["action_id"] = action_id
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def latest_run(sandbox: Sandbox) -> str | None:
    """最近一次还没撤销的运行。run_id 是时间戳，按名字排序即按时间排序。"""
    logs = sorted(p for p in (sandbox.root / ".oplog").glob("*.jsonl") if not p.name.endswith(UNDONE_SUFFIX))
    return logs[-1].stem if logs else None


def undo(sandbox: Sandbox, run_id: str | None = None, *, on_restore=None) -> list[str]:
    """倒序执行反向操作。位置被占用等情况跳过并报告，不强行覆盖。返回每一步的报告。"""
    with library_lock(sandbox):
        return _undo_locked(sandbox, run_id, on_restore=on_restore)


def _undo_locked(sandbox, run_id, *, on_restore=None):
    run_id = run_id or latest_run(sandbox)
    if not run_id:
        return ["没有可以撤销的运行"]
    path = sandbox.root / ".oplog" / f"{run_id}.jsonl"
    if not path.exists():
        done = path.with_name(run_id + UNDONE_SUFFIX).exists()
        return [f"运行 {run_id} {'已经撤销过' if done else '没有操作日志'}"]

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = []
    for row in reversed(rows):
        op = row["op"]
        try:
            # 日志可能被改过，反向操作同样要过沙盒检查
            orig, now = sandbox.path(row["src"], exact=True), sandbox.path(row["dst"], exact=True)
        except SandboxError as e:
            report.append(f"跳过 #{row['seq']} {op}：{e}")
            continue
        if op == "make_dir":
            if not now.is_dir():
                report.append(f"跳过 #{row['seq']} make_dir {row['dst']}：文件夹已不存在")
            elif any(now.iterdir()):
                report.append(f"跳过 #{row['seq']} make_dir {row['dst']}：文件夹不为空")
            else:
                now.rmdir()
                report.append(f"删除空文件夹 {row['dst']}")
            continue
        # move / rename / trash 的反向操作都是把 dst 挪回 src
        if not now.exists():
            report.append(f"跳过 #{row['seq']} {op}：{row['dst']} 已不存在")
        elif orig.exists() and not os.path.samefile(orig, now):  # samefile：仅大小写不同的改名
            report.append(f"跳过 #{row['seq']} {op}：原位置 {row['src']} 已被占用")
        elif not orig.parent.is_dir():
            report.append(f"跳过 #{row['seq']} {op}：原文件夹 {sandbox.rel(orig.parent)} 已不存在")
        else:
            try:
                atomic.rename_no_replace(now, orig)
            except OSError as e:
                report.append(f"跳过 #{row['seq']} {op}：无法安全还原 {row['dst']} → {row['src']}（{e}）")
                continue
            if on_restore:
                on_restore(row["dst"], row["src"])
            report.append(f"还原 {row['dst']} → {row['src']}")
    atomic.rename_no_replace(path, path.with_name(run_id + UNDONE_SUFFIX))  # 防止重复撤销，不覆盖已有日志
    return report
