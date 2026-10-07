"""第 2 周：操作日志、undo、确认机制。"""
import json
import os
import unicodedata
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



@pytest.mark.parametrize("answer,allowed,allow_all", [
    ("y", True, False), ("Y", True, False), (" a ", True, True), ("A", True, True), ("n", False, False),
])
def test_letter_options(answer, allowed, allow_all):
    c = Confirmer("always", ask=lambda q: answer)
    assert c.allow("trash", {"path": "x"}) is allowed and c.allow_all is allow_all


def test_invalid_input_asks_again_until_valid():
    answers = iter(["全部允许", "ok", "", "yes", "a"])   # 只有单个字母有效，其余都要重输
    asked = []
    c = Confirmer("always", ask=lambda q: asked.append(q) or next(answers))
    assert c.allow("trash", {"path": "x"}) and c.allow_all and c.rejections == 0
    assert len(asked) == 5
    assert asked[0].startswith("⚠ 需要确认：trash") and "path = x" in asked[0]
    assert "y = 允许   n = 拒绝   a = 本次全部允许" in asked[0] and asked[0].endswith("请输入 y / n / a：")
    assert asked[1] == "无效输入「全部允许」，请输入 y / n / a："


def test_eof_rejects():
    def eof(q):
        raise EOFError
    c = Confirmer("always", ask=eof)
    assert not c.allow("trash", {"path": "x"}) and c.rejections == 1



def test_filename_with_leading_space(tmp_path):
    """真实数据里有文件名以空格开头的论文；原样路径和丢了空格的路径都要能找到它。"""
    sb = Sandbox(tmp_path / "s")
    (sb.root / "t").mkdir()
    (sb.root / "t" / " Exploiting.pdf").write_text("x")
    (sb.root / "dst").mkdir()
    for path in ["t/ Exploiting.pdf", "t/Exploiting.pdf"]:
        assert sb.path(path) == (sb.root / "t" / " Exploiting.pdf").resolve()
    out = registry.dispatch("move", json.dumps({"src": "t/Exploiting.pdf", "dst_dir": "dst"}), sb)
    assert out.startswith("OK") and (sb.root / "dst" / " Exploiting.pdf").exists()
    assert sb.path("  ") == sb.root                       # 空白路径仍是沙盒根目录



@pytest.mark.parametrize("real,typed", [
    ("A Di\ufb00erent Kind.pdf", "A Different Kind.pdf"),          # 连字 ﬀ（真实论文里出现过）
    ("Report.PDF", "report.pdf"),                                     # 大小写
    (unicodedata.normalize("NFD", "论文é.pdf"), "论文é.pdf"),          # NFD 与 NFC
])
def test_move_keeps_real_name_and_undo_restores(tmp_path, real, typed):
    """macOS 不区分大小写且做 Unicode 折叠：模型写的名字和真实名字不同也能找到文件，
    但移动后必须保留真实名字，undo 后快照必须完全一致。"""
    sb = Sandbox(tmp_path / "s")
    (sb.root / real).write_text("x")
    (sb.root / "dst").mkdir()
    if not (sb.root / typed).exists():
        pytest.skip("当前文件系统区分大小写/不做折叠，不会出现这个问题")
    before = snapshot(sb.root, include_trash=True)
    log = OpLog(sb, "r")
    out = registry.dispatch("move", json.dumps({"src": typed, "dst_dir": "dst"}, ensure_ascii=False), sb, oplog=log)
    assert out.startswith("OK")
    assert os.listdir(sb.root / "dst") == [real]                    # 名字没被改
    undo(sb, "r")
    assert os.listdir(sb.root).count(real) == 1 and snapshot(sb.root, include_trash=True) == before
