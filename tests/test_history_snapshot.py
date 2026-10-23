"""Each request sees one task snapshot (chain plan 3, 2026-10-23).

Before: every run appended its snapshot (protocol rules + current task state) to
the stored conversation, so an 8-turn web conversation carried 8 of them, 49% of
its history, and the model saw old and new task states side by side.
"""
import json

from agent.history import prepare_history, undo_note
from agent.prompts import GUIDANCE_MARK, SYSTEM_PROMPT

import helpers as H
from helpers import Session, step, call, files_fixture


def snapshots(messages):
    return [m for m in messages if m.get('role') == 'system' and m.get('content', '').startswith(GUIDANCE_MARK)]


def test_ten_turns_each_see_one_current_snapshot(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    sizes = []
    for turn in range(10):
        steps = [step(call('list_dir', {'path': 'Inbox'}))] if turn % 3 == 0 else []
        result, client = session.run(f'第 {turn + 1} 个问题：Inbox 里有什么？', steps, final=f'回答 {turn + 1}')
        assert len(snapshots(result.messages)) == 1
        assert not snapshots(session.history)
        sizes.append(len(json.dumps(result.messages, ensure_ascii=False)))
    # Every user instruction, every call/result pair and every answer is kept.
    users = [m['content'] for m in session.history if m['role'] == 'user']
    assert len(users) == 10 and users[-1].startswith('第 10 个')
    calls = [tc['id'] for m in session.history for tc in m.get('tool_calls') or []]
    results = [m['tool_call_id'] for m in session.history if m['role'] == 'tool']
    assert len(calls) == 4 and calls == results
    assert [m['content'] for m in session.history if m['role'] == 'assistant' and m.get('content')][-1] == '回答 10'
    # A request grows by the turns, not by one more snapshot each turn.
    snapshot = len(json.dumps(snapshots(result.messages), ensure_ascii=False))
    assert sizes[-1] - sizes[0] < 9 * snapshot


def test_stored_snapshots_are_dropped_and_undo_notes_kept():
    old = lambda n: dict(role='system', content=f'{GUIDANCE_MARK}\n近期任务：第 {n} 轮的状态')
    history = [dict(role='system', content=SYSTEM_PROMPT),
               dict(role='user', content='整理 Inbox'), old(1), dict(role='assistant', content='已整理'),
               undo_note('run1', dict(restored=1)),
               dict(role='user', content='再看看'), old(2), dict(role='assistant', content='好'),
               dict(role='user', content='现在呢')]
    projected, stats = prepare_history(history, H.CFG)
    assert not snapshots(projected) and stats['snapshots_dropped'] == 2
    assert [m['content'] for m in projected if m['role'] == 'user'] == ['整理 Inbox', '再看看', '现在呢']
    assert any(m['content'].startswith('[本地撤销状态 run=run1]') for m in projected)
    assert projected == [m for m in history if m not in (history[2], history[6])]
