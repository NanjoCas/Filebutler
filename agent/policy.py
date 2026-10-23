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


def active_goals(runtime):
    """The goal a write serves: the only goal, or the only one verifying its receipt."""
    goals = (runtime.current or {}).get('goals', [])
    if len(goals) == 1:
        return [goals[0]['id']]
    # A write tool's receipt names its purpose when exactly one goal verifies it.
    from agent.verifier import CONSUMERS
    kinds = {k for p in getattr(runtime, '_producers', ()) or () for k in CONSUMERS.get(p, ())}
    matches = [g['id'] for g in goals if kinds and any(c['kind'] in kinds for c in g['conditions'])]
    return matches if len(matches) == 1 else []


def applies(constraint, goal_ids):
    scope = constraint.get('goal_ids', [])
    return not scope or bool(set(scope).intersection(goal_ids))


def evidence_policy(sb):
    task = getattr(getattr(sb, 'runtime', None), 'current', None) or {}
    return EvidencePolicy.model_validate(task.get('policy', {}).get('evidence', {}))
