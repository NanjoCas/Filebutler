"""第 2 周：任务集和 checker 的离线自检。
标准答案 Agent：按每个任务的断言推导出正确的工具调用，应当全部通过且 undo 后完全还原；
什么都不做的 Agent：凡是需要改动文件的任务都应当失败。两者合起来说明任务和 checker 没写错。"""
from pathlib import PurePosixPath

import pytest

from agent.llm import LLM
from eval.checker import _fill, check, select, snapshot
from eval.runner import load_tasks, prepare, run_one
from tests.test_week1 import FakeClient

TASKS = load_tasks("synthetic")


def oracle_script(task, manifest, before):
    calls = []
    for item in task["expect"]:
        (kind, arg), = item.items()
        if kind in ("moved", "renamed", "trashed"):
            for src in select(arg, before, manifest):
                if kind == "moved":
                    dst = _fill(arg["to"], src, manifest)
                    calls += [("make_dir", {"path": dst}), ("move", {"src": src, "dst_dir": dst})]
                elif kind == "renamed":
                    calls.append(("rename", {"path": src, "new_name": _fill(arg["to"], src, manifest)}))
                else:
                    calls.append(("trash", {"path": src}))
        elif kind == "dedup":
            groups = {}
            for src in select({k: v for k, v in arg.items() if k not in ("to", "keep")}, before, manifest):
                groups.setdefault(before[src], []).append(src)
            for g in (g for g in groups.values() if len(g) > 1):
                keep = min(g, key=lambda p: (len(PurePosixPath(p).name), p))
                for src in g:
                    if src == keep:
                        continue
                    if "to" in arg:
                        calls += [("make_dir", {"path": arg["to"]}), ("move", {"src": src, "dst_dir": arg["to"]})]
                    else:
                        calls.append(("trash", {"path": src}))
    answer = " ".join(str(a) for i in task["expect"] for k, a in i.items() if k == "answer_contains")
    return ([calls] if calls else []) + [answer or "完成"]


def make_oracle(cfg, task, manifest):
    peek = _tmp / "oracle_peek"  # 另复制一份场景，只用来算初始快照
    prepare(task["scenario"], task["seed"], peek)
    before = snapshot(peek, include_trash=True)
    return LLM({"model": "oracle"}, client=FakeClient(oracle_script(task, manifest, before)))


def make_idle(cfg, task, manifest):
    return LLM({"model": "idle"}, client=FakeClient(["什么也没做"]))


@pytest.fixture(autouse=True)
def _tmpdir(tmp_path):
    global _tmp
    _tmp = tmp_path


def test_task_ids_unique():
    ids = [t["id"] for t in TASKS]
    assert len(ids) == len(set(ids)) == 37


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t["id"])
def test_oracle_passes_and_undo_restores(task, tmp_path):
    r = run_one(task, 1, {"max_steps": 3}, tmp_path, make_llm=make_oracle)
    assert r["success"], [c for c in r["checks"] if not c["ok"]]
    assert r["restored"]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t["id"])
def test_idle_agent_fails_when_work_needed(task, tmp_path):
    r = run_one(task, 1, {"max_steps": 3}, tmp_path, make_llm=make_idle)
    kinds = {k for item in task["expect"] for k in item}
    needs_work = bool(kinds & {"moved", "renamed", "trashed", "dedup", "answer_contains"})
    assert r["success"] != needs_work


def test_others_unchanged_catches_extra_move(tmp_path):
    task = next(t for t in TASKS if t["id"] == "A02")
    manifest = prepare(task["scenario"], task["seed"], tmp_path / "s")
    before = snapshot(tmp_path / "s", include_trash=True)
    for z in ("照片备份.zip", "资料包(1).zip", "code.zip"):
        (tmp_path / "s" / "压缩包").mkdir(exist_ok=True)
        (tmp_path / "s" / z).rename(tmp_path / "s" / "压缩包" / z)
    (tmp_path / "s" / "备忘.txt").rename(tmp_path / "s" / "压缩包" / "备忘.txt")  # 多动了一个
    res = {c["assert"]: c["ok"] for c in check(task["expect"], before, snapshot(tmp_path / "s", True), manifest)}
    assert res["others_unchanged"] is False
    assert all(v for k, v in res.items() if k.startswith("moved"))


def test_dedup_catches_wrong_copy_kept(tmp_path):
    """D04 要求留文件名最短的：留下“IMG_9348 副本.jpg”、删掉“IMG_9348.jpg”应判失败；D02 不限定留哪份，应判成功。"""
    d02, d04 = (next(t for t in TASKS if t["id"] == i) for i in ("D02", "D04"))
    manifest = prepare("photos", 42, tmp_path / "s")
    root = tmp_path / "s"
    before = snapshot(root, include_trash=True)
    groups = {}
    for p, md5 in before.items():
        groups.setdefault(md5, []).append(p)
    (root / ".trash").mkdir()
    for g in (g for g in groups.values() if len(g) > 1):
        keep = max(g, key=lambda p: len(PurePosixPath(p).name))   # 故意留最长的
        for p in g:
            if p != keep:
                (root / p).rename(root / ".trash" / (PurePosixPath(p).name.replace(" ", "_") + "_x"))
    after = snapshot(root, include_trash=True)
    r04 = check(d04["expect"], before, after, manifest)
    r02 = check(d02["expect"], before, after, manifest)
    assert not all(c["ok"] for c in r04) and any("不是文件名最短的" in c["detail"] for c in r04)
    assert all(c["ok"] for c in r02)
