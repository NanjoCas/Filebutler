"""The finish_turn contract after the S5 audit (docs/s5-routing-audit-2026-10-25.md 7).

Each case states the business result the user should get, independent of the
model's path: changing nothing by choice is "by_choice" with or without a task;
a write with an asked-for explanation keeps it in the main answer; a request that
needs the user's choice ends with a question (buttons), not options in a reply.
The judgement's ``business`` check (eval/judge.py, added with these cases) reads
report.result; older cases do not declare it and are judged as before.

Not blind: written right after the fixes. Committed before any live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``s5-20261025``; select with ``--suite s5``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['s5-20261025']
NO_WRITE = [dict(effect='move'), dict(effect='rename'), dict(effect='trash')]


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def s5_fixture(root, kind):
    root = Path(root)
    if kind == 's5_plain':
        _write(root, {'Inbox/a.txt': 'a', 'Inbox/b.txt': 'b', 'Inbox/c.md': 'c'})
    elif kind == 's5_one_clash':
        _write(root, {'Inbox/a.txt': 'new a', 'Archive/a.txt': 'archived a'})
    elif kind == 's5_two_one_clash':
        _write(root, {'Inbox/a.txt': 'new a', 'Inbox/b.txt': 'new b', 'Archive/b.txt': 'archived b'})
    elif kind == 's5_tests':
        _write(root, {'测试_旧数据/x.txt': 'x', '测试_旧数据/y.txt': 'y', 'code/tests/test_main.py': 'def test(): pass',
                      'code/main.py': 'print(1)'})
    else:
        raise KeyError(kind)


S5_CASES = [
    dict(id='s5_plain_answer', fixture='s5_plain', tags=TAGS,
         turns=['Inbox 里有几个 txt 文件？只告诉我数量。'],
         expect=dict(turns=[dict(status=['answered'], business=['answered'],
                                 answer=dict(include_any=[['2 个', '2个', '两个']]),
                                 constraints=dict(effects=[]), efficiency_warn=dict(requests=3))])),
    dict(id='s5_nothing_by_choice', fixture='s5_one_clash', tags=TAGS,
         turns=['把 Inbox/a.txt 按原名移到 Archive。',
                {'choose_actions': {'has': [], 'lacks': NO_WRITE}}],
         # Either path is fine for the status; the business result must be the user's choice.
         expect=dict(turns=[dict(status=['waiting_input']),
                            dict(status=['answered', 'done'], business=['by_choice'],
                                 answer=dict(exclude=['已移动', '符合要求']),
                                 constraints=dict(effects=[]), efficiency_warn=dict(requests=4))])),
    dict(id='s5_explain_after_write', fixture='s5_plain', tags=TAGS,
         turns=['把 Inbox/a.txt 移到 Archive，然后告诉我 Inbox 里还剩几个文件。'],
         expect=dict(turns=[dict(status=['done'], business=['fulfilled'],
                                 answer=dict(include_any=[['2 个', '2个', '两个']]),
                                 constraints=dict(effects=['create', 'move'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=5))])),
    dict(id='s5_skip_b', fixture='s5_two_one_clash', tags=TAGS,
         turns=['把 Inbox/a.txt 和 Inbox/b.txt 按原名移到 Archive。',
                {'choose_actions': [
                    {'has': [dict(effect='move', path='Inbox/a.txt')],
                     'lacks': [dict(effect='move', path='Inbox/b.txt'), dict(effect='trash')]},
                    {'has': [], 'lacks': NO_WRITE}]}],
         expect=dict(turns=[dict(status=['waiting_input', 'done']),
                            dict(status=['done'], business=['fulfilled'],
                                 constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=6))])),
    dict(id='s5_cancel_rest', fixture='s5_two_one_clash', tags=TAGS,
         turns=['把 Inbox/a.txt 和 Inbox/b.txt 按原名移到 Archive。', '算了，剩下的都别做了，就这样吧。'],
         expect=dict(turns=[dict(status=['waiting_input', 'done']),
                            dict(status=['cancelled', 'answered', 'done'], business=['cancelled', 'by_choice', 'answered'],
                                 answer=dict(exclude=['已移动 Inbox/b.txt']),
                                 constraints=dict(effects=[]), efficiency_warn=dict(requests=4))])),
    dict(id='s5_needs_a_question', fixture='s5_tests', tags=TAGS,
         turns=['把测试文件都删了。'],
         # Two readings (the 测试_ folder, or the unit tests): a question with buttons, not options in a reply.
         expect=dict(turns=[dict(status=['waiting_input'], constraints=dict(effects=[]),
                                 efficiency_warn=dict(requests=4))])),
]


def s5_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    visible = {k: v for k, v in after.items() if not k.startswith('.trash')}
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id in ('s5_plain_answer', 's5_nothing_by_choice', 's5_needs_a_question'):
        # s5_needs_a_question: waiting_input only comes from a question with buttons (finish_turn mode=ask).
        result['nothing_changed'] = visible == before
    elif case_id == 's5_explain_after_write':
        result['a_archived'] = after.get('Archive/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
        result['others_unchanged'] = all(after.get(k) == before[k] for k in before if k != 'Inbox/a.txt')
    elif case_id in ('s5_skip_b', 's5_cancel_rest'):
        result['b_untouched'] = (after.get('Inbox/b.txt') == before['Inbox/b.txt']
                                 and after.get('Archive/b.txt') == before['Archive/b.txt'])
        if case_id == 's5_skip_b' and turn == 1:
            result['a_archived'] = after.get('Archive/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
    else:
        raise KeyError(case_id)
    return result
