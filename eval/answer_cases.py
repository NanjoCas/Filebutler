"""Answering a question with "do nothing" (2026-10-23, chain plan 2).

After 2636a19 an option with no actions settles an untouched write part only when
the model names it in finish_task.unfinished. These cases check that the model
does so, so a "do nothing" choice still ends as done, and that a choice about b
alone leaves a's work standing (the question may come before or after a is moved;
an answer to a question asked mid-task now resumes that task).

The choice is made by the actions an option declares, never by its label.
Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``answer-20261023``; select with ``--suite answer``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['answer-20261023']
NO_WRITE = [dict(effect='move'), dict(effect='rename'), dict(effect='trash')]


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def answer_fixture(root, kind):
    root = Path(root)
    if kind == 'ans_one_clash':
        _write(root, {'Inbox/a.txt': 'new a', 'Archive/a.txt': 'archived a'})
    elif kind == 'ans_two_one_clash':
        _write(root, {'Inbox/a.txt': 'new a', 'Inbox/b.txt': 'new b', 'Archive/b.txt': 'archived b'})
    else:
        raise KeyError(kind)


ANSWER_CASES = [
    dict(id='ans_do_nothing', fixture='ans_one_clash', tags=TAGS + ['whole'],
         turns=['把 Inbox/a.txt 按原名移到 Archive。',
                {'choose_actions': {'has': [], 'lacks': NO_WRITE}}],
         expect=dict(turns=[dict(status=['waiting_input']),
                            dict(status=['done'], constraints=dict(effects=[]), efficiency_warn=dict(requests=4))])),
    dict(id='ans_skip_b', fixture='ans_two_one_clash', tags=TAGS + ['partial'],
         turns=['把 Inbox/a.txt 和 Inbox/b.txt 按原名移到 Archive。',
                # "b stays": with a still to move, the option moves a; once a is moved, it declares nothing.
                {'choose_actions': [
                    {'has': [dict(effect='move', path='Inbox/a.txt')],
                     'lacks': [dict(effect='move', path='Inbox/b.txt'), dict(effect='trash')]},
                    {'has': [], 'lacks': NO_WRITE}]}],
         expect=dict(turns=[dict(status=['waiting_input']),
                            dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=6))])),
]


def answer_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id == 'ans_do_nothing':
        result['nothing_changed'] = {k: v for k, v in after.items() if not k.startswith('.trash')} == before
    elif case_id == 'ans_skip_b':
        result['b_untouched'] = (after.get('Inbox/b.txt') == before['Inbox/b.txt']
                                 and after.get('Archive/b.txt') == before['Archive/b.txt'])
        if turn == 1:
            result['a_archived'] = after.get('Archive/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
    else:
        raise KeyError(case_id)
    return result
