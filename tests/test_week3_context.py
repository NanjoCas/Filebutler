"""第 3 周：列表分页不截断、上下文压缩、按费用的预算。"""
import copy
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console

from agent.context import MARK, ContextManager, summarize
from agent.llm import LLM
from agent.loop import run_agent
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from tools import registry
import tools.fs_read  # noqa: F401


# ---------- A. 分页 ----------
@pytest.fixture
def many(tmp_path):
    sb = Sandbox(tmp_path / "s")
    for i in range(60):
        (sb.root / f"A very long paper title number {i:02d} about fake news detection and more.pdf").write_text("x")
    return sb


def d(sb, name, **args):
    return registry.dispatch(name, json.dumps(args, ensure_ascii=False), sb)


def test_list_dir_pages_never_truncate(many):
    seen, page = [], 1
    while True:
        out = d(many, "list_dir", page=page)
        assert "已截断" not in out and len(out) <= 2000
        seen += out.splitlines()[1:-1]
        if "下一页" not in out:
            break
        assert f"page={page + 1}" in out.splitlines()[-1]
        page += 1
    assert page > 1 and len(seen) == len(set(seen)) == 60
    assert d(many, "list_dir", page=99).startswith("ERROR: 没有第 99 页")


def test_search_files_pages(many):
    first = d(many, "search_files", ext="pdf")
    assert first.startswith("OK: 找到 60 个文件") and "下一页用 page=2" in first and "已截断" not in first
    small = d(many, "search_files", name_pattern="*number 01*")
    assert small.count("\n") == 1   # 只有一页时不加页脚


# ---------- B. 摘要 ----------
def test_summaries():
    read = "OK:\n<file_content>\nTitle Line\n" + "正文" * 500 + "\n</file_content>"
    s = summarize("read_file", {"path": "a.pdf"}, read)
    assert s.startswith(MARK) and "read_file(a.pdf)" in s and "<file_content>Title Line" in s and "</file_content>" in s
    assert "原 1011 字" in s and len(s) < 300
    ls = "OK:\na.pdf · 文件 · 1KB · 2026-01-01 00:00\nsub · 文件夹 · - · 2026-01-01 00:00\n共 2 项"
    assert summarize("list_dir", {"path": "."}, ls) == f"{MARK} list_dir(.)：a.pdf、sub/\n共 2 项"
    info = "OK:\n路径: a.jpg\n大小: 1KB\n修改时间: x\nMD5: abc\n拍摄日期: 2026:01:01"
    assert summarize("file_info", {"path": "a.jpg"}, info) == f"{MARK} file_info：路径: a.jpg | MD5: abc | 拍摄日期: 2026:01:01"
    assert summarize("read_file", {}, "ERROR: 不存在") is None          # ERROR 不压
    assert summarize("search_files", {}, "OK: 找到 1 个文件\na.pdf") is None
    assert summarize("read_file", {}, s) is None                       # 已压缩过的不再压


def history(rounds: int, size: int = 3000) -> list[dict]:
    msgs = [{"role": "system", "content": "系统"}, {"role": "user", "content": "指令"}]
    for r in range(rounds):
        msgs.append({"role": "assistant", "content": f"第{r}轮笔记：p{r}.pdf 讲强化学习",
                     "tool_calls": [{"id": f"c{r}", "type": "function",
                                     "function": {"name": "read_file", "arguments": json.dumps({"path": f"p{r}.pdf"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{r}",
                     "content": "OK:\n<file_content>\n" + f"标题{r} " + "x" * size + "\n</file_content>"})
    return msgs


def test_compress_keeps_pairs_and_recent_rounds():
    msgs = history(6)
    orig = copy.deepcopy(msgs)
    cm = ContextManager(high_water=2000, keep_recent=1)
    info = cm.maybe_compress(msgs)
    assert info and info["messages"] == 4 and info["tokens_after"] < info["tokens_before"]
    assert len(msgs) == len(orig)
    for a, b in zip(msgs, orig):
        assert a["role"] == b["role"] and a.get("tool_call_id") == b.get("tool_call_id")
        if a["role"] != "tool":
            assert a == b                                   # system / user / assistant 原封不动
    tools_ = [m for m in msgs if m["role"] == "tool"]
    assert all(m["content"].startswith(MARK) for m in tools_[:4])
    assert all(not m["content"].startswith(MARK) for m in tools_[4:])   # 最新一轮 + 最近 1 轮保留原文


def test_no_compression_below_water_and_batching():
    msgs = history(3, size=100)
    cm = ContextManager(high_water=12000)
    assert cm.maybe_compress(msgs) is None
    msgs = history(6)
    cm = ContextManager(high_water=2000)
    assert cm.maybe_compress(msgs)
    snapshot = copy.deepcopy(msgs)
    cm.after_call(len(msgs), 1500)                          # 压缩后调用一次，API 报告 1500 tokens
    msgs.append({"role": "assistant", "content": "继续", "tool_calls": [
        {"id": "z", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "z", "content": "OK:\n（空）\n共 0 项"})
    assert cm.maybe_compress(msgs) is None                  # 没超水位：不动旧消息，前缀缓存保持
    assert msgs[:len(snapshot)] == snapshot


def test_rereads_counted():
    msgs = history(6)
    cm = ContextManager(high_water=2000)
    cm.maybe_compress(msgs)
    cm.on_tool_call("read_file", json.dumps({"path": "p0.pdf"}))
    cm.on_tool_call("read_file", json.dumps({"path": "p5.pdf"}))   # 没被压缩过，不算
    assert cm.stats.rereads == 1


def test_loop_compresses_and_api_pairs_stay_valid(tmp_path):
    sb = Sandbox(tmp_path / "s")
    for i in range(8):
        (sb.root / f"p{i}.txt").write_text(f"标题{i}\n" + "内容" * 700, encoding="utf-8")
    script = [[("read_file", {"path": f"p{i}.txt"})] for i in range(8)] + ["读完了"]

    class SizedClient(FakeClient):  # 按实际发送的字数报告 prompt_tokens，像真实 API 一样
        def create(self, **kw):
            resp = super().create(**kw)
            resp.usage = NS(prompt_tokens=sum(len(m.get("content") or "") for m in kw["messages"]) // 2,
                            completion_tokens=10)
            return resp
    client = SizedClient(script)
    client.chat = NS(completions=NS(create=client.create))
    res = run_agent("读一遍", LLM({"model": "fake"}, client=client), sb,
                    {"context_compress": True, "context_high_water": 1500}, Console(quiet=True))
    assert res.stop_reason == "done" and res.compressions >= 1 and res.compressed_msgs >= 1
    for sent in client.seen:                                # 每次发给模型的历史都满足配对约束
        ids = {tc["id"] for m in sent if m["role"] == "assistant" for tc in m.get("tool_calls", [])}
        assert all(m["tool_call_id"] in ids for m in sent if m["role"] == "tool")
    assert any(MARK in m["content"] for m in client.seen[-1] if m["role"] == "tool")


# ---------- C. 按费用的预算 ----------
def test_cost_budget_stops_run(tmp_path):
    sb = Sandbox(tmp_path / "s")
    usage = NS(prompt_tokens=100_000, completion_tokens=0, prompt_cache_hit_tokens=0)   # 每次 ¥0.2
    call = NS(id="c", function=NS(name="list_dir", arguments="{}"))
    client = NS(chat=NS(completions=NS(create=lambda **kw: NS(
        choices=[NS(message=NS(content="", tool_calls=[call]))], usage=usage))))
    llm = LLM({"model": "m", "prices": {"m": {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0}}}, client=client)
    res = run_agent("x", llm, sb, {"cost_budget": 0.3, "max_steps": 10}, Console(quiet=True))
    assert res.stop_reason == "cost_budget" and res.steps == 2
    llm2 = LLM({"model": "no-price"}, client=client)                                # 没价格：退回按 Token
    res = run_agent("x", llm2, sb, {"cost_budget": 0.3, "token_budget": 250_000, "max_steps": 10,
                                 "repeat_detection": False}, Console(quiet=True))
    assert res.stop_reason == "token_budget" and res.steps == 3
