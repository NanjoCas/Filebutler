"""Latency accounting, safe cache reuse, bounded retries and archival; no API spend."""
import copy
import json
import threading
from types import SimpleNamespace as NS

try:
    import httpx
except ImportError:
    import httpx2 as httpx
import openai
import pytest
from rich.console import Console

from agent.history import prepare_history, undo_note, has_undo
from agent.llm import LLM
from agent.loop import run_agent
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from tests.test_dedup import pdf, BASE, apply
from tests.test_web_ui import files, ui  # shared temporary app fixtures
from tools.dedup import scan_duplicates
import tools.dedup as dedup
import webapp


def test_exact_timing_excludes_confirmation(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr('time.perf_counter', lambda: clock[0])
    client = FakeClient([[('trash', {'path': 'a.txt'})], '完成'])
    original = client.chat.completions.create
    def create(**kw):
        clock[0] += 2
        return original(**kw)
    client.chat.completions.create = create
    def ask(_):
        clock[0] += 17
        return 'y'
    sb = Sandbox(tmp_path); (tmp_path / 'a.txt').write_text('a')
    trace = tmp_path.parent / (tmp_path.name + '.trace.jsonl')
    result = run_agent('清理', LLM({'model': 'fake'}, client=client), sb, {}, Console(quiet=True),
                       confirmer=Confirmer('always', ask=ask), oplog=OpLog(sb, 'timing'), trace_path=trace)
    assert result.seconds == 21 and result.active_seconds == 4
    assert result.wait_seconds == 17 and result.llm_seconds == 4 and result.tool_seconds == 0
    tool = next(r for r in map(json.loads, trace.read_text().splitlines()) if 'tool' in r)
    assert tool['wait_ms'] == 17000 and tool['active_ms'] == 0


def test_sdk_retries_disabled_and_configurable(monkeypatch):
    kwargs = {}
    monkeypatch.setenv('TEST_FAKE_KEY', 'fake-key')
    monkeypatch.setattr('agent.llm.OpenAI', lambda **kw: kwargs.update(kw) or NS())
    llm = LLM({'model': 'fake', 'api_key_env': 'TEST_FAKE_KEY', 'base_url': 'https://example.invalid', 'llm_timeout': 12})
    assert kwargs['max_retries'] == 0 and kwargs['timeout'] == 12 and llm.retries == 1


def test_single_retry_diagnostics_and_usage(monkeypatch):
    client = FakeClient(['完成']); original = client.chat.completions.create
    attempts, events, sleeps = [], [], []
    def create(**kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise openai.APITimeoutError(request=httpx.Request('POST', 'https://example.invalid'))
        return original(**kw)
    client.chat.completions.create = create
    monkeypatch.setattr('agent.llm.time.sleep', sleeps.append)
    llm = LLM({'model': 'fake'}, client=client)
    assert llm.chat([{'role': 'user', 'content': 'test'}], on_request=events.append).content == '完成'
    assert len(attempts) == 2 and sleeps == [1]
    ends = [e for e in events if e['phase'] == 'end']
    assert ends[0]['retry'] and ends[0]['error_type'] == 'APITimeoutError'
    assert ends[1]['ok'] and ends[1]['prompt_tokens'] == llm.prompt_tokens
    assert all('test' not in json.dumps(e) for e in events)


def test_exhausted_request_emits_failed_metrics(monkeypatch, tmp_path):
    client = FakeClient([]); calls = []
    def fail(**kw):
        calls.append(1)
        raise openai.APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))
    client.chat.completions.create = fail
    monkeypatch.setattr('agent.llm.time.sleep', lambda _: None)
    events, trace = [], tmp_path / 'trace.jsonl'
    with pytest.raises(openai.APIConnectionError):
        run_agent('test', LLM({'model': 'fake'}, client=client), Sandbox(tmp_path / 'sb'), {},
                  Console(quiet=True), trace_path=trace, on_event=events.append)
    assert len(calls) == 2
    assert events[-1]['type'] == 'run_failed' and events[-1]['llm_seconds'] >= 0
    assert json.loads(trace.read_text().splitlines()[-1])['event'] == 'failed'


def test_nonretryable_failure_not_retried(monkeypatch):
    calls = []
    def fail(**kw):
        calls.append(1)
        raise ValueError('invalid request')
    client = NS(chat=NS(completions=NS(create=fail)))
    with pytest.raises(ValueError):
        LLM({'model': 'fake'}, client=client).chat([])
    assert len(calls) == 1


def test_cache_shared_across_sandbox_instances_and_modes(tmp_path, monkeypatch):
    pdf(tmp_path / 'a.pdf'); pdf(tmp_path / 'b.pdf', subject='v2')
    first = scan_duplicates(Sandbox(tmp_path), mode='text')
    assert first['stats']['parsed'] == 2
    monkeypatch.setattr(dedup, '_pdf_pages', lambda *a, **k: pytest.fail('warm scan must not parse'))
    second = scan_duplicates(Sandbox(tmp_path), mode='near')
    assert second['stats']['parsed'] == 0 and second['stats']['text_cache_hits'] == 2
    assert first['groups'] == second['groups']


def test_cache_invalidates_changed_file_and_never_reuses_plan(tmp_path):
    pdf(tmp_path / 'a.pdf'); pdf(tmp_path / 'b.pdf', subject='v2')
    sb = Sandbox(tmp_path); first = scan_duplicates(sb, mode='text')
    (tmp_path / 'b.pdf').unlink(); pdf(tmp_path / 'b.pdf', 'different ' * 200)
    second = scan_duplicates(Sandbox(tmp_path), mode='text')
    assert second['stats']['parsed'] == 1 and second['stats']['text_cache_hits'] == 1
    assert not second['groups']
    assert apply(sb, first).startswith('ERROR:')
    assert (tmp_path / 'a.pdf').exists() and (tmp_path / 'b.pdf').exists()


def test_cache_is_bounded_and_does_not_cache_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(dedup, '_PDF_CACHE', dedup.OrderedDict())
    monkeypatch.setattr(dedup, '_PDF_CACHE_BYTES', 0)
    monkeypatch.setattr(dedup, '_PDF_CACHE_ENTRIES', 1)
    for i in range(3): pdf(tmp_path / f'{i}.pdf', BASE + f' extra{i}', subject=str(i))
    scan_duplicates(Sandbox(tmp_path), mode='text')
    assert len(dedup._PDF_CACHE) == 1 and dedup._PDF_CACHE_BYTES <= dedup._PDF_CACHE_LIMIT
    (tmp_path / 'bad.pdf').write_text('not a pdf')
    for _ in range(2):
        assert scan_duplicates(Sandbox(tmp_path), mode='text')['stats']['parse_failed'] == 1


def history_fixture():
    history = [{'role': 'system', 'content': 'rules'}]
    for i in range(5):
        history += [{'role': 'user', 'content': f'constraint {i}'},
                    {'role': 'assistant', 'content': f'note {i}', 'tool_calls': [
                        {'id': str(i), 'function': {'name': 'read_file', 'arguments': '{"path":"a.txt"}'}}]},
                    {'role': 'tool', 'tool_call_id': str(i), 'content': 'OK ' + 'x' * 5000},
                    {'role': 'assistant', 'content': f'conclusion {i}'}]
    history += [undo_note('old', ['跳过 #1：原位置被占用']), {'role': 'user', 'content': 'next'}]
    return history


def test_history_projection_keeps_constraints_pairs_undo_and_ui_original():
    history = history_fixture(); original = copy.deepcopy(history)
    projected, stats = prepare_history(history, {'conversation_history_max_chars': 1000})
    assert stats['after_chars'] < stats['before_chars'] * .6 and stats['archived_messages'] == 6
    assert history == original and has_undo(projected, 'old')
    assert [m['content'] for m in projected if m['role'] == 'user'] == [m['content'] for m in history if m['role'] == 'user']
    for i, msg in enumerate(projected):
        if msg.get('tool_calls'):
            assert projected[i+1]['tool_call_id'] == msg['tool_calls'][0]['id']
    assert all(any(m.get('content') == f'conclusion {i}' for m in projected) for i in range(5))


def install_model(monkeypatch, script):
    fake, finished = FakeClient(script), threading.Event()
    original = webapp._run_turn
    def run(*args):
        try: return original(*args)
        finally: finished.set()
    monkeypatch.setattr(webapp, '_run_turn', run)
    monkeypatch.setattr(webapp, 'LLM', lambda cfg: LLM({'model': 'fake'}, client=fake))
    return fake, finished


def test_undo_sync_idempotent_partial_and_visible_after_reload(ui, monkeypatch, files):
    fake, done = install_model(monkeypatch, ['之前已完成'])
    cid = ui.post('/api/conversations').json()['id']
    tid = ui.post(f'/api/conversations/{cid}/messages', json={'content': 'test'}).json()['turn_id']
    assert done.wait(5)
    turn = ui.get(f'/api/turns/{tid}').json()
    # Simulate a logged directory operation that cannot be undone because it now has content.
    (files.root / 'occupied').mkdir(); (files.root / 'occupied/a').touch()
    OpLog(files, turn['run_id']).record('make_dir', 'occupied', 'occupied')
    report = ui.post(f'/api/turns/{tid}/undo').json()['report']
    assert '跳过' in report[0]
    assert ui.get(f'/api/turns/{tid}').json()['result']['undo'] == report
    assert ui.post(f'/api/turns/{tid}/undo').json()['report'] == report
    shown = ui.get(f'/api/conversations/{cid}').json()['messages']
    assert shown[-1]['role'] == 'event' and '跳过' in shown[-1]['content']
    with webapp._db() as db:
        h = json.loads(db.execute('SELECT history_json FROM conversations WHERE id=?', (cid,)).fetchone()[0])
    assert sum(has_undo([m], turn['run_id']) for m in h) == 1
    done.clear(); fake.script.append('知道撤销了')
    ui.post(f'/api/conversations/{cid}/messages', json={'content': '继续'})
    assert done.wait(5)
    assert has_undo(fake.seen[-1], turn['run_id'])


def test_live_clock_pauses_and_undo_guard(monkeypatch, ui):
    clock = [10.0]
    monkeypatch.setattr('time.perf_counter', lambda: clock[0])
    cid = ui.post('/api/conversations').json()['id']
    with webapp._db() as db:
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('timed', cid, 'timed', 'completed', webapp._now(), webapp._now()))
    state = webapp.TurnState('timed', cid, 'timed'); webapp.TURN_STATES['timed'] = state
    state.status = 'running'; webapp._event('timed', {'type': 'started'})
    clock[0] += 3; state.status = 'waiting_confirmation'
    webapp._event('timed', {'type': 'confirmation_required'})
    clock[0] += 20
    assert ui.get('/api/turns/timed').json()['clock']['active_seconds'] == 3
    webapp.ACTIVE_CONVERSATIONS.add('other')
    assert ui.post('/api/turns/timed/undo').status_code == 409
    webapp.ACTIVE_CONVERSATIONS.clear()
    webapp._event('timed', {'type': 'confirmation_answered'}); state.status = 'running'
    clock[0] += 2
    metrics = ui.get('/api/turns/timed').json()['clock']
    assert metrics['active_seconds'] == 5 and metrics['wait_seconds'] == 20


def test_legacy_undo_backfilled_before_next_request(ui, monkeypatch):
    fake, done = install_model(monkeypatch, ['已同步'])
    cid = ui.post('/api/conversations').json()['id']
    with webapp._db() as db:
        db.execute('UPDATE conversations SET history_json=? WHERE id=?',
                   (json.dumps([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'old'},
                                {'role': 'assistant', 'content': '已完成'}]), cid))
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at,result_json) VALUES(?,?,?,?,?,?,?)',
                   ('legacy', cid, 'legacy-run', 'completed', webapp._now(), webapp._now(), json.dumps({'undo': ['还原 a → b']})))
    ui.post(f'/api/conversations/{cid}/messages', json={'content': '重做'})
    assert done.wait(5) and has_undo(fake.seen[0], 'legacy-run')
    assert any('不是当前文件快照' in m.get('content', '') for m in fake.seen[0])
    assert fake.seen[0][-1] == {'role': 'user', 'content': '重做'}


def test_projection_does_not_erase_persisted_transcript(ui, monkeypatch):
    cfg = webapp._config()
    monkeypatch.setattr(webapp, '_config', lambda: {**cfg, 'conversation_history_max_chars': 1000})
    fake, done = install_model(monkeypatch, ['new reply'])
    cid = ui.post('/api/conversations').json()['id']
    original = history_fixture()[:-1]
    with webapp._db() as db:
        db.execute('UPDATE conversations SET history_json=? WHERE id=?', (json.dumps(original), cid))
    tid = ui.post(f'/api/conversations/{cid}/messages', json={'content': 'next'}).json()['turn_id']
    assert done.wait(5)
    with webapp._db() as db:
        saved = json.loads(db.execute('SELECT history_json FROM conversations WHERE id=?', (cid,)).fetchone()[0])
    assert len([m for m in saved if m['role'] == 'tool']) == 5
    assert len([m for m in fake.seen[0] if m['role'] == 'tool']) == 2
    assert saved[-1]['content'] == 'new reply'
    trace = ui.get(f'/api/turns/{tid}/trace').json()
    assert trace['requests'][-1]['ok'] and trace['requests'][-1]['phase'] == 'end'


def test_repetition_resets_for_new_user_request(tmp_path):
    client = FakeClient([[('list_dir', {})], 'done'])
    args = {'name': 'list_dir', 'arguments': '{}'}
    history = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'old'}]
    for i in range(2):
        history += [{'role': 'assistant', 'tool_calls': [{'id': str(i), 'function': args}], 'content': ''},
                    {'role': 'tool', 'tool_call_id': str(i), 'content': 'OK'}]
    history += [{'role': 'assistant', 'content': 'done'}, {'role': 'user', 'content': '再列一次'}]
    result = run_agent('再列一次', LLM({'model': 'fake'}, client=client), Sandbox(tmp_path), {},
                       Console(quiet=True), history=history)
    assert result.stop_reason == 'done'


def test_submission_failure_releases_conversation(ui, monkeypatch):
    cid = ui.post('/api/conversations').json()['id']
    def fail(*a, **kw): raise RuntimeError('closed pool')
    monkeypatch.setattr(webapp.POOL, 'submit', fail)
    with pytest.raises(RuntimeError):
        ui.post(f'/api/conversations/{cid}/messages', json={'content': 'test'})
    assert cid not in webapp.ACTIVE_CONVERSATIONS
    assert ui.get(f'/api/conversations/{cid}').json()['latest_turn']['status'] == 'failed'


def test_local_post_apply_verification_detects_keeper_change(tmp_path, monkeypatch):
    sb = Sandbox(tmp_path)
    (tmp_path / 'a.txt').write_text('same'); (tmp_path / 'b.txt').write_text('same')
    scan = scan_duplicates(sb, mode='bytes')
    keeper = scan['groups'][0]['keep']
    original = OpLog.record
    def record(self, *args):
        original(self, *args)
        (tmp_path / keeper).write_text('external change')
    monkeypatch.setattr(OpLog, 'record', record)
    result = apply(sb, scan, Confirmer('never'))
    assert result.startswith('ERROR: 已回收 1') and '保留文件复核失败' in result
    assert (tmp_path / '.oplog/test-dedup.jsonl').exists()


def test_history_archival_preserves_write_receipt_without_misattributing_reused_id():
    history = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'old'}]
    for name, content in [('trash', 'OK: 已回收 a.txt'), ('read_file', 'OK ' + 'x' * 5000)]:
        history += [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'reused-id', 'function': {'name': name, 'arguments': '{"path":"a.txt"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'reused-id', 'content': content}]
    history += [{'role': 'assistant', 'content': 'done'}, {'role': 'user', 'content': 'new'}]
    projected, stats = prepare_history(history, {'conversation_history_max_chars': 100, 'conversation_keep_full_turns': 0})
    text = json.dumps(projected, ensure_ascii=False)
    assert '已回收 a.txt' in text and 'x' * 100 not in text and stats['archived_messages'] == 4


def test_immediate_undo_waits_for_completion_save(ui, monkeypatch, files):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    cfg = webapp._config()
    monkeypatch.setattr(webapp, '_config', lambda: {**cfg, 'confirm': 'never'})
    terminal, release = threading.Event(), threading.Event()
    original_event = webapp._event
    def event(tid, info):
        original_event(tid, info)
        if info['type'] == 'completed':
            terminal.set()
            assert release.wait(5)
    monkeypatch.setattr(webapp, '_event', event)
    fake, done = install_model(monkeypatch, [[('trash', {'path': 'notes.md'})], 'done'])
    cid = ui.post('/api/conversations').json()['id']
    tid = ui.post(f'/api/conversations/{cid}/messages', json={'content': '清理'}).json()['turn_id']
    assert terminal.wait(5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ui.post, f'/api/turns/{tid}/undo')
        try:
            with pytest.raises(TimeoutError): future.result(timeout=.05)
        finally:
            release.set()
        response = future.result(timeout=5)
    assert response.status_code == 200 and done.wait(5)
    assert (files.root / 'notes.md').read_text() == '测试内容'
    assert ui.get(f'/api/turns/{tid}').json()['result']['undo'] == response.json()['report']
