"""Build the offline log-viewer mock from real local logs (prototype of a future web_logs.py).

    .venv/bin/python docs/mock/build_logs_mock.py

Reads .filebutler-ui/state.sqlite3, logs/*.trace.jsonl and sandbox/.oplog, writes
logs/mock/logs-viewer.html (logs/ is gitignored: the page embeds real conversation text).

The trace is the skeleton of every turn: each model request and each tool call.
The conversation's history_json fills in what the trace leaves out (the model's
reasoning and text, the full tool results it received, injected system messages);
the web UI's events_json places confirmations, task-state changes and file
operations under the call they happened in. Anything that cannot be matched is
reported on the page, never dropped. Exits non-zero if a turn shows fewer calls
than its run made.
"""
import glob
import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import webapp  # noqa: E402  (reuses the chat page's exact message rendering)
from agent.history import UNDO_MARK  # noqa: E402
from eval.judge import turn_states  # noqa: E402  (the product's own state reducer, saved or projected)

TASK_TOOLS = {"start_task", "set_task", "revise_task"}
TASK_STATES = {"running": "进行中", "completed": "已完成", "partial": "只完成了一部分", "needs_review": "待你复核",
               "waiting_input": "等你回答", "cancelled": "已取消", "interrupted": "被中断", "paused": "已暂停", "not_registered": "未登记"}
STATE_TONE = {"completed": "ok", "partial": "warn", "needs_review": "warn", "waiting_input": "wait", "cancelled": "wait", "interrupted": "bad"}
OUT = ROOT / "logs/mock/logs-viewer.html"
TEMPLATE = Path(__file__).with_name("logs-viewer.template.html")
WEB = Path(__file__).resolve().parents[2] / "web"  # the chat page's CSS, reused as is

TOOL_LABELS = {
    "prepare_file_plan": "生成文件操作清单并预检", "execute_file_plan": "执行文件计划并核验", "inspect_file_plan": "查看文件计划",
    "ask_user": "向你提问", "start_task": "登记你的要求", "revise_task": "修改任务", "set_task": "登记目标与范围",
    "finish_task": "核验任务完成条件", "inspect_run": "查询运行原因", "inventory_files": "统计文件",
    "list_dir": "查看文件夹", "search_files": "查找文件", "search_content": "检索文件内容", "read_file": "阅读文件",
    "file_info": "核对文件信息", "find_duplicates": "扫描重复文件", "apply_dedup": "处理重复文件", "deduplicate": "查重与清理",
    "report_findings": "核对证据并生成答复", "paper_profiles": "批量获取论文片段", "submit_classification": "保存分类计划",
    "apply_classification": "执行分类计划", "begin_step": "准备任务步骤", "make_dir": "创建文件夹", "move": "移动文件",
    "rename": "重命名文件", "trash": "移入回收区", "trash_many": "批量移入回收区",
    "finish_turn": "结束本轮",
}
KIND = {  # icon group for the timeline
    "list_dir": "look", "search_files": "look", "search_content": "look", "read_file": "look", "file_info": "look",
    "inventory_files": "look", "paper_profiles": "look", "find_duplicates": "look", "inspect_file_plan": "look", "inspect_run": "look",
    "make_dir": "write", "move": "write", "rename": "write", "trash": "write", "trash_many": "write", "apply_dedup": "write",
    "execute_file_plan": "write", "apply_classification": "write",
    "ask_user": "ask",
}
OUTCOMES = {
    "done": ("已完成", "ok"), "answered": ("已回答", "ok"), "explained": ("已回答", "ok"),
    "waiting_input": ("等你回答", "wait"), "needs_review": ("请你复核结果", "warn"), "partial": ("只完成了一部分", "warn"),
    "cost_budget": ("费用达上限被停止", "bad"), "token_budget": ("Token 达上限被停止", "bad"),
    "max_steps": ("步数达上限被停止", "bad"), "repeated_call": ("重复调用被停止", "bad"),
    "repair_limit": ("连续失败无进展", "bad"), "exception": ("运行出错", "bad"), "cancelled": ("已取消", "wait"),
}
OPS = {"trash": "移入回收区", "move": "移动", "rename": "重命名", "make_dir": "新建文件夹"}
EFFECTS = {"create": "新建", "move": "移动", "rename": "改名", "trash": "移入回收区"}
PART_KINDS = {"organize": "整理", "find": "查找", "answer": "回答", "classify": "分类", "dedup": "查重"}
CONDITIONS = {
    "file_plan_verified": "执行文件计划后，逐项核验位置和内容", "evidence_valid": "答复引用的证据必须有效",
    "manual_review": "需要你人工确认", "plan_ready": "分类方案已保存", "plan_applied": "分类方案已执行并核验",
    "empty_directories_removed": "空文件夹已清理", "tool_success": "工具调用成功",
}
PLACEMENTS = {"flatten": "平铺到目标位置", "keep_unchanged": "保持原样"}


def effects(xs):
    return "、".join(EFFECTS.get(x, x) for x in xs or [])


def paths(xs):
    return "、".join("全部文件" if x in (".", "") else f"「{x}」" for x in xs or [])


def limit_lines(limits):
    """start_task limits (dict in current protocol)."""
    out = []
    for k, v in (limits or {}).items():
        if k == "only_within": out.append(f"只在 {paths(v)} 内操作")
        elif k == "no_effects": out.append(f"不允许：{effects(v)}")
        elif k == "only_extensions": out.append(f"只处理 {'、'.join('.' + x for x in v)} 文件")
        else: out.append(f"{k}：{v}")
    return out


def rule_line(rule):
    who = {"source": "原文件", "destination": "目标位置"}.get(rule.get("selector"), "操作对象")
    act, vals = effects(rule.get("effects")), rule.get("values") or []
    pred = rule.get("predicate")
    text = {"within": f"{who}必须在 {paths(vals)} 内", "outside": f"{who}不能在 {paths(vals)} 内",
            "extensions": f"{who}只能是 {'、'.join('.' + v for v in vals)} 文件", "content_unchanged": "文件内容不能变",
            "same_extension": "扩展名不能变", "empty_directory": "只能是空文件夹"}.get(pred, f"{pred} {vals}")
    return f"{act}时：{text}"


def condition_line(c):
    text = CONDITIONS.get(c.get("kind"), c.get("kind"))
    if c.get("kind") == "tool_success" and c.get("tool"):
        text = f"{TOOL_LABELS.get(c['tool'], c['tool'])}成功"
    if c.get("placement") in PLACEMENTS:
        text += f"（{PLACEMENTS[c['placement']]}）"
    return text


def model_form(tool, a):
    """What the model wrote, in form fields."""
    if not isinstance(a, dict):
        return {"fields": [["原始参数", str(a)]], "parts": []}
    fields, parts = [], []
    if a.get("summary"): fields.append(["任务摘要", a["summary"]])
    if a.get("reason"): fields.append(["修改原因", a["reason"]])
    if a.get("expected_revision"): fields.append(["基于版本", f"第 {a['expected_revision']} 版"])
    for p in a.get("parts") or []:
        parts.append({"id": p.get("id"), "kind": PART_KINDS.get(p.get("kind"), p.get("kind") or "—"),
                      "description": p.get("description", ""), "scope": paths(p.get("scope"))})
    for g in a.get("goals") or []:  # older set_task protocol
        parts.append({"id": g.get("id"), "kind": "目标", "description": g.get("description", ""),
                      "scope": "；".join(condition_line(c) for c in g.get("conditions") or [])})
    lims = limit_lines(a.get("limits") if isinstance(a.get("limits"), dict) else {})
    if a.get("write_roots"): lims.append(f"写入范围：{paths(a['write_roots'])}")
    if a.get("allowed_effects"): lims.append(f"允许的操作：{effects(a['allowed_effects'])}")
    for c in a.get("constraints") or []:
        lims.append(c.get("description") or c.get("id"))
    return {"fields": fields, "parts": parts, "limits": lims}


def registered_form(tasks_db, task_id, revision):
    """What the system stored after validating the form (sandbox/.oplog/tasks.sqlite3)."""
    row = tasks_db.execute("SELECT data_json FROM task_revisions WHERE id=? AND revision=?", (task_id, revision)).fetchone()
    if not row:
        return None
    d = json.loads(row[0])
    write = d.get("write_roots") or []
    return {
        "goals": [{"id": g["id"], "description": g.get("description", ""),
                   "conditions": [condition_line(c) for c in g.get("conditions") or []],
                   "semantic_review": g.get("semantic_review", False), "scope": paths(g.get("scope"))} for g in d.get("goals", [])],
        "read": paths(d.get("read_roots")) or "—",
        "write": paths(write[:6]) + (f" 等 {len(write)} 处" if len(write) > 6 else "") if write else "不写入任何文件",
        "write_count": len(write),
        "effects": effects(d.get("allowed_effects")) or "无（只读）",
        "rules": [{"description": c.get("description", ""), "rules": [rule_line(r) for r in c.get("rules") or []],
                   "forbidden": effects(c.get("forbidden_effects"))} for c in d.get("constraints", [])],
        "evidence": d.get("policy", {}).get("evidence"),
    }


def status_form(trace, events, end, tasks_db):
    """The model's finish_task declaration next to the system's per-goal verification."""
    call = next((e for e in reversed(trace) if e.get("tool") == "finish_task"
                 or (e.get("tool") == "finish_turn" and '"task"' in str(e.get("args", "")))), None)
    runtime = end.get("runtime") or {}
    # status history from task_updated events, collapsing repeats
    history = []
    for ev in events:
        if ev["type"] != "task_updated" or not ev.get("task_id"):
            continue
        key = (ev["task_id"], ev.get("task_revision"), ev.get("task_status"))
        if not history or history[-1]["key"] != key:
            history.append({"key": key, "at": ev["at"][11:19], "revision": ev.get("task_revision"),
                            "status": ev.get("task_status"), "label": TASK_STATES.get(ev.get("task_status"), ev.get("task_status")),
                            "tone": STATE_TONE.get(ev.get("task_status"), "gray"), "blocked": ev.get("blocked_calls", 0)})
    for h in history:
        h.pop("key")
    if not call and not history:
        return None
    try:
        args = json.loads(call.get("args", "{}")) if call else {}
    except json.JSONDecodeError:
        args = {}
    task_id, revision = runtime.get("task_id"), runtime.get("task_revision")
    goals = {}
    if task_id and revision:
        row = tasks_db.execute("SELECT data_json FROM task_revisions WHERE id=? AND revision=?", (task_id, revision)).fetchone()
        if row:
            goals = {g["id"]: g.get("description", "") for g in json.loads(row[0]).get("goals", [])}
    unfinished = {u["part_id"]: u.get("reason", "") for u in args.get("unfinished") or []}
    not_needed = {u["part_id"]: u.get("reason", "") for u in args.get("not_needed") or []}
    rows = []
    for c in runtime.get("checks", []):
        gid = c.get("goal_id")
        claim = ("unfinished", "说没做到", unfinished[gid]) if gid in unfinished else \
                ("not_needed", "说无需操作", not_needed[gid]) if gid in not_needed else ("done", "说已完成", "")
        verdict = ("review", "需要你复核") if c.get("needs_review") else ("pass", "核验通过") if c.get("passed") else ("fail", "核验未通过")
        rows.append({"id": gid, "description": c.get("description") or goals.get(gid, gid),
                     "claim": claim[0], "claim_label": claim[1], "claim_reason": claim[2],
                     "verdict": verdict[0], "verdict_label": verdict[1],
                     "detail": "；".join([x.get("detail", "") for x in c.get("conditions", []) if x.get("detail")]
                                        + [n.get("text", "") for n in c.get("notes", []) if n.get("text")]),
                     "agree": verdict[0] == "review" or (claim[0] == "done") == (verdict[0] == "pass")})
    state = runtime.get("task_status")
    return {"step": call and call.get("step"), "tool": call and call.get("tool"), "has_call": bool(call),
            "receipt": result_line(call.get("result", "")) if call else "", "ok": call and call.get("ok"),
            "reply": args.get("reply", ""), "summary": args.get("summary", ""),
            "task_id": task_id, "revision": revision,
            "state": state, "state_label": TASK_STATES.get(state, state or "—"), "state_tone": STATE_TONE.get(state, "gray"),
            "rows": rows, "history": history}


def task_forms(trace, tasks_db):
    forms = []
    for e in trace:
        if e.get("tool") not in TASK_TOOLS:
            continue
        try:
            args = json.loads(e.get("args", "{}"))
        except json.JSONDecodeError:
            args = e.get("args")
        m = re.search(r"task_id=(T\w+) revision=(\d+)", str(e.get("result", "")))
        forms.append({"step": e.get("step"), "tool": e["tool"], "label": TOOL_LABELS.get(e["tool"], e["tool"]),
                      "ok": e.get("ok"), "receipt": result_line(e.get("result", "")),
                      "task_id": m and m.group(1), "revision": m and int(m.group(2)),
                      "model": model_form(e["tool"], args),
                      "registered": registered_form(tasks_db, m.group(1), int(m.group(2))) if m else None})
    return forms


def q(text, n=36):
    text = str(text)
    return f"「{text[:n]}…」" if len(text) > n else f"「{text}」"


def where(path):
    return "全部文件" if path in (None, "", ".") else q(path)


def sentence(tool, a):
    """One plain-language line for a tool call. Unknown tools fall back to their label."""
    if not isinstance(a, dict):
        return TOOL_LABELS.get(tool, tool)
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
        case "prepare_file_plan": return f"拟定文件操作清单：{len(a.get('actions') or [])} 个操作" + (f"（{a['description'][:30]}）" if a.get("description") else "")
        case "execute_file_plan": return "执行已确认的文件操作清单，并逐项核验"
        case "ask_user": return f"问你：{q(a.get('question', ''), 60)}"
        case "start_task" | "set_task": return f"把你的要求登记为任务：{q(a.get('summary', ''), 50)}"
        case "finish_task": return "逐项核验任务是否真的完成，然后答复你"
        case "finish_turn":
            return {"reply": "直接回答你，结束本轮（不登记任务、不核验）", "ask": f"问你：{q(a.get('question', ''), 60)}，等你选择",
                    "task": "收尾：本地逐部分核验登记的任务，然后答复你"}.get(a.get("mode"), "结束本轮")
        case "submit_classification": return f"提交分类方案：{len(a.get('categories') or [])} 个类别、{len(a.get('assignments') or [])} 个文件"
        case "report_findings": return "整理证据，生成答复"
    return TOOL_LABELS.get(tool, tool)


def result_line(result):
    lines = [l.strip() for l in str(result).splitlines() if l.strip()]
    if not lines:
        return ""
    first = re.sub(r"^(OK|ERROR|错误)[:：]\s*", "", lines[0])
    return (first or (lines[1] if len(lines) > 1 else ""))[:140]


def parse_at(s):
    return datetime.fromisoformat(s)


def thresholds():
    llm, tool = [], []
    for f in glob.glob(str(ROOT / "logs/*.trace.jsonl")):
        for line in open(f, encoding="utf-8"):
            e = json.loads(line)
            if e.get("event") == "llm":
                llm.append(e["ms"])
            elif e.get("tool") and e.get("ms") is not None:
                tool.append(e["ms"])
    p95 = lambda v: statistics.quantiles(v, n=100)[94]
    return {"llm_ms": round(p95(llm)), "tool_ms": round(p95(tool)), "llm_n": len(llm), "tool_n": len(tool)}


def oplog(run_id):
    base = ROOT / "sandbox/.oplog"
    for path, undone in ((base / f"{run_id}.jsonl", False), (base / f"{run_id}.undone.jsonl", True)):
        if path.exists():
            ops = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            return [{"op": o["op"], "label": OPS.get(o["op"], o["op"]), "src": o.get("src"), "dst": o.get("dst"),
                     "time": o.get("time", "")[11:19], "undone": undone} for o in ops]
    return []


def load_trace(run_id):
    path = ROOT / f"logs/{run_id}.trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def as_json(text):
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def tool_message(content):
    """A tool message as the model received it: the typed JSON envelope, or plain text in older runs."""
    d = as_json(content)
    if isinstance(d, dict) and "text" in d:
        return d.get("text") or "", {k: v for k, v in d.items() if k != "text" and v not in (None, [], {}, "")}
    return str(content or ""), {}


def system_item(m):
    text = m.get("content") or ""
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    kind = "undo" if text.startswith(UNDO_MARK) else "system"
    return {"kind": "injected", "source": kind, "title": first[:80], "chars": len(text), "content": text}


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


# local handles the tools hand out and later calls cite: file:F… evidence:E… catalog C… plan P…/X… task T…
HANDLE = re.compile(r"\b(?:[FECP][0-9a-f]{10}|[XT][0-9a-f]{12})\b")
HANDLE_KINDS = {"F": "文件", "E": "证据片段", "C": "目录快照", "P": "分类计划", "X": "文件计划", "T": "任务"}
ENDERS = {"finish_turn", "finish_task", "ask_user", "inspect_run"}


def link_handles(turns):
    """Where each handle first appeared and which later calls (or the report) cite it.

    turns share one index: a conversation keeps its handles from turn to turn."""
    index = {}
    def cite(ids):
        return [{"id": h, "kind": HANDLE_KINDS[h[0]], "from": index.get(h)} for h in dict.fromkeys(ids)]
    for t in turns:
        for s in t["steps"]:
            for c in s["items"]:
                c["uses"] = cite(HANDLE.findall(json.dumps([c["args"], c.get("requested")], ensure_ascii=False)))
                c["produced"] = []
                for h in dict.fromkeys(HANDLE.findall(str(c["result"]))):
                    if h not in index:
                        index[h] = {"turn": t["index"], "i": c["i"], "step": c["step"], "tool": c["tool"], "sentence": c["sentence"]}
                        c["produced"].append({"id": h, "kind": HANDLE_KINDS[h[0]]})
        for sec in (t.get("report") or {}).get("sections", []):
            sec["uses"] = cite(HANDLE.findall(sec.get("markdown", "")))


# events that only mark where things happen; the trace already holds their content
STRUCTURAL = {"started", "completed", "llm_start", "llm_end", "tool_start", "tool_end", "llm_retry", "llm_request"}


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
                answer = ev.get("answer")
                self.confirm.update(answer=answer, answer_label={"y": "允许", "a": "本轮全部允许", "n": "拒绝"}.get(answer, answer),
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
            return {"kind": "state", "task_id": ev["task_id"], "revision": ev.get("task_revision"), "status": ev.get("task_status"),
                    "label": TASK_STATES.get(ev.get("task_status"), ev.get("task_status")),
                    "tone": STATE_TONE.get(ev.get("task_status"), "gray"), "blocked": ev.get("blocked_calls", 0), "at": at}
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
    return {"kind": "call", "i": index, "step": rec.get("step"), "tool": rec["tool"], "label": TOOL_LABELS.get(rec["tool"], rec["tool"]),
            "group": KIND.get(rec["tool"], "task"), "sentence": sentence(rec["tool"], args), "ok": rec.get("ok"),
            "ms": rec.get("ms"), "active_ms": rec.get("active_ms"), "wait_ms": rec.get("wait_ms"),
            "repeat": rec.get("repeat_count", 0), "executed": rec.get("executed"),
            "args": args, "requested": requested if requested is not None and requested != args else None,
            "error_code": rec.get("error_code"), "error_class": rec.get("error_class"),
            "references": rec.get("references") or [], "operation_refs": rec.get("operation_refs"),
            "progress": rec.get("progress"), "result_id": rec.get("result_id"),
            "result": result, "envelope": {}, "raw_result": None, "result_source": "trace",
            "truncated": len(str(result)) >= 300, "outcome": result_line(result),
            "slow": (rec.get("ms") or 0) > limits["tool_ms"], "inner": []}


def build_turn(run_id, index, limits, tasks_db, events=None, seg=None, created_at="", turn_id=None, report=None):
    trace = load_trace(run_id)
    events = events or []
    start = next((e for e in trace if e.get("event") == "start"), {})
    end = next((e for e in trace if e.get("event") == "end"), {})
    failed = next((e for e in trace if e.get("event") == "failed"), None)
    completeness = []
    if not trace:
        completeness.append({"level": "bad", "text": f"找不到运行记录 logs/{run_id}.trace.jsonl"})

    # 1. skeleton from the trace: model requests (steps) and tool calls
    llm = {e["step"]: e for e in trace if e.get("event") == "llm"}
    reqs = requests_by_step(trace)
    recs = [e for e in trace if "tool" in e]
    calls = [call_item(r, i, limits) for i, r in enumerate(recs)]
    step_ids = sorted(set(llm) | set(reqs) | {c["step"] for c in calls if c["step"] is not None})
    steps = {s: {"step": s, "ms": (llm.get(s) or {}).get("ms"), "tokens": (llm.get(s) or {}).get("tokens"),
                 "slow": ((llm.get(s) or {}).get("ms") or 0) > limits["llm_ms"], "requests": reqs.get(s, []),
                 "has_history": False, "reasoning": "", "text": "", "decided": [], "items": [], "after": []}
             for s in step_ids}
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

    # 4. file operations: under the write call that ended at or after them, before its task state update
    ops = oplog(run_id)

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
    loose_forms = [f for q in queue.values() for f in q]
    sf = status_form(trace, events, end, tasks_db)
    if sf and sf["has_call"]:
        target = next((c for c in reversed(calls) if c["step"] == sf["step"] and c["tool"] == sf["tool"]), None)
        if target:
            target["finish"] = sf

    # 6. outcome, anomalies, completeness of the counts
    stop = end.get("stop_reason")
    label, tone = OUTCOMES.get(stop, ("已停止" if end else "没有结束记录（可能中途崩溃）", "bad"))
    anomalies = []
    if tone in ("warn", "bad"):
        anomalies.append({"level": tone, "text": f"本轮结局：{label}"})
    for s in steps.values():
        if s["slow"]:
            anomalies.append({"level": "slow", "text": f"第 {s['step']} 步模型请求用了 {s['ms'] / 1000:.1f} 秒，慢于 95% 的历史步骤"})
        for a in s["requests"]:
            if a.get("ok") is False:
                anomalies.append({"level": "bad", "text": f"第 {s['step']} 步模型请求失败（{a.get('error_type')}），"
                                                          f"{'已自动重试' if a.get('retry') else '未重试'}"})
    for c in calls:
        if c["ok"] is False:
            anomalies.append({"level": "bad", "text": f"第 {c['step']} 步 {c['tool']}（{c['label']}）失败：{c['outcome'][:60]}"})
        if c["repeat"] >= 2:
            anomalies.append({"level": "warn", "text": f"第 {c['step']} 步 {c['tool']} 与之前的调用重复（第 {c['repeat']} 次）"})
    if end.get("violations"):
        anomalies.append({"level": "bad", "text": f"触发了 {end['violations']} 次安全规则"})
    if end.get("rejections"):
        anomalies.append({"level": "warn", "text": f"有 {end['rejections']} 个操作被拒绝"})
    if trace and not end:
        completeness.append({"level": "bad", "text": "运行记录没有结束行，后面的步骤可能没写进来" + ("（有失败记录）" if failed else "")})
    if end and end.get("tool_calls") is not None and end["tool_calls"] != len(calls):
        completeness.append({"level": "bad", "text": f"运行结束时统计了 {end['tool_calls']} 次工具调用，记录里只有 {len(calls)} 次"})
    cut = sum(c["truncated"] for c in calls)
    if cut and seg is not None:
        completeness.append({"level": "info", "text": f"{cut} 次调用的结果只有前 300 字（对话历史里没找到全文）"})

    # 7. how the turn ended and what the runtime verified, by the product's reducer
    runtime = end.get("runtime") or {}
    task = None
    if runtime.get("task_id") and runtime.get("task_revision"):
        row = tasks_db.execute("SELECT data_json FROM task_revisions WHERE id=? AND revision=?",
                               (runtime["task_id"], runtime["task_revision"])).fetchone()
        task = json.loads(row[0]) if row else None
    states, states_source = turn_states({"report": report or {}, "task": task, "runtime": runtime, "stop_reason": stop})
    last = next((c for c in reversed(calls) if c["tool"] in ENDERS), None)
    ending = last and {"tool": last["tool"], "step": last["step"], "i": last["i"],
                       "mode": last["args"].get("mode") if isinstance(last["args"], dict) else None}
    evidence_calls = sum(1 for c in calls if "evidence:" in str(c["result"]))
    findings = any(c["tool"] == "report_findings" for c in calls)

    seconds = end.get("seconds") or 0
    llm_s = end.get("llm_seconds") or sum((s["ms"] or 0) for s in steps.values()) / 1000
    tool_s = end.get("tool_seconds")
    if tool_s is None:
        tool_s = sum((c.get("ms") or 0) for c in calls) / 1000
    wait_s = end.get("wait_seconds")
    if wait_s is None:
        wait_s = sum(x.get("wait_s") or 0 for c in calls for x in c["inner"] if x["kind"] == "confirm")
    checks = [{"description": c.get("description", ""), "outcome": c.get("outcome"), "passed": c.get("passed"),
               "needs_review": c.get("needs_review"),
               "detail": "；".join(x.get("detail", "") for x in c.get("conditions", []) if x.get("detail"))}
              for c in (end.get("runtime") or {}).get("checks", [])]
    return {
        "turn_id": turn_id, "run_id": run_id, "index": index, "instruction": start.get("instruction", ""),
        "model": start.get("model"), "created_at": created_at,
        "outcome": {"stop_reason": stop, "label": label, "tone": tone}, "final": end.get("final", ""),
        "stats": {"seconds": round(seconds, 1), "llm": round(llm_s, 1), "tool": round(tool_s, 1), "wait": round(wait_s, 1),
                  "steps": end.get("steps", len(llm) or None), "tool_calls": end.get("tool_calls", len(calls)),
                  "tokens": end.get("tokens"), "cost": end.get("cost"), "files_changed": len(ops)},
        "shown_calls": sum(1 for s in steps.values() for i in s["items"] if i["kind"] == "call"),
        "anomalies": anomalies, "completeness": completeness, "checks": checks,
        "prep": prep, "steps": [steps[s] for s in step_ids], "runtime_final": runtime_final, "after_turn": after_turn,
        "file_ops": ops, "loose_forms": loose_forms, "status_form": sf, "raw": trace,
        "report": report, "states": states, "states_source": states_source, "ending": ending,
        "evidence_calls": evidence_calls, "findings": findings,
    }


def undercounted(data):
    """Turns whose timeline shows a different number of calls than the run made."""
    return [(c["id"][:8], t["index"], t["run_id"], t["shown_calls"], t["stats"]["tool_calls"])
            for c in data for t in c["turns"] if t["shown_calls"] != (t["stats"]["tool_calls"] or 0)]


def render(data, limits):
    css = (WEB / "style.css").read_text(encoding="utf-8") + "\n" + (WEB / "chat.css").read_text(encoding="utf-8")
    payload = json.dumps({"conversations": data, "limits": limits}, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*APP_CSS*/", css).replace("/*DATA*/null", payload)


def main():
    limits = thresholds()
    db = sqlite3.connect(ROOT / ".filebutler-ui/state.sqlite3")
    db.row_factory = sqlite3.Row
    tasks_db = sqlite3.connect(ROOT / "sandbox/.oplog/tasks.sqlite3")
    data, seen = [], set()
    for conv in db.execute("SELECT id,title,updated_at,history_json FROM conversations ORDER BY updated_at DESC").fetchall():
        rows = db.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at,rowid", (conv["id"],)).fetchall()
        if not rows:
            continue
        history = json.loads(conv["history_json"] or "[]")
        segs = segments(history)
        if len(segs) != len(rows):
            print(f"warning: {conv['id'][:8]} has {len(segs)} user messages in history but {len(rows)} turns; history not used")
            segs = [None] * len(rows)
        turns = [build_turn(r["run_id"], i + 1, limits, tasks_db, json.loads(r["events_json"] or "[]"), segs[i],
                            r["created_at"][:19].replace("T", " "), r["id"], json.loads(r["result_json"] or "{}").get("report"))
                 for i, r in enumerate(rows)]
        seen.update(r["run_id"] for r in rows)
        messages, turn_no = [], 0
        for m in webapp.get_conversation(conv["id"])["messages"]:
            if m["role"] == "user":
                turn_no += 1
            if m.get("report") and turn_no:  # the chat page's own rendering of the report
                t = turns[turn_no - 1]
                t["report_html"] = {"summary": m.get("html") or "",
                                    "sections": {sec["title"]: sec.get("html") for sec in m["report"].get("sections", [])}}
            messages.append({"role": m["role"], "content": m.get("content", ""), "html": m.get("html"),
                             "turn": turn_no if m["role"] != "event" else None})
        assert turn_no == len(turns), (conv["id"], turn_no, len(turns))
        link_handles(turns)
        base = next((m.get("content", "") for m in history[:1] if m.get("role") == "system"), "")
        data.append({"id": conv["id"], "title": conv["title"], "updated_at": conv["updated_at"][:16].replace("T", " "),
                     "system_prompt": base, "messages": messages, "turns": turns})

    # runs that never went through the web UI: evals and the CLI
    other = sorted(Path(f).name[:-len(".trace.jsonl")] for f in glob.glob(str(ROOT / "logs/*.trace.jsonl")))
    other = [r for r in other if r not in seen]
    if other:
        turns, messages = [], []
        for i, run_id in enumerate(other):
            stamp = f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]} {run_id[9:11]}:{run_id[11:13]}:{run_id[13:15]}"
            t = build_turn(run_id, i + 1, limits, tasks_db, created_at=stamp)
            link_handles([t])  # separate runs share no handles
            turns.append(t)
            messages += [{"role": "user", "content": t["instruction"] or "（没有记录指令）", "html": None, "turn": i + 1},
                         {"role": "assistant", "content": t["final"] or "（没有记录答复）", "html": None, "turn": i + 1}]
        data.append({"id": "runs-without-conversation", "title": f"评测与命令行运行 · {len(other)} 次",
                     "updated_at": turns[-1]["created_at"][:16], "system_prompt": "", "messages": messages, "turns": turns,
                     "standalone": True})

    short = undercounted(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(data, limits), encoding="utf-8")
    n = sum(len(c["turns"]) for c in data)
    flagged = sum(1 for c in data for t in c["turns"] if any(x["level"] in ("warn", "bad") for x in t["completeness"]))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB): {len(data)} groups, {n} turns, "
          f"{sum(t['shown_calls'] for c in data for t in c['turns'])} calls, {flagged} turns with incomplete records; limits={limits}")
    if short:
        print("turns showing fewer calls than the run made:", *short, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main()
