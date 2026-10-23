"""Versioned task specifications, independent of the model's chat transcript."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from agent.policy import TaskPolicy


Effect = Literal['create', 'move', 'rename', 'trash']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


# The six conditions protocol v3 compiles (agent/intent.py TEMPLATES). Tasks
# stored by protocol v2 may hold others; they are legacy, kept read-only.
CONDITION_KINDS = ('evidence_valid', 'file_plan_verified', 'manual_review',
                   'plan_ready', 'plan_applied', 'receipt_verified')
RULE_PREDICATES = ('within', 'outside', 'extensions', 'max_count')
LEGACY_NOTE = '这是旧版本（协议 v2）登记的任务，只读，不再核验；需要继续请新建任务'


class CompletionCondition(StrictModel):
    kind: Literal[CONDITION_KINDS]
    reference: str = Field('', description='通常留空：本地把该目标的计划/回执/证据确定性绑定；仅引用已有对象时填精确 E/P/R/X 标识')


def is_legacy(task):
    """A v2-era task: a condition, evaluation mode or rule v3 no longer verifies."""
    return (any(c['kind'] not in CONDITION_KINDS or c.get('evaluation', 'current_state') != 'current_state'
                for g in task.get('goals', []) for c in g['conditions'])
            or any(r['predicate'] not in RULE_PREDICATES for c in task.get('constraints', []) for r in c.get('rules', [])))


def normalized(task):
    """Drop the empty v2 condition fields that older revisions stored, so unchanged
    goals compare equal to their re-validated form. Legacy tasks stay as stored."""
    if not task: return task
    from agent.choices import migrate
    migrate(task)
    if is_legacy(task): return task
    for g in task.get('goals', []):
        g['conditions'] = [{k: c[k] for k in ('kind', 'reference') if k in c} for c in g['conditions']]
    return task


class Goal(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=800)
    conditions: list[CompletionCondition] = Field(min_length=1, max_length=12)
    semantic_review: bool = Field(False, description='分类正确性等语义结论须为 true，本地不能证明')


class Constraint(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=800)
    forbidden_effects: list[Effect] = Field(default_factory=list)
    rules: list['OperationRule'] = Field(default_factory=list, max_length=20)
    goal_ids: list[str] = Field(default_factory=list, max_length=30,
                                description='空表示整项任务；非空只约束为这些目标执行的步骤')


class OperationRule(StrictModel):
    effects: list[Effect] = Field(min_length=1)
    selector: Literal['source', 'destination'] = 'source'
    predicate: Literal[RULE_PREDICATES]
    values: list[str] = Field(default_factory=list, max_length=30)
    limit: int | None = Field(None, ge=0)
    count_scope: Literal['task', 'run'] = Field('task', description='数量上限跨续做累计，或仅本轮')

    @model_validator(mode='after')
    def arguments_present(self):
        if self.predicate != 'max_count' and not self.values:
            raise ValueError('该约束需要 values')
        if self.predicate == 'max_count' and self.limit is None:
            raise ValueError('数量约束需要 limit')
        if self.predicate != 'max_count':
            if 'create' in self.effects and self.selector == 'source':
                raise ValueError('create 没有 source；路径约束必须 selector=destination，并与其他副作用规则分开')
            if 'trash' in self.effects and self.selector == 'destination':
                raise ValueError('trash 没有用户目标 destination；请使用 selector=source')
        return self


Constraint.model_rebuild()


class TaskSubmission(StrictModel):
    goals: list[Goal] = Field(min_length=1, max_length=30)
    read_roots: list[str] = Field(default_factory=lambda: ['.'], min_length=1, max_length=1000)
    write_roots: list[str] = Field(default_factory=list, max_length=1000)
    allowed_effects: list[Effect] = Field(default_factory=list,
                                       description='需求解释，不能替代本地确认或调用方授权策略')
    constraints: list[Constraint] = Field(default_factory=list, max_length=40)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    policy: TaskPolicy = Field(default_factory=TaskPolicy)

    @model_validator(mode='after')
    def unique_ids(self):
        for items in (self.goals, self.constraints):
            if len({item.id for item in items}) != len(items):
                raise ValueError('目标或约束 ID 重复')
        return self


class TaskPatch(StrictModel):
    expected_revision: int = Field(ge=1)
    upsert_goals: list[Goal] = Field(default_factory=list, max_length=30)
    remove_goal_ids: list[str] = Field(default_factory=list, max_length=30)
    upsert_constraints: list[Constraint] = Field(default_factory=list, max_length=40)
    remove_constraint_ids: list[str] = Field(default_factory=list, max_length=40)
    read_roots: list[str] | None = Field(None, min_length=1, max_length=1000)
    write_roots: list[str] | None = Field(None, max_length=1000)
    allowed_effects: list[Effect] | None = None
    unresolved_questions: list[str] | None = None
    reason: str = Field(min_length=1, max_length=800)
    cancel: bool | None = None
    policy: TaskPolicy | None = None


def add_blocker(task, source, obj, message, run_id=None):
    """Record one open blocker; identical open entries are not duplicated."""
    rows = task.setdefault('blockers', [])
    row = dict(source=source, object=obj or '', revision=task['revision'], run_id=run_id,
               message=message, status='open', resolved_by='')
    same = [b for b in rows if b.get('status', 'open') == 'open' and b.get('source') == source
            and b.get('object', '') == row['object']]
    if any(b['message'] == message for b in same): return
    # Only the latest attempt on one object is its current reason.
    for b in same: b.update(status='superseded', resolved_by='同一对象的后续尝试给出新的原因')
    rows.append(row)


def resolve_blockers(task, match, reason):
    """Mark matching open blockers resolved. Resolution is explicit, never a bulk clear."""
    for b in task.get('blockers', []):
        if b.get('status', 'open') == 'open' and match(b):
            b.update(status='resolved', resolved_by=reason)


def open_blockers(task):
    # Legacy rows without lifecycle fields stay visible rather than being hidden.
    return [b for b in task.get('blockers', []) if b.get('status', 'open') == 'open']


def task_state(task):
    if task.get('cancelled'):
        return 'cancelled'
    if task.get('constraint_failures'):
        return 'partial'
    if task['unresolved_questions']:
        return 'waiting_input'
    if not task.get('checks') or not all(c['passed'] for c in task['checks']):
        return 'partial'
    accepted = {g for r in task.get('reviews', []) if r['revision'] == task['revision']
                and r['decision'] == 'accept' for g in r.get('goal_ids', [])}
    accepted_constraints = {c for r in task.get('reviews', []) if r['revision'] == task['revision']
                            and r['decision'] == 'accept' for c in r.get('constraint_ids', [])}
    if (any(c['needs_review'] and c['goal_id'] not in accepted for c in task['checks']) or
            any(not c['forbidden_effects'] and not c.get('rules') and c['id'] not in accepted_constraints
                for c in task['constraints'])):
        # A write task that never executed an operation has no result to
        # review; the next step (a conflict, a choice) is the user's.
        declared = task.get('declared_unfinished') or {}
        nothing_needed = declared.get('revision') == task['revision'] and declared.get('not_needed')
        if task.get('allowed_effects') and not (task.get('operation_history') or task.get('operations')) and not nothing_needed:
            return 'waiting_input'
        return 'needs_review'
    latest = {}
    for r in task.get('reviews', []):
        if r['revision'] == task['revision']:
            for gid in r.get('goal_ids', []): latest['goal:'+gid] = r['decision']
            for cid in r.get('constraint_ids', []): latest['constraint:'+cid] = r['decision']
    if 'reject' in latest.values():
        return 'needs_review'
    return 'completed'


class TaskStore:
    """SQLite task revisions; root and session isolation also applies to direct IDs.

    This persists task checkpoints, not an atomic filesystem operation journal.
    Each method owns its connection so web workers do not share connections.
    """
    def __init__(self, path: Path, root: Path, session: str):
        self.path, self.root, self.session = Path(path), str(root.resolve()), session
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS task_revisions (
                id TEXT NOT NULL, revision INTEGER NOT NULL, root TEXT NOT NULL,
                session TEXT NOT NULL, data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(id, revision))''')
            db.execute('''CREATE TABLE IF NOT EXISTS task_edits (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, root TEXT NOT NULL,
                session TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending')''')
            db.execute('''CREATE TABLE IF NOT EXISTS file_plans (
                id TEXT PRIMARY KEY, root TEXT NOT NULL, session TEXT NOT NULL, data_json TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS run_outcomes (
                run_id TEXT NOT NULL, root TEXT NOT NULL, session TEXT NOT NULL,
                data_json TEXT NOT NULL, PRIMARY KEY(run_id,root,session))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('''SELECT data_json FROM task_revisions
                WHERE id=? AND root=? AND session=? ORDER BY revision DESC LIMIT 1''',
                (task_id, self.root, self.session)).fetchone()
        if not row:
            raise ValueError('任务不存在或不属于当前资料库/会话')
        return normalized(json.loads(row['data_json']))

    def save_file_plan(self, plan):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO file_plans VALUES(?,?,?,?)',
                       (plan['id'], self.root, self.session, json.dumps(plan, ensure_ascii=False)))

    def file_plan(self, plan_id):
        with self.connect() as db:
            row = db.execute('SELECT data_json FROM file_plans WHERE id=? AND root=? AND session=?',
                             (plan_id, self.root, self.session)).fetchone()
        if not row:
            raise ValueError('计划不存在或不属于当前资料库/会话')
        return json.loads(row['data_json'])

    def save_outcome(self, data):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO run_outcomes(run_id,root,session,data_json) VALUES(?,?,?,?)',
                       (data['run_id'],self.root,self.session,json.dumps(data,ensure_ascii=False)))

    def outcome(self, run_id=None):
        with self.connect() as db:
            query='SELECT data_json FROM run_outcomes WHERE root=? AND session=?'
            args=[self.root,self.session]
            if run_id:query+=' AND run_id=?';args.append(run_id)
            rows=db.execute(query+' ORDER BY rowid DESC',args).fetchall()
        for row in rows:
            data=json.loads(row['data_json'])
            if run_id or data.get('purpose')!='inspection':return data
        raise ValueError('当前会话没有对应的运行结果')

    @staticmethod
    def retained_observations(previous, after, run_id):
        """Same-run facts survive a revision; goal tags survive only for unchanged goals."""
        unchanged = {g['id'] for g in (previous or {}).get('goals', [])
                     if g in after.get('goals', [])}
        return [dict(o, goal_ids=[g for g in o.get('goal_ids', []) if g in unchanged])
                for o in (previous or {}).get('observations', [])
                if o['status'] == 'ok' and run_id and o.get('run_id') == run_id]

    @staticmethod
    def retained_references(previous):
        refs=list((previous or {}).get('references', []))
        refs += [r for o in (previous or {}).get('observations', []) if o['status']=='ok'
                 for r in o.get('references', [])]
        merged = {}
        for ref in refs:
            key = ref['collection'], ref['id']
            old = merged.get(key, {})
            merged[key] = dict(ref, goal_ids=sorted(set(old.get('goal_ids', []) + ref.get('goal_ids', []))))
        return list(merged.values())

    def recent(self, limit=5):
        with self.connect() as db:
            rows = db.execute('''SELECT data_json FROM task_revisions t
                WHERE root=? AND session=? AND revision=(
                    SELECT MAX(revision) FROM task_revisions WHERE id=t.id)
                ORDER BY updated_at DESC, rowid DESC LIMIT ?''',
                (self.root, self.session, limit)).fetchall()
        return [normalized(json.loads(row['data_json'])) for row in rows]

    def submit(self, spec: TaskSubmission, instruction: str, run_id: str):
        """A new task; later changes go through preview_edit/apply_edit."""
        data = spec.model_dump()
        data.update(id='T' + uuid.uuid4().hex[:12], revision=1, source_messages=[dict(run_id=run_id, content=instruction)],
                    status='running', observations=[], outputs=[], checks=[], reviews=[], references=[])
        self.inherit_execution(data, None)
        from agent.constraints import validate_spec
        from safety.sandbox import Sandbox
        validate_spec(data, Sandbox(self.root))
        with self.connect() as db:
            db.execute('''INSERT INTO task_revisions(id,revision,root,session,data_json)
                VALUES(?,?,?,?,?)''', (data['id'], 1, self.root, self.session, json.dumps(data, ensure_ascii=False)))
        return data

    def preview_edit(self, task_id, patch: TaskPatch):
        from copy import deepcopy
        before = self.get(task_id)
        if is_legacy(before):
            raise ValueError(LEGACY_NOTE)
        if before['revision'] != patch.expected_revision:
            raise ValueError('任务版本冲突')
        after = deepcopy(before)
        after.setdefault('cancelled', False)
        if patch.cancel is not None:
            after['cancelled'] = patch.cancel
        for collection, added, removed in (
            ('goals', patch.upsert_goals, patch.remove_goal_ids),
            ('constraints', patch.upsert_constraints, patch.remove_constraint_ids)):
            rows = {x['id']: x for x in before[collection]}
            if len(set(removed)) != len(removed) or len({x.id for x in added}) != len(added):
                raise ValueError('编辑中 ID 重复')
            if set(removed) - rows.keys() or set(removed) & {x.id for x in added}:
                raise ValueError('移除对象不存在或同时被更新')
            for key in removed: del rows[key]
            rows.update({x.id: x.model_dump() for x in added})
            after[collection] = list(rows.values())
        for key in ('read_roots', 'write_roots', 'allowed_effects', 'unresolved_questions', 'policy'):
            if getattr(patch, key) is not None:
                value = getattr(patch, key)
                after[key] = value.model_dump() if isinstance(value, BaseModel) else value
        # Reuse the creation schema for cardinality, IDs and nested validation.
        fields = TaskSubmission.model_fields.keys()
        validated = TaskSubmission.model_validate({k: after[k] for k in fields if k in after})
        after.update(validated.model_dump())
        from agent.constraints import validate_spec
        from safety.sandbox import Sandbox
        validate_spec(after, Sandbox(self.root))
        changes = [dict(field=key, before=before.get(key), after=after[key]) for key in fields
                   if before.get(key, TaskPolicy().model_dump() if key == 'policy' else None) != after[key]]
        if before.get('cancelled', False) != after['cancelled']:
            changes.append(dict(field='cancelled', before=before.get('cancelled', False), after=after['cancelled']))
        if not changes:
            raise ValueError('任务没有发生变化')
        edit = dict(id='D'+uuid.uuid4().hex[:12], task_id=task_id, revision=before['revision'],
                    reason=patch.reason, changes=changes, after=after)
        with self.connect() as db:
            db.execute('INSERT INTO task_edits(id,task_id,root,session,revision,payload) VALUES(?,?,?,?,?,?)',
                       (edit['id'], task_id, self.root, self.session, before['revision'], json.dumps(edit, ensure_ascii=False)))
        return edit

    def edit(self, edit_id):
        with self.connect() as db:
            row = db.execute('SELECT payload,status FROM task_edits WHERE id=? AND root=? AND session=?',
                             (edit_id, self.root, self.session)).fetchone()
        if not row or row['status'] != 'pending':
            raise ValueError('编辑不存在、不属于当前会话或已经处理')
        return json.loads(row['payload'])

    def apply_edit(self, edit_id, instruction, run_id, *, actor):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            edit = self.edit(edit_id)
            before = self.get(edit['task_id'])
            if before['revision'] != edit['revision']:
                raise ValueError('确认期间任务版本改变，旧编辑已失效')
            data = edit['after']
            from agent.constraints import validate_spec
            from safety.sandbox import Sandbox
            validate_spec(data, Sandbox(self.root))
            data.update(revision=before['revision']+1, status='cancelled' if data.get('cancelled') else 'running', observations=[], outputs=[], checks=[], reviews=[], constraint_failures=[],
                        source_messages=before['source_messages']+[dict(content=instruction, run_id=run_id, actor=actor)],
                        last_edit=dict(id=edit_id, reason=edit['reason'], actor=actor))
            data['references']=self.retained_references(before)
            data['observations']=self.retained_observations(before, data, run_id)
            self.inherit_execution(data, before)
            db.execute('INSERT INTO task_revisions(id,revision,root,session,data_json) VALUES(?,?,?,?,?)',
                       (data['id'], data['revision'], self.root, self.session, json.dumps(data, ensure_ascii=False)))
            db.execute("UPDATE task_edits SET status='applied' WHERE id=?", (edit_id,))
        return data

    @staticmethod
    def inherit_execution(data, previous):
        """A new revision is not a new budget or a new execution history."""
        from copy import deepcopy
        previous = previous or {}
        data['usage'] = deepcopy(previous.get('usage', {}))
        data['file_plan_receipts'] = deepcopy(previous.get('file_plan_receipts', {}))
        # A plan proves the goal it was prepared for; a rewritten goal starts over.
        unchanged = {g['id'] for g in previous.get('goals', []) if g in data.get('goals', [])}
        data['file_plan_bindings'] = {pid: dict(deepcopy(b), goal_ids=[g for g in b['goal_ids'] if g in unchanged])
                                      for pid, b in previous.get('file_plan_bindings', {}).items()}
        # Blockers keep their history; open ones belong to the old contract.
        data['blockers'] = deepcopy(previous.get('blockers', []))
        for b in data['blockers']:
            if b.get('status', 'open') == 'open':
                b.update(status='superseded', resolved_by=f"任务已修订为第 {data['revision']} 版")
        data['steps'] = deepcopy(previous.get('steps', []))
        data['operation_history'] = deepcopy(previous.get('operation_history', []) + previous.get('operations', []))
        data['operations'] = []

    def review(self, task_id, revision, goal_ids, constraint_ids, decision, note, sb):
        from agent.verifier import verify_task
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self.get(task_id)
            if decision not in {'accept', 'reject'}:
                raise ValueError('未知复核决定')
            if data['revision'] != revision:
                raise ValueError('任务版本冲突，复核已失效')
            if not goal_ids and not constraint_ids:
                raise ValueError('请选择需要复核的目标或约束')
            if set(goal_ids) - {g['id'] for g in data['goals']} or set(constraint_ids) - {c['id'] for c in data['constraints']}:
                raise ValueError('复核对象不存在')
            data['checks'] = verify_task(data, sb)
            if decision == 'accept' and any(not c['passed'] for c in data['checks'] if c['goal_id'] in goal_ids):
                raise ValueError('本地完成条件未满足，不能用人工确认覆盖')
            data.setdefault('reviews', []).append(dict(revision=revision, goal_ids=goal_ids,
                constraint_ids=constraint_ids, decision=decision, note=note, actor='user'))
            data['status'] = task_state(data)
            db.execute('UPDATE task_revisions SET data_json=? WHERE id=? AND revision=? AND root=? AND session=?',
                       (json.dumps(data, ensure_ascii=False), task_id, revision, self.root, self.session))
        return data

    def recheck(self, task_id, revision, sb):
        from agent.verifier import verify_task
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data=self.get(task_id)
            if data['revision']!=revision:raise ValueError('任务版本冲突')
            data['checks']=verify_task(data,sb)
            data['status']=task_state(data)
            db.execute('UPDATE task_revisions SET data_json=? WHERE id=? AND revision=? AND root=? AND session=?',
                       (json.dumps(data,ensure_ascii=False),task_id,revision,self.root,self.session))
        return data

    def checkpoint(self, data):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = self.get(data['id'])
            if latest['revision'] != data['revision']:
                raise ValueError('任务版本已被其他请求更新，停止当前执行')
            db.execute('''UPDATE task_revisions SET data_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND revision=? AND root=? AND session=?''',
                (json.dumps(data, ensure_ascii=False), data['id'], data['revision'], self.root, self.session))
