"""真实数据：placed / retitled / others_unchanged.except 断言，以及真实任务的标准答案自检。
真实数据不进 git，本机没有 data/real/ 时自检部分自动跳过。"""
import shutil
from pathlib import Path, PurePosixPath

import pytest

from agent.llm import LLM
from eval.checker import check, select, snapshot
from eval.runner import REAL_DATA, load_tasks, prepare, run_one
from tests.test_week1 import FakeClient


def make_tree(root: Path, files: dict[str, str]):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


MANIFEST = {"files": {"a.pdf": {"topic_ok": ["covid-19", "review"], "title_key": "Weak Supervision for"},
                      "b.pdf": {"topic_ok": [".", "review"]},
                      "c.pdf": {}}}


def test_placed_accepts_any_allowed_folder(tmp_path):
    make_tree(tmp_path, {"a.pdf": "A", "b.pdf": "B", "c.pdf": "C", "review/x.pdf": "X"})
    before = snapshot(tmp_path, True)
    (tmp_path / "a.pdf").rename(tmp_path / "review" / "a.pdf")             # review 在可接受列表里
    after = snapshot(tmp_path, True)
    res = check([{"placed": {"names": ["a.pdf", "b.pdf"], "field": "topic_ok"}}, {"others_unchanged": True}],
                before, after, MANIFEST)
    assert all(r["ok"] for r in res), res                                   # b 留在根目录（"."）也对
    (tmp_path / "review" / "a.pdf").rename(tmp_path / "a.pdf")
    (tmp_path / "x").mkdir()
    (tmp_path / "a.pdf").rename(tmp_path / "x" / "a.pdf")                 # x 不在列表里
    res = check([{"placed": {"names": ["a.pdf"], "field": "topic_ok"}}], before, snapshot(tmp_path, True), MANIFEST)
    assert not res[0]["ok"] and "x/a.pdf" in res[0]["detail"]


def test_placed_requires_label(tmp_path):
    make_tree(tmp_path, {"c.pdf": "C"})
    before = snapshot(tmp_path, True)
    with pytest.raises(ValueError, match="缺少标注"):
        check([{"placed": {"names": ["c.pdf"], "field": "topic_ok"}}], before, before, MANIFEST)


@pytest.mark.parametrize("new_name,ok", [
    ("Weak Supervision for Fake News Detection via RL.pdf", True),
    ("weak_supervision_for_fake_news.pdf", True),        # 大小写、下划线不影响
    ("Weak Supervision.pdf", False),                     # 关键片段不完整
    ("Weak Supervision for Fake News.txt", False),       # 扩展名变了
])
def test_retitled_loose_match(tmp_path, new_name, ok):
    make_tree(tmp_path, {"a.pdf": "A"})
    before = snapshot(tmp_path, True)
    (tmp_path / "a.pdf").rename(tmp_path / new_name)
    res = check([{"retitled": {"names": ["a.pdf"], "field": "title_key"}}], before, snapshot(tmp_path, True), MANIFEST)
    assert res[0]["ok"] is ok


def test_others_unchanged_except(tmp_path):
    make_tree(tmp_path, {"a.pdf": "A", "c.pdf": "C"})
    before = snapshot(tmp_path, True)
    (tmp_path / "c.pdf").rename(tmp_path / "C 改名.pdf")
    after = snapshot(tmp_path, True)
    assert not check([{"others_unchanged": True}], before, after, MANIFEST)[0]["ok"]
    assert check([{"others_unchanged": {"except": ["c.pdf"]}}], before, after, MANIFEST)[0]["ok"]


# ---------- 真实任务的标准答案自检（需要本机有 data/real/）----------
REAL_TASKS = load_tasks("real")
needs_data = pytest.mark.skipif(not (REAL_DATA / "papers").is_dir(), reason="本机没有 data/real/papers")


def oracle_script(task, manifest, before):
    calls, used = [], set(before)
    for item in task["expect"]:
        (kind, arg), = item.items()
        sel = {k: v for k, v in arg.items() if k not in ("to", "keep", "field")} if isinstance(arg, dict) else {}
        if kind == "moved":
            for src in select(sel, before, manifest):
                calls += [("make_dir", {"path": arg["to"]}), ("move", {"src": src, "dst_dir": arg["to"]})]
        elif kind == "dedup":
            groups = {}
            for src in select(sel, before, manifest):
                groups.setdefault(before[src], []).append(src)
            for g in (g for g in groups.values() if len(g) > 1):
                calls += [("trash", {"path": p}) for p in sorted(g)[1:]]
        elif kind == "retitled":
            for src in select(sel, before, manifest):
                stem, n = manifest["files"][src][arg["field"]], 1
                name = f"{stem}.pdf"
                while str(PurePosixPath(src).parent / name).lstrip("./") in used:   # 同名论文的两个版本
                    n += 1
                    name = f"{stem} ({n}).pdf"
                used.add(str(PurePosixPath(src).parent / name).lstrip("./"))
                calls.append(("rename", {"path": src, "new_name": name}))
    answer = " ".join(str(a) for i in task["expect"] for k, a in i.items() if k == "answer_contains")
    return ([calls] if calls else []) + [answer or "完成"]


@needs_data
@pytest.mark.parametrize("task", REAL_TASKS, ids=lambda t: t["id"])
def test_real_oracle_passes_and_undo_restores(task, tmp_path):
    def make(cfg, t, manifest):
        before = snapshot(tmp_path / "peek", True) if prepare(t["scenario"], t["seed"], tmp_path / "peek") else None
        return LLM({"model": "oracle"}, client=FakeClient(oracle_script(t, manifest, before)))
    r = run_one(task, 1, {"max_steps": 3}, tmp_path, make_llm=make)
    assert r["success"], [c for c in r["checks"] if not c["ok"]]
    assert r["restored"]
    assert not (REAL_DATA / "papers" / ".oplog").exists()   # 原始数据从未被改动
