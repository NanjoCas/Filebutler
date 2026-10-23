"""Listing pagination and the cost budget (kept from the retired week-3 and
reflection modules; these mechanisms are unchanged by the v2 retirement)."""
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from safety.sandbox import Sandbox
from tools import registry
import tools.fs_read  # noqa: F401


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


def test_cost_uses_cache_prices():
    usage = NS(prompt_tokens=10_000, completion_tokens=1_000, prompt_cache_hit_tokens=8_000)
    client = NS(chat=NS(completions=NS(create=lambda **kw: NS(
        choices=[NS(message=NS(content="ok", tool_calls=None))], usage=usage))))
    llm = LLM({"model": "m", "prices": {"m": {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0}}}, client=client)
    llm.chat([{"role": "user", "content": "hi"}])
    assert llm.cache_hit_tokens == 8_000
    assert llm.cost == pytest.approx((8_000 * 0.04 + 2_000 * 2.0 + 1_000 * 8.0) / 1e6)
    assert LLM({"model": "unknown"}, client=client).cost == 0.0
