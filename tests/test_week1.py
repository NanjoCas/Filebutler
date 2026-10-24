"""第 1 周离线测试：不调真实 API，用按剧本返回的假模型验证主循环、工具和沙盒。"""
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from eval.generator import make_demo
from safety.sandbox import Sandbox
from tools import registry


class FakeClient:
    """每次 create 返回剧本里的下一条消息；剧本项是 [(工具名, 参数dict), ...] 或最终文本。"""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.seen.append(list(kw["messages"]))  # 存快照，循环会继续往原列表追加
        item = self.script.pop(0)
        if isinstance(item, str):
            msg = NS(content=item, tool_calls=None)
        else:
            calls = [NS(id=f"call_{len(self.seen)}_{i}",
                        function=NS(name=n, arguments=a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)))
                     for i, (n, a) in enumerate(item)]
            msg = NS(content="", tool_calls=calls)
        return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=100, completion_tokens=10))


@pytest.fixture
def sb(tmp_path):
    make_demo(tmp_path / "sandbox")
    return Sandbox(tmp_path / "sandbox")


def run(script, sb, **cfg):
    client = FakeClient(script)
    res = run_agent("测试指令", LLM({"model": "fake"}, client=client), sb,
                    {"max_steps": 15, **cfg}, Console(quiet=True))
    return res, client


def test_count_pdfs(sb):
    res, client = run([[("search_files", {"ext": "pdf"})], "有 3 个 PDF。"], sb)
    assert res.stop_reason == "answered" and res.final == "有 3 个 PDF。"
    tool_msg = client.seen[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1_0"
    assert "找到 3 个文件" in tool_msg["content"]


def test_max_steps(sb):
    res, _ = run([[("list_dir", {"page": p})] for p in range(1, 4)], sb, max_steps=3)
    assert res.stop_reason == "max_steps" and res.steps == 3


def test_read_tools(sb):
    d = lambda n, a: registry.dispatch(n, json.dumps(a, ensure_ascii=False), sb)
    listing = d("list_dir", {})
    # Hidden files are not listed as entries; since 2026-10-21 a footer names them.
    assert not any(line.startswith(".DS_Store") for line in listing.splitlines()) and "共 11 项" in listing
    assert "个隐藏项未显示" in listing and ".DS_Store" in listing
    assert "牛奶" in d("read_file", {"path": "购物清单.txt"})          # GBK 编码
    assert "<file_content>" in d("read_file", {"path": "说明.txt"})
    # Since 2026-10-24 images are read by local OCR; this one is a blank 64x48 picture.
    image = d("read_file", {"path": "IMG_2041.jpg"})
    assert "没有识别到文字" in image or "无法识别图片文字" in image
    info = d("file_info", {"path": "IMG_2041.jpg"})
    assert "MD5" in info and "64x48" in info
    assert "页数: 3" in d("file_info", {"path": "季度报告.pdf"})
    assert "ERROR" in d("search_files", {"modified_after": "2026/01/01"})  # 日期格式校验


def test_schemas():
    names = {s["function"]["name"] for s in registry.schemas()}
    # Writes go through file plans (task runtime); single-step write tools are retired.
    assert names == {"list_dir", "search_files", "file_info", "read_file", "search_content", "find_duplicates", "apply_dedup", "deduplicate", "report_findings", "paper_profiles", "submit_classification", "inventory_files"}


def test_trace_records_instruction_and_final(sb, tmp_path):
    trace = tmp_path / "t.jsonl"
    run_agent("数一下 PDF", LLM({"model": "fake"}, client=FakeClient([[("search_files", {"ext": "pdf"})], "3 个"])),
              sb, {}, Console(quiet=True), trace_path=trace)
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"event": "start", "instruction": "数一下 PDF", "model": "fake"}
    assert next(r for r in rows if "tool" in r)["tool"] == "search_files"
    assert next(r for r in rows if r.get("event") == "llm")["ms"] >= 0
    assert any(r.get("event") == "llm_request" for r in rows)
    assert rows[-1]["llm_seconds"] >= 0
    assert rows[-1]["event"] == "end" and rows[-1]["final"] == "3 个" and rows[-1]["stop_reason"] == "answered"
