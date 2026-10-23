"""Shared offline harness: a scripted model replaying tool calls through the real loop.

No model API is called. ``Replay`` is an OpenAI-compatible fake client returning
scripted (or recorded, ``tests/fixtures/live_20261021``) tool calls one model
turn at a time. Local IDs (task T…, file plan X…, classification plan P…,
catalog C…, file F…, evidence E…, dedup scan ids) are random per run; the
replay maps a placeholder or recorded ID to the live one by aligning it with the
ID sequence of the live result of the same call, and substitutes it in later
arguments.

Moved from the retired v2 regression module (tag archive-v2-legacy-20261023).
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pymupdf
import yaml
from rich.console import Console

from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.prompts import SYSTEM_PROMPT
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox  # noqa: F401  re-exported for tests


REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'live_20261021'
ID = re.compile(r'(?<![0-9A-Za-z])(?:[TDXPCFER][0-9a-f]{10,12}|[0-9a-f]{12})(?![0-9A-Za-z])')


def production_cfg():
    """config.yaml plus the eval/live_policy overrides; values that change a replay
    are pinned so a local edit of config.yaml cannot silently change the tests."""
    cfg = yaml.safe_load((REPO / 'config.yaml').read_text(encoding='utf-8'))
    cfg = {k: v for k, v in cfg.items() if 'key' not in k.lower()}
    cfg.update(max_steps=25, task_repair_limit=6, repeat_detection=True, llm_retries=0,
               task_session='live-policy-acceptance')
    return cfg


CFG = production_cfg()


def recorded(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


def call(tool, arguments, alias=None):
    """A scripted (non-recorded) call in fixture format.

    ``alias`` is a placeholder ID that later scripted arguments may use; it is
    mapped to the first local ID returned by this call (e.g. the X plan id).
    """
    return dict(tool=tool, arguments=json.dumps(arguments, ensure_ascii=False),
                status='ok', result_head='', result_ids=[alias] if alias else [])


PLAN_ALIAS = 'Xaaaaaaaaaaaa'


def step(*calls, trace_step=None):
    return dict(calls=list(calls), trace_step=trace_step)


class Replay:
    """OpenAI-compatible fake client returning recorded tool calls one model turn at a time."""

    def __init__(self, steps, ids=None, final=''):
        self.steps, self.ids, self.final = list(steps), ids if ids is not None else {}, final
        self.pending, self.log, self.requests = [], [], 0
        self.chat = NS(completions=NS(create=self.create))

    def absorb(self, messages):
        results = {m['tool_call_id']: m['content'] for m in messages if m.get('role') == 'tool'}
        for call_id, rec in self.pending:
            content = results.get(call_id)
            if content is None:
                status, text = 'missing', ''
            else:
                try:
                    data = json.loads(content); status, text = data['status'], data['text']
                except (ValueError, KeyError, TypeError):
                    status, text = ('ok' if content.startswith('OK') else 'error'), content
            if status == 'ok' and rec['status'] == 'ok':
                for old, new in zip(rec['result_ids'], ID.findall(text)):
                    self.ids.setdefault(old, new)
            self.log.append(dict(tool=rec['tool'], trace_step=rec.get('trace_step'), sent=rec['sent'],
                                 recorded=rec['status'], status=status, text=text))
        self.pending = []

    def create(self, **kw):
        self.requests += 1
        self.absorb(kw['messages'])
        if not self.steps:
            msg = NS(content=self.final, tool_calls=None)
        else:
            item = self.steps.pop(0)
            calls = []
            for i, rec in enumerate(item['calls']):
                args = ID.sub(lambda m: self.ids.get(m.group(0), m.group(0)), rec['arguments'])
                cid = f'replay_{self.requests}_{i}'
                calls.append(NS(id=cid, function=NS(name=rec['tool'], arguments=args)))
                self.pending.append((cid, dict(rec, sent=args, trace_step=item.get('trace_step'))))
            msg = NS(content='', tool_calls=calls)
        return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=100, completion_tokens=10))

    # Query helpers -------------------------------------------------------
    def calls(self, tool=None, trace_step=None):
        return [c for c in self.log if (tool is None or c['tool'] == tool)
                and (trace_step is None or c['trace_step'] == trace_step)]

    def one(self, tool, trace_step):
        found = self.calls(tool, trace_step)
        assert len(found) == 1, (tool, trace_step, self.log)
        return found[0]


class Session:
    """One temporary library + artifact store + history, like eval/live_policy.py."""

    def __init__(self, root):
        self.sb = Sandbox(root)
        self.store = ArtifactStore(self.sb)
        self.history = []
        self.ids = {}
        self.turn = 0
        self.approvals = []
        self.answer = 'y'

    def run(self, instruction, steps, final=''):
        self.turn += 1
        # As webapp.py: the full history is kept, and each run sees its projection.
        full = (self.history or [dict(role='system', content=SYSTEM_PROMPT)]) + [dict(role='user', content=instruction)]
        history, _ = prepare_history(full, CFG, self.store)
        client = Replay(steps, self.ids, final)
        approve = lambda request: self.approvals.append(request) or self.answer
        result = run_agent(instruction, LLM({'model': 'fake'}, client=client), self.sb, CFG,
                           Console(file=io.StringIO()), artifacts=self.store, history=history,
                           oplog=OpLog(self.sb, f'v3_{self.turn}'),
                           confirmer=Confirmer('always', ask_action=approve))
        client.absorb(result.messages)
        self.history = full + result.added_messages
        return result, client


def details(result):
    return '\n\n'.join(s['title'] + '\n' + s['markdown'] for s in result.report.get('sections', []))


# --------------------------------------------------------------------------- fixtures (as in eval/live_policy.py)


def pdf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=11)
        document.save(path)


def files_fixture(root):
    for rel in ('Inbox/nested', 'Archive'): (root / rel).mkdir(parents=True)
    for rel, text in [('Inbox/a.txt', 'alpha'), ('Inbox/b.txt', 'beta'),
                      ('Inbox/nested/keep.txt', 'protected nested record')]:
        (root / rel).write_text(text)
    pdf(root / 'Inbox/keep.pdf', 'Document that must stay in its original location.')


def duplicates_fixture(root):
    (root / 'Inbox/empty').mkdir(parents=True)
    (root / 'Inbox/nonempty').mkdir()
    for rel, text in [('a.txt', 'same exact bytes'), ('copy_of_a.txt', 'same exact bytes'),
                      ('unique.txt', 'different'), ('nonempty/keep.txt', 'protected')]:
        (root / 'Inbox' / rel).write_text(text)


def applications_fixture(root):
    texts = [
        'Abstract: We train a convolutional neural network (CNN) to identify crop leaf disease in wheat fields. The application is agricultural crop protection, not clinical diagnosis or electricity systems.',
        'Abstract: We train a convolutional neural network (CNN) to diagnose lung disease using clinical chest X-ray images. This work is medical diagnosis, not agriculture or electricity generation.',
        'Abstract: We train a convolutional neural network (CNN) to forecast solar power production and electricity demand for power-grid operation. The application is energy systems, not agriculture or medicine.',
    ]
    for i, text in enumerate(texts, 1): pdf(root / '资料' / f'CNN_{i:02d}.pdf', text)


def assert_root_only_files(root):
    assert (root / 'Archive/a.txt').read_text() == 'alpha'
    assert (root / 'Archive/b.txt').read_text() == 'beta'
    assert not (root / 'Inbox/a.txt').exists() and not (root / 'Inbox/b.txt').exists()
    assert (root / 'Inbox/nested/keep.txt').read_text() == 'protected nested record'
    assert (root / 'Inbox/keep.pdf').is_file()


def spec(*goals, **kwargs):
    """A TaskSubmission dict: one goal per (description, conditions) pair."""
    return dict(goals=[dict(id=f'g{i}', description=description, conditions=conditions)
                       for i, (description, conditions) in enumerate(goals)], **kwargs)


def dispatch(r, name, args=None):
    """One tool call through a TaskRuntime ``r``, as the loop runs it (no model)."""
    raw, blocked = r.prepare(name, json.dumps(args or {}, ensure_ascii=False), r.sb)
    result = blocked or r.dispatch(name, raw, r.sb, OpLog(r.sb, r.run_id), Confirmer('never'))
    r.after(name, result, r.sb)
    return result


def classification(r, *, dimension='应用领域', base_dir=None, category_paths=None):
    """A saved two-category classification plan of every PDF in the library; returns its P id."""
    assert dispatch(r, 'paper_profiles', {'evidence': {'pdf_pages': None}}).startswith('OK')
    cid = next(reversed(r.sb.artifacts.data['catalogs']))
    ids = r.sb.artifacts.data['catalogs'][cid]['members']
    args = dict(catalog_id=cid, dimension=dimension, categories=['医学', '统计'], assignments=[
        dict(file_id=fid, category=1, secondary_categories=[2]) for fid in ids])
    if base_dir is not None: args['base_dir'] = base_dir
    if category_paths: args['category_paths'] = category_paths
    assert dispatch(r, 'submit_classification', args).startswith('OK')
    return next(reversed(r.sb.artifacts.data['classification_plans']))


def run_script(tmp_path, instruction, client, *, confirmer=None, **cfg):
    """Run the real loop with a scripted client; returns (result, sandbox, client)."""
    sb = Sandbox(tmp_path)
    result = run_agent(instruction, LLM({'model': 'fake'}, client=client), sb, {'max_steps': 8, **cfg},
                       Console(quiet=True), confirmer=confirmer, oplog=OpLog(sb, 'script'))
    return result, sb, client
