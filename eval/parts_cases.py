"""One plan serving several parts of the same kind (2026-10-23).

parts_archive_rename: two organize parts (archive a, rename b) that one plan may
do together; each part must be proved by its own action. parts_flatten_recycle:
flatten a folder, then recycle it once empty -- a selection and an action in one
plan, or two plans.

Whether the model tags actions with ``part`` is read from the histories, not
judged here: the acceptance is files, status and no false completion.

Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``parts-20261023``; select with ``--suite parts``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['parts-20261023']


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def parts_fixture(root, kind):
    root = Path(root)
    if kind == 'parts_inbox':
        _write(root, {'Inbox/a.txt': 'alpha', 'Inbox/b.txt': 'beta', 'Inbox/keep.md': 'stay', 'Archive/old.txt': 'old'})
    elif kind == 'parts_tree':
        _write(root, {'文献/01/x.pdf': 'paper x', '文献/02/y.pdf': 'paper y', 'notes.txt': 'root notes'})
    else:
        raise KeyError(kind)


PARTS_CASES = [
    dict(id='parts_archive_rename', fixture='parts_inbox', tags=TAGS + ['same_kind'],
         turns=['把 Inbox/a.txt 按原名移到现有的 Archive 文件夹，再把 Inbox/b.txt 改名为 c.txt。其他文件不要动。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'rename'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=6))])),
    dict(id='parts_flatten_recycle', fixture='parts_tree', tags=TAGS + ['selection_and_action'],
         turns=['把 文献 文件夹里（包括子文件夹）的所有文件按原名平铺到根目录，然后把清空后的 文献 文件夹移入回收区。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'trash', 'create'], write_scope=['.']),
                                 efficiency_warn=dict(requests=6))])),
]


def parts_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id == 'parts_archive_rename':
        result['a_archived'] = after.get('Archive/a.txt') == before['Inbox/a.txt'] and 'Inbox/a.txt' not in after
        result['b_renamed'] = after.get('Inbox/c.txt') == before['Inbox/b.txt'] and 'Inbox/b.txt' not in after
        result['others_unchanged'] = (after.get('Inbox/keep.md') == before['Inbox/keep.md']
                                      and after.get('Archive/old.txt') == before['Archive/old.txt'])
    elif case_id == 'parts_flatten_recycle':
        result['flattened'] = after.get('x.pdf') == before['文献/01/x.pdf'] and after.get('y.pdf') == before['文献/02/y.pdf']
        result['folder_recycled'] = not (root / '文献').exists()
        result['notes_unchanged'] = after.get('notes.txt') == before['notes.txt']
    else:
        raise KeyError(case_id)
    return result
