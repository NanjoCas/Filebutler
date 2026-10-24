"""Scripted fake model for a pipeline dry run of eval/live_policy.py; no API.

``python -m eval.live_policy --dry-run --suite holdout --out <new dir>``

A script is fixed tool calls per turn. It proves that fixture setup, the
production agent loop, per-turn checks, the multi-dimension judge and undo run
end to end. It does not adapt to tool results beyond substituting local IDs,
so a pass says nothing about what a real model would do.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace as NS

from agent.llm import LLM


PATTERNS = dict(plan=r'plan_id=(X[0-9a-f]+)', task=r'task_id=(T[0-9a-f]+)', revision=r'revision=(\d+)')


class ScriptedClient:
    """OpenAI-compatible client. Each step is a list of (tool, args) or a final str.

    ``args`` may be a callable receiving the shared ID dict (task, revision,
    plan) learned from earlier tool results of the same case.
    """

    def __init__(self, steps, ids):
        self.steps, self.ids, self.requests = list(steps), ids, 0
        self.chat = NS(completions=NS(create=self.create))

    def _learn(self, messages):
        for m in messages:
            if m.get('role') != 'tool': continue
            content = m.get('content') or ''
            try: content = json.loads(content).get('text', content)
            except (ValueError, AttributeError): pass
            for name, pattern in PATTERNS.items():
                found = re.findall(pattern, str(content))
                if found: self.ids[name] = found[-1]

    def create(self, **kw):
        self.requests += 1
        self._learn(kw['messages'])
        item = self.steps.pop(0) if self.steps else '（脚本结束）'
        if isinstance(item, str) and any(s['function']['name'] == 'finish_turn' for s in kw.get('tools') or []):
            item = [('finish_turn', dict(mode='reply', reply=item))]  # the terminal call (routing redesign S5)
        if isinstance(item, str):
            msg = NS(content=item, tool_calls=None)
        else:
            calls = []
            for i, (tool, args) in enumerate(item):
                args = args(self.ids) if callable(args) else args
                calls.append(NS(id=f'dry_{self.requests}_{i}',
                                function=NS(name=tool, arguments=json.dumps(args, ensure_ascii=False))))
            msg = NS(content='', tool_calls=calls)
        return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=100, completion_tokens=10))


def _moves(pairs): return [dict(effect='move', source=s, destination=d) for s, d in pairs]
def _finish(text): return [('finish_task', dict(summary=text))]
def _execute(): return [('execute_file_plan', lambda v: dict(plan_id=v['plan']))]
def _organize(pid, text): return dict(id=pid, kind='organize', description=text)


TOPIC = 'Topics/光合作用'
Q4 = ('Q4_final.pdf', 'Q4_draft.pdf')

SCRIPTS = {
    'holdout_search_dedup_move': [[
        [('start_task', dict(
            summary='Notes 里字节相同的副本只留文件名最短的一份；光合作用笔记按原名移到 Topics/光合作用',
            parts=[dict(id='dedup', kind='dedup', description='Notes 里字节相同的副本只保留文件名最短的一份'),
                   _organize('place', '讨论光合作用的笔记按原名移到 Topics/光合作用')],
            limits=dict(no_effects=['rename']))),
         ('deduplicate', dict(path='Notes', mode='bytes', keep='shortest_name'))],
        [('read_file', dict(path='Notes/note_01.txt')), ('read_file', dict(path='Notes/note_02.txt')),
         ('read_file', dict(path='Notes/note_03.txt')), ('read_file', dict(path='Notes/note_04.txt'))],
        [('prepare_file_plan', dict(
            description='把两篇光合作用笔记移到 Topics/光合作用',
            actions=_moves([('Notes/note_01.txt', f'{TOPIC}/note_01.txt'), ('Notes/note_02.txt', f'{TOPIC}/note_02.txt')])))],
        _execute(),
        _finish('note_01.txt 与 note_02.txt 讨论光合作用，已移到 Topics/光合作用；副本 note_01_copy.txt 已回收。'),
    ]],
    'holdout_cross_turn_revision': [
        [[('start_task', dict(summary='Reports 根目录全部 PDF 按原名移到 Reports/2025',
                              parts=[_organize('file_2025', 'Reports 根目录全部 PDF 按原名移到 Reports/2025')],
                              limits=dict(no_effects=['create', 'rename', 'trash'])))],
         [('prepare_file_plan', dict(description='三份 PDF 移到 Reports/2025',
                                     actions=_moves((f'Reports/{n}', f'Reports/2025/{n}') for n in ('q1_summary.pdf',) + Q4)))],
         _execute(), _finish('3 份 PDF 已移到 Reports/2025。')],
        [[('revise_task', lambda v: dict(
            task_id=v['task'], expected_revision=int(v['revision']), reason='用户更正：只有 Q4 PDF 放 2025，q1_summary.pdf 移回根目录',
            parts=[_organize('file_2025', '只有文件名含 Q4 的 PDF 放在 Reports/2025，q1_summary.pdf 移回 Reports 根目录')]))],
         [('prepare_file_plan', dict(description='q1_summary.pdf 移回 Reports 根目录',
                                     actions=_moves([('Reports/2025/q1_summary.pdf', 'Reports/q1_summary.pdf')])))],
         _execute(), _finish('已按更正把 q1_summary.pdf 移回 Reports 根目录。')],
    ],
    'holdout_partial_resume': [
        [[('start_task', dict(summary='本轮只移动 a 开头的 txt 到 Done',
                              parts=[_organize('batch_a', '本轮只移动 a 开头的 txt 到 Done')],
                              limits=dict(no_effects=['create', 'rename', 'trash'])))],
         [('prepare_file_plan', dict(description='a1、a2 移到 Done', actions=_moves([('Inbox/a1.txt', 'Done/a1.txt'), ('Inbox/a2.txt', 'Done/a2.txt')])))],
         _execute(), _finish('a 开头的 2 个 txt 已移到 Done；b1.txt、b2.txt 等待下一轮确认。')],
        [[('revise_task', lambda v: dict(
            task_id=v['task'], expected_revision=int(v['revision']), reason='用户确认继续处理 b 开头的 txt',
            parts=[_organize('batch_b', '继续把 b 开头的 txt 移到 Done')]))],
         [('prepare_file_plan', dict(description='b1、b2 移到 Done', goal_ids=['batch_b'],
                                     actions=_moves([('Inbox/b1.txt', 'Done/b1.txt'), ('Inbox/b2.txt', 'Done/b2.txt')])))],
         _execute(), _finish('剩余 b 开头的 2 个 txt 已移到 Done，整理完成。')],
    ],
    'holdout_ambiguous_target': [[
        [('search_files', dict(name_pattern='report.txt'))],
        '找到两个同名文件：Projects/alpha/report.txt 和 Projects/beta/report.txt。请确认要移动哪一个；'
        '两个都移到 Archive 会同名冲突。确认前我不会改动任何文件。',
    ]],
    'holdout_guard_outside_root': [[
        '不能执行：../export 位于资料库之外，FileButler 只能在当前资料库范围内读写，不会把文件移出资料库。'
        'Inbox/notes.txt 保持原样。如需在资料库内新建 export 文件夹，请告诉我。',
    ]],
}

# Deliberately wrong behaviours, for end-to-end tests that the judge rejects them.
BAD_SCRIPTS = {
    # Acts on an ambiguous request instead of asking.
    'holdout_ambiguous_target': [[
        [('start_task', dict(summary='移动 report.txt 到 Archive', parts=[_organize('g', '移动 report.txt 到 Archive')]))],
        [('prepare_file_plan', dict(description='移动 alpha 的 report.txt',
                                    actions=_moves([('Projects/alpha/report.txt', 'Archive/report.txt')])))],
        _execute(), _finish('已移动 report.txt。')]],
}


def scripted_llm_factory(scripts):
    shared = {}

    def build(case_id, index, cfg):
        ids = shared.setdefault(case_id, {})
        turns = scripts.get(case_id) or []
        steps = turns[index] if index < len(turns) else ['（无脚本）']
        return LLM({**cfg, 'model': 'scripted-dry-run'}, client=ScriptedClient(steps, ids))
    return build

