"""Acceptance for plan ordering (2026-10-22): a name vacated then filled, case-only
renames, a folder renamed after it is emptied, and a swap that one plan cannot do.

Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model.

Tag ``p1-20261022``; select with ``--suite p1``.
"""
import os
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['p1-20261022']


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def p1_fixture(root, kind):
    root = Path(root)
    if kind == 'p1_clash':
        _write(root, {'x.pdf': 'old x at root', '文献/a/x.pdf': 'new x', '文献/b/y.pdf': 'new y'})
    elif kind == 'p1_case':
        _write(root, {'photos/a.JPG': 'image a', 'photos/b.JPG': 'image b', 'photos/c.png': 'image c'})
    elif kind == 'p1_folder':
        _write(root, {'草稿/one.txt': 'draft one', '草稿/sub/two.txt': 'draft two'})
    elif kind == 'p1_swap':
        _write(root, {'a.txt': 'content of a', 'b.txt': 'content of b'})
    else:
        raise KeyError(kind)


P1_CASES = [
    dict(id='p1_flatten_clash', fixture='p1_clash', tags=TAGS + ['vacate'],
         turns=['把 文献 里的 PDF 全部平铺到根目录。根目录里已有的同名文件先改名，在原名后加 _old（如 x_old.pdf），再放入新文件。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'rename', 'create'], write_scope=['.']),
                                 efficiency_warn=dict(requests=6))])),
    dict(id='p1_case_rename', fixture='p1_case', tags=TAGS + ['case'],
         turns=['把 photos 里扩展名是大写 .JPG 的文件改成小写 .jpg，文件名其余部分不变。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['rename', 'move'], write_scope=['photos']),
                                 efficiency_warn=dict(requests=5))])),
    dict(id='p1_empty_then_rename_folder', fixture='p1_folder', tags=TAGS + ['folder'],
         turns=['把 草稿 文件夹里的所有文件平铺到根目录，然后把 草稿 文件夹改名为 草稿_已清空。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'rename', 'create'], write_scope=['.']),
                                 efficiency_warn=dict(requests=6))])),
    # One plan cannot swap; two plans via a temporary name, or asking, are both acceptable.
    dict(id='p1_swap', fixture='p1_swap', tags=TAGS + ['cycle'],
         turns=['把 a.txt 和 b.txt 的文件名互换。'],
         expect=dict(turns=[dict(status=['done', 'waiting_input'], constraints=dict(effects=['move', 'rename'], write_scope=['.']),
                                 efficiency_warn=dict(requests=8))])),
]


def _names(folder):
    return sorted(os.listdir(folder)) if folder.is_dir() else []


def p1_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id == 'p1_flatten_clash':
        result['new_at_root'] = after.get('x.pdf') == before['文献/a/x.pdf'] and after.get('y.pdf') == before['文献/b/y.pdf']
        result['old_renamed'] = after.get('x_old.pdf') == before['x.pdf']
    elif case_id == 'p1_case_rename':
        # Exact names: a case-insensitive volume also "finds" a.JPG as a.jpg.
        result['exact_names'] = _names(root / 'photos') == ['a.jpg', 'b.jpg', 'c.png']
    elif case_id == 'p1_empty_then_rename_folder':
        result['files_flattened'] = after.get('one.txt') == before['草稿/one.txt'] and after.get('two.txt') == before['草稿/sub/two.txt']
        result['folder_renamed'] = (root / '草稿_已清空').is_dir() and not (root / '草稿').exists()
    elif case_id == 'p1_swap':
        swapped = after.get('a.txt') == before['b.txt'] and after.get('b.txt') == before['a.txt']
        untouched = after.get('a.txt') == before['a.txt'] and after.get('b.txt') == before['b.txt']
        result['swapped_or_untouched'] = swapped or untouched
        result['no_leftovers'] = sorted(p for p in after if not p.startswith('.trash')) == ['a.txt', 'b.txt']
    else:
        raise KeyError(case_id)
    return result
