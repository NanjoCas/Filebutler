"""用假模型验证重复调用的提醒、拦截和恢复，不调用 API，不碰用户沙盒。"""
import json

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient


def run(tmp_path, script, **cfg):
    sb = Sandbox(tmp_path / "sandbox")
    client = FakeClient(script)
    trace = tmp_path / "trace.jsonl"
    res = run_agent("测试重复调用", LLM({"model": "fake"}, client=client), sb,
                    {"max_steps": 8, **cfg}, Console(quiet=True), trace_path=trace,
                    oplog=OpLog(sb, "repeat-test"))
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    return res, client, sb, rows


def test_second_call_warns_and_model_can_recover(tmp_path):
    res, client, _, _ = run(tmp_path, [
        [("list_dir", {})], [("list_dir", {})], "目录为空，无需整理。",
    ])
    assert res.stop_reason == "answered"
    assert "重复调用提醒" not in client.seen[1][-1]["content"]
    assert "重复调用提醒" in client.seen[2][-1]["content"]
    assert "OK:" in client.seen[2][-1]["content"]


def test_json_formatting_does_not_bypass_detection(tmp_path):
    res, client, _, rows = run(tmp_path, [
        [("list_dir", '{"path": ".", "page": 1}')],
        [("list_dir", '{ "page":1, "path":"." }')],
        [("list_dir", {"path": ".", "page": 1})],
    ])
    assert res.stop_reason == "repeated_call" and res.steps == 3
    assert len(client.seen) == 3 and res.tool_calls == 3
    calls = [r for r in rows if "tool" in r]
    assert [r["repeat_count"] for r in calls] == [1, 2, 3]
    assert [r["executed"] for r in calls] == [True, True, False]
    assert rows[-1]["stop_reason"] == "repeated_call"
    assert "任务已中止" in res.final and "重复调用保护" in res.final


@pytest.mark.parametrize("middle", [
    ("list_dir", {"page": 2}),       # 换参数，包括合法的翻页
    ("search_files", {}),           # 换工具
])
def test_different_call_resets_count(tmp_path, middle):
    res, _, _, rows = run(tmp_path, [
        [("list_dir", {})], [("list_dir", {})], [middle],
        [("list_dir", {})], [("list_dir", {})], "完成",
    ])
    assert res.stop_reason == "answered"
    assert [r["repeat_count"] for r in rows if r.get("tool") not in (None, "finish_turn")] == [1, 2, 1, 1, 2]


def test_detection_can_be_disabled_for_ablation(tmp_path):
    res, _, _, rows = run(tmp_path, [[("list_dir", {})]] * 4 + ["完成"],
                          repeat_detection=False)
    assert res.stop_reason == "answered" and res.tool_calls == 5  # four list_dir and the finish_turn that ends it
    assert all(r["executed"] for r in rows if "tool" in r)
    assert not any("重复调用提醒" in m["content"] for m in res.messages if m["role"] == "tool")


def test_replay_history_preserves_consecutive_count(tmp_path):
    first, _, sb, _ = run(tmp_path, [[("list_dir", {})]] * 2, max_steps=2)
    client = FakeClient([[("list_dir", {})]])
    res = run_agent("继续", LLM({"model": "fake"}, client=client), sb, {},
                    Console(quiet=True), history=first.messages)
    assert res.stop_reason == "repeated_call"
    assert any(m['role']=='tool' and '未执行' in m.get('content','') for m in res.messages)
    assert res.messages[-1]['content']==res.final and '重复调用保护' in res.final


# User decision 2026-10-23: a call the runtime blocks before dispatch (invalid JSON,
# arguments the tool does not accept, out of scope, over budget) counts as a repeat too.
@pytest.mark.parametrize("raw,reason", [
    ("{bad json", "Expecting property name"),
    ('{"page": "two"}', "page"),
])
def test_repeated_blocked_call_is_warned_then_stopped(tmp_path, raw, reason):
    res, client, _, rows = run(tmp_path, [[("list_dir", raw)]] * 3)
    assert reason in client.seen[1][-1]["content"] and "重复调用提醒" not in client.seen[1][-1]["content"]
    assert "重复调用提醒" in client.seen[2][-1]["content"]
    assert res.stop_reason == "repeated_call" and len(client.seen) == 3
    assert [r["repeat_count"] for r in rows if "tool" in r] == [1, 2, 3]
    assert not any(r["executed"] for r in rows if "tool" in r)
