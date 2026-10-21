"""Protocol v3: the model states intent and objects; the machine contract is compiled here.

The model names what the user wants (parts of a request, each of one kind) and
the limits the user actually stated. Completion conditions, effect permissions
and rules are derived locally, so the model no longer writes them by hand.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from agent.task import StrictModel, Effect, TaskSubmission, TaskPatch


Kind = Literal['find', 'dedup', 'organize', 'classify', 'answer']

# Kind -> (completion conditions, semantic review, effects the part may need).
# References stay empty: plans, receipts and evidence bind locally.
TEMPLATES = {
    'find': ([dict(kind='evidence_valid')], True, []),
    'dedup': ([dict(kind='receipt_verified')], False, ['trash']),
    'organize': ([dict(kind='file_plan_verified')], False, ['create', 'move', 'rename', 'trash']),
    'classify': ([dict(kind='plan_ready')], True, []),
    'answer': ([dict(kind='manual_review')], True, []),
}
LIMITS_ID = 'user_limits'


class Part(StrictModel):
    id: str | None = Field(None, max_length=60, description='可省略，本地生成；revise_task 用同一 ID 替换该部分')
    kind: Kind = Field(description='find 查找并给证据；dedup 去重回收；organize 移动/改名/建目录/回收；'
                                   'classify 形成分类方案；answer 只解释或回答')
    description: str = Field(min_length=1, max_length=800)
    scope: list[str] = Field(default_factory=list, max_length=20,
        description='这部分涉及的目录；只是范围估计，不是用户限制，可留空')
    execute: bool = Field(False, description='仅 classify：用户要求本轮按方案移动文件')


class Limits(StrictModel):
    """Only what the user said. Each field compiles to a local rule; none grants access."""
    no_effects: list[Effect] = Field(default_factory=list, description='用户禁止的操作')
    keep_unchanged: list[str] = Field(default_factory=list, max_length=30, description='用户要求保持原样的路径')
    only_within: list[str] = Field(default_factory=list, max_length=30, description='用户限定的操作范围')
    only_extensions: list[str] = Field(default_factory=list, max_length=30, description='只处理这些后缀')
    max_files: int | None = Field(None, ge=0, description='移动/改名/回收的对象总数上限，不含自动创建的目录')
    notes: list[str] = Field(default_factory=list, max_length=20,
        description='只放用户提出、但以上字段和 parts 都无法表达的限制，通常留空。'
                    '不要复述 parts 的内容，也不要重复已由其他字段表达的限制（如后缀、保持原样、禁止操作、去重保留规则）。'
                    '每写一条，执行前都要用户确认，结果会进入人工复核')


class StartTask(StrictModel):
    summary: str = Field(min_length=1, max_length=800, description='一句话概括用户要求')
    parts: list[Part] = Field(min_length=1, max_length=10)
    limits: Limits = Field(default_factory=Limits)
    questions: list[str] = Field(default_factory=list, max_length=20,
        description='必须由用户回答才能继续的问题；有问题时不开放写入')
    criteria: list[str] = Field(default_factory=list, max_length=30, description='用户的判断标准，如分类维度')
    detail: Literal['concise', 'full'] = 'concise'

    @model_validator(mode='after')
    def unique_parts(self):
        ids = [p.id for p in self.parts if p.id]
        if len(ids) != len(set(ids)):
            raise ValueError('parts 的 ID 重复')
        return self


class ReviseTask(StrictModel):
    task_id: str
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=800, description='用户的更正或补充')
    parts: list[Part] = Field(default_factory=list, max_length=10, description='新增部分；同 ID 替换原部分')
    remove_part_ids: list[str] = Field(default_factory=list, max_length=10)
    limits: Limits | None = Field(None, description='完整的新限制；放宽原限制会在确认卡片中显示')
    questions: list[str] | None = None
    cancel: bool | None = None


def _effects(part):
    effects = TEMPLATES[part.kind][2]
    return ['create', 'move'] if part.kind == 'classify' and part.execute else effects


def compile_goal(part, index):
    conditions, review, _ = TEMPLATES[part.kind]
    if part.kind == 'classify' and part.execute:
        conditions = [dict(kind='plan_applied')]
    return dict(id=part.id or f'{part.kind}_{index}', description=part.description,
                conditions=[dict(c) for c in conditions], semantic_review=review)


def compile_limits(limits):
    """User limits -> constraints. Text notes stay text and need review."""
    rules = []
    sources, targets = ['move', 'rename', 'trash'], ['create', 'move', 'rename']
    if limits.only_within:
        rules += [dict(effects=sources, selector='source', predicate='within', values=limits.only_within),
                  dict(effects=targets, selector='destination', predicate='within', values=limits.only_within)]
    if limits.keep_unchanged:
        rules += [dict(effects=sources, selector='source', predicate='outside', values=limits.keep_unchanged),
                  dict(effects=targets, selector='destination', predicate='outside', values=limits.keep_unchanged)]
    if limits.only_extensions:
        rules.append(dict(effects=sources, selector='source', predicate='extensions', values=limits.only_extensions))
    if limits.max_files is not None:
        rules.append(dict(effects=sources, predicate='max_count', limit=limits.max_files, count_scope='task'))
    constraints = []
    if rules or limits.no_effects:
        constraints.append(dict(id=LIMITS_ID, description=describe_limits(limits),
                                forbidden_effects=list(limits.no_effects), rules=rules))
    constraints += [dict(id=f'note_{i}', description=text) for i, text in enumerate(limits.notes, 1)]
    return constraints


def describe_limits(limits):
    names = {'create': '建目录', 'move': '移动', 'rename': '改名', 'trash': '回收'}
    parts = []
    if limits.no_effects: parts.append('不' + '、不'.join(names[e] for e in limits.no_effects))
    if limits.keep_unchanged: parts.append('保持原样：' + '、'.join(limits.keep_unchanged))
    if limits.only_within: parts.append('只在 ' + '、'.join(limits.only_within) + ' 内操作')
    if limits.only_extensions: parts.append('只处理 ' + '、'.join(limits.only_extensions))
    if limits.max_files is not None: parts.append(f'最多 {limits.max_files} 个对象')
    return '；'.join(parts) or '用户限制'


def _write_roots(parts):
    writing = [p for p in parts if _effects(p)]
    if not writing: return []
    # An unscoped writing part is the whole library; plans narrow it to exact objects.
    if any(not p.scope for p in writing): return ['.']
    return list(dict.fromkeys(s for p in writing for s in p.scope))


def compile_start(args: StartTask) -> TaskSubmission:
    goals = [compile_goal(p, i) for i, p in enumerate(args.parts, 1)]
    if len({g['id'] for g in goals}) != len(goals):
        raise ValueError('parts 的 ID 重复')
    effects = sorted({e for p in args.parts for e in _effects(p)} - set(args.limits.no_effects))
    return TaskSubmission.model_validate(dict(
        goals=goals, read_roots=['.'], write_roots=_write_roots(args.parts) if effects else [],
        allowed_effects=effects, constraints=compile_limits(args.limits),
        unresolved_questions=args.questions,
        policy=dict(criteria=args.criteria, output=dict(detail=args.detail))))


def compile_revision(args: ReviseTask, task) -> TaskPatch:
    """A revision of an existing task. Only the edit preview and the user's
    confirmation apply it; nothing here bypasses TaskStore.preview_edit."""
    known = {g['id'] for g in task['goals']}
    if set(args.remove_part_ids) - known:
        raise ValueError('要移除的部分不存在：' + '、'.join(sorted(set(args.remove_part_ids) - known)))
    goals = [compile_goal(p, len(task['goals']) + i) for i, p in enumerate(args.parts, 1)]
    patch = dict(expected_revision=args.expected_revision, reason=args.reason,
                 upsert_goals=goals, remove_goal_ids=args.remove_part_ids)
    added = {e for p in args.parts for e in _effects(p)}
    if args.limits is not None:
        old = [c['id'] for c in task['constraints'] if c['id'] == LIMITS_ID or c['id'].startswith('note_')]
        new = compile_limits(args.limits)
        patch['upsert_constraints'] = new
        patch['remove_constraint_ids'] = [c for c in old if c not in {n['id'] for n in new}]
        forbidden = set(args.limits.no_effects)
    else:
        forbidden = {e for c in task['constraints'] if c['id'] == LIMITS_ID for e in c['forbidden_effects']}
    if added:
        effects = sorted((set(task['allowed_effects']) | added) - forbidden)
        if effects != sorted(task['allowed_effects']): patch['allowed_effects'] = effects
        roots = list(dict.fromkeys(task['write_roots'] + _write_roots(args.parts)))
        if roots != task['write_roots']: patch['write_roots'] = roots
    if args.questions is not None: patch['unresolved_questions'] = args.questions
    if args.cancel is not None: patch['cancel'] = args.cancel
    return TaskPatch.model_validate(patch)


def project(task):
    """The v3 view of a stored task: parts, limits and state, not raw conditions."""
    kinds = {'evidence_valid': 'find', 'receipt_verified': 'dedup', 'file_plan_verified': 'organize',
             'plan_ready': 'classify', 'plan_applied': 'classify', 'manual_review': 'answer'}
    parts = [dict(id=g['id'], kind=next((kinds[c['kind']] for c in g['conditions'] if c['kind'] in kinds), 'other'),
                  description=g['description']) for g in task['goals']]
    limits = [c['description'] for c in task['constraints']]
    plans = list(task.get('file_plan_bindings', {}))
    return dict(id=task['id'], revision=task['revision'], status=task.get('status'), parts=parts,
                limits=limits, questions=task['unresolved_questions'], plans=plans[-6:],
                checks=[dict(part=c['goal_id'], passed=c['passed']) for c in task.get('checks', [])])
