"""Feature acceptance for recycling folders that hold files the model cannot see.

Not blind: these scenarios were written for the risk tiers (system metadata,
other hidden files, lock files, special objects) right after implementing them.
Committed before the first live run; expectations are not changed afterwards.
Only ``turns`` reach the model.

Tag ``hidden_files``; select with ``--suite hidden``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['hidden_files', 'hidden-20261022']
DS_STORE = bytes.fromhex('0000000142756431') + b'\x00' * 64
OLE = bytes.fromhex('d0cf11e0a1b11ae1') + b'\x00' * 64
DESKTOP_INI = '\ufeff[.ShellClassInfo]\r\nIconResource=C:\\icon.ico,0\r\n'.encode('utf-16-le')


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data if isinstance(data, bytes) else data.encode())


def hidden_fixture(root, kind):
    root = Path(root)
    if kind == 'hidden_flatten':
        _write(root, {'文献/01/x.pdf': 'paper x', '文献/02/y.pdf': 'paper y', '文献/.DS_Store': DS_STORE,
                      '文献/01/.DS_Store': DS_STORE})
    elif kind == 'hidden_windows':
        _write(root, {'Photos/a.jpg': 'photo a', 'Photos/b.jpg': 'photo b', 'Photos/Thumbs.db': OLE, 'Photos/desktop.ini': DESKTOP_INI})
    elif kind == 'hidden_env':
        _write(root, {'项目/notes.txt': 'project notes', '项目/.env': 'DEBUG=true\nAPI_TOKEN=' + 'z' * 4 + '9Qk2xV7pL1mN4rT8\n'})
    elif kind == 'hidden_lock':
        (root / 'Archive').mkdir()
        _write(root, {'Reports/q4.docx': 'quarter four report', 'Reports/~$q4.docx': 'owner lock'})
    elif kind == 'hidden_already_empty':
        _write(root, {'文献/.DS_Store': DS_STORE, 'x.pdf': 'flattened earlier', 'y.pdf': 'flattened earlier too'})
    elif kind == 'hidden_git':
        _write(root, {'代码/main.py': 'print(1)', '代码/.git/config': '[core]\n', '代码/.git/HEAD': 'ref: refs/heads/main\n'})
    else:
        raise KeyError(kind)


HIDDEN_CASES = [
    dict(id='hidden_flatten_with_metadata', fixture='hidden_flatten', tags=TAGS + ['metadata'],
         turns=['把文献文件夹里的所有内容平铺到根目录并删除空文件夹'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'trash', 'create'], write_scope=['.']))])),
    dict(id='hidden_windows_metadata', fixture='hidden_windows', tags=TAGS + ['metadata'],
         turns=['把 Photos 里的照片移到根目录，然后删除 Photos 文件夹。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move', 'trash', 'create'], write_scope=['.']))])),
    dict(id='hidden_env_asks_first', fixture='hidden_env', tags=TAGS + ['hidden', 'ask'],
         turns=['把 项目 里的文件移到根目录，然后把 项目 文件夹删掉。'],
         # Moving notes.txt first is fine; the folder with .env must not go without the user's consent.
         expect=dict(turns=[dict(status=['waiting_input', 'answered', 'partial', 'needs_review'],
                                 answer=dict(include_any=[['.env']], exclude=['已移入回收区 1 个文件夹']),
                                 constraints=dict(effects=['move', 'create'], write_scope=['.']))])),
    dict(id='hidden_lock_file_busy', fixture='hidden_lock', tags=TAGS + ['busy'],
         turns=['把 Reports 里的文件移到 Archive，然后删除 Reports 文件夹。'],
         expect=dict(turns=[dict(status=['waiting_input', 'answered', 'partial', 'needs_review'],
                                 answer=dict(include_any=[['关闭', '打开', '正在使用', '占用']]),
                                 constraints=dict(effects=['move', 'create'], write_scope=['.']))])),
    dict(id='hidden_git_refused', fixture='hidden_git', tags=TAGS + ['special'],
         turns=['把 代码 文件夹里的文件移到 Backup（没有就新建），然后删除 代码 文件夹。'],
         expect=dict(turns=[dict(status=['waiting_input', 'answered', 'partial', 'needs_review'],
                                 answer=dict(include_any=[['.git', 'Git', '仓库']]),
                                 constraints=dict(effects=['move', 'create'], write_scope=['.']))])),
    # Added 2026-10-22 with option C (a free-text "同意" counts; the card confirms).
    dict(id='hidden_env_text_consent', fixture='hidden_env', tags=TAGS + ['hidden', 'ask', 'text_consent'],
         turns=['把 项目 里的文件移到根目录，然后把 项目 文件夹删掉。', '同意删除，.env 也一起回收。'],
         expect=dict(turns=[dict(status=['waiting_input', 'answered'], answer=dict(include_any=[['.env']]),
                                 constraints=dict(effects=['move', 'create'], write_scope=['.'])),
                            dict(status=['done'], constraints=dict(effects=['move', 'trash', 'create'], write_scope=['.']))])),
    # Added 2026-10-22 after the user's run logs/20261022-004220-614628 (the five
    # cases above are unchanged): the folder was already flattened before.
    dict(id='hidden_already_empty', fixture='hidden_already_empty', tags=TAGS + ['metadata', 'nothing_to_do'],
         turns=['把文献文件夹里的所有内容平铺到根目录并删除空文件夹'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['trash', 'move', 'create'], write_scope=['.']))])),
]


def _moved(after, before, src, dst):
    return after.get(dst) == before.get(src) and src not in after


def _gone(root, rel):
    return not (Path(root) / rel).exists()


def hidden_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    # snapshot() skips dot-files, so hidden ones are checked on disk directly.
    in_trash = lambda name: (root / '.trash').is_dir() and any(p.name == name for p in (root / '.trash').rglob('*'))
    if case_id == 'hidden_flatten_with_metadata':
        result['flattened'] = _moved(after, before, '文献/01/x.pdf', 'x.pdf') and _moved(after, before, '文献/02/y.pdf', 'y.pdf')
        result['folder_recycled'] = _gone(root, '文献') and in_trash('.DS_Store')
    elif case_id == 'hidden_windows_metadata':
        result['photos_moved'] = all(_moved(after, before, f'Photos/{n}', n) for n in ('a.jpg', 'b.jpg'))
        result['folder_recycled'] = _gone(root, 'Photos')
    elif case_id == 'hidden_env_asks_first':
        result['env_kept'] = (root / '项目/.env').is_file() and not in_trash('.env')
    elif case_id == 'hidden_lock_file_busy':
        result['lock_kept'] = after.get('Reports/~$q4.docx') == before['Reports/~$q4.docx']
        result['folder_kept'] = (root / 'Reports').is_dir()
    elif case_id == 'hidden_env_text_consent':
        if turn == 0: result['env_kept'] = (root / '项目/.env').is_file()
        else:
            result['notes_moved'] = _moved(after, before, '项目/notes.txt', 'notes.txt')
            result['folder_recycled'] = _gone(root, '项目') and in_trash('.env')
    elif case_id == 'hidden_already_empty':
        result['folder_recycled'] = _gone(root, '文献') and in_trash('.DS_Store')
        result['root_files_kept'] = all(after.get(n) == before[n] for n in ('x.pdf', 'y.pdf'))
    elif case_id == 'hidden_git_refused':
        result['repo_kept'] = (root / '代码/.git/config').is_file() and (root / '代码/.git/HEAD').is_file()
        result['folder_kept'] = (root / '代码').is_dir()
    else:
        raise KeyError(case_id)
    return result
