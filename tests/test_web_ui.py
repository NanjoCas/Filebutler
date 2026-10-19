"""Web rendering and live step events; fake models, temporary files, no API spend."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from web_render import FileCatalog, WEB_PRESENTATION_GUIDANCE, render_markdown
import webapp


@pytest.fixture
def files(tmp_path):
    sb = Sandbox(tmp_path / "sandbox")
    (sb.root / "文献").mkdir()
    (sb.root / "文献" / "paper #1 (初稿).pdf").touch()
    (sb.root / "notes.md").write_text("测试内容", encoding="utf-8")
    return sb


def test_markdown_table_and_page_reference(files):
    href = "sandbox:/" + quote("文献/paper #1 (初稿).pdf") + "#page=3"
    result = render_markdown(f"## 相关论文\n\n| 论文 | 主题 |\n| --- | --- |\n| [论文]({href}) | **强化学习** |", FileCatalog(files))
    assert "<table>" in result and "<th>论文</th>" in result
    assert "<h2>相关论文</h2>" in result and "<strong>强化学习</strong>" in result
    assert 'data-file-path="文献/paper #1 (初稿).pdf"' in result
    assert 'data-file-page="3"' in result


def test_legacy_paths_are_linked_but_fenced_code_is_literal(files):
    result = render_markdown("`notes.md`\n\n查看 notes.md。\n\n```text\nnotes.md\n```", FileCatalog(files))
    assert result.count('class="file-reference"') == 2
    assert '<pre><code class="language-text">notes.md' in result


def test_unambiguous_names_only_and_deleted_files(files):
    (files.root / "文献" / "notes.md").touch()
    catalog = FileCatalog(files)
    assert catalog.resolve("notes.md") == ("notes.md", None)  # an exact root path remains valid
    (files.root / "x").mkdir()
    (files.root / "x" / "same.pdf").touch()
    (files.root / "文献" / "same.pdf").touch()
    catalog = FileCatalog(files)
    assert catalog.resolve("same.pdf") is None
    assert catalog.resolve("文献/same.pdf") == ("文献/same.pdf", None)
    (files.root / "notes.md").unlink()
    assert catalog.resolve("notes.md") is None


def test_html_and_active_urls_are_not_executed(files):
    result = render_markdown('<script>alert(1)</script>\n\n[x](javascript:alert%281%29)\n\n![x](https://example.com/track.png)\n\n[x](data:text/html,bad)', FileCatalog(files))
    assert "<script>" not in result and "&lt;script&gt;" in result
    assert "<img" not in result and 'href="javascript:' not in result and 'href="data:' not in result
    assert 'src="https://' not in result


def test_unavailable_hidden_symlink_and_external_references(files, tmp_path):
    (files.root / ".secret.pdf").touch()
    outside = tmp_path / "outside.pdf"; outside.touch()
    (files.root / "link.pdf").symlink_to(outside)
    result = render_markdown("[missing](sandbox:/missing.pdf) [.secret](sandbox:/.secret.pdf) [outside](sandbox:/../outside.pdf) [link](sandbox:/link.pdf) [web](https://example.com)", FileCatalog(files))
    assert 'class="file-reference"' not in result
    assert result.count('class="unavailable-reference"') == 4
    assert 'rel="noopener noreferrer"' in result and 'target="_blank"' in result


def test_reference_label_code_does_not_create_nested_buttons(files):
    result = render_markdown("[`notes.md`](sandbox:/notes.md)", FileCatalog(files))
    assert result.count("<button") == 1 and result.count("</button>") == 1
    assert "<code>notes.md</code>" in result


def test_file_attributes_and_labels_are_escaped(files):
    name = 'a"&<b>.txt'; (files.root / name).touch()
    result = render_markdown(f"`{name}`", FileCatalog(files))
    assert 'data-file-path="a&quot;&amp;&lt;b&gt;.txt"' in result
    assert '<b>' not in result


def test_event_callback_reports_real_work_in_order(files):
    events = []
    result = run_agent("读取", LLM({"model": "fake"}, client=FakeClient([
        [("read_file", {"path": "notes.md"})], "已读取。",
    ])), files, {}, Console(quiet=True), on_event=events.append)
    assert result.stop_reason == "done"
    assert [e["type"] for e in events] == ["llm_start", "llm_end", "tool_start", "tool_end", "llm_start", "llm_end"]
    assert events[2]["call_id"] == events[3]["call_id"]
    assert events[2]["args"] == {"path": "notes.md"} and events[3]["ok"]
    assert not any("reasoning_content" in e for e in events)


def test_broken_status_observer_does_not_interrupt_agent(files):
    def broken(event):
        raise RuntimeError("display disconnected")
    result = run_agent("完成", LLM({"model": "fake"}, client=FakeClient(["完成"])), files,
                       {}, Console(quiet=True), on_event=broken)
    assert result.final == "完成" and result.stop_reason == "done"


@pytest.fixture
def ui(tmp_path, monkeypatch, files):
    state = tmp_path / "ui"; state.mkdir()
    monkeypatch.setattr(webapp, "STATE_DIR", state)
    monkeypatch.setattr(webapp, "DB_PATH", state / "state.sqlite3")
    monkeypatch.setattr(webapp, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(webapp, "TURN_STATES", {})
    monkeypatch.setattr(webapp, "ACTIVE_CONVERSATIONS", set())
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(webapp, "POOL", pool)
    monkeypatch.setattr(webapp, "_config", lambda: {"model": "fake", "sandbox": str(files.root), "prompt": "v4_evidence", "confirm": "always"})
    with TestClient(webapp.app) as client:
        try:
            yield client
        finally:
            for turn in webapp.TURN_STATES.values():
                if turn.confirm_event:
                    turn.confirm_answer = "n"
                    turn.confirm_event.set()
            pool.shutdown(wait=True)


def test_api_reports_model_start_before_completion_and_renders_saved_markdown(ui, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    fake = FakeClient([[("read_file", {"path": "notes.md"})], "| 文件 | 说明 |\n| --- | --- |\n| [笔记](sandbox:/notes.md) | **已读取** |"])
    original_chat = fake.chat.completions.create
    def blocking_chat(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_chat(**kwargs)
    fake.chat.completions.create = blocking_chat
    monkeypatch.setattr(webapp, "LLM", lambda cfg: LLM({"model": "fake"}, client=fake))
    original_event = webapp._event
    def notify(turn_id, event):
        original_event(turn_id, event)
        if event["type"] in ("completed", "failed"):
            finished.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    turn = ui.post(f"/api/conversations/{cid}/messages", json={"content": "查看笔记"}).json()
    try:
        assert entered.wait(3)
        live = ui.get(f"/api/turns/{turn['turn_id']}").json()
        assert live["status"] == "running" and any(e["type"] == "llm_start" for e in live["events"])
    finally:
        release.set()
    assert finished.wait(5)
    saved = ui.get(f"/api/conversations/{cid}").json()
    assert "<table>" in saved["messages"][-1]["html"]
    assert 'data-file-path="notes.md"' in saved["messages"][-1]["html"]
    assert saved["messages"][-1]["content"].startswith("| 文件")
    assert len([e for e in saved["latest_turn"]["events"] if e["type"] == "tool_end"]) == 1
    with webapp._db() as db:
        history = json.loads(db.execute("SELECT history_json FROM conversations WHERE id=?", (cid,)).fetchone()[0])
    assert WEB_PRESENTATION_GUIDANCE in history[0]["content"]
    assert any(m["role"] == "tool" for m in history)


def test_ui_confirmation_remains_interactive_with_live_steps(ui, monkeypatch, files):
    waiting, finished = threading.Event(), threading.Event()
    fake = FakeClient([[("make_dir", {"path": "blocked"})], "操作已拒绝。"])
    monkeypatch.setattr(webapp, "LLM", lambda cfg: LLM({"model": "fake"}, client=fake))
    original_event = webapp._event
    def notify(turn_id, event):
        original_event(turn_id, event)
        if event["type"] == "confirmation_required": waiting.set()
        if event["type"] in ("completed", "failed"): finished.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    tid = ui.post(f"/api/conversations/{cid}/messages", json={"content": "创建目录"}).json()["turn_id"]
    assert waiting.wait(4)
    turn = ui.get(f"/api/turns/{tid}").json()
    assert turn["pending_confirmation"] and turn["events"][-2]["type"] == "tool_start"
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "y", "confirmation_id": "expired"}).status_code == 409
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "n", "confirmation_id": turn["pending_confirmation"]["id"]}).status_code == 200
    assert finished.wait(5)
    assert not (files.root / "blocked").exists()
    assert ui.get(f"/api/turns/{tid}").json()["status"] == "completed"
