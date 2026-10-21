"""Offline regressions for the 2026-10-21 live acceptance failures.

No model API is called. ``Replay`` is a scripted fake client that re-sends the
exact tool calls the real model (deepseek-flash) emitted, taken from the
recorded histories/traces under
``eval/results/policy_live_20261021_01/<case>/turn_N.{history.json,trace.jsonl}``.
Those directories are git-ignored, so the calls are frozen in
``tests/fixtures/live_20261021/*.json`` (arguments verbatim; tool results are
reduced to their first line plus the ordered list of local IDs, no PDF text).

Local IDs (task T…, edit D…, file plan X…, classification plan P…, catalog C…,
file F…, evidence E…, dedup scan ids) are random per run. The replay remaps a
recorded ID to the live one by aligning the ID sequence of the recorded result
with the live result of the same call, and substitutes it in later arguments.
A scripted model cannot adapt to a different tool response, so where a fix may
legitimately reject a call earlier (pre-check), the assertion accepts that
branch explicitly instead of requiring the old call sequence to keep working.

Expected behaviour is asserted through public outcomes: run stop_reason / task
status, the main answer (``result.final``), the collapsible report sections
(``result.report['sections']``), whether a tool call returned an error, and the
files on disk. Each test names the live failure it reproduces.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pymupdf
import pytest
import yaml
from rich.console import Console

from agent.artifacts import ArtifactStore
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.prompts import SYSTEM_PROMPTS
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox


REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'live_20261021'
ID = re.compile(r'(?<![0-9A-Za-z])(?:[TDXPCFER][0-9a-f]{10,12}|[0-9a-f]{12})(?![0-9A-Za-z])')


def production_cfg():
    """Same runtime switches as the live run (config.yaml + eval/live_policy overrides).

    Values that change the replay are pinned to what the 2026-10-21 run used so a
    local edit of config.yaml cannot silently change these regressions.
    """
    cfg = yaml.safe_load((REPO / 'config.yaml').read_text(encoding='utf-8'))
    cfg = {k: v for k, v in cfg.items() if 'key' not in k.lower()}
    cfg.update(local_runtime=True, task_runtime=True, prompt='v8_tasks', max_steps=25,
               task_repair_limit=6, repeat_detection=True, llm_retries=0, reflect=False,
               context_compress=False, task_session='live-policy-acceptance')
    return cfg


CFG = production_cfg()


# --------------------------------------------------------------------------- replay


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

    def run(self, instruction, steps, final=''):
        self.turn += 1
        if self.history:
            history, _ = prepare_history(self.history + [dict(role='user', content=instruction)], CFG, self.store)
        else:
            history = [dict(role='system', content=SYSTEM_PROMPTS[CFG['prompt']]),
                       dict(role='user', content=instruction)]
        client = Replay(steps, self.ids, final)
        approve = lambda request: self.approvals.append(request) or 'y'
        result = run_agent(instruction, LLM({'model': 'fake'}, client=client), self.sb, CFG,
                           Console(file=io.StringIO()), artifacts=self.store, history=history,
                           oplog=OpLog(self.sb, f'replay_{self.turn}'),
                           confirmer=Confirmer('always', ask_action=approve))
        client.absorb(result.messages)
        self.history = result.messages
        return result, client


def replay(session, name, *, until=None, extra=(), final=''):
    """Replay a recorded turn; ``until`` keeps steps with trace_step < until."""
    data = recorded(name)
    steps = [s for s in data['steps'] if until is None or s['trace_step'] < until]
    return session.run(data['instruction'], steps + list(extra), final)


def details(result):
    return '\n\n'.join(s['title'] + '\n' + s['markdown'] for s in result.report.get('sections', []))


def finish(summary='收尾'):
    return step(call('finish_task', {'summary': summary}))


# --------------------------------------------------------------------------- fixtures (copied from eval/live_policy.py)


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


# Synthetic stand-ins for the six real papers (data/real/ is never touched).
# Only local plumbing is under test; the category choice is the recorded model's.
PAPERS = [
    ('Weak Supervision for Fake News Detection via Reinforcement Learning. We use a reinforced selector.', '强化学习'),
    ('Domain Adaptive Fake News Detection via Reinforcement Learning. REAL-FND uses an RL agent.', '强化学习'),
    ('Big Self-Supervised Models are Strong Semi-Supervised Learners. Contrastive ResNet pretraining.', '其他'),
    ('Two-path Deep Semi-supervised Learning for Timely Fake News Detection with CNN paths.', '其他'),
    ('Deep learning for fake news detection: A comprehensive survey of methods and datasets.', '综述'),
    ('BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding.', 'Transformer'),
]


def papers_fixture(root):
    (root / '文献').mkdir()
    for i, (text, _) in enumerate(PAPERS, 1): pdf(root / '文献' / f'doc_{i:02d}.pdf', text)


def assert_root_only_files(root):
    assert (root / 'Archive/a.txt').read_text() == 'alpha'
    assert (root / 'Archive/b.txt').read_text() == 'beta'
    assert not (root / 'Inbox/a.txt').exists() and not (root / 'Inbox/b.txt').exists()
    assert (root / 'Inbox/nested/keep.txt').read_text() == 'protected nested record'
    assert (root / 'Inbox/keep.pdf').is_file()


# ============================================================================ 1. root_only
# Live failure: eval/results/policy_live_20261021_01/root_only/turn_1.trace.jsonl
# The model added goal report_count with tool_success(list_dir, path=Inbox); the
# only such call happened in step 1 before set_task, so no observation was bound
# to it. Files and answer were correct; finish_task returned partial.


def test_root_only_replay_unbound_count_goal_is_not_false_partial(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'root_only_turn1')
    registration = client.one('set_task', 3)
    if registration['status'] == 'error':
        # Accepted alternative: the unbindable machine condition is rejected at
        # registration (pre-check) instead of failing at the end. The rejection
        # must be about the condition, not an unrelated schema change.
        assert re.search(r'report_count|tool_success|list_dir|条件|绑定', registration['text']), registration['text']
        assert not (tmp_path / 'Archive/a.txt').exists()  # Nothing written under a rejected task.
        return
    assert_root_only_files(tmp_path)
    assert client.one('execute_file_plan', 9)['status'] == 'ok'
    assert result.stop_reason != 'partial', result.final
    assert result.runtime['task_status'] != 'partial'
    assert '尚未全部完成' not in result.final
    assert '未找到满足参数的成功调用' not in result.final
    assert '2' in result.final  # The requested count is still reported in the main answer.


def test_root_only_guard_out_of_scope_move_is_still_rejected_without_auto_expansion(tmp_path):
    """Guard: the step-5 plan (write_roots=[Archive], sources in Inbox) must stay rejected."""
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'root_only_turn1', until=6, extra=[finish('未能移动')])
    if client.one('set_task', 3)['status'] == 'error':
        pytest.skip('set_task pre-check rejected the recorded task; covered by the test above')
    rejected = client.one('prepare_file_plan', 5)
    assert rejected['status'] == 'error' and 'Inbox/a.txt' in rejected['text']
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'alpha' and not (tmp_path / 'Archive/a.txt').exists()
    assert session.sb.runtime.current['write_roots'] == ['Archive']  # no silent permission growth
    # Genuinely unfinished: must be partial, and the unresolved blocker stays in the main answer.
    assert result.stop_reason == 'partial'
    assert '尚未全部完成' in result.final
    assert '写入范围' in result.final and 'Inbox/a.txt' in result.final


def test_root_only_guard_unexecuted_move_remains_partial(tmp_path):
    """Guard: registering the task and then finishing without moving is not 'done'/'needs_review'."""
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'root_only_turn1', until=4, extra=[finish('已完成')])
    if client.one('set_task', 3)['status'] == 'error':
        pytest.skip('set_task pre-check rejected the recorded task')
    assert (tmp_path / 'Inbox/a.txt').exists() and not (tmp_path / 'Archive/a.txt').exists()
    assert result.stop_reason == 'partial' and result.runtime['task_status'] == 'partial'
    assert '尚未全部完成' in result.final


def test_root_only_guard_out_of_scope_legacy_move_rejected(tmp_path):
    """Guard: the legacy single-item tool cannot write outside write_roots either."""
    files_fixture(tmp_path)
    session = Session(tmp_path)
    task = dict(goals=[dict(id='g', description='移动 a.txt 到 Archive',
                            conditions=[dict(kind='path_exists', path='Archive/a.txt')])],
                read_roots=['.'], write_roots=['Archive'], allowed_effects=['move'])
    result, client = session.run('把 Inbox/a.txt 移到 Archive', [
        step(call('set_task', task)), step(call('move', {'src': 'Inbox/a.txt', 'dst_dir': 'Archive'})),
        finish('已移动')])
    assert client.calls('move')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/a.txt').exists() and not (tmp_path / 'Archive/a.txt').exists()
    assert result.stop_reason == 'partial'


# ============================================================================ 2. real_classification turn 2
# Live failure: eval/results/policy_live_20261021_01/real_classification/turn_2.trace.jsonl
# (turn 1 in turn_1.trace.jsonl builds plan P via paper_profiles/submit_classification).
# Turn 2 edit_task added goal 'apply' with {"kind": "file_plan_verified"} and no
# reference; execute_file_plan stored the receipt under the concrete X id, the
# verifier looked up '' -> "尚无此计划的完整执行回执：" while files_placed passed.


def classification_session(tmp_path):
    papers_fixture(tmp_path)
    session = Session(tmp_path)
    first, first_client = replay(session, 'real_classification_turn1')
    assert first_client.one('submit_classification', 5)['status'] == 'ok', first_client.log
    return session, first, first_client


def expected_classification_layout(root):
    for i, (_, category) in enumerate(PAPERS, 1):
        assert (root / '文献/分类' / category / f'doc_{i:02d}.pdf').is_file()
        assert not (root / '文献' / f'doc_{i:02d}.pdf').exists()


def test_real_classification_turn2_empty_plan_reference_is_bound_or_rejected(tmp_path):
    session, _, _ = classification_session(tmp_path)
    result, client = replay(session, 'real_classification_turn2')
    edits = client.calls('edit_task')
    if any(e['status'] == 'error' for e in edits):
        # Accepted alternative: a model-authored file_plan_verified without a
        # resolvable reference is refused when the edit is previewed.
        rejected = next(e for e in edits if e['status'] == 'error')
        assert re.search(r'file_plan_verified|reference|引用|计划', rejected['text']), rejected['text']
        return
    assert client.one('execute_file_plan', 6)['status'] == 'ok'
    expected_classification_layout(tmp_path)
    assert result.stop_reason != 'partial', result.final
    assert result.runtime['task_status'] in {'needs_review', 'completed'}
    assert '尚无此计划的完整执行回执' not in result.final
    apply = next(c for c in result.runtime['checks'] if c['goal_id'] == 'apply')
    assert apply['passed'], apply
    assert all(c['passed'] for c in apply['conditions'] if c['kind'] == 'file_plan_verified')


def test_real_classification_guard_prepared_but_unexecuted_plan_is_partial(tmp_path):
    """Guard: the same turn without execute_file_plan must stay partial (not needs_review)."""
    session, _, _ = classification_session(tmp_path)
    result, client = replay(session, 'real_classification_turn2', until=6, extra=[finish('已执行')])
    if any(e['status'] == 'error' for e in client.calls('edit_task')):
        pytest.skip('edit pre-check rejected the recorded goal')
    assert client.one('prepare_file_plan', 5)['status'] == 'ok'
    assert all((tmp_path / '文献' / f'doc_{i:02d}.pdf').exists() for i in range(1, 7))
    assert result.stop_reason == 'partial' and result.runtime['task_status'] == 'partial'
    assert '尚未全部完成' in result.final


def test_real_classification_guard_wrong_explicit_plan_reference_never_passes(tmp_path):
    """Guard: an explicit reference to a plan that does not exist cannot be satisfied by another plan."""
    session, _, _ = classification_session(tmp_path)
    data = recorded('real_classification_turn2')
    steps = json.loads(json.dumps(data['steps']))
    for s in steps:
        for c in s['calls']:
            if c['tool'] == 'edit_task':
                c['arguments'] = c['arguments'].replace('{"kind": "file_plan_verified"}',
                                                        '{"kind": "file_plan_verified", "reference": "X000000000000"}')
    result, client = session.run(data['instruction'], steps)
    edits = client.calls('edit_task')
    if any(e['status'] == 'error' for e in edits):
        return  # Rejected at pre-check: acceptable.
    assert result.stop_reason == 'partial', result.final
    apply = next(c for c in result.runtime['checks'] if c['goal_id'] == 'apply')
    assert not apply['passed']


# ============================================================================ 3. cross_turn_correction turn 1
# Live failure: eval/results/policy_live_20261021_01/cross_turn_correction/turn_1.trace.jsonl
# A plain two-file move registered {"kind": "plan_applied", "reference": ""}
# (classification-plan-only). set_task accepted it (step 2), the move executed
# correctly, and only finish_task reported "本任务尚无可核验的分类计划引用".


def test_cross_turn_plain_move_with_classification_condition_is_caught_before_execution(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'cross_turn_correction_turn1')
    registration = client.one('set_task', 2)
    plan = client.one('prepare_file_plan', 6)
    caught = registration['status'] == 'error' or plan['status'] == 'error'
    assert caught, ('plan_applied on a plain move must be refused at set_task/prepare_file_plan '
                    'pre-check, not discovered by finish_task: ' + result.final)
    rejection = registration if registration['status'] == 'error' else plan
    assert re.search(r'plan_applied|分类|条件|操作', rejection['text']), rejection['text']
    # Nothing was executed under the incompatible contract.
    assert (tmp_path / 'Inbox/a.txt').exists() and (tmp_path / 'Inbox/b.txt').exists()
    assert '本任务尚无可核验的分类计划引用' not in result.final


def test_cross_turn_guard_compatible_move_contract_completes(tmp_path):
    """Guard/positive: the same recorded calls with only the plan_applied condition removed finish done."""
    files_fixture(tmp_path)
    session = Session(tmp_path)
    data = recorded('cross_turn_correction_turn1')
    steps = json.loads(json.dumps(data['steps']))
    for s in steps:
        for c in s['calls']:
            c['arguments'] = c['arguments'].replace(', {"kind": "plan_applied", "reference": ""}', '')
    result, client = session.run(data['instruction'], steps)
    assert client.one('set_task', 2)['status'] == 'ok'
    assert (tmp_path / 'Archive/a.txt').read_text() == 'alpha' and (tmp_path / 'Archive/b.txt').read_text() == 'beta'
    assert result.stop_reason == 'done', result.final


def test_cross_turn_guard_half_executed_move_is_partial(tmp_path):
    """Guard: if only a.txt is moved while the mapping requires both, the task is partial."""
    files_fixture(tmp_path)
    session = Session(tmp_path)
    task = dict(goals=[dict(id='g_move', description='将 a.txt、b.txt 移到 Archive', conditions=[dict(
        kind='files_placed', path='Inbox', destination='Archive', extensions=['txt'], recursive=False,
        placement='mapping', placements={'Inbox/a.txt': 'Archive/a.txt', 'Inbox/b.txt': 'Archive/b.txt'})])],
        read_roots=['.'], write_roots=['Inbox', 'Archive'], allowed_effects=['move'],
        constraints=[dict(id='c_no_side_effects', description='不创建目录、不改名、不回收',
                          forbidden_effects=['create', 'rename', 'trash'])])
    result, client = session.run('仅将 Inbox 根目录的 a.txt、b.txt 移到现有 Archive', [
        step(call('set_task', task)),
        step(call('prepare_file_plan', dict(description='只移 a', actions=[
            dict(effect='move', source='Inbox/a.txt', destination='Archive/a.txt')]), alias=PLAN_ALIAS)),
        step(call('execute_file_plan', dict(plan_id=PLAN_ALIAS))),
        finish('已全部移动')])
    assert client.calls('execute_file_plan')[0]['status'] == 'ok', client.log
    assert (tmp_path / 'Archive/a.txt').exists() and (tmp_path / 'Inbox/b.txt').exists()
    assert result.stop_reason == 'partial' and '尚未全部完成' in result.final


# ============================================================================ 4. dedup_cleanup
# Live failure: eval/results/policy_live_20261021_01/dedup_cleanup/turn_1.trace.jsonl
# Goal 'cleanup' bound receipt_verified(deduplicate) and empty_directories_removed
# together; the empty-directory guard then rejected the duplicate *file* in
# deduplicate (step 3), apply_dedup (5), prepare_file_plan (6) and trash (8):
# "空目录清理目标只允许回收空文件夹：Inbox/copy_of_a.txt".

EMPTY_DIR_GUARD = '空目录清理目标只允许回收空文件夹：Inbox/copy_of_a.txt'


def test_dedup_replay_duplicate_file_is_not_blocked_by_empty_directory_condition(tmp_path):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'dedup_cleanup_turn1')
    blocked = [c for c in client.log if EMPTY_DIR_GUARD in c['text']]
    assert not blocked, [(c['trace_step'], c['tool']) for c in blocked]
    # Whatever path the replay then takes, the protected objects are intact.
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'same exact bytes'
    assert (tmp_path / 'Inbox/unique.txt').read_text() == 'different'
    assert (tmp_path / 'Inbox/nonempty/keep.txt').read_text() == 'protected'


# The live model's two task shapes, minus the text-only constraint
# ("keep_shortest" with no rules), which separately and legitimately blocks writes
# until user review. Global forbidden move/rename and the trash content rule stay.
DEDUP_CONSTRAINTS = [dict(id='no_near_dup', description='只处理字节完全相同的副本，不合并近似版本',
                          forbidden_effects=['move', 'rename'],
                          rules=[dict(effects=['trash'], predicate='content_unchanged', selector='source')])]
DEDUP_CONDITION = dict(kind='receipt_verified', tool='deduplicate',
                       arguments=dict(path='Inbox', mode='bytes', keep='shortest_name'))
EMPTY_CONDITION = dict(kind='empty_directories_removed', path='Inbox')
SHAPES = {
    # trace step 2: one goal with both conditions
    'one_goal': ([dict(id='cleanup', description='清理字节相同副本并回收空文件夹',
                       conditions=[DEDUP_CONDITION, EMPTY_CONDITION])], ['cleanup']),
    # trace step 1 (invalid JSON in the live run): two goals bound by one step
    'two_goals': ([dict(id='dedup', description='清理字节相同副本', conditions=[DEDUP_CONDITION]),
                   dict(id='empty_dirs', description='回收空文件夹', conditions=[EMPTY_CONDITION])],
                  ['dedup', 'empty_dirs']),
}


def dedup_task(shape):
    goals, step_goals = SHAPES[shape]
    task = dict(goals=goals, read_roots=['Inbox'], write_roots=['Inbox'], allowed_effects=['trash'],
                constraints=DEDUP_CONSTRAINTS)
    return task, step_goals


@pytest.mark.parametrize('shape', SHAPES)
def test_dedup_and_empty_dir_goals_bound_together_complete(tmp_path, shape):
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    task, step_goals = dedup_task(shape)
    result, client = session.run(recorded('dedup_cleanup_turn1')['instruction'], [
        step(call('set_task', task)),
        step(call('begin_step', dict(id='dedup_and_prune', goal_ids=step_goals, description='查重并清理空目录')),
             call('deduplicate', dict(keep='shortest_name', mode='bytes', path='Inbox'))),
        finish('已清理副本')])
    dedup = client.calls('deduplicate')[0]
    assert dedup['status'] == 'ok', dedup['text']
    assert not (tmp_path / 'Inbox/copy_of_a.txt').exists()
    assert (tmp_path / 'Inbox/a.txt').read_text() == 'same exact bytes'
    assert (tmp_path / 'Inbox/unique.txt').read_text() == 'different'
    assert (tmp_path / 'Inbox/nonempty/keep.txt').read_text() == 'protected'


@pytest.mark.parametrize('shape', SHAPES)
def test_dedup_then_empty_dir_plan_finishes_done(tmp_path, shape):
    """Full success: dedup, then a file plan trashing Inbox/empty, then finish -> done."""
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    task, step_goals = dedup_task(shape)
    steps = [
        step(call('set_task', task)),
        step(call('begin_step', dict(id='dedup_and_prune', goal_ids=step_goals, description='查重并清理空目录')),
             call('deduplicate', dict(keep='shortest_name', mode='bytes', path='Inbox'))),
        step(call('prepare_file_plan', dict(description='回收空文件夹 Inbox/empty', goal_ids=step_goals,
                                            actions=[dict(effect='trash', source='Inbox/empty')]), alias=PLAN_ALIAS)),
        step(call('execute_file_plan', dict(plan_id=PLAN_ALIAS))),
        finish('已清理'),
    ]
    result, client = session.run(recorded('dedup_cleanup_turn1')['instruction'], steps)
    assert all(EMPTY_DIR_GUARD not in c['text'] for c in client.log), client.log
    assert client.calls('deduplicate')[0]['status'] == 'ok'
    assert client.calls('execute_file_plan')[0]['status'] == 'ok', client.log
    assert not (tmp_path / 'Inbox/copy_of_a.txt').exists() and not (tmp_path / 'Inbox/empty').exists()
    assert (tmp_path / 'Inbox/nonempty/keep.txt').read_text() == 'protected'
    assert result.stop_reason == 'done', result.final


@pytest.mark.parametrize('shape', SHAPES)
def test_dedup_guard_empty_directory_rule_still_protects_nonempty_folder(tmp_path, shape):
    """Guard: action-level attribution must not drop the cleanup protection for real folders."""
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    task, step_goals = dedup_task(shape)
    result, client = session.run('回收 Inbox 中的空文件夹', [
        step(call('set_task', task)),
        step(call('begin_step', dict(id='prune', goal_ids=step_goals, description='回收空文件夹')),
             call('prepare_file_plan', dict(description='回收 nonempty', goal_ids=step_goals,
                                            actions=[dict(effect='trash', source='Inbox/nonempty')]))),
        step(call('trash', dict(path='Inbox/nonempty'))),
        finish('已回收')])
    assert client.calls('prepare_file_plan')[0]['status'] == 'error'
    assert client.calls('trash')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/nonempty/keep.txt').read_text() == 'protected'
    assert result.stop_reason != 'done'


@pytest.mark.parametrize('shape', SHAPES)
def test_dedup_guard_global_forbidden_effect_still_applies(tmp_path, shape):
    """Guard: the task-wide forbidden 'move' is not lost when constraints are attributed per action."""
    duplicates_fixture(tmp_path)
    session = Session(tmp_path)
    task, step_goals = dedup_task(shape)
    task = dict(task, allowed_effects=['trash', 'move'])
    result, client = session.run('清理副本', [
        step(call('set_task', task)),
        step(call('begin_step', dict(id='dedup', goal_ids=step_goals, description='查重')),
             call('prepare_file_plan', dict(description='把 unique 移走', goal_ids=step_goals,
                                            actions=[dict(effect='move', source='Inbox/unique.txt',
                                                          destination='Inbox/nonempty/unique.txt')]))),
        step(call('move', dict(src='Inbox/unique.txt', dst_dir='Inbox/nonempty'))),
        finish('已处理')])
    assert client.calls('prepare_file_plan')[0]['status'] == 'error'
    assert client.calls('move')[0]['status'] == 'error'
    assert (tmp_path / 'Inbox/unique.txt').read_text() == 'different'
    assert result.stop_reason != 'done'


# ============================================================================ 5. blocker lifecycle
# Live failure: eval/results/policy_live_20261021_01/application_dimension/turn_1.trace.jsonl
# prepare_file_plan (step 7) failed with a source write-scope error, edit_task +
# apply_task_edit fixed it, the plan executed; the final answer still said
# "阻塞原因：操作包含任务写入范围外的对象：资料/CNN_01.pdf；effect=move".
# Same pattern: root_only/turn_1 (prepare at step 5, fixed by edit) and
# real_classification turn_1/turn_2 (malformed begin_step at turn-1 step 2,
# fixed by the next begin_step) - see each recorded_final in the fixtures.


def test_application_dimension_resolved_scope_blocker_moves_to_details(tmp_path):
    applications_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'application_dimension_turn1')
    assert client.one('prepare_file_plan', 7)['status'] == 'error'
    assert client.one('execute_file_plan', 11)['status'] == 'ok'
    for i, category in enumerate(('农业', '医疗', '能源'), 1):
        assert (tmp_path / '资料/按领域' / category / f'CNN_{i:02d}.pdf').is_file()
    assert result.stop_reason == 'needs_review'  # semantic review is still required
    assert '写入范围外的对象' not in result.final, result.final
    assert '阻塞原因' not in result.final
    assert '写入范围外的对象' in details(result)  # kept as process detail, not dropped


def test_root_only_resolved_scope_blocker_not_in_main_answer(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'root_only_turn1')
    if client.one('set_task', 3)['status'] == 'error':
        pytest.skip('set_task pre-check rejected the recorded task')
    assert client.one('prepare_file_plan', 5)['status'] == 'error'
    assert client.one('execute_file_plan', 9)['status'] == 'ok'
    assert '写入范围外的对象' not in result.final, result.final
    assert '写入范围外的对象' in details(result)


def test_real_classification_resolved_step_blocker_not_carried_into_answers(tmp_path):
    session, first, first_client = classification_session(tmp_path)
    assert first_client.one('begin_step', 2)['status'] == 'error'
    assert first_client.one('begin_step', 3)['status'] == 'ok'
    assert 'ExecutionStep' not in first.final, first.final
    assert 'ExecutionStep' in details(first)
    second, client = replay(session, 'real_classification_turn2')
    if any(e['status'] == 'error' for e in client.calls('edit_task')):
        pytest.skip('edit pre-check rejected the recorded goal')
    assert 'ExecutionStep' not in second.final, second.final  # a previous turn's resolved error


def test_blocker_guard_unresolved_failure_stays_in_main_answer(tmp_path):
    """Guard: application_dimension stopped right after the scope failure must say why, in the main answer."""
    applications_fixture(tmp_path)
    session = Session(tmp_path)
    result, client = replay(session, 'application_dimension_turn1', until=8, extra=[finish('已整理')])
    assert client.one('prepare_file_plan', 7)['status'] == 'error'
    assert all((tmp_path / '资料' / f'CNN_{i:02d}.pdf').exists() for i in (1, 2, 3))
    assert result.stop_reason == 'partial'
    assert '尚未全部完成' in result.final
    assert '写入范围外的对象' in result.final and '资料/CNN_01.pdf' in result.final


def test_blocker_guard_failed_execution_after_fix_attempt_stays_visible(tmp_path):
    """Guard: a later failure of the *same* plan is current, even though an earlier error was fixed."""
    applications_fixture(tmp_path)
    session = Session(tmp_path)
    data = recorded('application_dimension_turn1')
    steps = [s for s in data['steps'] if s['trace_step'] < 11]
    # Make execution fail for a real reason: occupy one target before executing.
    def occupy(request):
        if request.get('tool') != 'file_plan':
            return 'y'
        target = tmp_path / '资料/按领域/医疗'
        target.mkdir(parents=True, exist_ok=True)
        (target / 'CNN_02.pdf').write_text('occupied')
        return 'y'
    session_confirm = Confirmer('always', ask_action=occupy)
    client = Replay(steps + [s for s in data['steps'] if s['trace_step'] >= 11], session.ids)
    history = [dict(role='system', content=SYSTEM_PROMPTS[CFG['prompt']]), dict(role='user', content=data['instruction'])]
    result = run_agent(data['instruction'], LLM({'model': 'fake'}, client=client), session.sb, CFG,
                       Console(file=io.StringIO()), artifacts=session.store, history=history,
                       oplog=OpLog(session.sb, 'replay_fail'), confirmer=session_confirm)
    client.absorb(result.messages)
    execution = client.calls('execute_file_plan')
    assert execution and execution[0]['status'] == 'error', client.log
    assert (tmp_path / '资料/按领域/医疗/CNN_02.pdf').read_text() == 'occupied'
    assert result.stop_reason == 'partial'
    assert execution[0]['text'].removeprefix('ERROR: ')[:20] in result.final, result.final
