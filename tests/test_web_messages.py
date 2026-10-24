"""Web messages through receipts (routing redesign S2, docs/agent-routing-redesign-2026-10-24.md 4.4, 13).
Fake models and temporary files only."""
import threading
import uuid

from agent.llm import LLM
from tests.helpers import Replay, call, step
from tests.test_web_ui import files, ui  # noqa: F401  (fixtures)
import webapp

ASK = dict(question='notes.md 怎么处理？', reason='multiple_candidates', subject=['notes.md'], options=[
    dict(label='保留原样', actions=[]), dict(label='移到 归档', actions=[dict(effect='move', path='notes.md', destination='文献')])])


def scripted(monkeypatch, *replays):
    """Each run gets the next replay; returns (counter of model clients made, finished events)."""
    made, finished = [], []
    queue = list(replays)
    def llm(cfg):
        made.append(cfg)
        return LLM({"model": "fake"}, client=queue.pop(0))
    monkeypatch.setattr(webapp, "LLM", llm)
    original = webapp._event
    def notify(turn_id, event):
        original(turn_id, event)
        if event["type"] in ("completed", "failed"):
            finished.append(turn_id)
    monkeypatch.setattr(webapp, "_event", notify)
    return made, finished


def wait_for(finished, count, timeout=5):
    done = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if len(finished) >= count: return True
        done.wait(0.02)
    return False


def post(ui, cid, content, message_id=None, response=None):
    body = dict(content=content, client_message_id=message_id or uuid.uuid4().hex)
    if response: body['response'] = response
    return ui.post(f"/api/conversations/{cid}/messages", json=body)


def test_a_page_without_message_ids_is_asked_to_reload(ui):
    cid = ui.post("/api/conversations").json()["id"]
    reply = ui.post(f"/api/conversations/{cid}/messages", json={"content": "你好"})
    assert reply.status_code == 426 and '刷新' in reply.json()['detail']


def test_a_retried_message_returns_its_turn_and_runs_once(ui, monkeypatch):
    made, finished = scripted(monkeypatch, Replay([], final='你好。'))
    cid = ui.post("/api/conversations").json()["id"]
    first = post(ui, cid, '你好', 'message-1').json()
    assert wait_for(finished, 1)
    again = post(ui, cid, '你好', 'message-1')
    assert again.status_code == 202 and again.json()['turn_id'] == first['turn_id'] and again.json()['existing']
    assert post(ui, cid, '你好！', 'message-1').status_code == 409  # same id, changed text
    assert len(made) == 1 and len(ui.get(f"/api/conversations/{cid}").json()['messages']) == 2


def test_a_button_sends_the_exact_option_and_a_stale_one_is_refused(ui, monkeypatch):
    made, finished = scripted(monkeypatch, Replay([step(call('ask_user', ASK))]), Replay([], final='好的，保留原样。'))
    cid = ui.post("/api/conversations").json()["id"]
    post(ui, cid, '整理 notes.md')
    assert wait_for(finished, 1)
    asked = ui.get(f"/api/conversations/{cid}").json()['messages'][-1]['report']
    assert asked['question_id'].startswith('Q') and all(c['option_id'].startswith('O') for c in asked['choices'])
    keep = dict(question_id=asked['question_id'], version=asked['question_version'], option_id=asked['choices'][0]['option_id'])
    answer = post(ui, cid, '选择 A：保留原样', 'answer-1', keep)
    assert answer.status_code == 202 and wait_for(finished, 2)
    assert made[1]['answer_id'].startswith('A')  # the run was told exactly which option
    # The same answer from a new message: the question is already answered.
    assert post(ui, cid, '选择 A：保留原样', 'answer-2', keep).status_code == 409
    # Its retry with the original id is the original receipt, not a third run.
    assert post(ui, cid, '选择 A：保留原样', 'answer-1', keep).json()['existing'] and len(made) == 2


def test_a_message_accepted_before_a_crash_gets_its_turn_once_after_restart(ui, monkeypatch, files):
    made, finished = scripted(monkeypatch, Replay([], final='恢复后的回答。'))
    cid = ui.post("/api/conversations").json()["id"]
    # Received and committed, then the process stopped before the web turn was made.
    webapp._interactions(cid).accept('before-crash', '列出文件')
    webapp._recover_outbox()
    assert wait_for(finished, 1) and len(made) == 1
    webapp._recover_outbox()  # nothing left to recover
    assert len(made) == 1
    assert post(ui, cid, '列出文件', 'before-crash').json()['existing']


def test_stop_while_a_card_waits_interrupts_it_and_writes_nothing(ui, monkeypatch, files):
    from tests.test_web_ui import make_folder_plan
    waiting = threading.Event()
    made, finished = scripted(monkeypatch, make_folder_plan('不应出现'))
    original = webapp._event
    def notify(turn_id, event):
        original(turn_id, event)
        if event["type"] == "confirmation_required": waiting.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    turn = post(ui, cid, '新建 blocked').json()
    assert waiting.wait(4)
    reply = ui.post(f"/api/conversations/{cid}/cancel", json={"client_message_id": "stop-press-1"})
    stopped = reply.json()
    assert reply.status_code == 200, stopped
    assert stopped["status"] == "cancelled" and stopped["running"]
    assert wait_for(finished, 1)
    done = ui.get(f"/api/turns/{turn['turn_id']}").json()
    assert done["result"]["stop_reason"] == "cancelled" and done["final"].startswith("已取消")
    assert not (files.root / "blocked").exists()
    # The same press again is the same stop; a new message after it is a new request.
    assert ui.post(f"/api/conversations/{cid}/cancel", json={"client_message_id": "stop-press-1"}).json()["status"] == "cancelled"
