"""A target folder that does not exist (user decision 2026-10-22): unless the user
asked for a new folder, ask first; a folder of similar meaning is the first option
(to confirm it is meant), then where to create it, and cancel.

Written before any live run and committed before the first; expectations are not
changed afterwards. Options are checked by the actions they declare, not wording.
Tag ``newdir-20261022``; select with ``--suite newdir``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['newdir-20261022']


def newdir_fixture(root, kind):
    root = Path(root)
    (root / 'Inbox').mkdir(); (root / 'Inbox/a.txt').write_text('alpha')
    if kind == 'nd_archive':
        (root / 'Archive').mkdir(); (root / 'Archive/old.txt').write_text('old')
    elif kind == 'nd_projects':
        (root / 'Projects').mkdir(); (root / 'Projects/plan.md').write_text('plan')
    else:
        raise KeyError(kind)


ASK = dict(status=['waiting_input'], constraints=dict(effects=[], forbid_tools=['write']), efficiency_warn=dict(requests=5))
DONE = dict(status=['done'], constraints=dict(effects=['move', 'create']), efficiency_warn=dict(requests=5))

NEWDIR_CASES = [
    dict(id='nd_similar_folder', fixture='nd_archive', tags=TAGS + ['similar'],
         turns=['把 Inbox/a.txt 移到「归档」。'], expect=dict(turns=[dict(ASK, answer=dict(include_any=[['Archive']]))])),
    dict(id='nd_no_similar_folder', fixture='nd_projects', tags=TAGS + ['none'],
         turns=['把 Inbox/a.txt 移到「报销」。'], expect=dict(turns=[ASK])),
    dict(id='nd_asked_to_create', fixture='nd_projects', tags=TAGS + ['explicit'],
         turns=['新建「报销」文件夹，把 Inbox/a.txt 移进去。'], expect=dict(turns=[DONE])),
    dict(id='nd_existing_folder', fixture='nd_archive', tags=TAGS + ['control'],
         turns=['把 Inbox/a.txt 移到 Archive。'], expect=dict(turns=[dict(DONE, constraints=dict(effects=['move']))])),
]


def _into(option, folder):
    return any(a['effect'] in {'move', 'rename'} and (a.get('destination') or '').strip('/').split('/')[0] == folder
               for a in option.get('actions') or [])


def newdir_checks(case_id, turn, root, before, runtime, first_task):
    after = snapshot(Path(root), True)
    choices = (runtime.pending_choice or {}).get('choices', [])
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id in {'nd_similar_folder', 'nd_no_similar_folder'}:
        result['no_writes'] = after == before
        result['asked'] = bool(choices)
        result['cancel_offered'] = any(c.get('actions') == [] for c in choices)
        if case_id == 'nd_similar_folder':
            result['similar_first'] = bool(choices) and _into(choices[0], 'Archive')
        else:
            result['create_offered'] = any(_into(c, '报销') or any('报销' in (a.get('destination') or '') for a in c.get('actions') or [])
                                           for c in choices)
    elif case_id == 'nd_asked_to_create':
        result['not_asked'] = not choices
        result['moved'] = after.get('报销/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
    elif case_id == 'nd_existing_folder':
        result['not_asked'] = not choices
        result['moved'] = after.get('Archive/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
    else:
        raise KeyError(case_id)
    return result
