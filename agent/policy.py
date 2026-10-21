"""Task preferences are data; they never grant permissions or prove semantics."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EvidencePolicy(PolicyModel):
    pdf_pages: int | None = Field(2, ge=1, le=10000,
                                 description='抽取页数；null 为全部文字层，不能代替 OCR')
    excerpt_chars: int = Field(600, ge=100, le=600,
                               description='模型可见片段长度；全文抽取仍在本地')


class OutputPolicy(PolicyModel):
    format: Literal['markdown', 'text', 'json'] = 'markdown'
    detail: Literal['concise', 'full'] = 'concise'


class TaskPolicy(PolicyModel):
    criteria: list[str] = Field(default_factory=list, max_length=30,
                               description='用户的判断标准；由模型解释，不能冒充本地安全谓词')
    evidence: EvidencePolicy = Field(default_factory=EvidencePolicy)
    output: OutputPolicy = Field(default_factory=OutputPolicy)


class ExecutionStep(PolicyModel):
    id: str = Field(min_length=1, max_length=60)
    goal_ids: list[str] = Field(min_length=1, max_length=30)
    description: str = Field(min_length=1, max_length=800)


def active_goals(runtime):
    """Single-goal tasks have an unambiguous compatibility step."""
    if runtime.active_step:
        return runtime.active_step['goal_ids']
    goals = (runtime.current or {}).get('goals', [])
    return [goals[0]['id']] if len(goals) == 1 else []


def applies(constraint, goal_ids):
    scope = constraint.get('goal_ids', [])
    return not scope or bool(set(scope).intersection(goal_ids))


def evidence_policy(sb):
    task = getattr(getattr(sb, 'runtime', None), 'current', None) or {}
    return EvidencePolicy.model_validate(task.get('policy', {}).get('evidence', {}))


def retain_step(before, after, step):
    """Preserve binding only when its semantic and permission dependencies match."""
    if not before or not step or before['id'] != after['id']:
        return None
    ids = set(step['goal_ids'])
    def dependencies(task):
        policy = task.get('policy', {})
        return dict(goals=[g for g in task['goals'] if g['id'] in ids],
                    constraints=[c for c in task['constraints'] if applies(c, ids)],
                    read_roots=task['read_roots'], write_roots=task['write_roots'],
                    allowed_effects=task['allowed_effects'], cancelled=task.get('cancelled', False),
                    unresolved_questions=task['unresolved_questions'],
                    criteria=policy.get('criteria', []), evidence=policy.get('evidence', {}))
    if ids <= {g['id'] for g in after['goals']} and dependencies(before) == dependencies(after):
        return step
    return None
