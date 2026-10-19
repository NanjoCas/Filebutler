"""Local FastAPI UI/API for FileButler. Start with: uvicorn webapp:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import json
import io
import mimetypes
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.artifacts import ArtifactStore
from agent.history import prepare_history, undo_note, has_undo, UNDO_MARK
from agent.llm import LLM  # noqa: E402
from agent.loop import run_agent  # noqa: E402
from agent.prompts import SYSTEM_PROMPTS, LATENCY_GUIDANCE, LOCAL_RUNTIME_GUIDANCE, CLASSIFICATION_GUIDANCE  # noqa: E402
from rich.console import Console  # noqa: E402
from safety.confirm import Confirmer  # noqa: E402
from safety.oplog import OpLog, undo  # noqa: E402
from safety.sandbox import Sandbox, is_hidden  # noqa: E402
from tools.fs_read import IMAGE_EXTS, TEXT_EXTS, _fmt_size  # noqa: E402
from web_render import FileCatalog, WEB_PRESENTATION_GUIDANCE, render_markdown  # noqa: E402
from web_confirm import build_confirmation  # noqa: E402

WEB_DIR = ROOT / "web"
STATE_DIR = ROOT / ".filebutler-ui"
DB_PATH = STATE_DIR / "state.sqlite3"
LOG_DIR = ROOT / "logs"
MAX_PREVIEW_CHARS = 1800
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="filebutler")


def _config() -> dict:
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["sandbox"] = str((ROOT / cfg["sandbox"]).resolve())
    return cfg


@contextmanager
def _db():
    import sqlite3
    STATE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db():
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, history_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            events_json TEXT NOT NULL DEFAULT '[]', final TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(conversation_id) REFERENCES conversations(id)
        );
        CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, created_at);
        """)
        columns = {r[1] for r in db.execute('PRAGMA table_info(conversations)')}
        if 'artifacts_json' not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '{}'")
        db.execute("PRAGMA journal_mode=WAL")


def _artifacts(conversation_id, sb=None, persist=False):
    row = _get_conversation(conversation_id)
    def save(data):
        with _db() as db:
            db.execute('UPDATE conversations SET artifacts_json=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), conversation_id))
    return ArtifactStore(sb or Sandbox(_config()['sandbox']), json.loads(row['artifacts_json']),
                         on_change=save if persist else None)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _event(turn_id: str, event: dict):
    state = TURN_STATES.get(turn_id)
    if state:
        with state.lock:
            now = time.perf_counter()
            if event["type"] == "run_failed":
                state.result.update({k: v for k, v in event.items() if k != "type"})
            if event["type"] == "started":
                state.started = now
            elif event["type"] == "confirmation_required":
                state.wait_started = now
            elif event["type"] == "confirmation_answered" and state.wait_started is not None:
                state.wait_seconds += now - state.wait_started
                state.wait_started = None
            state.events.append({"at": _now(), **event})
            _save_state(state)


class TurnState:
    def __init__(self, turn_id: str, conversation_id: str, run_id: str):
        self.id, self.conversation_id, self.run_id = turn_id, conversation_id, run_id
        self.started = None
        self.wait_started = None
        self.wait_seconds = 0.0
        self.status = "queued"
        self.events: list[dict] = []
        self.final = ""
        self.error = ""
        self.result: dict = {}
        self.pending_confirmation: dict | None = None
        self.confirm_event: threading.Event | None = None
        self.confirm_answer: str | None = None
        self.lock = threading.RLock()


TURN_STATES: dict[str, TurnState] = {}
ACTIVE_CONVERSATIONS: set[str] = set()
ACTIVE_LOCK = threading.Lock()
CONVERSATION_LOCKS: dict[str, threading.Lock] = {}
CONVERSATION_LOCKS_GUARD = threading.Lock()


def _clock(state):
    if state.status not in ("queued", "running", "waiting_confirmation"):
        return state.result
    now = time.perf_counter()
    elapsed = max(0, now - state.started) if state.started is not None else 0
    wait = state.wait_seconds + (now - state.wait_started if state.wait_started is not None else 0)
    return {"seconds": elapsed, "active_seconds": max(0, elapsed - wait), "wait_seconds": wait}


def _save_state(state: TurnState):
    with _db() as db:
        db.execute("""UPDATE turns SET status=?, updated_at=?, events_json=?, final=?, error=?, result_json=?
                     WHERE id=?""", (state.status, _now(), json.dumps(state.events, ensure_ascii=False),
                                        state.final, state.error, json.dumps(state.result, ensure_ascii=False),
                                        state.id))


def _conversation_lock(conversation_id: str) -> threading.Lock:
    with CONVERSATION_LOCKS_GUARD:
        return CONVERSATION_LOCKS.setdefault(conversation_id, threading.Lock())


def _get_conversation(conversation_id: str):
    with _db() as db:
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "会话不存在")
    return row


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class Confirmation(BaseModel):
    answer: Literal["y", "n", "a"]
    confirmation_id: str | None = None


def _run_turn(state: TurnState, instruction: str, history: list[dict]):
    with _conversation_lock(state.conversation_id):
        confirmer = None
        terminal_status = "failed"

        def ask_in_browser(request: dict) -> str:
            action = build_confirmation(request, Sandbox(cfg["sandbox"]))
            question = f"{action['title']}：{action['summary']}"
            state.confirm_event = threading.Event()
            state.confirm_answer = None
            state.pending_confirmation = {"id": uuid.uuid4().hex, "question": question, "action": action, "choices": ["y", "n", "a"]}
            state.status = "waiting_confirmation"
            _event(state.id, {"type": "confirmation_required", "question": question})
            answered = state.confirm_event.wait(timeout=900)
            answer = state.confirm_answer if answered else "n"
            state.pending_confirmation = None
            state.confirm_event = None
            state.status = "running"
            _event(state.id, {"type": "confirmation_answered", "answer": answer or "n"})
            return answer or "n"

        try:
            cfg = _config()
            state.status = "running"
            _event(state.id, {"type": "started", "message": "Agent 正在处理"})
            LOG_DIR.mkdir(exist_ok=True)
            confirmer = Confirmer(cfg.get("confirm", "risky"), cfg.get("confirm_threshold", 10),
                                  ask_action=ask_in_browser)
            sb = Sandbox(cfg['sandbox'])
            artifacts = _artifacts(state.conversation_id, sb, persist=True) if cfg.get('local_runtime') else None
            working_history, context_stats = prepare_history(history, cfg, artifacts)
            _event(state.id, {"type": "context_prepared", **context_stats})
            result = run_agent(
                instruction, LLM(cfg), sb, cfg,
                console=Console(file=io.StringIO()),
                trace_path=LOG_DIR / f"{state.run_id}.trace.jsonl",
                oplog=OpLog(Sandbox(cfg["sandbox"]), state.run_id), confirmer=confirmer,
                history=working_history, artifacts=artifacts,
                on_event=lambda event: _event(state.id, event),
            )
            state.final = result.final
            state.result = {"stop_reason": result.stop_reason, "steps": result.steps,
                            "tool_calls": result.tool_calls, "tokens": result.tokens,
                            "cost": result.cost, "seconds": result.seconds,
                            "violations": result.violations, "rejections": result.rejections,
                            "active_seconds": result.active_seconds, "wait_seconds": result.wait_seconds,
                            "llm_seconds": result.llm_seconds, "tool_seconds": result.tool_seconds,
                            "context": context_stats, "runtime": result.runtime}
            # Save the complete tool-call history for the next turn, while the UI gets a filtered view.
            with _db() as db:
                db.execute("UPDATE conversations SET history_json=?, updated_at=? WHERE id=?",
                           (json.dumps(history + result.messages[len(working_history):], ensure_ascii=False), _now(), state.conversation_id))
            terminal_status = "completed"
        except (Exception, SystemExit) as exc:
            state.result = {**_clock(state), **state.result}
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Publish terminal state and release the workspace activity guard together.
            # An immediate undo must wait for the final save, not race it or get a
            # spurious "still running" response after the UI reports completion.
            with ACTIVE_LOCK:
                state.status = terminal_status
                try:
                    _event(state.id, {"type": terminal_status,
                                      "message": "本轮已完成" if terminal_status == "completed" else state.error})
                finally:
                    try:
                        _save_state(state)
                    finally:
                        ACTIVE_CONVERSATIONS.discard(state.conversation_id)


app = FastAPI(title="FileButler Local", version="0.1.0", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.on_event("startup")
def startup():
    _init_db()
    # A restart cannot safely resume a partially executed tool loop.
    with _db() as db:
        db.execute("""UPDATE turns SET status='failed', error='服务重启，任务中断；如有写操作可尝试撤销', updated_at=?
                     WHERE status IN ('queued','running','waiting_confirmation')""", (_now(),))


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    cfg = _config()
    return {"status": "ok", "model": cfg.get("model"), "sandbox": Path(cfg["sandbox"]).name}


@app.get("/api/conversations")
def conversations():
    with _db() as db:
        rows = db.execute("SELECT id,title,created_at,updated_at FROM conversations ORDER BY updated_at DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/conversations")
def create_conversation():
    cid, now = uuid.uuid4().hex, _now()
    with _db() as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at,history_json) VALUES(?,?,?,?,?)",
                   (cid, "新对话", now, now, "[]"))
    return {"id": cid, "title": "新对话", "created_at": now, "updated_at": now}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    row = _get_conversation(conversation_id)
    history = json.loads(row["history_json"])
    display = []
    files = FileCatalog(Sandbox(_config()["sandbox"]), _artifacts(conversation_id), conversation_id)
    for message in history:
        if message.get("role") == "system" and message.get("content", "").startswith(UNDO_MARK):
            report = json.loads(message["content"].rsplit("\n", 1)[-1])
            label = "历史撤销记录（之后可能已有新操作）" if "历史撤销事件补录" in message["content"] else "撤销结果（跳过项未还原）"
            display.append({"role": "event", "content": label + "：\n" + "\n".join(report)})
        elif message.get("role") == "user":
            display.append({"role": "user", "content": message.get("content", "")})
        elif message.get("role") == "assistant" and not message.get("tool_calls") and message.get("content"):
            display.append({"role": "assistant", "content": message["content"],
                            "html": render_markdown(message["content"], files)})
    with _db() as db:
        latest = db.execute("SELECT id,status,run_id,final,error,result_json,events_json FROM turns WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                            (conversation_id,)).fetchone()
    active = None
    if latest:
        active = {"id": latest["id"], "status": latest["status"], "run_id": latest["run_id"],
                  "final": latest["final"], "error": latest["error"],
                  "events": json.loads(latest["events_json"]),
                  "result": json.loads(latest["result_json"])}
        state = TURN_STATES.get(latest["id"])
        if state:
            active["pending_confirmation"] = state.pending_confirmation
            active["events"] = state.events
            active["clock"] = _clock(state)
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "messages": display, "latest_turn": active}


@app.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: str, body: NewMessage):
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "消息不能为空")
    with ACTIVE_LOCK:
        if conversation_id in ACTIVE_CONVERSATIONS:
            raise HTTPException(409, "该对话已有任务运行中")
        row = _get_conversation(conversation_id)
        ACTIVE_CONVERSATIONS.add(conversation_id)

    state = None
    try:
        cfg = _config()
        history = json.loads(row["history_json"])
        # Backfill historical UI undos that predate history synchronization.
        with _db() as db:
            undone = db.execute("SELECT run_id,result_json,updated_at FROM turns WHERE conversation_id=? ORDER BY updated_at,rowid", (conversation_id,)).fetchall()
        for old in undone:
            report = json.loads(old["result_json"]).get("undo")
            if report is not None and not has_undo(history, old["run_id"]):
                history.append(undo_note(old["run_id"], report, at=old["updated_at"], historical=True))
        if not history:
            cfg = _config()
            prompt = cfg.get("prompt", "v1")
            if prompt not in SYSTEM_PROMPTS:
                raise HTTPException(500, f"未知系统提示词版本：{prompt}")
            history = [{"role": "system", "content": SYSTEM_PROMPTS[prompt]}]
        for message in history:
            if message.get("role") == "system":
                if WEB_PRESENTATION_GUIDANCE not in message["content"]:
                    message["content"] += WEB_PRESENTATION_GUIDANCE
                if LATENCY_GUIDANCE not in message["content"]:
                    message["content"] += LATENCY_GUIDANCE
                if cfg.get('local_runtime') and LOCAL_RUNTIME_GUIDANCE not in message['content']:
                    message['content'] += LOCAL_RUNTIME_GUIDANCE
                if cfg.get('local_runtime') and CLASSIFICATION_GUIDANCE not in message['content']:
                    message['content'] += CLASSIFICATION_GUIDANCE
                break
        history.append({"role": "user", "content": content})
        turn_id, run_id, now = uuid.uuid4().hex, datetime.now().strftime("%Y%m%d-%H%M%S-%f"), _now()
        state = TurnState(turn_id, conversation_id, run_id)
        TURN_STATES[turn_id] = state
        title = row["title"] if row["title"] != "新对话" else content[:42] + ("…" if len(content) > 42 else "")
        with _db() as db:
            db.execute("UPDATE conversations SET title=?, updated_at=?, history_json=? WHERE id=?",
                       (title, now, json.dumps(history, ensure_ascii=False), conversation_id))
            db.execute("INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (turn_id, conversation_id, run_id, "queued", now, now))
        POOL.submit(_run_turn, state, content, history)
        return {"turn_id": turn_id, "run_id": run_id, "status": "queued"}
    except Exception:
        try:
            if state is not None:
                state.status = "failed"
                state.error = "任务提交失败，请重试"
                _save_state(state)
        finally:
            with ACTIVE_LOCK:
                ACTIVE_CONVERSATIONS.discard(conversation_id)
        raise


@app.get("/api/turns/{turn_id}")
def get_turn(turn_id: str):
    state = TURN_STATES.get(turn_id)
    if state:
        with state.lock:
            return {"id": state.id, "conversation_id": state.conversation_id, "run_id": state.run_id,
                    "status": state.status, "events": state.events, "pending_confirmation": state.pending_confirmation,
                    "final": state.final, "error": state.error, "result": state.result, "clock": _clock(state)}
    with _db() as db:
        row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not row:
        raise HTTPException(404, "运行记录不存在")
    return {"id": row["id"], "conversation_id": row["conversation_id"], "run_id": row["run_id"],
            "status": row["status"], "events": json.loads(row["events_json"]),
            "final": row["final"], "error": row["error"], "result": json.loads(row["result_json"])}


@app.post("/api/turns/{turn_id}/confirm")
def confirm_turn(turn_id: str, body: Confirmation):
    state = TURN_STATES.get(turn_id)
    if not state:
        raise HTTPException(409, "当前运行没有等待确认")
    with state.lock:
        if state.status != "waiting_confirmation" or not state.confirm_event or state.confirm_event.is_set():
            raise HTTPException(409, "当前运行没有等待确认")
        if body.confirmation_id and body.confirmation_id != (state.pending_confirmation or {}).get("id"):
            raise HTTPException(409, "该确认请求已过期，请确认当前显示的操作")
        state.confirm_answer = body.answer
        state.confirm_event.set()
    return {"ok": True}


@app.post("/api/turns/{turn_id}/undo")
def undo_turn(turn_id: str):
    with _db() as db:
        row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not row:
        raise HTTPException(404, "运行记录不存在")
    if row["status"] not in ("completed", "failed"):
        raise HTTPException(409, "运行尚未结束，暂时不能撤销")
    # All conversations share one sandbox. No undo may race an active run.
    with ACTIVE_LOCK:
        if ACTIVE_CONVERSATIONS:
            raise HTTPException(409, "仍有任务运行中，请结束或完成确认后再撤销")
        with _db() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            result = json.loads(row["result_json"])
            if "undo" in result:
                return {"run_id": row["run_id"], "report": result["undo"]}
            restored = []
            report = undo(Sandbox(_config()["sandbox"]), row["run_id"], on_restore=lambda src,dst: restored.append((src,dst)))
            result["undo"] = report
            db.execute("UPDATE turns SET result_json=?, updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), _now(), turn_id))
            # Synchronize every existing conversation because filesystem state is shared.
            for conv in db.execute("SELECT id,history_json,artifacts_json FROM conversations").fetchall():
                artifacts = ArtifactStore(Sandbox(_config()['sandbox']), json.loads(conv['artifacts_json']))
                for src, dst in restored:
                    artifacts.relocate(src, dst)
                db.execute('UPDATE conversations SET artifacts_json=? WHERE id=?',
                           (json.dumps(artifacts.export(), ensure_ascii=False), conv['id']))
                history = json.loads(conv["history_json"])
                if history and not has_undo(history, row["run_id"]):
                    history.append(undo_note(row["run_id"], report, at=_now()))
                    db.execute("UPDATE conversations SET history_json=?,updated_at=? WHERE id=?",
                               (json.dumps(history, ensure_ascii=False), _now(), conv["id"]))
        state = TURN_STATES.get(turn_id)
        if state:
            with state.lock:
                state.result = result
    return {"run_id": row["run_id"], "report": report}


@app.get("/api/turns/{turn_id}/trace")
def turn_trace(turn_id: str):
    with _db() as db:
        row = db.execute("SELECT run_id FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not row:
        raise HTTPException(404, "运行记录不存在")
    path = LOG_DIR / f"{row['run_id']}.trace.jsonl"
    items, requests = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "llm_request":
                requests.append(event)
            if "tool" not in event:
                continue
            try:
                args = json.loads(event.get("args", "{}"))
            except json.JSONDecodeError:
                args = event.get("args", "")
            items.append({"step": event.get("step"), "tool": event["tool"], "args": args,
                          "ok": event.get("ok"), "ms": event.get("ms"),
                          "active_ms": event.get("active_ms"), "wait_ms": event.get("wait_ms"),
                          "repeat_count": event.get("repeat_count", 0),
                          "executed": event.get("executed", True), "result": event.get("result", "")})
    return {"items": items, "requests": requests}


def _visible_path(raw: str) -> Path:
    requested = Path(raw)
    if requested.is_absolute():
        raise HTTPException(400, "请使用相对沙盒的路径")
    parts = requested.parts
    if any(part.startswith(".") for part in parts if part not in ("", ".")):
        raise HTTPException(404, "文件不存在")
    sb = Sandbox(_config()["sandbox"])
    # Reject symlink traversal before Sandbox.path resolves it to the target.
    cursor = sb.root
    for part in parts:
        if part in ("", "."):
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise HTTPException(404, "文件不存在")
    try:
        path = sb.path(raw)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists() or not path.is_relative_to(sb.root) or path.is_symlink():
        raise HTTPException(404, "文件不存在")
    return path


@app.get("/api/files")
def list_files(path: str = "."):
    directory = _visible_path(path)
    if not directory.is_dir():
        raise HTTPException(400, "目标不是文件夹")
    sb = Sandbox(_config()["sandbox"])
    entries = []
    for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        if is_hidden(item) or item.is_symlink():
            continue
        stat = item.stat()
        entries.append({"name": item.name, "path": sb.rel(item), "type": "directory" if item.is_dir() else "file",
                        "size": None if item.is_dir() else stat.st_size,
                        "size_text": "—" if item.is_dir() else _fmt_size(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="minutes"),
                        "previewable": item.is_dir() or item.suffix.lower() in TEXT_EXTS | IMAGE_EXTS | {".pdf"}})
    return {"path": sb.rel(directory), "parent": sb.rel(directory.parent) if directory != sb.root else None,
            "items": entries}


@app.get("/api/files/preview")
def preview_file(path: str, page: int | None = None):
    return _preview(_visible_path(path), path)


def _preview(file, path):
    if file.is_dir():
        raise HTTPException(400, "目标是文件夹")
    ext = file.suffix.lower()
    if ext not in TEXT_EXTS:
        stat = file.stat()
        return {"path": path, "type": "pdf" if ext == ".pdf" else "image" if ext in IMAGE_EXTS else "download",
                "size": stat.st_size, "size_text": _fmt_size(stat.st_size)}
    raw = file.read_bytes()
    content = None
    for encoding in ("utf-8", "gbk"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if content is None:
        raise HTTPException(415, "无法识别文件编码")
    return {"path": path, "type": "text", "content": content[:MAX_PREVIEW_CHARS],
            "truncated": len(content) > MAX_PREVIEW_CHARS, "chars": len(content)}


@app.get("/api/files/raw")
def raw_file(path: str):
    return _raw(_visible_path(path))


def _raw(file):
    if file.is_dir():
        raise HTTPException(400, "目标是文件夹")
    media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    inline = file.suffix.lower() in IMAGE_EXTS | {".pdf"}
    disposition = "inline" if inline else "attachment"
    # Keep paths out of response headers; RFC 5987 escaping handles Unicode names.
    from urllib.parse import quote
    encoded = quote(file.name, safe="")
    return FileResponse(file, media_type=media_type,
                        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded}",
                                 "X-Content-Type-Options": "nosniff"})


def _artifact_file(conversation_id, file_id):
    try:
        return _artifacts(conversation_id).resolve(file_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get('/api/conversations/{conversation_id}/files/{file_id}/preview')
def artifact_preview(conversation_id: str, file_id: str):
    file, row = _artifact_file(conversation_id, file_id)
    return {**_preview(file, row['path']), 'name': row['name'], 'status': row['status']}


@app.get('/api/conversations/{conversation_id}/files/{file_id}/raw')
def artifact_raw(conversation_id: str, file_id: str):
    file, _ = _artifact_file(conversation_id, file_id)
    return _raw(file)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="127.0.0.1", port=8000, reload=False)
