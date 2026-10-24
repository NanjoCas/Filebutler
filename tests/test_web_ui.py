"""Web rendering and live step events; fake models, temporary files, no API spend."""
import uuid
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
    assert result.stop_reason == "answered"
    assert [e["type"] for e in events] == ["llm_start", "llm_end", "tool_start", "tool_end", "llm_start", "llm_end"]
    assert events[2]["call_id"] == events[3]["call_id"]
    assert events[2]["args"] == {"path": "notes.md"} and events[3]["ok"]
    assert not any("reasoning_content" in e for e in events)


def test_broken_status_observer_does_not_interrupt_agent(files):
    def broken(event):
        raise RuntimeError("display disconnected")
    result = run_agent("完成", LLM({"model": "fake"}, client=FakeClient(["完成"])), files,
                       {}, Console(quiet=True), on_event=broken)
    assert result.final == "完成" and result.stop_reason == "answered"


def test_task_api_persists_revisions_and_isolates_conversations(ui, files):
    from agent.task import TaskStore, TaskSubmission
    a = ui.post('/api/conversations').json()['id']
    b = ui.post('/api/conversations').json()['id']
    store = TaskStore(files.root / '.oplog' / 'tasks.sqlite3', files.root, a)
    spec = TaskSubmission(goals=[dict(id='g', description='核对笔记',
                                      conditions=[dict(kind='manual_review')])])
    task = store.submit(spec, '核对笔记', 'run-a')
    assert ui.get(f'/api/conversations/{a}/tasks').json()['items'][0]['id'] == task['id']
    assert ui.get(f'/api/conversations/{a}/tasks/{task["id"]}').json()['revision'] == 1
    assert ui.get(f'/api/conversations/{b}/tasks').json()['items'] == []
    assert ui.get(f'/api/conversations/{b}/tasks/{task["id"]}').status_code == 404
    assert ui.get('/api/conversations/missing/tasks').status_code == 404


def test_task_editor_api_previews_applies_and_reviews_without_model(ui, files):
    from agent.task import TaskSubmission
    cid=ui.post('/api/conversations').json()['id']; store=webapp._tasks(cid)
    task=store.submit(TaskSubmission(goals=[dict(id='g',description='核对记录',conditions=[dict(kind='manual_review')])]),'原始请求','r1')
    base=f'/api/conversations/{cid}/tasks/{task["id"]}'
    preview=ui.post(base+'/edits',json=dict(expected_revision=1,write_roots=['文献'],reason='调整范围'))
    assert preview.status_code==200 and preview.json()['changes']
    assert ui.get(base).json()['revision']==1
    assert ui.post(base+'/edits',json=dict(expected_revision=1,write_roots=['../outside'],reason='越界')).status_code==409
    edit_id=preview.json()['id']
    applied=ui.post(base+'/edits/apply',json=dict(edit_id=edit_id))
    assert applied.status_code==200 and applied.json()['revision']==2
    assert ui.post(base+'/edits/apply',json=dict(edit_id=edit_id)).status_code==409
    accepted=ui.post(base+'/review',json=dict(expected_revision=2,goal_ids=['g'],decision='accept',note='已核对'))
    assert accepted.status_code==200 and accepted.json()['status']=='completed'
    assert ui.post(base+'/review',json=dict(expected_revision=1,goal_ids=['g'],decision='accept')).status_code==409
    other=ui.post('/api/conversations').json()['id']
    assert ui.post(f'/api/conversations/{other}/tasks/{task["id"]}/edits/apply',json=dict(edit_id=edit_id)).status_code==409
    webapp.ACTIVE_CONVERSATIONS.add(cid)
    try:
        assert ui.post(base+'/edits',json=dict(expected_revision=2,cancel=True,reason='运行中修改')).status_code==409
    finally:webapp.ACTIVE_CONVERSATIONS.discard(cid)


def test_historical_verbose_report_is_only_compacted_for_display(ui):
    cid=ui.post('/api/conversations').json()['id']
    original='部分结果待核实\n\n| 类型 | 数量 |\n| --- | --- |\n| pdf | 126 |\n\n模型说明（不替代上述核验）：\n已移动文件并清理空目录。'
    history=[{'role':'user','content':'归集资料'},{'role':'assistant','content':original}]
    result={'runtime':{'task':'structured','task_status':'needs_review'}}
    with webapp._db() as db:
        db.execute('UPDATE conversations SET history_json=? WHERE id=?',(json.dumps(history),cid))
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at,final,result_json) VALUES(?,?,?,?,?,?,?,?)',
                   ('legacy',cid,'old-run','completed',webapp._now(),webapp._now(),original,json.dumps(result)))
    reply=ui.get(f'/api/conversations/{cid}').json()['messages'][-1]
    assert '|' not in reply['content'] and '待核实状态保留' in reply['content']
    assert reply['report']['state']=='needs_review'
    assert '<table>' in reply['report']['sections'][0]['html']
    with webapp._db() as db:
        row=db.execute('SELECT final,result_json FROM turns WHERE id=?',('legacy',)).fetchone()
        assert row['final']==original and json.loads(row['result_json'])==result
        assert json.loads(db.execute('SELECT history_json FROM conversations WHERE id=?',(cid,)).fetchone()[0])==history


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
    turn = ui.post(f"/api/conversations/{cid}/messages", json={"client_message_id": uuid.uuid4().hex, "content": "查看笔记"}).json()
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
    # The per-run task snapshot is rebuilt next run, never stored in the conversation.
    from agent.prompts import GUIDANCE_MARK
    assert not any(m["content"].startswith(GUIDANCE_MARK) for m in history if m["role"] == "system")


def make_folder_plan(final):
    """A v3 run whose file plan (new folder "blocked") stops at the confirmation."""
    from tests.helpers import Replay, call, step
    return Replay([step(call('start_task', dict(summary='建目录', parts=[dict(kind='organize', description='新建 blocked')])),
                        call('prepare_file_plan', dict(description='新建 blocked', actions=[dict(effect='create', destination='blocked')]),
                             alias='Xaaaaaaaaaaaa')),
                   step(call('execute_file_plan', dict(plan_id='Xaaaaaaaaaaaa')))], final=final)


def test_ui_confirmation_remains_interactive_with_live_steps(ui, monkeypatch, files):
    waiting, finished = threading.Event(), threading.Event()
    fake = make_folder_plan("操作已拒绝。")
    monkeypatch.setattr(webapp, "LLM", lambda cfg: LLM({"model": "fake"}, client=fake))
    original_event = webapp._event
    def notify(turn_id, event):
        original_event(turn_id, event)
        if event["type"] == "confirmation_required": waiting.set()
        if event["type"] in ("completed", "failed"): finished.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    tid = ui.post(f"/api/conversations/{cid}/messages", json={"client_message_id": uuid.uuid4().hex, "content": "创建目录"}).json()["turn_id"]
    assert waiting.wait(4)
    turn = ui.get(f"/api/turns/{tid}").json()
    assert turn["pending_confirmation"] and turn["events"][-2]["type"] == "tool_start"
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "y", "confirmation_id": "expired"}).status_code == 409
    confirmation_id = turn["pending_confirmation"]["id"]
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "n", "confirmation_id": confirmation_id}).status_code == 200
    assert finished.wait(5)
    # A retry whose first response was lost gets the same decision; "y" cannot replace it.
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "n", "confirmation_id": confirmation_id}).json()["answer"] == "n"
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "y", "confirmation_id": confirmation_id}).status_code == 409
    assert not (files.root / "blocked").exists()
    assert ui.get(f"/api/turns/{tid}").json()["status"] == "completed"


def _seed_turn(cid, run_id, status='completed'):
    with webapp._db() as db:
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('t-' + run_id, cid, run_id, status, webapp._now(), webapp._now()))
        db.execute('UPDATE conversations SET history_json=?, artifacts_json=? WHERE id=?',
                   (json.dumps([{'role': 'user', 'content': run_id}]), json.dumps({'files': {}}), cid))


def test_delete_conversation_removes_only_its_ui_state(ui, files):
    from agent.task import TaskStore, TaskSubmission
    a = ui.post('/api/conversations').json()['id']
    b = ui.post('/api/conversations').json()['id']
    # A real move in each conversation, so both have an undo log in the shared .oplog.
    (files.root / 'a.txt').write_text('A', encoding='utf-8'); (files.root / 'b.txt').write_text('B', encoding='utf-8')
    (files.root / '归档').mkdir()
    from safety.oplog import OpLog
    for cid, run, name in ((a, 'run-a', 'a.txt'), (b, 'run-b', 'b.txt')):
        log = OpLog(files, run); log.record('move', name, f'归档/{name}')
        (files.root / name).rename(files.root / '归档' / name)
        _seed_turn(cid, run)
    tasks = files.root / '.oplog' / 'tasks.sqlite3'
    TaskStore(tasks, files.root, a).submit(TaskSubmission(goals=[dict(id='g', description='核对',
        conditions=[dict(kind='manual_review')])]), '核对', 'run-a')
    (files.root / '.trash').mkdir(exist_ok=True); (files.root / '.trash' / 'kept.txt').write_text('x')
    cache = files.root / '.oplog' / 'document-cache.sqlite3'; cache.write_bytes(b'cache')
    oplog_before = {p.name: p.read_bytes() for p in (files.root / '.oplog').iterdir()}
    tasks_before = tasks.read_bytes()
    response = ui.delete(f'/api/conversations/{a}')
    assert response.status_code == 200
    assert response.json() == {'id': a, 'deleted_turns': 1, 'undoable_run_ids': ['run-a']}
    assert ui.get(f'/api/conversations/{a}').status_code == 404
    assert [c['id'] for c in ui.get('/api/conversations').json()['items']] == [b]
    with webapp._db() as db:
        assert db.execute('SELECT COUNT(*) FROM turns WHERE conversation_id=?', (a,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM turns WHERE conversation_id=?', (b,)).fetchone()[0] == 1
    # Shared workspace records and user files are untouched.
    assert {p.name: p.read_bytes() for p in (files.root / '.oplog').iterdir()} == oplog_before
    assert tasks.read_bytes() == tasks_before and cache.read_bytes() == b'cache'
    assert (files.root / '.trash' / 'kept.txt').exists() and (files.root / '归档' / 'a.txt').exists()
    assert ui.post('/api/turns/t-run-a/undo').status_code == 404
    # The other conversation keeps its history and can still undo its own run.
    assert ui.get(f'/api/conversations/{b}').json()['messages'][0]['content'] == 'run-b'
    undone = ui.post('/api/turns/t-run-b/undo')
    assert undone.status_code == 200 and (files.root / 'b.txt').exists()
    assert (files.root / '.oplog' / 'run-a.jsonl').exists()  # still undoable from the CLI
    assert ui.delete(f'/api/conversations/{a}').status_code == 404


def test_delete_unknown_conversation_is_404(ui):
    assert ui.delete('/api/conversations/missing').status_code == 404


def test_delete_refuses_running_or_pending_confirmation(ui, monkeypatch, files):
    waiting, finished = threading.Event(), threading.Event()
    fake = make_folder_plan("操作已拒绝。")
    monkeypatch.setattr(webapp, "LLM", lambda cfg: LLM({"model": "fake"}, client=fake))
    original_event = webapp._event
    def notify(turn_id, event):
        original_event(turn_id, event)
        if event["type"] == "confirmation_required": waiting.set()
        if event["type"] in ("completed", "failed"): finished.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    other = ui.post("/api/conversations").json()["id"]
    tid = ui.post(f"/api/conversations/{cid}/messages", json={"client_message_id": uuid.uuid4().hex, "content": "创建目录"}).json()["turn_id"]
    assert waiting.wait(4)
    refused = ui.delete(f"/api/conversations/{cid}")
    assert refused.status_code == 409 and "等待确认" in refused.json()["detail"]
    # The pending request is still answerable: nothing was cancelled or left dangling.
    pending = ui.get(f"/api/turns/{tid}").json()["pending_confirmation"]
    assert pending and ui.get(f"/api/conversations/{cid}").status_code == 200
    # Another, idle conversation can be deleted while this one waits.
    assert ui.delete(f"/api/conversations/{other}").status_code == 200
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "n", "confirmation_id": pending["id"]}).status_code == 200
    assert finished.wait(5)
    for _ in range(50):
        if cid not in webapp.ACTIVE_CONVERSATIONS: break
        threading.Event().wait(0.05)
    assert ui.delete(f"/api/conversations/{cid}").status_code == 200
    assert tid not in webapp.TURN_STATES
    assert ui.get(f"/api/turns/{tid}").status_code == 404
    assert not (files.root / "blocked").exists()


def test_delete_refuses_queued_turn_and_blocks_new_messages_after(ui):
    cid = ui.post('/api/conversations').json()['id']
    webapp.ACTIVE_CONVERSATIONS.add(cid)
    try:
        assert ui.delete(f'/api/conversations/{cid}').status_code == 409
    finally:
        webapp.ACTIVE_CONVERSATIONS.discard(cid)
    assert ui.delete(f'/api/conversations/{cid}').status_code == 200
    assert ui.post(f'/api/conversations/{cid}/messages', json={'client_message_id': uuid.uuid4().hex, 'content': '还在吗'}).status_code == 404
    assert ui.get(f'/api/conversations/{cid}/tasks').status_code == 404


def test_conversation_render_cache_tracks_file_changes(ui, files):
    cid = ui.post('/api/conversations').json()['id']
    with webapp._db() as db:
        db.execute('UPDATE conversations SET history_json=? WHERE id=?',
                   (json.dumps([{'role': 'assistant', 'content': '见 [笔记](sandbox:/notes.md)'}]), cid))
    first = ui.get(f'/api/conversations/{cid}').json()['messages'][0]['html']
    assert 'data-file-path="notes.md"' in first
    assert ui.get(f'/api/conversations/{cid}').json()['messages'][0]['html'] == first
    (files.root / 'notes.md').rename(files.root / 'moved.md')
    after = ui.get(f'/api/conversations/{cid}').json()['messages'][0]['html']
    assert 'file-reference' not in after and 'unavailable-reference' in after
