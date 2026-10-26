"""Run logs for the log page (web/logs.html): every turn, step by step.

The trace (logs/<run>.trace.jsonl) is the skeleton of a turn: each model request
and each tool call. The conversation's history_json fills in what the trace leaves
out (the model's reasoning and text, the full tool results it received, injected
system messages), but only when it lines up with the trace call for call. The web
UI's events_json places confirmations, task-state changes and file operations under
the call they happened in. Anything that cannot be matched is reported on the page,
never dropped.

This module returns facts and codes (tool names, stop reasons, task states,
condition kinds); the page turns them into words with the chat page's own labels
(web/labels.js). States come from the product's reducer (agent/states.py via
eval.judge.turn_states). Read-only: nothing here writes.
"""
import json
import re
import sqlite3
import statistics
import threading
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.history import UNDO_MARK
from agent.task_runtime import TaskRuntime
from eval.judge import turn_states
from tools import registry

RUN_ID = re.compile(r"^\d{8}-\d{6}(?:-\d{6})?$")
TASK_TOOLS = {"start_task", "set_task", "revise_task"}
ENDERS = {"finish_turn", "finish_task", "ask_user", "inspect_run"}
# Single-step writes retired on 2026-10-23 (tools/registry.py); old runs still call them.
RETIRED_WRITES = {"make_dir", "move", "rename", "trash", "trash_many", "apply_classification"}
# local handles the tools hand out and later calls cite: file:F… evidence:E… catalog C… plan P…/X… task T…
HANDLE = re.compile(r"\b(?:[FECP][0-9a-f]{10}|[XT][0-9a-f]{12})\b")
# events that only mark where things happen; the trace already holds their content
STRUCTURAL = {"started", "completed", "failed", "llm_start", "llm_end", "tool_start", "tool_end", "llm_retry", "llm_request"}
BAD_STOPS = {"cost_budget", "token_budget", "max_steps", "repeated_call", "repair_limit", "exception",
             "unverified_answer", "unstructured_terminal"}


@dataclass
class Sources:
    """Where one FileButler home keeps its records."""
    log_dir: Path      # <home>/logs
    state_db: Path     # <home>/.filebutler-ui/state.sqlite3
    oplog_dir: Path    # <sandbox>/.oplog
    task_db: Path      # tasks.sqlite3

    def tasks(self):
        if not self.task_db.exists():
            return closing(sqlite3.connect(":memory:"))
        return closing(sqlite3.connect(f"file:{self.task_db}?mode=ro", uri=True))


def group(tool):
    """Icon group: a write changes files, ask waits for the user, task is a control call."""
    spec = registry.TOOLS.get(tool)
    if (spec and spec.writes) or tool == "execute_file_plan" or tool in RETIRED_WRITES:
        return "write"
    if tool == "ask_user":
        return "ask"
    return "task" if tool in TaskRuntime.control_names or tool in TASK_TOOLS else "look"


def as_json(text):
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def q(text, n=36):
    text = str(text)
    return f"「{text[:n]}…」" if len(text) > n else f"「{text}」"


def where(path):
    return "全部文件" if path in (None, "", ".") else q(path)


def sentence(tool, a):
    """One plain-language line for a call, from its arguments; empty when there is none to say."""
    if not isinstance(a, dict):
        return ""
    p = a.get("path")
    match tool:
        case "list_dir": return f"查看 {where(p)} 里有什么"
        case "search_files":
            what = a.get("name_pattern") or (a.get("ext") and f"*.{a['ext']}") or "文件"
            return f"在 {where(p)} 里查找 {q(what)}"
        case "search_content": return f"在 {where(p)} 里检索 {q(a.get('query', ''))}，最多取 {a.get('top_k', '若干')} 条"
        case "read_file": return f"阅读 {q(p, 48)}" + (f" 第 {a['page']} 页" if a.get("page") else "")
        case "file_info": return f"核对 {q(p, 48)} 的大小、页数和指纹"
        case "inventory_files": return f"统计 {where(p)} 的文件数量和类型"
        case "paper_profiles": return f"批量读取 {where(p)} 里论文的标题和摘要" + (f"（第 {a['page']} 页）" if a.get("page") else "")
        case "find_duplicates": return f"扫描 {where(p)} 里{'内容近似' if a.get('mode') == 'near' else '完全相同'}的文件"
        case "apply_dedup": return f"按你确认的保留项清理 {len(a.get('keepers') or {})} 组重复文件"
        case "make_dir": return f"新建文件夹 {q(p)}"
        case "move": return f"把 {q(a.get('src'), 40)} 移到 {q(a.get('dst_dir'))}"
        case "trash": return f"把 {q(p, 48)} 移入回收区"
        case "prepare_file_plan":
            return f"拟定文件操作清单：{len(a.get('actions') or [])} 个操作" + (f"（{a['description'][:30]}）" if a.get("description") else "")
        case "execute_file_plan": return "执行已确认的文件操作清单，并逐项核验"
        case "ask_user": return f"问你：{q(a.get('question', ''), 60)}"
        case "start_task" | "set_task": return f"把你的要求登记为任务：{q(a.get('summary', ''), 50)}"
        case "finish_task": return "逐项核验任务是否真的完成，然后答复你"
        case "finish_turn":
            return {"reply": "直接回答你，结束本轮（不登记任务、不核验）", "ask": f"问你：{q(a.get('question', ''), 60)}，等你选择",
                    "task": "收尾：本地逐部分核验登记的任务，然后答复你"}.get(a.get("mode"), "")
        case "submit_classification": return f"提交分类方案：{len(a.get('categories') or [])} 个类别、{len(a.get('assignments') or [])} 个文件"
        case "report_findings": return "整理证据，生成答复"
    return ""


def result_line(result):
    lines = [l.strip() for l in str(result).splitlines() if l.strip()]
    if not lines:
        return ""
    first = re.sub(r"^(OK|ERROR|错误)[:：]\s*", "", lines[0])
    return (first or (lines[1] if len(lines) > 1 else ""))[:140]


def parse_at(s):
    return datetime.fromisoformat(s)


def load_trace(src, run_id):
    path = src.log_dir / f"{run_id}.trace.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line cut off by a crash
    return out


def thresholds(src):
    """95th percentile of model and tool times over every saved run: what "slow" means here."""
    llm, tool = [], []
    for f in src.log_dir.glob("*.trace.jsonl"):
        for e in load_trace(src, f.name[:-len(".trace.jsonl")]):
            if e.get("event") == "llm" and e.get("ms") is not None:
                llm.append(e["ms"])
            elif e.get("tool") and e.get("ms") is not None:
                tool.append(e["ms"])
    p95 = lambda v: round(statistics.quantiles(v, n=100)[94]) if len(v) >= 2 else None
    return {"llm_ms": p95(llm), "tool_ms": p95(tool), "llm_n": len(llm), "tool_n": len(tool)}


def oplog(src, run_id):
    for path, undone in ((src.oplog_dir / f"{run_id}.jsonl", False), (src.oplog_dir / f"{run_id}.undone.jsonl", True)):
        if path.exists():
            ops = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            return [{"op": o["op"], "src": o.get("src"), "dst": o.get("dst"), "time": o.get("time", "")[11:19], "undone": undone}
                    for o in ops]
    return []


def task_revision(tasks_db, task_id, revision):
    row = tasks_db.execute("SELECT data_json FROM task_revisions WHERE id=? AND revision=?", (task_id, revision)).fetchone()
    return json.loads(row[0]) if row else None


def model_form(a):
    """What the model wrote, as it wrote it."""
    if not isinstance(a, dict):
        return {"raw": str(a)}
    keep = ("summary", "reason", "expected_revision", "parts", "goals", "limits", "write_roots", "allowed_effects", "constraints")
    return {k: a[k] for k in keep if a.get(k)}


def registered_form(tasks_db, task_id, revision):
    """What the runtime stored after validating the form."""
    d = task_revision(tasks_db, task_id, revision)
    if d is None:
        return None
    return {"goals": [{"id": g["id"], "description": g.get("description", ""), "conditions": g.get("conditions") or [],
                       "semantic_review": g.get("semantic_review", False), "scope": g.get("scope") or []} for g in d.get("goals", [])],
            "read_roots": d.get("read_roots") or [], "write_roots": d.get("write_roots") or [],
            "allowed_effects": d.get("allowed_effects") or [],
            "constraints": [{"description": c.get("description", ""), "rules": c.get("rules") or [],
                             "forbidden_effects": c.get("forbidden_effects") or []} for c in d.get("constraints", [])],
            "evidence": d.get("policy", {}).get("evidence")}


def task_forms(trace, tasks_db):
    forms = []
    for e in trace:
        if e.get("tool") not in TASK_TOOLS:
            continue
        m = re.search(r"task_id=(T\w+) revision=(\d+)", str(e.get("result", "")))
        forms.append({"step": e.get("step"), "tool": e["tool"], "ok": e.get("ok"), "receipt": result_line(e.get("result", "")),
                      "task_id": m and m.group(1), "revision": m and int(m.group(2)),
                      "model": model_form(as_json(e.get("args", "{}"))),
                      "registered": registered_form(tasks_db, m.group(1), int(m.group(2))) if m else None})
    return forms


def status_form(trace, end, tasks_db):
    """The model's finish declaration next to the runtime's per-goal verification."""
    call = next((e for e in reversed(trace) if e.get("tool") == "finish_task"
                 or (e.get("tool") == "finish_turn" and '"task"' in str(e.get("args", "")))), None)
    runtime = end.get("runtime") or {}
    if not call and not runtime.get("checks"):
        return None
    args = as_json(call.get("args", "{}")) if call else {}
    args = args if isinstance(args, dict) else {}
    task_id, revision = runtime.get("task_id"), runtime.get("task_revision")
    goals = {g["id"]: g.get("description", "") for g in (task_revision(tasks_db, task_id, revision) or {}).get("goals", [])} \
        if task_id and revision else {}
    unfinished = {u["part_id"]: u.get("reason", "") for u in args.get("unfinished") or []}
    not_needed = {u["part_id"]: u.get("reason", "") for u in args.get("not_needed") or []}
    rows = []
    for c in runtime.get("checks", []):
        gid = c.get("goal_id")
        claim = "unfinished" if gid in unfinished else "not_needed" if gid in not_needed else "done"
        verdict = "review" if c.get("needs_review") else "pass" if c.get("passed") else "fail"
        rows.append({"id": gid, "description": c.get("description") or goals.get(gid, gid),
                     "claim": claim, "claim_reason": unfinished.get(gid) or not_needed.get(gid) or "", "verdict": verdict,
                     "detail": "；".join([x.get("detail", "") for x in c.get("conditions", []) if x.get("detail")]
                                        + [n.get("text", "") for n in c.get("notes", []) if n.get("text")]),
                     "agree": verdict == "review" or (claim == "done") == (verdict == "pass")})
    return {"step": call and call.get("step"), "tool": call and call.get("tool"), "has_call": bool(call),
            "receipt": result_line(call.get("result", "")) if call else "", "ok": call and call.get("ok"),
            "reply": args.get("reply", ""), "summary": args.get("summary", ""),
            "task_id": task_id, "revision": revision, "state": runtime.get("task_status"), "rows": rows}


def tool_message(content):
    """A tool message as the model received it: the typed JSON envelope, or plain text in older runs."""
    d = as_json(content)
    if isinstance(d, dict) and "text" in d:
        return d.get("text") or "", {k: v for k, v in d.items() if k != "text" and v not in (None, [], {}, "")}
    return str(content or ""), {}


def system_item(m):
    text = m.get("content") or ""
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return {"kind": "injected", "source": "undo" if text.startswith(UNDO_MARK) else "system",
            "title": first[:80], "chars": len(text), "content": text}


def segments(history):
    """history_json split into turns: each starts at a user message."""
    segs = []
    for m in history:
        if m.get("role") == "user":
            segs.append([m])
        elif segs:
            segs[-1].append(m)
    return segs


def requests_by_step(trace):
    reqs = defaultdict(list)
    for e in trace:
        if e.get("event") != "llm_request":
            continue
        attempts = reqs[e["step"]]
        if e.get("phase") == "start":
            attempts.append({"attempt": e.get("attempt"), "input_chars": e.get("input_chars"), "messages": e.get("messages")})
        else:
            a = next((a for a in reversed(attempts) if a["attempt"] == e.get("attempt")), None)
            if a is None:
                a = {"attempt": e.get("attempt")}
                attempts.append(a)
            a.update({k: e.get(k) for k in ("ok", "ms", "prompt_tokens", "completion_tokens", "cache_hit_tokens",
                                             "retry", "error_type") if k in e})
    return reqs


class Placer:
    """Turns events_json into timeline items, collapsing repeated task states and pairing confirmations."""
    def __init__(self):
        self.last_state, self.confirm, self.unknown = None, None, 0

    def item(self, ev):
        t, at = ev["type"], ev.get("at", "")[11:19]
        if t == "confirmation_required":
            self.confirm = {"kind": "confirm", "question": ev.get("question", ""), "at": at, "_t": ev.get("at")}
            return self.confirm
        if t == "confirmation_answered":
            if self.confirm:
                self.confirm.update(answer=ev.get("answer"),
                                    wait_s=round((parse_at(ev["at"]) - parse_at(self.confirm.pop("_t"))).total_seconds()))
                self.confirm = None
            return None
        if t == "task_updated":
            if not ev.get("task_id"):
                return None
            key = (ev["task_id"], ev.get("task_revision"), ev.get("task_status"))
            if key == self.last_state:
                return None
            self.last_state = key
            return {"kind": "state", "task_id": ev["task_id"], "revision": ev.get("task_revision"),
                    "status": ev.get("task_status"), "blocked": ev.get("blocked_calls", 0), "at": at}
        if t == "context_prepared":
            return {"kind": "context", "before": ev.get("before_chars"), "after": ev.get("after_chars"),
                    "archived": ev.get("archived_messages", 0), "at": at,
                    "detail": {k: v for k, v in ev.items() if k not in ("type", "at")}}
        if t == "local_step":
            return {"kind": "local", "message": ev.get("message", ""), "at": at}
        if t == "protocol_retry":
            return {"kind": "protocol", "reason": ev.get("reason"), "at": at}
        if t == "run_failed":
            return {"kind": "failed", "detail": {k: v for k, v in ev.items() if k not in ("type", "at")}, "at": at}
        if t in STRUCTURAL:
            return None
        self.unknown += 1
        return {"kind": "unknown", "type": t, "detail": ev, "at": at}


def call_item(rec, index, limits):
    args = as_json(rec.get("args", "{}"))
    requested = as_json(rec["requested_args"]) if rec.get("requested_args") is not None else None
    result = rec.get("result", "")
    return {"kind": "call", "i": index, "step": rec.get("step"), "tool": rec["tool"], "group": group(rec["tool"]),
            "sentence": sentence(rec["tool"], args), "ok": rec.get("ok"),
            "ms": rec.get("ms"), "active_ms": rec.get("active_ms"), "wait_ms": rec.get("wait_ms"),
            "repeat": rec.get("repeat_count", 0), "executed": rec.get("executed"),
            "args": args, "requested": requested if requested is not None and requested != args else None,
            "error_code": rec.get("error_code"), "error_class": rec.get("error_class"),
            "references": rec.get("references") or [], "operation_refs": rec.get("operation_refs"),
            "progress": rec.get("progress"), "result_id": rec.get("result_id"),
            "result": result, "envelope": {}, "raw_result": None, "result_source": "trace",
            "truncated": len(str(result)) >= 300, "outcome": result_line(result),
            "slow": bool(limits["tool_ms"]) and (rec.get("ms") or 0) > limits["tool_ms"], "inner": []}


def build_turn(src, tasks_db, run_id, index, limits, events=None, seg=None, created_at="", turn_id=None, report=None):
    trace = load_trace(src, run_id)
    events = events or []
    start = next((e for e in trace if e.get("event") == "start"), {})
    failed = next((e for e in trace if e.get("event") == "failed"), None)
    # a run that stopped on an error writes "failed" instead of "end", with the same totals
    end = next((e for e in trace if e.get("event") == "end"), None) or failed or {}
    completeness = []
    if not trace:
        completeness.append({"level": "bad", "text": f"找不到运行记录 logs/{run_id}.trace.jsonl"})

    # 1. skeleton from the trace: model requests (steps) and tool calls
    llm = {e["step"]: e for e in trace if e.get("event") == "llm"}
    reqs = requests_by_step(trace)
    calls = [call_item(r, i, limits) for i, r in enumerate(e for e in trace if "tool" in e)]
    step_ids = sorted(set(llm) | set(reqs) | {c["step"] for c in calls if c["step"] is not None})
    steps = {s: {"step": s, "ms": (llm.get(s) or {}).get("ms"), "tokens": (llm.get(s) or {}).get("tokens"),
                 "slow": bool(limits["llm_ms"]) and ((llm.get(s) or {}).get("ms") or 0) > limits["llm_ms"],
                 "requests": reqs.get(s, []), "has_history": False, "reasoning": "", "text": "", "decided": [],
                 "items": [], "after": [], "unfinished": False} for s in step_ids}
    for c in calls:
        steps[c["step"]]["items"].append(c)
    prep = {"items": [], "system": [], "request": (reqs.get(step_ids[0]) or [{}])[0] if step_ids else {}}
    runtime_final, after_turn = None, []

    # 2. history_json: what the model wrote and what it received
    if seg is not None:
        asst = [m for m in seg if m.get("role") == "assistant"]
        hcalls = [tc for m in asst for tc in m.get("tool_calls") or []]
        if [tc["function"]["name"] for tc in hcalls] != [c["tool"] for c in calls] or len(asst) not in (len(llm), len(llm) + 1):
            completeness.append({"level": "warn", "text": f"对话历史和运行记录对不上（历史里 {len(hcalls)} 次调用、{len(asst)} 条模型回复；"
                                                          f"记录里 {len(calls)} 次调用、{len(llm)} 次模型请求），没有用历史补全"})
        else:
            results = {m.get("tool_call_id"): m.get("content") for m in seg if m.get("role") == "tool"}
            for c, tc in zip(calls, hcalls):
                c["call_id"] = tc.get("id")
                if tc.get("id") in results:
                    text, envelope = tool_message(results[tc["id"]])
                    c.update(result=text, envelope=envelope, raw_result=results[tc["id"]], result_source="history",
                             truncated=False, outcome=result_line(text))
                model_args = as_json(tc["function"].get("arguments"))
                if c["requested"] is None and isinstance(model_args, dict) and model_args != c["args"]:
                    c["requested"] = model_args
            llm_steps, k = sorted(llm), 0
            for m in seg[1:]:
                role = m.get("role")
                if role == "assistant":
                    k += 1
                    if k <= len(llm_steps):
                        steps[llm_steps[k - 1]].update(
                            has_history=True, reasoning=m.get("reasoning_content") or "", text=m.get("content") or "",
                            decided=[tc["function"]["name"] for tc in m.get("tool_calls") or []])
                    else:
                        runtime_final = m.get("content") or ""
                elif role == "system":
                    item = system_item(m)
                    if runtime_final is not None or (k >= len(llm_steps) and item["source"] == "undo"):
                        after_turn.append(item)
                    elif k == 0:
                        prep["system"].append(item)
                    else:
                        steps[llm_steps[k - 1]]["after"].append(item)
    else:
        completeness.append({"level": "info", "text": "这次运行不在网页对话里（评测或命令行），没有对话历史和网页端事件："
                                                      "看不到模型每一步的思考和输出，工具结果只有运行记录里的前 300 字，"
                                                      "确认和任务状态变化没有记录"})

    # 3. events_json: confirmations, task states, context and local steps, under the call they happened in
    placer, open_call, pointer, last_step, ends = Placer(), None, 0, None, {}
    tool_events = any(e["type"] == "tool_end" for e in events)
    for ev in events:
        t = ev["type"]
        if t == "tool_start":
            j = next((j for j in range(pointer, len(calls))
                      if calls[j]["step"] == ev.get("step") and calls[j]["tool"] == ev.get("tool")), None)
            if j is not None:
                open_call, pointer = calls[j], j + 1
            continue
        if t == "tool_end":
            if open_call is not None:
                ends[open_call["i"]] = ev.get("at", "")[11:19]
            open_call = None
            continue
        if t in ("llm_start", "llm_end"):
            last_step = ev.get("step")
            if last_step is not None and last_step not in steps:  # a request that started and never came back
                steps[last_step] = {"step": last_step, "ms": None, "tokens": None, "slow": False, "requests": [],
                                    "has_history": False, "reasoning": "", "text": "", "decided": [],
                                    "items": [], "after": [], "unfinished": True}
            continue
        item = placer.item(ev)
        if item is None:
            continue
        if open_call is not None:
            open_call["inner"].append(item)
        elif last_step is None or last_step not in steps:
            prep["items"].append(item)
        else:
            steps[last_step]["after"].append(item)
    if placer.confirm:
        placer.confirm.pop("_t", None)
    if calls and not tool_events and seg is not None:
        completeness.append({"level": "warn", "text": "网页端没有保存这一轮的过程事件：确认、任务状态变化和文件改动无法定位到具体步骤"})
    if placer.unknown:
        completeness.append({"level": "warn", "text": f"有 {placer.unknown} 条页面不认识的事件，已原样显示"})

    # 4. file operations: under the write call they belong to, before its task state update
    ops = oplog(src, run_id)

    def place(o, w, inferred=None):
        o["placed"] = True
        box = next((x for x in w["inner"] if x["kind"] == "ops"), None)
        if box is None:
            at = len(w["inner"])
            while at and w["inner"][at - 1]["kind"] == "state":
                at -= 1
            box = {"kind": "ops", "ops": [], "inferred": inferred}
            w["inner"].insert(at, box)
        box["ops"].append(o)

    if tool_events:
        for o in ops:
            w = next((c for c in calls if c["group"] == "write" and c["i"] in ends and ends[c["i"]] >= o["time"]), None)
            if w:
                place(o, w)
    elif ops:  # no event times: place only where the order leaves no doubt
        writes = [c for c in calls if c["group"] == "write" and c["ok"] is not False]
        if len(writes) == 1:
            for o in ops:
                place(o, writes[0], "这一轮只有这一次写入调用")
        elif [c["tool"] for c in writes] == [o["op"] for o in ops]:
            for o, w in zip(ops, writes):
                place(o, w, "每次调用对应一项改动，按顺序对应")
    if any(not o.get("placed") for o in ops):
        completeness.append({"level": "warn", "text": f"{sum(not o.get('placed') for o in ops)} 项文件改动没能对应到具体步骤，列在最后"})

    # 5. task forms and the finish declaration, on the call that filed them
    queue = defaultdict(list)
    for f in task_forms(trace, tasks_db):
        queue[(f["step"], f["tool"])].append(f)
    for c in calls:
        if queue.get((c["step"], c["tool"])):
            c["form"] = queue[(c["step"], c["tool"])].pop(0)
    loose_forms = [f for fs in queue.values() for f in fs]
    sf = status_form(trace, end, tasks_db)
    if sf and sf["has_call"]:
        target = next((c for c in reversed(calls) if c["step"] == sf["step"] and c["tool"] == sf["tool"]), None)
        if target:
            target["finish"] = sf

    # 6. what to look at: facts as codes, worded by the page
    stop = end.get("stop_reason")
    anomalies = []
    for s in steps.values():
        if s["slow"]:
            anomalies.append({"level": "slow", "code": "slow_request", "step": s["step"], "ms": s["ms"]})
        for a in s["requests"]:
            if a.get("ok") is False:
                anomalies.append({"level": "bad", "code": "request_failed", "step": s["step"],
                                  "error": a.get("error_type"), "retry": bool(a.get("retry"))})
    for c in calls:
        if c["ok"] is False:
            anomalies.append({"level": "bad", "code": "call_failed", "step": c["step"], "tool": c["tool"], "i": c["i"], "text": c["outcome"][:80]})
        if c["repeat"] >= 2:
            anomalies.append({"level": "warn", "code": "repeat", "step": c["step"], "tool": c["tool"], "i": c["i"], "count": c["repeat"]})
    if failed:
        anomalies.append({"level": "bad", "code": "run_failed", "stage": failed.get("error_stage"), "error": failed.get("error_type")})
    if end.get("violations"):
        anomalies.append({"level": "bad", "code": "violations", "count": end["violations"]})
    if end.get("rejections"):
        anomalies.append({"level": "warn", "code": "rejections", "count": end["rejections"]})
    if trace and not end:
        completeness.append({"level": "bad", "text": "运行记录没有结束行，后面的步骤可能没写进来"})
    if end and end.get("tool_calls") is not None and end["tool_calls"] != len(calls):
        completeness.append({"level": "bad", "text": f"运行结束时统计了 {end['tool_calls']} 次工具调用，记录里只有 {len(calls)} 次"})
    cut = sum(c["truncated"] for c in calls)
    if cut and seg is not None:
        completeness.append({"level": "info", "text": f"{cut} 次调用的结果只有前 300 字（对话历史里没找到全文）"})

    # 7. how the turn ended and what the runtime verified, by the product's reducer
    runtime = end.get("runtime") or {}
    task = task_revision(tasks_db, runtime["task_id"], runtime["task_revision"]) \
        if runtime.get("task_id") and runtime.get("task_revision") else None
    states, states_source = turn_states({"report": report or {}, "task": task, "runtime": runtime, "stop_reason": stop})
    last = next((c for c in reversed(calls) if c["tool"] in ENDERS), None)
    ending = last and {"tool": last["tool"], "step": last["step"], "i": last["i"],
                       "mode": last["args"].get("mode") if isinstance(last["args"], dict) else None}

    seconds = end.get("seconds") or 0
    llm_s = end.get("llm_seconds") or sum((s["ms"] or 0) for s in steps.values()) / 1000
    tool_s = end.get("tool_seconds")
    if tool_s is None:
        tool_s = sum((c.get("ms") or 0) for c in calls) / 1000
    wait_s = end.get("wait_seconds")
    if wait_s is None:
        wait_s = sum(x.get("wait_s") or 0 for c in calls for x in c["inner"] if x["kind"] == "confirm")
    return {
        "turn_id": turn_id, "run_id": run_id, "index": index, "instruction": start.get("instruction", ""),
        "model": start.get("model"), "created_at": created_at, "ended": bool(end),
        "stop_reason": stop, "display": (report or {}).get("display"), "final": end.get("final", ""),
        "failure": failed and {"stage": failed.get("error_stage"), "error_type": failed.get("error_type")},
        "stats": {"seconds": round(seconds, 1), "llm": round(llm_s, 1), "tool": round(tool_s, 1), "wait": round(wait_s, 1),
                  "steps": end.get("steps", len(llm) or None), "tool_calls": end.get("tool_calls", len(calls)),
                  "tokens": end.get("tokens"), "cost": end.get("cost"), "files_changed": len(ops)},
        "shown_calls": sum(1 for s in steps.values() for i in s["items"] if i["kind"] == "call"),
        "anomalies": anomalies, "completeness": completeness,
        "prep": prep, "steps": [steps[s] for s in sorted(steps)], "runtime_final": runtime_final, "after_turn": after_turn,
        "file_ops": ops, "loose_forms": loose_forms, "status_form": sf, "raw": trace,
        "report": report, "states": states, "states_source": states_source, "ending": ending,
        "evidence_calls": sum(1 for c in calls if "evidence:" in str(c["result"])),
        "findings": any(c["tool"] == "report_findings" for c in calls),
    }


def link_handles(turns):
    """Where each handle first appeared and which later calls (or the report) cite it.

    turns share one index: a conversation keeps its handles from turn to turn."""
    index = {}

    def cite(ids):
        return [{"id": h, "kind": h[0], "from": index.get(h)} for h in dict.fromkeys(ids)]
    for t in turns:
        for s in t["steps"]:
            for c in s["items"]:
                c["uses"] = cite(HANDLE.findall(json.dumps([c["args"], c.get("requested")], ensure_ascii=False)))
                c["produced"] = []
                for h in dict.fromkeys(HANDLE.findall(str(c["result"]))):
                    if h not in index:
                        index[h] = {"turn": t["index"], "i": c["i"], "step": c["step"], "tool": c["tool"], "sentence": c["sentence"]}
                        c["produced"].append({"id": h, "kind": h[0]})
        for sec in (t.get("report") or {}).get("sections", []):
            sec["uses"] = cite(HANDLE.findall(sec.get("markdown", "")))


def summary(t):
    """What the turn list needs: enough to colour a dot and say why."""
    issues = sum(1 for a in t["anomalies"] if a["level"] in ("bad", "warn"))
    return {"index": t["index"], "run_id": t["run_id"], "instruction": t["instruction"][:80], "created_at": t["created_at"],
            "stop_reason": t["stop_reason"], "display": t["display"], "ended": t["ended"], "seconds": t["stats"]["seconds"],
            "tool_calls": t["stats"]["tool_calls"], "issues": issues,
            "incomplete": any(x["level"] in ("bad", "warn") for x in t["completeness"])}


class Logs:
    """Builds and caches the log page's data for one FileButler home.

    A conversation is rebuilt when its row or any of its traces changed; turns in
    one conversation are built together so handles link across them."""
    def __init__(self, src, conversation_messages):
        self.src = src
        self.conversation_messages = conversation_messages  # the chat page's own rendering, by conversation id
        self._cache, self._lock = {}, threading.Lock()

    def _db(self):
        db = sqlite3.connect(f"file:{self.src.state_db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return closing(db)

    def _stamp(self, run_ids):
        stamp = []
        for r in run_ids:
            p = self.src.log_dir / f"{r}.trace.jsonl"
            stamp.append(p.stat().st_mtime_ns if p.exists() else 0)
        return tuple(stamp)

    def limits(self):
        files = sorted(self.src.log_dir.glob("*.trace.jsonl"))
        key = ("limits", len(files), max((f.stat().st_mtime_ns for f in files), default=0))
        return self._cached(key, lambda: thresholds(self.src))

    def _cached(self, key, build):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = build()
        with self._lock:
            self._cache[key] = value
        return value

    def conversation(self, conversation_id):
        """Every turn of one conversation, full detail, plus the chat messages and system prompt."""
        with self._db() as db:
            conv = db.execute("SELECT id,title,updated_at,history_json FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if conv is None:
                return None
            rows = db.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)).fetchall()
        key = ("conv", conversation_id, conv["updated_at"], tuple(r["updated_at"] for r in rows), self._stamp(r["run_id"] for r in rows))
        return self._cached(key, lambda: self._build_conversation(conv, rows))

    def _build_conversation(self, conv, rows):
        limits = self.limits()
        history = json.loads(conv["history_json"] or "[]")
        segs = segments(history)
        mismatch = len(segs) != len(rows)
        if mismatch:
            segs = [None] * len(rows)
        with self.src.tasks() as tasks_db:
            turns = [build_turn(self.src, tasks_db, r["run_id"], i + 1, limits, json.loads(r["events_json"] or "[]"),
                                segs[i], r["created_at"][:19].replace("T", " "), r["id"],
                                json.loads(r["result_json"] or "{}").get("report")) for i, r in enumerate(rows)]
        if mismatch:
            for t in turns:
                t["completeness"].insert(0, {"level": "warn", "text": f"对话历史里有 {len(segments(history))} 条用户消息，"
                                                                     f"但有 {len(rows)} 轮运行，没有用历史补全"})
        messages, turn_no = [], 0
        for m in self.conversation_messages(conv["id"]):
            if m["role"] == "user":
                turn_no += 1
            if m.get("report") and 0 < turn_no <= len(turns):  # the chat page's own rendering of the report
                turns[turn_no - 1]["report_html"] = {"summary": m.get("html") or "",
                                                     "sections": {s["title"]: s.get("html") for s in m["report"].get("sections", [])}}
            messages.append({"role": m["role"], "content": m.get("content", ""), "html": m.get("html"),
                             "turn": turn_no if m["role"] != "event" else None})
        link_handles(turns)
        base = next((m.get("content", "") for m in history[:1] if m.get("role") == "system"), "")
        return {"id": conv["id"], "title": conv["title"], "updated_at": conv["updated_at"][:16].replace("T", " "),
                "system_prompt": base, "messages": messages, "turns": turns, "limits": limits}

    def standalone_ids(self):
        """Runs with a trace but no web turn: evals and the CLI."""
        with self._db() as db:
            seen = {r[0] for r in db.execute("SELECT run_id FROM turns")}
        return sorted(r for r in (f.name[:-len(".trace.jsonl")] for f in self.src.log_dir.glob("*.trace.jsonl"))
                      if r not in seen and RUN_ID.match(r))

    def run(self, run_id):
        """One run outside the web UI, full detail."""
        if not RUN_ID.match(run_id or "") or run_id not in self.standalone_ids():
            return None
        key = ("run", run_id, self._stamp([run_id]))

        def build():
            limits = self.limits()
            with self.src.tasks() as tasks_db:
                t = build_turn(self.src, tasks_db, run_id, 1, limits,
                               created_at=f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]} {run_id[9:11]}:{run_id[11:13]}:{run_id[13:15]}")
            link_handles([t])  # separate runs share no handles
            return {"turn": t, "limits": limits}
        return self._cached(key, build)

    def index(self):
        """The list on the left: each conversation and run with one summary per turn."""
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT c.id FROM conversations c WHERE EXISTS "
                                            "(SELECT 1 FROM turns t WHERE t.conversation_id=c.id) ORDER BY c.updated_at DESC")]
        conversations = []
        for cid in ids:
            c = self.conversation(cid)
            if c:
                conversations.append({"id": c["id"], "title": c["title"], "updated_at": c["updated_at"],
                                      "turns": [summary(t) for t in c["turns"]]})
        runs = [summary(self.run(r)["turn"]) for r in self.standalone_ids()]
        return {"conversations": conversations, "runs": runs}
