"""Task revisions recompute allowed effects and write scope (2026-10-23).

rev_relax_trash: the user first forbids deleting, then allows one file to be
trashed; the confirmed relaxation must let the trash through (before the fix it
stayed blocked). rev_switch_folder: the user corrects which folder was meant;
the revised part's scope replaces the old one, and moving files back into the
old folder must still work (listed on the plan card, not refused).

Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``revise-20261023``; select with ``--suite revise``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['revise-20261023']


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def revise_fixture(root, kind):
    root = Path(root)
    if kind == 'rev_downloads':
        _write(root, {'下载/report.pdf': 'quarterly report', '下载/setup_old.zip': 'old installer',
                      '下载/setup_new.zip': 'new installer', '文档/readme.txt': 'docs'})
    elif kind == 'rev_folders':
        _write(root, {'甲/one.txt': 'one in jia', '甲/two.txt': 'two in jia', '乙/three.txt': 'three in yi',
                      '乙/four.txt': 'four in yi', '归档/keep.txt': 'archived'})
    else:
        raise KeyError(kind)


REVISE_CASES = [
    dict(id='rev_relax_trash', fixture='rev_downloads', tags=TAGS + ['relax'],
         turns=['把 下载 里的 PDF 按原名移到现有的 文档 文件夹。不要删除或回收任何文件。',
                '改一下刚才的要求：下载 里的 setup_old.zip 是没用的旧安装包，现在允许把它移入回收区，其他文件仍然不要删。'
                '请更新原任务，不要新建任务。'],
         expect=dict(turns=[
             dict(status=['done'], constraints=dict(effects=['move'], write_scope=['下载', '文档']),
                  efficiency_warn=dict(requests=6)),
             dict(status=['done'], constraints=dict(effects=['trash'], write_scope=['下载']),
                  efficiency_warn=dict(requests=6))])),
    dict(id='rev_switch_folder', fixture='rev_folders', tags=TAGS + ['scope'],
         turns=['把 甲 文件夹里的 txt 文件按原名移到现有的 归档 文件夹。',
                '我说错了，要整理的是 乙 不是 甲：请把刚才从 甲 移过去的文件按原名移回 甲，再把 乙 里的 txt 按原名移到 归档。'
                '归档 里原有的 keep.txt 不要动。请更新原任务，不要新建任务。'],
         expect=dict(turns=[
             dict(status=['done'], constraints=dict(effects=['move'], write_scope=['甲', '归档']),
                  efficiency_warn=dict(requests=6)),
             dict(status=['done'], constraints=dict(effects=['move'], write_scope=['甲', '乙', '归档']),
                  efficiency_warn=dict(requests=8))])),
]


def _trashed(after, before, rel):
    return rel not in after and any(k.startswith('.trash/') and v == before[rel] for k, v in after.items())


def revise_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id == 'rev_relax_trash':
        result['pdf_moved'] = after.get('文档/report.pdf') == before['下载/report.pdf'] and '下载/report.pdf' not in after
        result['new_installer_kept'] = after.get('下载/setup_new.zip') == before['下载/setup_new.zip']
        result['readme_kept'] = after.get('文档/readme.txt') == before['文档/readme.txt']
        if turn == 0:
            result['nothing_trashed'] = not any(k.startswith('.trash/') for k in after)
            result['old_installer_kept'] = after.get('下载/setup_old.zip') == before['下载/setup_old.zip']
        else:
            result['old_installer_trashed'] = _trashed(after, before, '下载/setup_old.zip')
    elif case_id == 'rev_switch_folder':
        result['keep_unchanged'] = after.get('归档/keep.txt') == before['归档/keep.txt']
        if turn == 0:
            result['jia_moved'] = all(after.get(f'归档/{n}') == before[f'甲/{n}'] and f'甲/{n}' not in after
                                      for n in ('one.txt', 'two.txt'))
            result['yi_untouched'] = all(after.get(f'乙/{n}') == before[f'乙/{n}'] for n in ('three.txt', 'four.txt'))
        else:
            result['jia_back'] = all(after.get(f'甲/{n}') == before[f'甲/{n}'] and f'归档/{n}' not in after
                                     for n in ('one.txt', 'two.txt'))
            result['yi_moved'] = all(after.get(f'归档/{n}') == before[f'乙/{n}'] and f'乙/{n}' not in after
                                     for n in ('three.txt', 'four.txt'))
    else:
        raise KeyError(case_id)
    if turn == 1:
        result['same_task_updated'] = bool(runtime.current and runtime.current['id'] == first_task
                                           and runtime.current['revision'] > 1)
    return result
