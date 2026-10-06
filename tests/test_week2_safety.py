"""第 2 周：操作日志、undo、确认机制。"""
import json
from collections import Counter

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from eval.checker import snapshot
from eval.generator import make_demo
from safety.confirm import Confirmer
from safety.oplog import OpLog, latest_run, undo
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from tools import registry


@pytest.fixture
def sb(tmp_path):
    make_demo(tmp_path / "sandbox")
    return Sandbox(tmp_path / "sandbox")


def run(script, sb, run_id="r1", confirmer=None):
    return run_agent("测试", LLM({"model": "fake"}, client=FakeClient(script)), sb, {}, Console(quiet=True),
                     oplog=OpLog(sb, run_id), confirmer=confirmer)


MESSY_RUN = [
    [("make_dir", {"path": "文档/PDF"})],                                   # 一次建两层
    [("move", {"src": "季度报告.pdf", "dst_dir": "文档/PDF"}),
     ("move", {"src": "invoice_2026_03.pdf", "dst_dir": "文档/PDF"})],
    [("rename", {"path": "文档/PDF/invoice_2026_03.pdf", "new_name": "发票.pdf"}),
     ("rename", {"path": "notes.md", "new_name": "NOTES.md"}),               # 仅大小写不同
     ("trash", {"path": "IMG_2041.jpg"}),
     ("move", {"src": "不存在.txt", "dst_dir": "文档"})],                     # 失败的操作不记日志
    "完成",
]


def test_undo_restores_exactly(sb):
    before = snapshot(sb.root)
    run(MESSY_RUN, sb)
    assert snapshot(sb.root) != before
    # 全局不变量：没有删除，所以文件 MD5 集合不变（回收区里的也算）
    files = lambda s: Counter(v for v in s.values() if v != "<dir>")
    assert files(snapshot(sb.root, include_trash=True)) == files(before)

    ops = [json.loads(l)["op"] for l in (sb.root / ".oplog" / "r1.jsonl").read_text().splitlines()]
    assert ops == ["make_dir", "make_dir", "move", "move", "rename", "rename", "trash"]

    report = undo(sb)
    assert not any(line.startswith("跳过") for line in report), report
    assert snapshot(sb.root) == before
    assert (sb.root / "notes.md").exists() and not any(p.name == "NOTES.md" for p in sb.root.iterdir())


def test_undo_twice_and_latest(sb):
    assert undo(sb) == ["没有可以撤销的运行"]
    run([[("make_dir", {"path": "A"})], "ok"], sb, run_id="20260101-000000")
    run([[("make_dir", {"path": "B"})], "ok"], sb, run_id="20260102-000000")
    assert latest_run(sb) == "20260102-000000"
    undo(sb)
    assert not (sb.root / "B").exists() and (sb.root / "A").exists()
    assert latest_run(sb) == "20260101-000000"
    assert "已经撤销过" in undo(sb, "20260102-000000")[0]


def test_undo_skips_when_occupied(sb):
    run([[("make_dir", {"path": "报告"})], [("move", {"src": "notes.md", "dst_dir": "报告"})], "ok"], sb)
    (sb.root / "notes.md").write_text("用户后来新建的同名文件")
    report = undo(sb)
    assert any("已被占用" in l for l in report) and any("不为空" in l for l in report)
    assert (sb.root / "notes.md").read_text() == "用户后来新建的同名文件"  # 没有被覆盖
    assert (sb.root / "报告" / "notes.md").exists()


def test_confirm_risky_trash_rejected(sb):
    asked = []
    c = Confirmer("risky", ask=lambda q: asked.append(q) or "n")
    res = run([[("move", {"src": "notes.md", "dst_dir": "."}),
                ("make_dir", {"path": "X"}),
                ("trash", {"path": "data.csv"})], "好的，没有删除"], sb, confirmer=c)
    assert len(asked) == 1 and "trash" in asked[0]          # 只有 trash 被问
    assert (sb.root / "data.csv").exists()
    assert res.rejections == 1
    tool_msgs = [m["content"] for m in res.messages if m["role"] == "tool"]
    assert "用户拒绝" in tool_msgs[-1]
    assert res.stop_reason == "done"                         # 拒绝不中止，交给模型调整


def test_confirm_threshold_and_allow_all():
    asked = []
    c = Confirmer("risky", threshold=2, ask=lambda q: asked.append(q) or "a")
    assert all(c.allow("move", {"src": f"{i}"}) for i in range(5))
    assert len(asked) == 1                                   # 第 3 个开始问，答 a 后不再问
    c2 = Confirmer("always", ask=lambda q: "y")
    assert c2.allow("make_dir", {"path": "x"})
    c3 = Confirmer("never", ask=lambda q: pytest.fail("never 模式不应询问"))
    assert c3.allow("trash", {"path": "x"})


def test_blocked_paths_never_reach_confirm(sb):
    c = Confirmer("always", ask=lambda q: pytest.fail("越界操作不应该问用户"))
    for name, args in [("trash", {"path": "../x"}), ("move", {"src": "notes.md", "dst_dir": ".trash"}),
                       ("rename", {"path": ".oplog", "new_name": "x"})]:
        assert registry.dispatch(name, json.dumps(args), sb, confirmer=c).startswith("ERROR: 拒绝")
    assert sb.violations == 3
