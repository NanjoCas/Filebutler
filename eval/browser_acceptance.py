"""Real-browser acceptance of the web page (routing redesign S8, 13). No paid model.

    .venv/bin/python -m eval.browser_acceptance --out eval/results/browser_acceptance_<date> [--headed]

Starts, in temporary folders only: a scripted OpenAI-compatible model server, the
web app (FILEBUTLER_HOME points it at a temporary config, state and library, so
the real sandbox/ and .filebutler-ui/ are never touched) and Google Chrome with a
fresh profile. Each scenario is driven like a user: mouse clicks at the element's
position and typed text through the DevTools protocol (Input.*), page reloads and
network loss through the browser itself. Checks read the page, the app's API and
the library on disk. Screenshots and a results.json go to --out (must not exist).

The model is scripted, so this tests the page, the server and the local runtime,
not the model's choices; those are the live suites' job (eval/live_policy.py).
"""
import argparse
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


# --------------------------------------------------------------------------- DevTools protocol

class WebSocket:
    """A minimal RFC 6455 client: text frames, masking, fragments, ping/pong."""

    def __init__(self, url):
        host, port, path = re.match(r'ws://([^:/]+):(\d+)(/.*)', url).groups()
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += self.sock.recv(1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise RuntimeError('WebSocket handshake failed: ' + head.decode(errors='replace'))

    def _read(self, n):
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk: raise ConnectionError('DevTools connection closed')
            data += chunk
        return data

    def _frame(self, op, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n)
                                     if n < 65536 else bytes([0x80 | 127]) + struct.pack('>Q', n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode())

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7f
            if n == 126: n = struct.unpack('>H', self._read(2))[0]
            elif n == 127: n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0f
            if op == 0x9: self._frame(0xA, data); continue
            if op == 0x8: raise ConnectionError('DevTools connection closed')
            if op in (0x1, 0x0): parts.append(data)
            if b0 & 0x80 and parts: return b''.join(parts).decode()


class Page:
    """One Chrome tab driven through the DevTools protocol."""

    def __init__(self, ws_url):
        self.ws, self.next_id, self.events = WebSocket(ws_url), 0, []
        for domain in ('Page', 'Runtime', 'Network'):
            self.call(f'{domain}.enable')

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps(dict(id=self.next_id, method=method, params=params)))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.next_id:
                if 'error' in msg: raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get('result', {})
            self.events.append(msg)

    def js(self, expression):
        out = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if out.get('exceptionDetails'): raise RuntimeError(f"JS error in {expression[:80]}: {out['exceptionDetails']}")
        return out['result'].get('value')

    def wait(self, expression, timeout=20, what=None):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                value = self.js(expression)
            except RuntimeError:
                value = None
            if value: return value
            time.sleep(0.15)
        raise TimeoutError(what or expression)

    def click(self, selector, index=0):
        """A real mouse click at the element's centre (pressed, released), as a user does."""
        box = self.wait(f"""(() => {{ const el = document.querySelectorAll({json.dumps(selector)})[{index}];
            if (!el || el.disabled) return null; el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect(); return r.width && r.height ? [r.x + r.width / 2, r.y + r.height / 2] : null; }})()""",
                        what=f'clickable {selector}[{index}]')
        for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', type=kind, x=box[0], y=box[1], button='left', clickCount=1)

    def type(self, selector, text):
        self.click(selector)
        self.call('Input.insertText', text=text)

    def press_enter(self):
        for kind in ('keyDown', 'keyUp'):
            self.call('Input.dispatchKeyEvent', type=kind, key='Enter', code='Enter', windowsVirtualKeyCode=13,
                      **({'text': '\r'} if kind == 'keyDown' else {}))

    def reload(self):
        self.call('Page.reload', ignoreCache=True)
        time.sleep(0.3)
        self.wait("document.readyState === 'complete' && !!document.querySelector('#conversation-list .conversation-row')",
                  what='page reloaded')

    def offline(self, on):
        self.call('Network.emulateNetworkConditions', offline=on, latency=0, downloadThroughput=-1, uploadThroughput=-1)

    def screenshot(self, path):
        Path(path).write_bytes(base64.b64decode(self.call('Page.captureScreenshot', format='png')['data']))

    def text(self, selector='#messages'):
        return self.js(f"(document.querySelector({json.dumps(selector)}) || {{}}).innerText || ''")


# --------------------------------------------------------------------------- scripted model

ASK = dict(mode='ask', question='notes.md 要怎么处理？', reason='multiple_candidates', subject=['notes.md'], options=[
    dict(label='保留原样', actions=[]), dict(label='移到 文献', actions=[dict(effect='move', path='notes.md', destination='文献')])])


def folder_plan(name):
    return [[('start_task', dict(summary=f'新建 {name}', parts=[dict(kind='organize', description=f'新建 {name} 文件夹')])),
             ('prepare_file_plan', dict(description=f'新建 {name}', actions=[dict(effect='create', destination=name)]))],
            [('execute_file_plan', dict(plan_id='{plan_id}'))],
            [('finish_turn', dict(mode='task', summary=f'已新建 {name}'))]]


def reply(text):
    return [[('finish_turn', dict(mode='reply', reply=text))]]


# marker in the first user message -> turn number (1-based) -> steps; a step is a list of
# calls, or a dict with sleep (seconds before answering) / fail (HTTP status, once).
SCRIPTS = {
    '验收1': {1: [[('finish_turn', ASK)]], 2: reply('好的，保持原样。')},
    '验收2': {1: folder_plan('blocked')},
    '验收3': {1: folder_plan('approved')},
    '验收4': {1: [dict(sleep=5, calls=[('finish_turn', dict(mode='reply', reply='慢回答完成。'))])]},
    '验收5': {1: reply('断网后重发的回答。')},
    '验收6': {1: [dict(fail=500, calls=[('finish_turn', dict(mode='reply', reply='重试后回答。'))])]},
    '验收7': {
        1: [[('start_task', dict(summary='移动 notes', parts=[dict(id='notes', kind='organize', description='移动 notes.md')])),
             ('finish_turn', dict(ASK, question='notes.md 要移到哪里？', reason='missing_criterion'))]],
        2: [[('prepare_file_plan', dict(task_id='{task_id}', description='移动 notes', input_resolution=dict(kind='answer'),
                    actions=[dict(effect='move', source='notes.md', destination='文献/notes-v2.md')]))],
            [('execute_file_plan', dict(plan_id='{plan_id}'))],
            [('finish_turn', dict(mode='task', summary='已移动 notes'))]],
    },
    '验收9': {1: [[('start_task', dict(summary='回收测试', parts=[dict(id='trash', kind='organize', description='回收测试文件夹')])),
                  ('prepare_file_plan', dict(description='回收测试', actions=[dict(effect='trash', source='测试')]))],
                 [('execute_file_plan', dict(plan_id='{plan_id}'))],
                 [('finish_turn', dict(mode='task', summary='已回收测试文件夹'))]]},
    '验收8': {1: [[('start_task', dict(summary='回答 notes 内容', parts=[dict(id='ans', kind='answer', description='说明 notes.md 写了什么')]))],
                 [('finish_turn', dict(mode='task', summary='notes.md 写的是测试内容。'))]]},
}


class FakeModel:
    def __init__(self):
        self.requests, self.failed, self.lock = [], set(), threading.Lock()

    def respond(self, body):
        messages = body['messages']
        users = [i for i, m in enumerate(messages) if m['role'] == 'user']
        first = messages[users[0]]['content'] if users else ''
        marker = next((m for m in SCRIPTS if m in first), None)
        turn = len(users)
        index = sum(m['role'] == 'assistant' for m in messages[users[-1] + 1:]) if users else 0
        with self.lock:
            self.requests.append(dict(marker=marker, turn=turn, step=index, at=time.time()))
        steps = (SCRIPTS.get(marker) or {}).get(turn) or reply('（脚本外的请求）')
        step = steps[index] if index < len(steps) else [('finish_turn', dict(mode='reply', reply='完成。'))]
        if isinstance(step, dict):
            key = (marker, turn, index)
            if step.get('fail') and key not in self.failed:
                self.failed.add(key)
                return step['fail'], dict(error=dict(message='scripted failure', type='server_error'))
            time.sleep(step.get('sleep', 0))
            step = step['calls']
        tools = '\n'.join(m.get('content') or '' for m in messages if m['role'] == 'tool')
        plan = (re.findall(r'plan_id=(X[0-9a-f]{12})', tools) or ['X000000000000'])[-1]
        task = (re.findall(r'task_id=(T[0-9a-f]{12})', tools) or
                re.findall(r'T[0-9a-f]{12}', '\n'.join(m.get('content') or '' for m in messages)) or ['T000000000000'])[-1]
        calls = [dict(id=f'call_{turn}_{index}_{n}', type='function',
                      function=dict(name=name, arguments=json.dumps(args, ensure_ascii=False)
                                    .replace('{plan_id}', plan).replace('{task_id}', task)))
                 for n, (name, args) in enumerate(step)]
        return 200, dict(id='fake', object='chat.completion', created=int(time.time()), model=body.get('model'),
                         choices=[dict(index=0, finish_reason='tool_calls',
                                       message=dict(role='assistant', content='', tool_calls=calls))],
                         usage=dict(prompt_tokens=100, completion_tokens=10, total_tokens=110))

    def count(self, marker, turn=None, step=None):
        return sum(r['marker'] == marker and (turn is None or r['turn'] == turn) and (step is None or r['step'] == step)
                   for r in self.requests)


def serve_model(model, port):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            status, data = model.respond(body)
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- the app, Chrome, scenarios

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def wait_http(url, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try: return get(url)
        except (urllib.error.URLError, ConnectionError, OSError): time.sleep(0.2)
    raise TimeoutError(url)


class Run:
    def __init__(self, page, app, home, out, model):
        self.page, self.app, self.home, self.out, self.model = page, app, home, out, model
        self.library = home / 'library'

    def new_chat(self):
        before = self.page.js("document.querySelector('#conversation-title').dataset.cid || ''")
        self.page.click('#new-chat')
        self.page.wait("document.querySelector('#messages .welcome') && !document.querySelector('#send-button').disabled",
                       what='new conversation')
        return before

    def send(self, text):
        self.page.type('#message-input', text)
        self.page.press_enter()

    def status(self):
        return self.page.text('#run-status').strip()

    def wait_status(self, *labels, timeout=25):
        return self.page.wait(f"{json.dumps(list(labels))}.includes(document.querySelector('#run-status').innerText.trim())",
                              timeout, what=f'status in {labels} (now {self.status()!r})')

    def conversation(self):
        cid = self.page.js("document.querySelector('.conversation-row.active')?.dataset.conversationId")
        return get(f'{self.app}/api/conversations/{cid}'), cid

    def shot(self, name):
        self.page.screenshot(self.out / f'{name}.png')


def s1_choice_buttons(run):
    """A question card: the exact option is sent by a click; nothing chosen to change reads as by choice."""
    run.new_chat(); run.send('整理 notes.md（验收1）')
    run.page.wait("document.querySelectorAll('.choice-buttons button').length === 2", what='two option buttons')
    asked = run.status()
    run.shot('s1_question')
    run.page.click('.choice-buttons button', 0)
    run.wait_status('已按你的选择处理')
    run.shot('s1_answered')
    conv, _ = run.conversation()
    last = [m for m in conv['messages'] if m['role'] == 'assistant'][-1]
    return dict(question_status_waiting=asked == '等你回答',
                answered_by_choice=last['content'].startswith('已按你的选择保持原样'),
                exact_option_sent=any(m['role'] == 'user' and m['content'] == '选择 A：保留原样' for m in conv['messages']),
                old_buttons_disabled=run.page.js("[...document.querySelectorAll('.choice-buttons button')].every(b => b.disabled)"),
                file_unchanged=(run.library / 'notes.md').is_file(),
                one_model_request_for_answer=run.model.count('验收1', turn=2) == 1)


def s2_stop_at_card(run):
    """The stop button while a confirmation card waits: nothing is written, the turn says cancelled."""
    run.new_chat(); run.send('新建 blocked 文件夹（验收2）')
    run.page.wait("!document.querySelector('#confirmation').classList.contains('hidden')", what='confirmation card')
    run.shot('s2_card')
    run.page.click('.activity-stop')
    run.wait_status('已取消')
    run.shot('s2_stopped')
    conv, _ = run.conversation()
    last = [m for m in conv['messages'] if m['role'] == 'assistant'][-1]
    return dict(card_closed=run.page.js("document.querySelector('#confirmation').classList.contains('hidden')"),
                says_cancelled=last['content'].startswith('已取消'),
                nothing_written=not (run.library / 'blocked').exists())


def s3_approve_and_undo(run):
    """Approve the card, the folder appears; the undo button takes it back."""
    run.new_chat(); run.send('新建 approved 文件夹（验收3）')
    run.page.wait("!document.querySelector('#confirmation').classList.contains('hidden')", what='confirmation card')
    run.page.click('.confirmation-approve')
    run.wait_status('已完成')
    created = (run.library / 'approved').is_dir()
    run.shot('s3_done')
    run.page.click('.run-actions .text-button:last-child')
    run.page.wait("[...document.querySelectorAll('.run-actions .text-button')].some(b => b.innerText.includes('已执行撤销'))",
                  what='undo done')
    run.shot('s3_undone')
    return dict(created_after_approve=created, removed_by_undo=not (run.library / 'approved').exists(),
                undo_says_all_restored='撤销结果（已全部还原）' in run.page.text())


def s4_reload_mid_run(run):
    """Reload the page while the model is still answering: the run continues, shown once."""
    run.new_chat(); run.send('慢慢回答（验收4）')
    run.wait_status('正在处理', '提交中…')
    time.sleep(1.2)
    run.page.reload()
    running_after_reload = run.status() in ('正在处理', '提交中…')
    run.page.wait("document.querySelector('#messages').innerText.includes('慢回答完成')", timeout=20, what='slow answer')
    run.shot('s4_after_reload')
    conv, _ = run.conversation()
    users = [m for m in conv['messages'] if m['role'] == 'user']
    answers = [m for m in conv['messages'] if m['role'] == 'assistant']
    shown = run.page.js("[...document.querySelectorAll('#messages article.message.user')].length")
    return dict(still_running_after_reload=running_after_reload, one_user_message=len(users) == 1 and shown == 1,
                one_answer=len(answers) == 1, one_model_request=run.model.count('验收4') == 1)


def s5_offline_retry(run):
    """Send while offline: the text comes back with its message id; sending again is the same message."""
    run.new_chat()
    run.page.offline(True)
    run.send('断网重试（验收5）')
    run.wait_status('发送失败')
    pending = run.page.js("Object.entries(localStorage).filter(([k]) => k.startsWith('fb-pending')).map(([k, v]) => JSON.parse(v))")
    restored = run.page.js("document.querySelector('#message-input').value")
    said = run.page.text()
    run.shot('s5_offline')
    run.page.offline(False)
    run.page.click('#send-button')
    run.page.wait("document.querySelector('#messages').innerText.includes('断网后重发的回答')", what='answer after retry')
    run.shot('s5_retried')
    conv, _ = run.conversation()
    users = [m for m in conv['messages'] if m['role'] == 'user']
    left = run.page.js("Object.keys(localStorage).filter(k => k.startsWith('fb-pending')).length")
    return dict(failed_while_offline=bool(pending) and pending[0].get('content') == '断网重试（验收5）',
                says_not_sent='消息没有发出' in said and 'Failed to fetch' not in said and '本轮处理已中断' not in said,
                text_restored=restored == '断网重试（验收5）', one_user_message=len(users) == 1,
                one_run=run.model.count('验收5') == 1, pending_cleared=left == 0)


def s6_model_error_retry(run):
    """The model service fails once: the app retries and answers, without a second user message."""
    run.new_chat(); run.send('模型出错重试（验收6）')
    run.page.wait("document.querySelector('#messages').innerText.includes('重试后回答')", timeout=30, what='answer after retry')
    run.shot('s6_answered')
    conv, _ = run.conversation()
    return dict(answered=True, two_requests_one_failed=run.model.count('验收6', step=0) == 2,
                one_user_message=sum(m['role'] == 'user' for m in conv['messages']) == 1)


def s7_typed_answer_and_task_panel(run):
    """2.0: a text answer continues the task without a task-edit card; history is read-only."""
    run.new_chat(); run.send('移动 notes.md（验收7）')
    run.page.wait("document.querySelectorAll('.choice-buttons button').length === 2", what='typed-answer question')
    run.page.click('#open-tasks')
    run.page.wait("!!document.querySelector('.task-card')", what='task panel')
    waiting = '等你回答' in run.page.text('#task-editor-body')
    run.page.click('.task-card .text-button')
    read_only = run.page.js("[...document.querySelectorAll('#task-editor-body textarea')].some(t => t.readOnly)")
    run.shot('s7_history_readonly')
    run.page.click('#task-editor-close')
    run.send('放到文献，文件名 notes-v2.md')
    run.page.wait("!document.querySelector('#confirmation').classList.contains('hidden')", what='file move confirmation')
    run.page.click('#confirmation .confirmation-details summary')
    file_card = 'task_edit' not in run.page.text('#confirmation') and 'notes-v2.md' in run.page.text('#confirmation')
    run.page.click('.confirmation-approve')
    run.wait_status('已完成')
    _, cid = run.conversation()
    tasks = get(f'{run.app}/api/conversations/{cid}/tasks')['items']
    run.shot('s7_typed_answer_done')
    return dict(task_was_waiting=waiting, historical_notes_readonly=read_only, one_file_confirmation=file_card,
                moved=(run.library / '文献/notes-v2.md').is_file() and not (run.library / 'notes.md').exists(),
                same_task_completed=len(tasks) == 1 and tasks[0]['status'] == 'completed',
                three_model_requests_for_answer=run.model.count('验收7', turn=2) == 3)


def s8_shown_state_from_the_server(run):
    """2.0 P1-D: a read-only task under review reads as answered, in the status line and the task card alike."""
    files = lambda: sorted(str(p.relative_to(run.library)) for p in run.library.rglob('*') if '.oplog' not in p.parts)
    before = files()
    run.new_chat(); run.send('notes.md 写了什么（验收8）')
    run.wait_status('已回答', '请确认结果', '已完成', '没有完成')
    status = run.status()
    _, cid = run.conversation()
    tasks = get(f'{run.app}/api/conversations/{cid}/tasks')['items']
    run.page.click('#open-tasks')
    run.page.wait("!!document.querySelector('.task-card')", what='task panel')
    card = run.page.text('.task-card h3').strip()
    run.shot('s8_answered_task')
    run.page.click('#task-editor-close')
    return dict(status_answered=status == '已回答', task_under_review=len(tasks) == 1 and tasks[0]['status'] == 'needs_review',
                server_sends_display=len(tasks) == 1 and tasks[0].get('display') == 'answered', card_answered=card == '已回答',
                nothing_changed=files() == before)


def s9_recycle_a_non_empty_folder(run):
    """User decision A2: one card recycles a non-empty folder and lists every file it takes."""
    for rel in ('测试/a.txt', '测试/sub/b.md'):
        (run.library / rel).parent.mkdir(parents=True, exist_ok=True); (run.library / rel).write_text(rel)
    run.new_chat(); run.send('回收测试文件夹（验收9）')
    run.page.wait("!document.querySelector('#confirmation').classList.contains('hidden')", what='confirmation card')
    card = run.page.text('#confirmation')
    run.page.click('#confirmation .confirmation-details summary')
    details = run.page.text('#confirmation')
    run.shot('s9_card')
    run.page.click('.confirmation-approve')
    run.wait_status('已完成')
    trashed = sorted(p.name for p in (run.library / '.trash').rglob('*') if p.is_file())
    return dict(one_card_names_the_count='其中 2 个文件' in card, details_list_each_file='a.txt' in details and 'b.md' in details,
                folder_recycled=not (run.library / '测试').exists(), files_in_trash={'a.txt', 'b.md'} <= set(trashed),
                one_plan=run.model.count('验收9') == 3)


SCENARIOS = [('问题卡按钮与按选择处理', s1_choice_buttons), ('确认卡片等待时停止', s2_stop_at_card),
             ('允许后撤销', s3_approve_and_undo), ('运行中刷新页面', s4_reload_mid_run),
             ('断网发送后重试', s5_offline_retry), ('模型服务失败一次后重试', s6_model_error_retry),
             ('任务内文字回答与只读历史备注', s7_typed_answer_and_task_panel),
             ('服务器给出的显示状态', s8_shown_state_from_the_server),
             ('一张卡片整体回收非空文件夹', s9_recycle_a_non_empty_folder)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--headed', action='store_true', help='show the Chrome window')
    ap.add_argument('--scenario', action='append', choices=[fn.__name__ for _, fn in SCENARIOS],
                    help='run only selected scenarios when investigating a failed check')
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=False)
    home = Path(tempfile.mkdtemp(prefix='filebutler-browser-'))
    profile = Path(tempfile.mkdtemp(prefix='filebutler-chrome-'))
    model, model_port, app_port, cdp_port = FakeModel(), free_port(), free_port(), free_port()
    server = serve_model(model, model_port)
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    cfg.update(base_url=f'http://127.0.0.1:{model_port}', api_key_env='FILEBUTLER_FAKE_KEY', sandbox='./library',
               confirm='always', llm_retries=1, llm_timeout=20, cost_budget=1.0)
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    (home / 'library/文献').mkdir(parents=True)
    (home / 'library/notes.md').write_text('测试内容', encoding='utf-8')
    env = dict(os.environ, FILEBUTLER_HOME=str(home), FILEBUTLER_FAKE_KEY='fake-key')
    env.pop('DEEPSEEK_API_KEY', None)  # never reach the paid model from this harness
    app_log = open(args.out / 'app.log', 'w')
    app = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'webapp:app', '--host', '127.0.0.1', '--port', str(app_port)],
                           cwd=ROOT, env=env, stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    results, ok = [], False
    try:
        wait_http(f'http://127.0.0.1:{app_port}/api/health')
        chrome = subprocess.Popen([CHROME, *([] if args.headed else ['--headless=new']), f'--remote-debugging-port={cdp_port}',
                                   f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                                   '--window-size=1400,1000', f'http://127.0.0.1:{app_port}/'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_http(f'http://127.0.0.1:{cdp_port}/json/version')
        target = next(t for t in wait_http(f'http://127.0.0.1:{cdp_port}/json/list') if t['type'] == 'page')
        page = Page(target['webSocketDebuggerUrl'])
        page.wait("document.readyState === 'complete' && document.querySelector('#health').innerText.includes('已连接')",
                  what='page connected')
        run = Run(page, f'http://127.0.0.1:{app_port}', home, args.out, model)
        selected = [(name, fn) for name, fn in SCENARIOS if not args.scenario or fn.__name__ in args.scenario]
        for name, scenario in selected:
            started = time.perf_counter()
            try:
                checks, error = scenario(run), None
            except Exception as exc:  # recorded, the next scenario still runs
                checks, error = {}, f'{type(exc).__name__}: {exc}'
                try: run.shot(f'error_{scenario.__name__}')
                except Exception: pass
            passed = bool(checks) and all(checks.values()) and not error
            results.append(dict(scenario=name, passed=passed, checks=checks, error=error,
                                seconds=round(time.perf_counter() - started, 2)))
            print(f"{'✓' if passed else '✗'} {name}：{json.dumps(checks, ensure_ascii=False)}" + (f'  {error}' if error else ''),
                  flush=True)
        ok = all(r['passed'] for r in results)
    finally:
        (args.out / 'results.json').write_text(json.dumps(dict(chrome=CHROME, results=results,
                                                               model_requests=model.requests), ensure_ascii=False, indent=2))
        for proc in (chrome, app):
            if proc:
                proc.terminate()
                try: proc.wait(10)
                except subprocess.TimeoutExpired: proc.kill()
        server.shutdown(); app_log.close()
        shutil.rmtree(home, ignore_errors=True); shutil.rmtree(profile, ignore_errors=True)
    print(f"{sum(r['passed'] for r in results)}/{len(results)} 个浏览器场景通过")
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
