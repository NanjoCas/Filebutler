"""The offline log viewer (docs/mock/build_logs_mock.py): the timeline must show every call.

Built from small synthetic runs, never real logs (logs/ is private and gitignored).
Pins what the viewer promises the reader learning the chain: each call the run made
is drawn; the conversation history fills in the model's output and the full results
only when it lines up with the trace; confirmations, file operations and task states
sit under the call they happened in; anything that cannot be placed is reported, not
dropped; handles link back to the call that produced them; a turn answered without
a task says why nothing was verified. The last test renders the page in headless
Chrome when one is installed.
"""
import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("build_logs_mock", Path(__file__).resolve().parents[1] / "docs/mock/build_logs_mock.py")
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)

LIMITS = {"llm_ms": 10_000, "tool_ms": 1_000, "llm_n": 1, "tool_n": 1}
LONG = "OK: file:F0123456789 evidence:E0123456789；仅支持片段范围内结论。\n<file_content>\n" + "正文" * 400


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "sandbox/.oplog").mkdir(parents=True)
    monkeypatch.setattr(V, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def tasks_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE task_revisions (id TEXT, revision INTEGER, data_json TEXT)")
    return db


def write_trace(root, run_id, records):
    (root / f"logs/{run_id}.trace.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


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


# a two-step read-only answer: search, then read and answer with finish_turn(mode=reply)
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


def test_every_call_is_drawn_from_the_trace_even_without_ui_events(root, tasks_db):
    # The bug that started this: a run whose UI events held only started/completed showed no calls at all.
    write_trace(root, "r1", TRACE)
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, events=[ev("started", 0), ev("completed", 9)], seg=HISTORY)
    assert [c["tool"] for c in calls_of(turn)] == ["search_files", "read_file", "finish_turn"]
    assert turn["shown_calls"] == turn["stats"]["tool_calls"] == 3
    assert any("网页端没有保存这一轮的过程事件" in text for _, text in levels(turn))
    assert V.undercounted([{"id": "c", "turns": [turn]}]) == []


def test_a_run_counting_more_calls_than_it_recorded_is_flagged(root, tasks_db):
    write_trace(root, "r1", TRACE[:-1] + [end(4)])
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, seg=None)
    assert ("bad", "运行结束时统计了 4 次工具调用，记录里只有 3 次") in levels(turn)
    assert V.undercounted([{"id": "conversation", "turns": [turn]}]) == [("conversa", 1, "r1", 3, 4)]


def test_a_run_without_an_end_line_is_flagged(root, tasks_db):
    write_trace(root, "r1", TRACE[:-1])
    turn = V.build_turn("r1", 1, LIMITS, tasks_db)
    assert turn["outcome"]["tone"] == "bad"
    assert any(level == "bad" and "没有结束行" in text for level, text in levels(turn))


def test_history_fills_in_the_model_output_and_full_results(root, tasks_db):
    write_trace(root, "r1", TRACE)
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, seg=HISTORY)
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


def test_history_that_does_not_line_up_is_not_used(root, tasks_db):
    write_trace(root, "r1", TRACE)
    wrong = [m if m.get("tool_calls") is None or m["tool_calls"][0]["id"] != "c1"
             else {**m, "tool_calls": [tc("c1", "list_dir", {})]} for m in HISTORY]
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, seg=wrong)
    read = calls_of(turn)[1]
    assert read["result_source"] == "trace" and read["truncated"] and len(read["result"]) == 300
    assert not any(s["has_history"] for s in turn["steps"])
    assert any(level == "warn" and "对话历史和运行记录对不上" in text for level, text in levels(turn))


def test_runs_outside_the_web_ui_say_what_is_missing(root, tasks_db):
    write_trace(root, "r1", TRACE)
    turn = V.build_turn("r1", 1, LIMITS, tasks_db)
    assert [lv for lv, _ in levels(turn)] == ["info"]
    assert "前 300 字" in levels(turn)[0][1]


# a write: prepare, execute (confirmed, two files moved, task state updated), then finish
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


def write_ops(root, run_id, ops):
    (root / f"sandbox/.oplog/{run_id}.jsonl").write_text("\n".join(json.dumps(o) for o in ops), encoding="utf-8")


def test_confirmation_files_and_state_sit_under_their_call_in_order(root, tasks_db):
    write_trace(root, "w1", WRITE_TRACE)
    write_ops(root, "w1", OPS)
    turn = V.build_turn("w1", 1, LIMITS, tasks_db, events=WRITE_EVENTS, seg=None)
    prepare, execute = calls_of(turn)
    assert prepare["inner"] == []
    assert [x["kind"] for x in execute["inner"]] == ["confirm", "ops", "state"]
    confirm, ops, state = execute["inner"]
    assert (confirm["answer_label"], confirm["wait_s"]) == ("允许", 2)
    assert [o["src"] for o in ops["ops"]] == ["a.txt", "b.txt"] and ops["inferred"] is None
    assert (state["status"], state["revision"]) == ("running", 1)
    assert all(o["placed"] for o in turn["file_ops"])
    # an event the page does not know is shown as is and reported
    after = turn["steps"][-1]["after"]
    assert [x["kind"] for x in after] == ["unknown"] and after[0]["type"] == "brand_new_event"
    assert any("1 条页面不认识的事件" in text for _, text in levels(turn))


def test_without_event_times_files_go_to_the_only_write_call(root, tasks_db):
    write_trace(root, "w1", WRITE_TRACE)
    write_ops(root, "w1", OPS)
    turn = V.build_turn("w1", 1, LIMITS, tasks_db)
    box = calls_of(turn)[1]["inner"][0]
    assert box["kind"] == "ops" and len(box["ops"]) == 2 and box["inferred"] == "这一轮只有这一次写入调用"


def test_without_event_times_ambiguous_files_are_listed_not_guessed(root, tasks_db):
    trace = WRITE_TRACE[:-1] + [tool(2, "execute_file_plan", {"plan_id": "Xbbbbbbbbbbbb"}, "OK"), end(3, stop_reason="done")]
    write_trace(root, "w1", trace)
    write_ops(root, "w1", OPS + [OPS[0] | {"src": "c.txt"}])
    turn = V.build_turn("w1", 1, LIMITS, tasks_db)
    assert all(c["inner"] == [] for c in calls_of(turn))
    assert not any(o.get("placed") for o in turn["file_ops"])
    assert any("3 项文件改动没能对应到具体步骤" in text for _, text in levels(turn))


def test_one_atomic_call_per_change_is_matched_in_order(root, tasks_db):
    trace = [TRACE[0], {"event": "llm", "step": 1, "ms": 1, "tokens": 1},
             tool(1, "trash", {"path": "a"}), tool(1, "trash", {"path": "b"}), end(2, stop_reason="done")]
    write_trace(root, "t1", trace)
    write_ops(root, "t1", [{"op": "trash", "src": "a", "time": "x"}, {"op": "trash", "src": "b", "time": "x"}])
    turn = V.build_turn("t1", 1, LIMITS, tasks_db)
    assert [c["inner"][0]["ops"][0]["src"] for c in calls_of(turn)] == ["a", "b"]


def test_an_answer_without_a_task_says_nothing_was_verified(root, tasks_db):
    write_trace(root, "r1", TRACE)
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, seg=HISTORY, report={"state": "answered", "summary": "找到 1 个", "sections": []})
    states = turn["states"]
    assert states["registered"] is False
    assert states["verification"] == {"mechanical": "not_applicable", "semantic": "not_required"}
    assert turn["states_source"] == "projected"  # no saved report.states: the product reducer projects them
    assert turn["ending"] == {"tool": "finish_turn", "step": 2, "i": 2, "mode": "reply"}
    assert (turn["evidence_calls"], turn["findings"]) == (1, False)


def test_saved_states_are_used_as_saved(root, tasks_db):
    write_trace(root, "r1", TRACE)
    saved = {"version": 1, "flow_state": "ended", "result_state": "answered", "registered": False, "parts": [],
             "verification": {"mechanical": "not_applicable", "semantic": "not_required"}, "next_action": "none"}
    turn = V.build_turn("r1", 1, LIMITS, tasks_db, report={"state": "answered", "sections": [], "states": saved})
    assert (turn["states"], turn["states_source"]) == (saved, "saved")


def test_handles_link_back_to_the_call_that_produced_them(root, tasks_db):
    write_trace(root, "r1", TRACE)
    first = V.build_turn("r1", 1, LIMITS, tasks_db, seg=HISTORY)
    cite = {"path": "a.pdf", "evidence": "E0123456789"}
    write_trace(root, "r2", [TRACE[0], {"event": "llm", "step": 1, "ms": 1, "tokens": 1},
                             tool(1, "read_file", cite, "OK"), end(1)])
    second = V.build_turn("r2", 2, LIMITS, tasks_db,
                          report={"state": "answered", "sections": [{"title": "文件与证据", "markdown": "[片段](evidence:E0123456789) Eggggggggggg"}]})
    V.link_handles([first, second])
    read = calls_of(first)[1]
    assert read["produced"] == [{"id": "F0123456789", "kind": "文件"}, {"id": "E0123456789", "kind": "证据片段"}]
    origin = {"turn": 1, "i": 1, "step": 2, "tool": "read_file", "sentence": read["sentence"]}
    assert calls_of(second)[0]["uses"] == [{"id": "E0123456789", "kind": "证据片段", "from": origin}]
    assert second["report"]["sections"][0]["uses"] == [{"id": "E0123456789", "kind": "证据片段", "from": origin}]


def test_a_handle_mentioned_again_is_not_produced_twice(root, tasks_db):
    write_trace(root, "r1", TRACE[:-1] + [tool(2, "read_file", READ, LONG), end(4)])
    turn = V.build_turn("r1", 1, LIMITS, tasks_db)
    V.link_handles([turn])
    assert [len(c["produced"]) for c in calls_of(turn)] == [0, 2, 0, 0]


def chrome():
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.exists() else None


@pytest.mark.skipif(chrome() is None, reason="needs a local Chrome for headless rendering")
def test_every_turn_renders_in_a_browser_and_draws_every_call(root, tasks_db, tmp_path):
    write_trace(root, "r1", TRACE)
    write_trace(root, "w1", WRITE_TRACE)
    write_ops(root, "w1", OPS)
    turns = [V.build_turn("r1", 1, LIMITS, tasks_db, seg=HISTORY, report={"state": "answered", "summary": "找到 1 个", "sections": [
                 {"title": "文件与证据", "markdown": "[片段](evidence:E0123456789)", "audience": "dev"}]}),
             V.build_turn("w1", 2, LIMITS, tasks_db, events=WRITE_EVENTS, seg=None)]
    V.link_handles(turns)
    conv = {"id": "conv-test", "title": "测试", "updated_at": "2026-10-26 10:00", "system_prompt": "系统提示",
            "messages": [{"role": "user", "content": "我有哪些 offer", "html": None, "turn": 1},
                         {"role": "assistant", "content": "找到 1 个", "html": None, "turn": 1},
                         {"role": "user", "content": "整理", "html": None, "turn": 2}], "turns": turns}
    html = V.render([conv], LIMITS)
    probe = """<script>(function(){const out=[],errs=[];
      for(const c of DATA.conversations)for(const t of c.turns){try{select(c,t.index);const log=document.getElementById("log");
        out.push({calls:log.querySelectorAll(".item.call").length,want:t.stats.tool_calls,states:log.querySelectorAll(".states").length,
          report:log.querySelectorAll(".item.report").length,linked:log.querySelectorAll(".handle.linked").length,
          text:log.textContent});}catch(e){errs.push(String(e&&e.stack||e));}}
      const p=document.createElement("pre");p.id="probe";p.textContent=JSON.stringify({out,errs});document.body.append(p);})();</script>"""
    i = html.rindex("</body>")
    page = tmp_path / "page.html"
    page.write_text(html[:i] + probe + html[i:], encoding="utf-8")
    dom = subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", page.as_uri()],
                         capture_output=True, text=True, timeout=60).stdout
    import html as H
    start = dom.index('<pre id="probe">') + len('<pre id="probe">')
    result = json.loads(H.unescape(dom[start:dom.index("</pre>", start)]))
    assert result["errs"] == []
    answer, write = result["out"]
    assert (answer["calls"], write["calls"]) == (3, 2)
    assert answer["states"] == write["states"] == 1
    assert answer["report"] == 1 and answer["linked"] >= 1  # the report cites E… from the read
    assert "没有登记任务" in answer["text"] and "没有调用 report_findings" in answer["text"]
    assert "请你确认操作" in write["text"] and "改动了 2 个文件" in write["text"] and "任务状态变为" in write["text"]
