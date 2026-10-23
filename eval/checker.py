"""Sandbox state for evaluation: content snapshots and the no-deletion invariant.

The old per-task assertions (tasks_*.yaml: moved/renamed/placed/...) were retired
with eval/runner.py on 2026-10-23 (tag archive-v2-legacy-20261023); the live
acceptance suites (eval/live_policy.py) check files with their own oracles.
"""
import hashlib
import unicodedata
from collections import Counter
from pathlib import Path

DIR = "<dir>"


def snapshot(root: Path, include_trash: bool = False) -> dict[str, str]:
    """{相对路径: MD5}，文件夹记为 "<dir>"。跳过点开头的项（.DS_Store、.oplog 等），
    include_trash=True 时把 .trash 里的文件也算上（用于“文件一个不少”检查）。"""
    root = Path(root)
    snap = {}
    for p in sorted(root.rglob("*")):
        parts = p.relative_to(root).parts
        hidden = [x for x in parts if x.startswith(".")]
        if hidden and not (include_trash and hidden == [".trash"] == list(parts[:1])):
            continue
        if parts == (".trash",):  # 回收区文件夹本身不算内容，undo 后留下空的 .trash 也算还原
            continue
        key = unicodedata.normalize("NFC", "/".join(parts))  # 同一中文名可能有 NFC/NFD 两种写法
        snap[key] = DIR if p.is_dir() else hashlib.md5(p.read_bytes()).hexdigest()
    return snap


def files_preserved(before: dict, after: dict) -> bool:
    """全局不变量：文件内容的多重集合不变（回收区里的也算）。"""
    md5s = lambda s: Counter(v for v in s.values() if v != DIR)
    return md5s(before) == md5s(after)
