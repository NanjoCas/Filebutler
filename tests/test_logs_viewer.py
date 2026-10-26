"""The log page (web_logs.py, /api/logs, web/logs.html): the timeline must show every call.

Built from small synthetic runs, never real logs (logs/ is private and gitignored).
Pins what the page promises the reader learning the chain: each call the run made
is drawn; the conversation history fills in the model's output and the full results
only when it lines up with the trace; confirmations, file operations and task states
sit under the call they happened in; anything that cannot be placed is reported, not
dropped; handles link back to the call that produced them; a turn answered without
a task says why nothing was verified. The server returns codes and the page words
them with the chat page's labels (web/labels.js). The last test drives the page in
headless Chrome when one is installed.
"""
import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import web_logs as V
import webapp

LIMITS = {"llm_ms": 10_000, "tool_ms": 1_000, "llm_n": 1, "tool_n": 1}
LONG = "OK: file:F0123456789 evidence:E0123456789；仅支持片段范围内结论。\n<file_content>\n" + "正文" * 400


@pytest.fixture
def src(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "oplog").mkdir()
    state = tmp_path / "state.sqlite3"
    with sqlite3.connect(state) as db:
        db.executescript("""
            CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, history_json TEXT);
            CREATE TABLE turns (id TEXT PRIMARY KEY, conversation_id TEXT, run_id TEXT, status TEXT, created_at TEXT,
                                updated_at TEXT, events_json TEXT, final TEXT, error TEXT, result_json TEXT);""")
    tasks = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(tasks) as db:
        db.execute("CREATE TABLE task_revisions (id TEXT, revision INTEGER, data_json TEXT)")
    return V.Sources(log_dir=tmp_path / "logs", state_db=state, oplog_dir=tmp_path / "oplog", task_db=tasks)


@pytest.fixture
def tasks_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE task_revisions (id TEXT, revision INTEGER, data_json TEXT)")
    return db


def write_trace(src, run_id, records):
    (src.log_dir / f"{run_id}.trace.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def write_ops(src, run_id, ops):
    (src.oplog_dir / f"{run_id}.jsonl").write_text("\n".join(json.dumps(o) for o in ops), encoding="utf-8")


def tool(step, name, args, result="OK", **kw):
    return {"step": step, "tool": name, "args": json.dumps(args, ensure_ascii=False), "ok": True,
            "result": result[:300], "ms": 5, "repeat_count": 1, "executed": True, **kw}


def end(calls, **kw):
    return {"event": "end", "final": "答复", "stop_reason": "answered", "steps": 2, "tool_calls": calls,
            "seconds": 1.0, "runtime": {"task_id": None, "task_revision": None}, **kw}


def ev(t, at, **kw):
    return {"type": t, "at": f"2026-10-26T10:00:{at:02d}+08:00", **kw}


def tc(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def build(src, tasks_db, run_id, **kw):
    return V.build_turn(src, tasks_db, run_id, kw.pop("index", 1), LIMITS, **kw)


# a two-step read-only answer: search, then read and answer with finish_turn(mode=reply)
R1, R2, W1 = "20261026-100000-000001", "20261026-100100-000002", "20261026-100200-000003"
SEARCH = {"name_pattern": "*offer*"}
READ = {"path": "a.pdf"}
REPLY = {"mode": "reply", "reply": "找到 1 个"}
TRACE = [
    {"event": "start", "instruction": "我有哪些 offer", "model": "m"},
    {"event": "llm_request", "step": 1, "phase": "start", "attempt": 1, "input_chars": 900, "messages": 3},
    {"event": "llm_request", "step": 1, "phase": "end", "attempt": 1, "ok": True, "ms": 40, "prompt_tokens": 300},
    {"event": "llm", "step": 1, "ms": 40, "tokens": 320},
    tool(1, "search_files", SEARCH, "OK: 找到 1 个文件\na.pdf"),
    {"event": "llm", "step": 2, "ms": 50, "tokens": 400},
    tool(2, "read_file", READ, LONG),
    tool(2, "finish_turn", REPLY, "OK: 已回复用户，本轮结束"),
    end(3),
]
HISTORY = [
    {"role": "user", "content": "我有哪些 offer"},
    {"role": "system", "content": "【任务协议 v3】注入"},
    {"role": "assistant", "content": "", "reasoning_content": "先按文件名找", "tool_calls": [tc("c1", "search_files", SEARCH)]},
    {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "ok", "text": "OK: 找到 1 个文件\na.pdf"}, ensure_ascii=False)},
    {"role": "assistant", "content": "读一下", "reasoning_content": "确认内容",
     "tool_calls": [tc("c2", "read_file", READ), tc("c3", "finish_turn", REPLY)]},
    {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"status": "ok", "text": LONG}, ensure_ascii=False)},
    {"role": "tool", "tool_call_id": "c3", "content": json.dumps({"status": "ok", "text": "OK: 已回复用户，本轮结束"}, ensure_ascii=False)},
    {"role": "assistant", "content": "找到 1 个"},  # written by the runtime from finish_turn, not a model step
]


def calls_of(turn):
    return [i for s in turn["steps"] for i in s["items"]]


def levels(turn):
    return [(x["level"], x["text"]) for x in turn["completeness"]]


def test_every_call_is_drawn_from_the_trace_even_without_ui_events(src, tasks_db):
    # The bug that started this: a run whose UI events held only started/completed showed no calls at all.
    write_trace(src, R1, TRACE)
    turn = build(src, tasks_db, R1, events=[ev("started", 0), ev("completed", 9)], seg=HISTORY)
    assert [c["tool"] for c in calls_of(turn)] == ["search_files", "read_file", "finish_turn"]
    assert turn["shown_calls"] == turn["stats"]["tool_calls"] == 3
    assert any("网页端没有保存这一轮的过程事件" in text for _, text in levels(turn))


def test_a_run_counting_more_calls_than_it_recorded_is_flagged(src, tasks_db):
    write_trace(src, R1, TRACE[:-1] + [end(4)])
    turn = build(src, tasks_db, R1)
    assert ("bad", "运行结束时统计了 4 次工具调用，记录里只有 3 次") in levels(turn)


def test_a_run_without_an_end_line_is_flagged(src, tasks_db):
    write_trace(src, R1, TRACE[:-1])
    turn = build(src, tasks_db, R1)
    assert turn["ended"] is False
    assert any(level == "bad" and "没有结束行" in text for level, text in levels(turn))


def test_a_run_that_failed_ends_with_its_stage_and_error(src, tasks_db):
    # 2026-10-27: three runs stopped in the model request (ImportError); the trace has a
    # "failed" line and no "end", and the page called them crashed.
    failed = {"event": "failed", "final": "任务失败。原因：模型请求失败：程序内部出错，后续步骤已停止。", "stop_reason": "exception",
              "error_type": "ImportError", "error_stage": "模型请求", "tool_calls": 0, "steps": 1, "seconds": 0.06,
              "runtime": {"task_id": None, "task_revision": None, "checks": []}}
    write_trace(src, R1, [TRACE[0], failed])
    events = [ev("started", 0), ev("llm_start", 0, step=1), ev("run_failed", 1, **{k: v for k, v in failed.items() if k != "event"}),
              ev("failed", 1, message=failed["final"])]  # the web UI's own end marker, like "completed"
    turn = build(src, tasks_db, R1, events=events, seg=[{"role": "user", "content": "我的房子是什么保险公司"}])
    [step] = turn["steps"]  # the request that started and never came back, with the failure under it
    assert (step["step"], step["unfinished"], [x["kind"] for x in step["after"]]) == (1, True, ["failed"])
    assert turn["prep"]["items"] == []
    assert turn["ended"] is True and turn["stop_reason"] == "exception"
    assert turn["failure"] == {"stage": "模型请求", "error_type": "ImportError"}
    assert {"level": "bad", "code": "run_failed", "stage": "模型请求", "error": "ImportError"} in turn["anomalies"]
    assert not any("没有结束行" in text for _, text in levels(turn))
    assert turn["final"].startswith("任务失败") and turn["stats"]["seconds"] == 0.1


def test_history_fills_in_the_model_output_and_full_results(src, tasks_db):
    write_trace(src, R1, TRACE)
    turn = build(src, tasks_db, R1, seg=HISTORY)
    s1, s2 = turn["steps"]
    assert (s1["reasoning"], s1["decided"]) == ("先按文件名找", ["search_files"])
    assert (s2["reasoning"], s2["text"], s2["decided"]) == ("确认内容", "读一下", ["read_file", "finish_turn"])
    read = calls_of(turn)[1]
    assert read["result_source"] == "history" and read["result"] == LONG and not read["truncated"]
    assert read["call_id"] == "c2" and json.loads(read["raw_result"])["text"] == LONG
    # the protocol message before the first request, and the reply the runtime wrote
    assert [x["title"] for x in turn["prep"]["system"]] == ["【任务协议 v3】注入"]
    assert turn["runtime_final"] == "找到 1 个"
    assert turn["prep"]["request"] == {"attempt": 1, "input_chars": 900, "messages": 3, "ok": True, "ms": 40, "prompt_tokens": 300}


def test_history_that_does_not_line_up_is_not_used(src, tasks_db):
    write_trace(src, R1, TRACE)
    wrong = [m if m.get("tool_calls") is None or m["tool_calls"][0]["id"] != "c1"
             else {**m, "tool_calls": [tc("c1", "list_dir", {})]} for m in HISTORY]
    turn = build(src, tasks_db, R1, seg=wrong)
    read = calls_of(turn)[1]
    assert read["result_source"] == "trace" and read["truncated"] and len(read["result"]) == 300
    assert not any(s["has_history"] for s in turn["steps"])
    assert any(level == "warn" and "对话历史和运行记录对不上" in text for level, text in levels(turn))


def test_runs_outside_the_web_ui_say_what_is_missing(src, tasks_db):
    write_trace(src, R1, TRACE)
    turn = build(src, tasks_db, R1)
    assert [lv for lv, _ in levels(turn)] == ["info"]
    assert "前 300 字" in levels(turn)[0][1]


# a write: prepare, execute (confirmed, two files moved, task state updated)
PLAN = {"actions": [{"effect": "move", "source": "a.txt", "destination": "Archive"}]}
WRITE_TRACE = [
    {"event": "start", "instruction": "整理", "model": "m"},
    {"event": "llm", "step": 1, "ms": 10, "tokens": 10},
    tool(1, "prepare_file_plan", PLAN, "OK: plan_id=Xaaaaaaaaaaaa；已预检 2 项"),
    {"event": "llm", "step": 2, "ms": 10, "tokens": 10},
    tool(2, "execute_file_plan", {"plan_id": "Xaaaaaaaaaaaa"}, "OK: 本地完成并核验 2 项"),
    end(2, stop_reason="done"),
]
OPS = [{"op": "move", "src": "a.txt", "dst": "Archive/a.txt", "time": "2026-10-26T10:00:05+08:00"},
       {"op": "move", "src": "b.txt", "dst": "Archive/b.txt", "time": "2026-10-26T10:00:06+08:00"}]
WRITE_EVENTS = [
    ev("started", 0), ev("llm_start", 1, step=1), ev("llm_end", 1, step=1),
    ev("tool_start", 1, step=1, tool="prepare_file_plan"), ev("tool_end", 2, step=1, tool="prepare_file_plan"),
    ev("llm_start", 3, step=2), ev("llm_end", 3, step=2),
    ev("tool_start", 3, step=2, tool="execute_file_plan"),
    ev("confirmation_required", 3, question="移动 2 项？"), ev("confirmation_answered", 5, answer="y"),
    ev("task_updated", 6, task_id="T1", task_revision=1, task_status="running"),
    ev("task_updated", 6, task_id="T1", task_revision=1, task_status="running"),  # a repeat, collapsed
    ev("tool_end", 7, step=2, tool="execute_file_plan"),
    ev("brand_new_event", 8),
    ev("completed", 9),
]


def test_confirmation_files_and_state_sit_under_their_call_in_order(src, tasks_db):
    write_trace(src, W1, WRITE_TRACE)
    write_ops(src, W1, OPS)
    turn = build(src, tasks_db, W1, events=WRITE_EVENTS, seg=None)
    prepare, execute = calls_of(turn)
    assert prepare["inner"] == [] and (prepare["group"], execute["group"]) == ("task", "write")
    assert [x["kind"] for x in execute["inner"]] == ["confirm", "ops", "state"]
    confirm, ops, state = execute["inner"]
    assert (confirm["answer"], confirm["wait_s"]) == ("y", 2)
    assert [o["src"] for o in ops["ops"]] == ["a.txt", "b.txt"] and ops["inferred"] is None
    assert (state["status"], state["revision"]) == ("running", 1)
    assert all(o["placed"] for o in turn["file_ops"])
    # an event the page does not know is shown as is and reported
    after = turn["steps"][-1]["after"]
    assert [x["kind"] for x in after] == ["unknown"] and after[0]["type"] == "brand_new_event"
    assert any("1 条页面不认识的事件" in text for _, text in levels(turn))


def test_without_event_times_files_go_to_the_only_write_call(src, tasks_db):
    write_trace(src, W1, WRITE_TRACE)
    write_ops(src, W1, OPS)
    box = calls_of(build(src, tasks_db, W1))[1]["inner"][0]
    assert box["kind"] == "ops" and len(box["ops"]) == 2 and box["inferred"] == "这一轮只有这一次写入调用"


def test_without_event_times_ambiguous_files_are_listed_not_guessed(src, tasks_db):
    write_trace(src, W1, WRITE_TRACE[:-1] + [tool(2, "execute_file_plan", {"plan_id": "Xbbbbbbbbbbbb"}), end(3, stop_reason="done")])
    write_ops(src, W1, OPS + [OPS[0] | {"src": "c.txt"}])
    turn = build(src, tasks_db, W1)
    assert all(c["inner"] == [] for c in calls_of(turn))
    assert not any(o.get("placed") for o in turn["file_ops"])
    assert any("3 项文件改动没能对应到具体步骤" in text for _, text in levels(turn))


def test_one_retired_single_write_per_change_is_matched_in_order(src, tasks_db):
    write_trace(src, W1, [TRACE[0], {"event": "llm", "step": 1, "ms": 1, "tokens": 1},
                          tool(1, "trash", {"path": "a"}), tool(1, "trash", {"path": "b"}), end(2, stop_reason="done")])
    write_ops(src, W1, [{"op": "trash", "src": "a", "time": "x"}, {"op": "trash", "src": "b", "time": "x"}])
    assert [c["inner"][0]["ops"][0]["src"] for c in calls_of(build(src, tasks_db, W1))] == ["a", "b"]


def test_tool_groups_come_from_the_registry_and_the_runtime():
    assert [V.group(t) for t in ("read_file", "apply_dedup", "execute_file_plan", "move", "ask_user", "start_task", "finish_turn")] == \
        ["look", "write", "write", "write", "ask", "task", "task"]


def test_an_answer_without_a_task_says_nothing_was_verified(src, tasks_db):
    write_trace(src, R1, TRACE)
    turn = build(src, tasks_db, R1, seg=HISTORY, report={"state": "answered", "display": "done", "summary": "找到 1 个", "sections": []})
    states = turn["states"]
    assert states["registered"] is False
    assert states["verification"] == {"mechanical": "not_applicable", "semantic": "not_required"}
    assert turn["states_source"] == "projected"  # no saved report.states: the product reducer projects them
    assert turn["ending"] == {"tool": "finish_turn", "step": 2, "i": 2, "mode": "reply"}
    assert (turn["evidence_calls"], turn["findings"]) == (1, False)
    assert (turn["stop_reason"], turn["display"]) == ("answered", "done")  # codes; the page words them


def test_saved_states_are_used_as_saved(src, tasks_db):
    write_trace(src, R1, TRACE)
    saved = {"version": 1, "flow_state": "ended", "result_state": "answered", "registered": False, "parts": [],
             "verification": {"mechanical": "not_applicable", "semantic": "not_required"}, "next_action": "none"}
    turn = build(src, tasks_db, R1, report={"state": "answered", "sections": [], "states": saved})
    assert (turn["states"], turn["states_source"]) == (saved, "saved")


def test_handles_link_back_to_the_call_that_produced_them(src, tasks_db):
    write_trace(src, R1, TRACE)
    first = build(src, tasks_db, R1, seg=HISTORY)
    cite = {"path": "a.pdf", "evidence": "E0123456789"}
    write_trace(src, R2, [TRACE[0], {"event": "llm", "step": 1, "ms": 1, "tokens": 1}, tool(1, "read_file", cite), end(1)])
    second = build(src, tasks_db, R2, index=2,
                   report={"state": "answered", "sections": [{"title": "文件与证据", "markdown": "[片段](evidence:E0123456789) Eggggggggggg"}]})
    V.link_handles([first, second])
    read = calls_of(first)[1]
    assert read["produced"] == [{"id": "F0123456789", "kind": "F"}, {"id": "E0123456789", "kind": "E"}]
    origin = {"turn": 1, "i": 1, "step": 2, "tool": "read_file", "sentence": read["sentence"]}
    assert calls_of(second)[0]["uses"] == [{"id": "E0123456789", "kind": "E", "from": origin}]
    assert second["report"]["sections"][0]["uses"] == [{"id": "E0123456789", "kind": "E", "from": origin}]


def test_a_handle_mentioned_again_is_not_produced_twice(src, tasks_db):
    write_trace(src, R1, TRACE[:-1] + [tool(2, "read_file", READ, LONG), end(4)])
    turn = build(src, tasks_db, R1)
    V.link_handles([turn])
    assert [len(c["produced"]) for c in calls_of(turn)] == [0, 2, 0, 0]


# --- the store behind /api/logs: a conversation, a standalone run, caching, refusals ---

ANSWERED = {"state": "answered", "display": "done", "summary": "找到 1 个", "sections": []}


def add_conversation(src, cid="conv1", history=HISTORY, turns=((R1, "t1"),), events=(), report=ANSWERED):
    with sqlite3.connect(src.state_db) as db:
        db.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?)",
                   (cid, "测试对话", "2026-10-26T10:00:00", "2026-10-26T10:05:00", json.dumps(history, ensure_ascii=False)))
        for run_id, tid in turns:
            db.execute("INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (tid, cid, run_id, "completed", "2026-10-26T10:00:00", "2026-10-26T10:01:00", json.dumps(list(events)), "", "",
                        json.dumps({"report": report})))


def chat_messages(cid):
    return [{"role": "user", "content": "我有哪些 offer"},
            {"role": "assistant", "content": "找到 1 个", "html": "<p>找到 1 个</p>", "report": {"sections": []}}]


def test_the_store_lists_conversations_and_standalone_runs(src):
    write_trace(src, R1, TRACE)
    write_trace(src, W1, WRITE_TRACE)  # never went through the web UI
    (src.log_dir / "not-a-run.trace.jsonl").write_text("{}")
    add_conversation(src)
    logs = V.Logs(src, chat_messages)
    idx = logs.index()
    [conv] = idx["conversations"]
    assert (conv["id"], [t["run_id"] for t in conv["turns"]]) == ("conv1", [R1])
    assert conv["turns"][0]["display"] == "done" and conv["turns"][0]["tool_calls"] == 3
    assert [r["run_id"] for r in idx["runs"]] == [W1]
    full = logs.conversation("conv1")
    assert full["turns"][0]["steps"][0]["reasoning"] == "先按文件名找"  # history used
    assert full["turns"][0]["report_html"]["summary"] == "<p>找到 1 个</p>"
    assert logs.run(W1)["turn"]["shown_calls"] == 2
    assert logs.run(R1) is None  # a web turn is read through its conversation
    assert logs.run("../../config") is None and logs.conversation("missing") is None


def test_the_store_rebuilds_when_a_trace_changes(src):
    write_trace(src, R1, TRACE)
    add_conversation(src)
    logs = V.Logs(src, chat_messages)
    assert logs.conversation("conv1")["turns"][0]["shown_calls"] == 3
    assert logs.conversation("conv1") is logs.conversation("conv1")  # cached
    time.sleep(0.01)
    write_trace(src, R1, TRACE[:-1] + [tool(2, "read_file", READ, LONG), end(4)])
    assert logs.conversation("conv1")["turns"][0]["shown_calls"] == 4


def test_history_with_a_different_number_of_turns_is_not_used(src):
    write_trace(src, R1, TRACE)
    add_conversation(src, history=HISTORY + [{"role": "user", "content": "又一句"}])
    turn = V.Logs(src, chat_messages).conversation("conv1")["turns"][0]
    assert not turn["steps"][0]["has_history"]
    assert "没有用历史补全" in turn["completeness"][0]["text"]


@pytest.fixture
def client(src, monkeypatch):
    write_trace(src, R1, TRACE)
    write_trace(src, W1, WRITE_TRACE)
    write_ops(src, W1, OPS)
    add_conversation(src)
    monkeypatch.setattr(webapp, "_LOGS", V.Logs(src, chat_messages))
    return TestClient(webapp.app)


@pytest.fixture
def browser_client(src, monkeypatch):
    write_trace(src, R1, TRACE)
    write_trace(src, W1, WRITE_TRACE)
    write_ops(src, W1, OPS)
    write_trace(src, R2, TRACE)  # outside the web UI
    add_conversation(src)
    add_conversation(src, "conv2", history=[{"role": "user", "content": "整理"}], turns=((W1, "t2"),), events=WRITE_EVENTS)
    failed = {"event": "failed", "final": "任务失败。", "stop_reason": "exception", "error_type": "ImportError",
              "error_stage": "模型请求", "tool_calls": 0, "steps": 1, "seconds": 0.06, "runtime": {}}
    F1 = "20261026-100300-000004"
    write_trace(src, F1, [TRACE[0], failed])
    add_conversation(src, "conv3", history=[{"role": "user", "content": "我有哪些 offer"}], turns=((F1, "t3"),),
                     events=[ev("started", 0), ev("llm_start", 0, step=1), ev("run_failed", 1, error_type="ImportError"), ev("failed", 1)],
                     report={"state": "failed", "reason": "exception", "summary": "任务失败。", "sections": []})  # no display, as saved
    monkeypatch.setattr(webapp, "_LOGS", V.Logs(src, chat_messages))
    return TestClient(webapp.app)


def test_the_api_serves_summaries_and_one_turn_at_a_time(client):
    idx = client.get("/api/logs").json()
    assert [c["id"] for c in idx["conversations"]] == ["conv1"] and [r["run_id"] for r in idx["runs"]] == [W1]
    conv = client.get("/api/logs/conversations/conv1").json()
    assert set(conv) == {"id", "title", "updated_at", "system_prompt", "messages", "limits", "turns"}
    assert "steps" not in conv["turns"][0]  # summaries only; a turn is fetched on its own
    turn = client.get("/api/logs/conversations/conv1/turns/1").json()
    assert turn["shown_calls"] == 3 and turn["steps"][1]["items"][0]["result"] == LONG
    assert client.get(f"/api/logs/runs/{W1}").json()["turn"]["file_ops"][0]["placed"] is True
    for url in ("/api/logs/conversations/nope", "/api/logs/conversations/conv1/turns/2", "/api/logs/conversations/conv1/turns/0",
                f"/api/logs/runs/{R1}", "/api/logs/runs/..%2F..%2Fconfig"):
        assert client.get(url).status_code == 404, url


def test_the_page_and_shared_labels_are_served(client):
    page = client.get("/static/logs.html").text
    assert page.index("/static/labels.js") < page.index("/static/logs.js")
    chat = client.get("/").text
    assert chat.index("/static/labels.js") < chat.index("/static/app.js") and 'id="open-logs"' in chat
    labels = client.get("/static/labels.js").text
    for name in ("TOOL_LABELS", "outcomeLabel", "TASK_STATES", "CONDITION_NAMES", "EFFECT_NAMES"):
        assert name in labels
        for other in ("app.js", "tasks.js", "logs.js"):  # one copy only
            assert f"const {name} =" not in client.get(f"/static/{other}").text and f"function {name}(" not in client.get(f"/static/{other}").text


def chrome():
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.exists() else None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(chrome() is None, reason="needs a local Chrome for headless rendering")
def test_the_page_draws_every_call_and_says_why_nothing_was_verified(browser_client):
    from eval.browser_acceptance import Page, wait_http
    # the app without its startup hook, so the real state database and sandbox are never touched
    server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=free_port(), lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    profile, port = tempfile.mkdtemp(), free_port()
    browser = subprocess.Popen([chrome(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                                "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        app = f"http://127.0.0.1:{server.config.port}"
        wait_http(f"{app}/api/logs")
        wait_http(f"http://127.0.0.1:{port}/json/version")
        page = Page(next(t for t in wait_http(f"http://127.0.0.1:{port}/json/list") if t["type"] == "page")["webSocketDebuggerUrl"])
        page.call("Page.navigate", url=f"{app}/static/logs.html")
        page.wait("!!document.querySelector('#log .card')", 30, "first turn")
        answer = page.js("""(async () => { await selectGroup("conv1", 1); const log = document.getElementById("log");
          return {calls: log.querySelectorAll(".item.call").length, linked: log.querySelectorAll(".handle.linked").length, text: log.innerText,
                  batches: [...log.querySelectorAll(".batch")].map(b => b.querySelectorAll(".item.call").length),
                  decided: [...log.querySelectorAll(".decided-list")].map(l => l.innerText),
                  reasoning_open: [...log.querySelectorAll(".model-out details.fold")].filter(d => d.open).map(d => d.innerText.split("\\n")[0])}; })()""")
        assert answer["calls"] == 3
        # step 2 sent read_file and finish_turn in one reply: boxed together, each with its key arguments
        assert answer["batches"] == [2] and "一起发出的 2 个调用" in answer["text"]
        assert answer["decided"][1].splitlines() == ['read_file path="a.pdf"', 'finish_turn mode="reply"']
        assert answer["reasoning_open"] == ["模型的思考内容 · 6 字", "模型的思考内容 · 4 字"]
        assert "没有登记任务" in answer["text"] and "没有调用 report_findings" in answer["text"]
        assert "已完成" in answer["text"]  # display=done, worded by labels.js outcomeLabel
        assert "结束本轮" in answer["text"] or "直接回答你" in answer["text"]
        write = page.js("""(async () => { await selectGroup("conv2", 1); const log = document.getElementById("log");
          return {calls: log.querySelectorAll(".item.call").length, text: log.innerText}; })()""")
        assert write["calls"] == 2
        assert "请你确认操作" in write["text"] and "你选了「允许」" in write["text"] and "改动了 2 个文件" in write["text"]
        assert "任务状态变为「待继续」" in write["text"]  # TASK_STATES.running, the task editor's own word
        outside = page.js("""(async () => { await selectGroup("runs", 1); const log = document.getElementById("log");
          return {calls: log.querySelectorAll(".item.call").length, text: log.innerText}; })()""")
        assert outside["calls"] == 3 and "不在网页对话里" in outside["text"]
        stopped = page.js("""(async () => { await selectGroup("conv3", 1); return document.getElementById("log").innerText; })()""")
        assert "运行失败" in stopped and "请求没有完成，运行在这里停止" in stopped and "ImportError" in stopped
        assert not [e for e in page.events if e.get("method") == "Runtime.exceptionThrown"]
    finally:
        browser.terminate()
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
