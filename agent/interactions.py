"""Messages, questions and answers (routing redesign S2, docs/agent-routing-redesign-2026-10-24.md 4.3-4.4, 11).

Every user submission (web, CLI, evaluation) is received once: a message receipt
keyed by (root, session, client_message_id) and one durable run request. A retry
with the same id and payload gets the same receipt back, whatever happened since
(the question answered, a newer question asked, the task revised); the same id
with a different payload is a conflict. Only a first submission is checked
against the current question and consumes it.

Questions get stable ids; options get option ids. An answer names the exact
question, version and option: the letter a page shows is display only. A message
without one is ordinary text: it answers nothing locally and grants nothing.

These tables live in the TaskStore database (agent/task.py) and are the only
authority for answers; the web database only projects turns (run_request_id).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime

API_VERSION = 1
SCHEMA_VERSION = 4  # PRAGMA user_version of tasks.sqlite3 once these tables are authoritative
# 4 (S5 audit, 2026-10-25): a request the user settled by a choice keeps that decision.
# 3 (S3): a request can be paused or cancelled by the user; cancel_epoch fences running work.
REQUEST_COLUMNS = (('cancel_epoch', 'INTEGER NOT NULL DEFAULT 0'), ('status_actor', 'TEXT'),
                   ('status_receipt', 'TEXT'), ('status_at', 'TEXT'), ('decision_json', 'TEXT'))
LEASE_SECONDS = 1800

DDL = '''
CREATE TABLE IF NOT EXISTS message_receipts (
    receipt_id TEXT PRIMARY KEY, root TEXT NOT NULL, session TEXT NOT NULL,
    client_message_id TEXT NOT NULL, api_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    request_id TEXT NOT NULL, run_request_id TEXT NOT NULL, answer_id TEXT,
    created_at TEXT NOT NULL, UNIQUE(root, session, client_message_id));
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, root TEXT NOT NULL, session TEXT NOT NULL,
    source_receipt TEXT NOT NULL UNIQUE, status TEXT NOT NULL, task_id TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY, root TEXT NOT NULL, session TEXT NOT NULL,
    request_id TEXT, run_id TEXT, task_id TEXT, task_revision INTEGER,
    version INTEGER NOT NULL, scope_json TEXT NOT NULL, data_json TEXT NOT NULL,
    status TEXT NOT NULL, answer_id TEXT, superseded_by TEXT, closed_by_receipt TEXT,
    created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_question ON questions(root, session) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS answers (
    answer_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, version INTEGER NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE, option_id TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(question_id, version));
CREATE TABLE IF NOT EXISTS run_requests (
    run_request_id TEXT PRIMARY KEY, root TEXT NOT NULL, session TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE, answer_id TEXT, status TEXT NOT NULL,
    lease_owner TEXT, lease_until REAL, execution_epoch INTEGER NOT NULL DEFAULT 0,
    ui_turn_id TEXT UNIQUE, run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS protocol_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


class InteractionError(ValueError):
    """code: IDEMPOTENCY_CONFLICT | QUESTION_STALE | QUESTION_ALREADY_ANSWERED | INVALID_ARGUMENTS."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now().isoformat(timespec='milliseconds')


def _id(prefix):
    return prefix + uuid.uuid4().hex[:16]


def migrate(path, db):
    """Bring a task database to SCHEMA_VERSION; idempotent and safe under concurrent opens.

    Before the first migration of a database that holds runs, a copy is taken with
    the SQLite backup API (WAL included). The one question a session was waiting
    on (its latest run outcome's pending_choice) becomes an open legacy question
    whose scope is unknown: it can be answered exactly, and grants nothing wider.
    """
    if db.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA_VERSION:
        return
    db.commit()
    has_runs = db.execute("SELECT count(*) FROM sqlite_master WHERE name='run_outcomes'").fetchone()[0] and \
        db.execute('SELECT count(*) FROM run_outcomes').fetchone()[0]
    if has_runs:
        # Outside the write transaction: a backup waits for locks, including its own connection's.
        version = db.execute('PRAGMA user_version').fetchone()[0]
        backup = sqlite3.connect(f'{path}.bak-v{version}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}')
        try: db.backup(backup)
        finally: backup.close()
    db.execute('BEGIN IMMEDIATE')
    try:
        current = db.execute('PRAGMA user_version').fetchone()[0]
        if current >= SCHEMA_VERSION:  # migrated meanwhile
            db.execute('COMMIT')
            return
        for statement in DDL.split(';'):  # not executescript: it would commit the open transaction
            if statement.strip(): db.execute(statement)
        present = {r[1] for r in db.execute('PRAGMA table_info(requests)')}
        for column, kind in REQUEST_COLUMNS:
            if column not in present:
                db.execute(f'ALTER TABLE requests ADD COLUMN {column} {kind}')
        db.execute("INSERT OR REPLACE INTO protocol_meta VALUES('stage', 'copying')")
        if has_runs and current < 2:  # questions exist from version 2 on
            _backfill_legacy_questions(db)
        db.execute("INSERT OR REPLACE INTO protocol_meta VALUES('stage', 'authoritative')")
        db.execute("INSERT OR REPLACE INTO protocol_meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def _backfill_legacy_questions(db):
    sessions = db.execute('SELECT DISTINCT root, session FROM run_outcomes').fetchall()
    for root, session in sessions:
        row = db.execute('SELECT run_id, data_json FROM run_outcomes WHERE root=? AND session=? ORDER BY rowid DESC LIMIT 1',
                         (root, session)).fetchone()
        asked = json.loads(row[1]).get('pending_choice') if row else None
        if not asked:
            continue
        data = _question_data(asked)
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (_id('Q'), root, session, None, row[0], json.loads(row[1]).get('task_id'), None, 1,
                    json.dumps(dict(level='legacy_scope_unknown')), json.dumps(data, ensure_ascii=False),
                    'open', None, None, None, _now()))


def _question_data(asked):
    """The stored question: options keep their letters for display and gain option ids."""
    data = json.loads(json.dumps(asked, ensure_ascii=False))
    for choice in data.get('choices', []):
        choice.setdefault('option_id', _id('O'))
    return data


def canonical(content, response, action=None):
    """The payload a message id stands for: the text exactly as sent, the exact response,
    and an action (cancel: the stop button, which starts no run)."""
    if response is not None:
        response = dict(question_id=str(response['question_id']), version=int(response['version']),
                        option_id=str(response['option_id']))
    payload = dict(api_version=API_VERSION, content=content, response=response)
    if action:
        payload['action'] = action
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return payload, hashlib.sha256(text.encode()).hexdigest()


class Interactions:
    """Receipts, questions, answers and run requests of one root and session."""

    def __init__(self, store):
        self.store, self.root, self.session = store, store.root, store.session

    # ----- receiving a message -----

    def accept_cancel(self, client_message_id):
        """The stop button, received once: (receipt, request it stopped or None)."""
        payload, digest = canonical('[停止]', None, 'cancel')
        with self.store.connect() as db:
            known = self._receipt(db, client_message_id)
            if known:
                return self._returned(db, known, digest), None
        target = self.latest_request()
        receipt_id, run_request_id = _id('M'), _id('R')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._receipt(db, client_message_id):
                db.execute('ROLLBACK')
                return self.accept_cancel(client_message_id)
            request_id = target['request_id'] if target else _id('U')
            db.execute('INSERT INTO message_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (receipt_id, self.root, self.session, client_message_id, API_VERSION, digest,
                        json.dumps(payload, ensure_ascii=False), request_id, run_request_id, None, _now()))
            db.execute('INSERT INTO run_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (run_request_id, self.root, self.session, receipt_id, None, 'no_run',
                        None, None, 0, None, None, _now(), _now()))
        stopped = self.set_status(target['request_id'], 'cancelled', actor='user_button', receipt_id=receipt_id) if target else None
        return dict(receipt_id=receipt_id, request_id=request_id, run_request_id=run_request_id, answer_id=None,
                    status='no_run', ui_turn_id=None, run_id=None, existing=False), stopped

    def accept(self, client_message_id, content, response=None):
        """The receipt of one submission: dict(receipt_id, request_id, run_request_id, answer_id,
        status, ui_turn_id, run_id, existing). The caller has already checked access to the session."""
        if not client_message_id or len(client_message_id) > 100:
            raise InteractionError('INVALID_ARGUMENTS', '缺少或无效的 client_message_id')
        payload, digest = canonical(content, response)
        with self.store.connect() as db:
            known = self._receipt(db, client_message_id)
            if known:
                return self._returned(db, known, digest)
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            known = self._receipt(db, client_message_id)  # a concurrent first submission won
            if known:
                return self._returned(db, known, digest)
            receipt_id, run_request_id, answer_id = _id('M'), _id('R'), None
            question = None
            if response is not None:
                question, option = self._open_option(db, payload['response'])
                answer_id = _id('A')
                updated = db.execute("UPDATE questions SET status='answered', answer_id=?, closed_by_receipt=? "
                                     "WHERE question_id=? AND version=? AND status='open'",
                                     (answer_id, receipt_id, question['question_id'], question['version'])).rowcount
                if updated != 1:
                    raise InteractionError('QUESTION_ALREADY_ANSWERED', '这个问题已经有回答')
                db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?)',
                           (answer_id, question['question_id'], question['version'], receipt_id,
                            option['option_id'], _now()))
                request_id = question['request_id'] or _id('U')
            else:
                # Ordinary text answers nothing locally; the open question is closed by it,
                # and the run is told the text may be its answer or a new request.
                db.execute("UPDATE questions SET status='replied', closed_by_receipt=? WHERE root=? AND session=? "
                           "AND status='open'", (receipt_id, self.root, self.session))
                request_id = _id('U')
            if not db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone():
                db.execute('INSERT INTO requests(request_id, root, session, source_receipt, status, task_id, created_at) '
                           'VALUES(?,?,?,?,?,?,?)', (request_id, self.root, self.session, receipt_id, 'active',
                                                    question['task_id'] if question else None, _now()))
            db.execute('INSERT INTO message_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (receipt_id, self.root, self.session, client_message_id, API_VERSION, digest,
                        json.dumps(payload, ensure_ascii=False), request_id, run_request_id, answer_id, _now()))
            db.execute('INSERT INTO run_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (run_request_id, self.root, self.session, receipt_id, answer_id, 'queued',
                        None, None, 0, None, None, _now(), _now()))
            return dict(receipt_id=receipt_id, request_id=request_id, run_request_id=run_request_id,
                        answer_id=answer_id, status='queued', ui_turn_id=None, run_id=None, existing=False)

    def known(self, client_message_id, content, response=None):
        """The receipt already given to this message id (None: a first submission)."""
        _, digest = canonical(content, response)
        with self.store.connect() as db:
            row = self._receipt(db, client_message_id)
            return self._returned(db, row, digest) if row else None

    def _receipt(self, db, client_message_id):
        return db.execute('SELECT * FROM message_receipts WHERE root=? AND session=? AND client_message_id=?',
                          (self.root, self.session, client_message_id)).fetchone()

    def _returned(self, db, known, digest):
        if known['payload_hash'] != digest:
            raise InteractionError('IDEMPOTENCY_CONFLICT', '同一消息 ID 已用于不同内容；修改后的消息请重新发送')
        run = db.execute('SELECT * FROM run_requests WHERE run_request_id=?', (known['run_request_id'],)).fetchone()
        return dict(receipt_id=known['receipt_id'], request_id=known['request_id'], run_request_id=known['run_request_id'],
                    answer_id=known['answer_id'], status=run['status'], ui_turn_id=run['ui_turn_id'],
                    run_id=run['run_id'], existing=True)

    def _open_option(self, db, response):
        row = db.execute('SELECT * FROM questions WHERE question_id=? AND root=? AND session=?',
                         (response['question_id'], self.root, self.session)).fetchone()
        if not row:
            raise InteractionError('QUESTION_STALE', '问题不存在或不属于当前会话')
        if row['status'] == 'answered':
            raise InteractionError('QUESTION_ALREADY_ANSWERED', '这个问题已经有回答')
        if row['status'] != 'open' or row['version'] != response['version']:
            raise InteractionError('QUESTION_STALE', '这个问题已经过期，请按最新的问题回答')
        data = json.loads(row['data_json'])
        option = next((c for c in data['choices'] if c.get('option_id') == response['option_id']), None)
        if not option:
            raise InteractionError('INVALID_ARGUMENTS', '选项不属于这个问题')
        return dict(row), option

    # ----- requests: pause, cancel, resume (routing redesign S3, 5) -----

    def request(self, request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=? AND root=? AND session=?',
                             (request_id, self.root, self.session)).fetchone()
        return dict(row) if row else None

    def latest_request(self, exclude=None):
        """The session's most recent request that is not cancelled, other than exclude
        (the one a stop button or "停一下" means)."""
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE root=? AND session=? AND status!='cancelled' AND request_id!=? "
                             "ORDER BY created_at DESC, rowid DESC LIMIT 1", (self.root, self.session, exclude or '')).fetchone()
        return dict(row) if row else None

    def link_task(self, request_id, task_id):
        with self.store.connect() as db:
            db.execute('UPDATE requests SET task_id=? WHERE request_id=? AND root=? AND session=?',
                       (task_id, request_id, self.root, self.session))

    def set_status(self, request_id, status, *, actor, receipt_id=None):
        """paused | cancelled | active. Cancelling raises the epoch running work checks,
        closes the request's questions and cancels its run requests that have not started.
        Returns the request after the change (None: not this session's request)."""
        assert status in ('paused', 'cancelled', 'active')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=? AND root=? AND session=?',
                             (request_id, self.root, self.session)).fetchone()
            if not row:
                return None
            if row['status'] == 'cancelled' and status != 'cancelled':
                raise InteractionError('QUESTION_STALE', '这个请求已经取消；需要时请重新提出')
            epoch = row['cancel_epoch'] + (1 if status == 'cancelled' and row['status'] != 'cancelled' else 0)
            db.execute('UPDATE requests SET status=?, cancel_epoch=?, status_actor=?, status_receipt=?, status_at=? '
                       'WHERE request_id=?', (status, epoch, actor, receipt_id, _now(), request_id))
            closed = 'closed' if status == 'cancelled' else 'suspended'
            if status in ('paused', 'cancelled'):
                db.execute("UPDATE questions SET status=? WHERE request_id=? AND status IN ('open', 'suspended')",
                           (closed, request_id))
            if status == 'cancelled':
                db.execute("UPDATE run_requests SET status='cancelled', updated_at=? WHERE root=? AND session=? "
                           "AND status='queued' AND receipt_id IN (SELECT receipt_id FROM message_receipts WHERE request_id=?)",
                           (_now(), self.root, self.session, request_id))
            return dict(db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone())

    def settle(self, request_id, decision):
        """The user settled the request by a choice that changes nothing ("不移动"): the
        decision is kept with the request, whether or not a task was registered."""
        with self.store.connect() as db:
            db.execute("UPDATE requests SET status='settled', decision_json=?, status_actor='user_option', status_at=? "
                       "WHERE request_id=? AND root=? AND session=? AND status IN ('active', 'settled')",
                       (json.dumps(decision, ensure_ascii=False), _now(), request_id, self.root, self.session))

    def suspend_question(self, question_id):
        with self.store.connect() as db:
            db.execute("UPDATE questions SET status='suspended' WHERE question_id=? AND root=? AND session=? "
                       "AND status IN ('open', 'replied')", (question_id, self.root, self.session))

    # ----- questions -----

    def record_question(self, asked, *, run_id, task_id=None, task_revision=None, request_id=None):
        """Store a question the user is about to see; an older open one is superseded."""
        data = _question_data(asked)
        question_id = _id('Q')
        with self.store.connect() as db:
            db.execute("UPDATE questions SET status='superseded', superseded_by=? WHERE root=? AND session=? AND status='open'",
                       (question_id, self.root, self.session))
            db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (question_id, self.root, self.session, request_id, run_id, task_id, task_revision, 1,
                        json.dumps(dict(level='legacy_scope_unknown')), json.dumps(data, ensure_ascii=False),
                        'open', None, None, None, _now()))
        return dict(data, question_id=question_id, version=1)

    def answered(self, answer_id):
        """(question, chosen option) of an accepted answer."""
        with self.store.connect() as db:
            answer = db.execute('SELECT * FROM answers WHERE answer_id=?', (answer_id,)).fetchone()
            question = answer and db.execute('SELECT * FROM questions WHERE question_id=? AND root=? AND session=?',
                                             (answer['question_id'], self.root, self.session)).fetchone()
        if not question:
            raise InteractionError('QUESTION_STALE', '回答不属于当前会话')
        data = json.loads(question['data_json'])
        option = next(c for c in data['choices'] if c.get('option_id') == answer['option_id'])
        return dict(question, data=data), option

    def replied_to(self, receipt_id):
        """The question a text message closed without choosing an option, if any."""
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM questions WHERE closed_by_receipt=? AND status='replied' AND root=? AND session=?",
                             (receipt_id, self.root, self.session)).fetchone()
        return dict(row, data=json.loads(row['data_json'])) if row else None

    def open_question(self, task_id=None, *, include_suspended=False):
        with self.store.connect() as db:
            sql = "SELECT * FROM questions WHERE root=? AND session=? AND "
            sql += "status IN ('open', 'suspended')" if include_suspended else "status='open'"
            params = [self.root, self.session]
            if task_id is not None:
                sql += ' AND task_id=?'
                params.append(task_id)
            row = db.execute(sql + ' ORDER BY rowid DESC LIMIT 1', params).fetchone()
        return dict(row, data=json.loads(row['data_json'])) if row else None

    def text_relation(self, question_id, kind):
        """A library caller has no receipt to close its open question; close it explicitly."""
        status = 'suspended' if kind in {'new_request', 'independent_query'} else 'replied'
        with self.store.connect() as db:
            db.execute("UPDATE questions SET status=? WHERE question_id=? AND root=? AND session=? "
                       "AND status IN ('open', 'replied')", (status, question_id, self.root, self.session))

    def bind_question_task(self, question_id, task_id):
        with self.store.connect() as db:
            db.execute('UPDATE questions SET task_id=? WHERE question_id=? AND root=? AND session=? AND task_id IS NULL',
                       (task_id, question_id, self.root, self.session))

    # ----- run requests (the outbox the web turns are created from) -----

    def claim(self, run_request_id, owner, run_id):
        """queued -> running for one owner; False when someone else already holds it."""
        with self.store.connect() as db:
            return db.execute("UPDATE run_requests SET status='running', lease_owner=?, lease_until=?, "
                              "execution_epoch=execution_epoch+1, run_id=?, updated_at=? "
                              "WHERE run_request_id=? AND status='queued'",
                              (owner, time.time() + LEASE_SECONDS, run_id, _now(), run_request_id)).rowcount == 1

    def attach_turn(self, run_request_id, turn_id):
        with self.store.connect() as db:
            db.execute('UPDATE run_requests SET ui_turn_id=?, updated_at=? WHERE run_request_id=? AND ui_turn_id IS NULL',
                       (turn_id, _now(), run_request_id))
            return db.execute('SELECT ui_turn_id FROM run_requests WHERE run_request_id=?',
                              (run_request_id,)).fetchone()['ui_turn_id']

    def finish(self, run_request_id, status):
        with self.store.connect() as db:
            db.execute('UPDATE run_requests SET status=?, lease_until=NULL, updated_at=? WHERE run_request_id=?',
                       (status, _now(), run_request_id))

    def run_request(self, run_request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM run_requests WHERE run_request_id=? AND root=? AND session=?',
                             (run_request_id, self.root, self.session)).fetchone()
        return dict(row) if row else None

    def receipt(self, receipt_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM message_receipts WHERE receipt_id=?', (receipt_id,)).fetchone()
        return dict(row) if row else None


def recover(store):
    """After a restart: a run that was running has an unknown result (interrupted, never
    replayed); queued ones without a turn are returned so the caller can create them."""
    with store.connect() as db:
        db.execute("UPDATE run_requests SET status='interrupted', updated_at=? WHERE root=? AND status='running'",
                   (_now(), store.root))
        return [dict(r) for r in db.execute(
            "SELECT * FROM run_requests WHERE root=? AND status='queued' ORDER BY created_at", (store.root,))]


def store_for(cfg, sb):
    """The TaskStore a run with this configuration uses (same rule as TaskRuntime)."""
    from pathlib import Path
    from agent.task import TaskStore
    return TaskStore(Path(cfg.get('task_db') or sb.root / '.oplog' / 'tasks.sqlite3'), sb.root, cfg.get('task_session', 'cli'))


def receive(cfg, sb, content, *, client_message_id=None, response=None, pressed=None):
    """Receive one submission and return (receipt, cfg for its run).

    pressed is the key of an option on the question the caller just displayed (a CLI
    prompt, an evaluation's scripted click): it becomes the exact response for the
    open question. A key that is not an option there is sent as ordinary text.
    """
    repo = Interactions(store_for(cfg, sb))
    if pressed and response is None:
        question = repo.open_question()
        option = question and next((c for c in question['data']['choices'] if c['key'] == pressed), None)
        if option:
            response = dict(question_id=question['question_id'], version=question['version'], option_id=option['option_id'])
    receipt = repo.accept(client_message_id or uuid.uuid4().hex, content, response)
    run_cfg = dict(cfg, receipt_id=receipt['receipt_id'], request_id=receipt['request_id'],
                   run_request_id=receipt['run_request_id'])
    if receipt['answer_id']:
        run_cfg['answer_id'] = receipt['answer_id']
    else:
        run_cfg.pop('answer_id', None)
    return receipt, run_cfg
