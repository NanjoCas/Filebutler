"""结束前自检：触发条件、只触发一次、核对通过沿用原总结、发现问题能修正、成本统计。"""
from types import SimpleNamespace as NS

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from eval.generator import make_demo
from eval.runner import load_tasks, run_one
from safety.sandbox import Sandbox
from tests.test_regressions import recorded
from tests.test_week1 import FakeClient


@pytest.fixture
def sb(tmp_path):
    make_demo(tmp_path / "sandbox")
    return Sandbox(tmp_path / "sandbox")


def run(script, sb, reflect=True, min_writes=1):
    client = FakeClient(script)  # 剧本用完还被调用会 IndexError，所以多余的调用会直接暴露
    res = run_agent("把 PDF 移到报告", LLM({"model": "fake"}, client=client), sb,
                    {"reflect": reflect, "reflect_min_writes": min_writes}, Console(quiet=True))
    return res, client


def reflection_msgs(res):
    return [m for m in res.messages if m["role"] == "user" and "结束前自检" in m["content"]]


MOVE_ONE = [[("make_dir", {"path": "报告"})], [("move", {"src": "季度报告.pdf", "dst_dir": "报告"})]]


def test_pass_keeps_original_summary(sb):
    res, _ = run(MOVE_ONE + ["已移动 1 个 PDF。", "核对通过，没有问题。"], sb)
    assert res.reflection == "pass" and res.final == "已移动 1 个 PDF。"
    (msg,) = reflection_msgs(res)
    assert "move(src=季度报告.pdf, dst_dir=报告)" in msg["content"]     # 写操作清单
    assert "invoice_2026_03.pdf" in msg["content"]                       # 沙盒现状：没移的 PDF 也看得到
    assert "报告/季度报告.pdf" in msg["content"]


def test_fix_after_reflection_and_only_once(sb):
    res, _ = run(MOVE_ONE + [
        "已移动 1 个 PDF。",
        [("move", {"src": "invoice_2026_03.pdf", "dst_dir": "报告"}),
         ("move", {"src": "Attention Is All You Need.pdf", "dst_dir": "报告"})],
        "补移了 2 个遗漏的 PDF，现在 3 个都在报告里。",                    # 第二次结束不再自检
    ], sb)
    assert res.reflection == "fixed" and len(reflection_msgs(res)) == 1
    assert res.final.startswith("补移了") and len(list((sb.root / "报告").iterdir())) == 3


def test_no_reflection_without_writes_or_when_off(sb):
    res, _ = run([[("search_files", {"ext": "pdf"})], "有 3 个 PDF。"], sb)          # 只读
    assert res.reflection == "" and not reflection_msgs(res)
    res, _ = run([[("move", {"src": "不存在.pdf", "dst_dir": "."})], "没找到"], sb)  # 写操作失败也不算
    assert res.reflection == ""
    res, _ = run(MOVE_ONE + ["完成"], sb, reflect=False)                              # 关闭
    assert res.reflection == ""


def test_reflection_can_fix_recorded_c05_failure(tmp_path):
    """录下来的 C05 漏文件案例：如果模型在自检里发现并补上 scan_2616.pdf，任务就判成功。"""
    data, _ = recorded("C05_3_missed_file.json")
    turns = [t if isinstance(t, str) else [tuple(c) for c in t] for t in data["assistant_turns"]]
    script = turns + [[("move", {"src": "scan_2616.pdf", "dst_dir": "上半年"})], "补上了 scan_2616.pdf（05-23）"]
    make = lambda cfg, task, manifest: LLM({"model": "fake"}, client=FakeClient(script))
    task = load_tasks("synthetic", ids=["C05"])[0]
    r = run_one(task, 1, {"max_steps": 15, "reflect": True}, tmp_path, make_llm=make)
    assert r["success"] and r["reflection"] == "fixed" and r["restored"]
    refl = next(m for m in r["messages"] if m["role"] == "user" and "结束前自检" in m["content"])
    assert "\nscan_2616.pdf\n" in refl["content"]   # 自检清单里能看到它还留在根目录


def test_cost_uses_cache_prices():
    usage = NS(prompt_tokens=10_000, completion_tokens=1_000, prompt_cache_hit_tokens=8_000)
    client = NS(chat=NS(completions=NS(create=lambda **kw: NS(
        choices=[NS(message=NS(content="ok", tool_calls=None))], usage=usage))))
    llm = LLM({"model": "m", "prices": {"m": {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0}}}, client=client)
    llm.chat([{"role": "user", "content": "hi"}])
    assert llm.cache_hit_tokens == 8_000
    assert llm.cost == pytest.approx((8_000 * 0.04 + 2_000 * 2.0 + 1_000 * 8.0) / 1e6)
    assert LLM({"model": "unknown"}, client=client).cost == 0.0


def test_read_only_check_after_reflection_counts_as_pass(sb):
    """A06#2 的真实情况：自检时只做了只读检查，然后核对通过——不应记为“修正”。"""
    res, _ = run(MOVE_ONE + ["已移动 1 个 PDF。", [("file_info", {"path": "IMG_2041.jpg"})], "核对通过。"], sb)
    assert res.reflection == "pass" and res.final == "已移动 1 个 PDF。"


def test_min_writes_threshold(sb):
    """默认阈值 5：动了 4 个文件不自检，5 个才自检；make_dir 不计数。"""
    names = ["季度报告.pdf", "invoice_2026_03.pdf", "Attention Is All You Need.pdf", "notes.md", "data.csv"]
    four = [[("make_dir", {"path": "报告"}), ("make_dir", {"path": "归档"})]] + \
           [[("move", {"src": n, "dst_dir": "报告"}) for n in names[:4]]]
    res, _ = run(four + ["完成"], sb, min_writes=5)
    assert res.reflection == ""
    res, _ = run([[("move", {"src": "报告/" + names[0], "dst_dir": "."})],   # 再动 1 个，本次运行共 1 个
                  "完成"], sb, min_writes=5)
    assert res.reflection == ""


def test_threshold_reached_triggers(sb):
    names = ["季度报告.pdf", "invoice_2026_03.pdf", "Attention Is All You Need.pdf", "notes.md", "data.csv"]
    script = [[("make_dir", {"path": "报告"})], [("move", {"src": n, "dst_dir": "报告"}) for n in names],
              "完成", "核对通过。"]
    res, _ = run(script, sb, min_writes=5)
    assert res.reflection == "pass"
