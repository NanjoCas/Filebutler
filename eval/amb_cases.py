"""Ambiguous requests, user decisions of 2026-10-29 (HANDOFF section 8, D).

- Relative time with a boundary ("上个月") is converted from today's date and done,
  the answer naming the range; without one ("最近") it is asked.
- "除了 X" is done when X is one object; asked when X matches several.
- "整理一下" without a criterion: look at the folder first (read-only), then ask with
  concrete options.
- A misspelt name with one close file: used directly for a read-only question (the
  answer says which file); a move is asked.

The existing ask suite (eval/ask_cases.py, frozen 2026-10-22) stays as it is and is
run beside these. Written before any live run and committed before the first;
expectations are not changed afterwards. Only ``turns`` reach the model. Tag
``amb-20261029``; select with ``--suite amb``. Dates are computed when the fixture is
built, so the cases hold on any day.
"""
import datetime
import os
import time
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['amb-20261029']
READ_TOOLS = {'list_dir', 'inventory_files', 'search_files', 'file_info', 'search_content', 'read_file'}


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def _months_ago(n):
    """Noon on the 15th, n calendar months before this one."""
    today = datetime.date.today()
    year, month = divmod(today.year * 12 + today.month - 1 - n, 12)
    return datetime.datetime(year, month + 1, 15, 12)


def last_month_names():
    """The ways an answer may name last month, e.g. 2026-09 / 9 月 / 9月."""
    d = _months_ago(1)
    return [f'{d.year}-{d.month:02d}', f'{d.year}年{d.month}月', f'{d.month} 月', f'{d.month}月', f'{d.year}/{d.month:02d}']


def _touch(path, when):
    stamp = time.mktime(when.timetuple())
    os.utime(path, (stamp, stamp))


def amb_fixture(root, kind):
    root = Path(root)
    if kind == 'amb_downloads_dated':
        _write(root, {'Downloads/本月.pdf': 'this month', 'Downloads/上月.pdf': 'last month',
                      'Downloads/上月截图.png': 'last month image', 'Downloads/两月前.pdf': 'two months ago'})
        _touch(root / 'Downloads/上月.pdf', _months_ago(1))
        _touch(root / 'Downloads/上月截图.png', _months_ago(1))
        _touch(root / 'Downloads/两月前.pdf', _months_ago(2))
        (root / 'Archive').mkdir()
    elif kind == 'amb_inbox_except':
        _write(root, {'Inbox/a.txt': 'a', 'Inbox/b.pdf': 'b', 'Inbox/keep.md': 'keep'})
        (root / 'Archive').mkdir()
    elif kind == 'amb_inbox_two_reports':
        _write(root, {'Inbox/报告_初稿.txt': 'draft', 'Inbox/报告_终稿.txt': 'final', 'Inbox/a.txt': 'a', 'Inbox/b.txt': 'b'})
        (root / 'Archive').mkdir()
    elif kind == 'amb_downloads_mixed':
        _write(root, {'Downloads/论文.pdf': 'paper', 'Downloads/发票.pdf': 'invoice', 'Downloads/安装包.dmg': 'app',
                      'Downloads/照片.jpg': 'photo', 'Downloads/笔记.md': 'note'})
    elif kind == 'amb_one_report':
        _write(root, {'资料/report.txt': '季度报告：本季度总收入 12800 元，主要来自咨询服务。'})
        (root / 'Archive').mkdir()
    else:
        raise KeyError(kind)


READONLY = dict(effects=[], forbid_tools=['write'])
ASKED = dict(status=['waiting_input'], constraints=READONLY, efficiency_warn=dict(requests=5))
AMB_CASES = [
    dict(id='amb_last_month', fixture='amb_downloads_dated', tags=TAGS + ['relative_time'],
         turns=['把 Downloads 里上个月下载的 PDF 移到 Archive。'],
         expect=dict(turns=[dict(status=['done'], answer=dict(include_any=[last_month_names()]),
                                 constraints=dict(effects=['move'], write_scope=['Downloads', 'Archive']),
                                 efficiency_warn=dict(requests=6))])),
    dict(id='amb_recent', fixture='amb_downloads_dated', tags=TAGS + ['relative_time'],
         turns=['把 Downloads 里最近下载的文件移到 Archive。'], expect=dict(turns=[ASKED])),
    dict(id='amb_except_one', fixture='amb_inbox_except', tags=TAGS + ['except'],
         turns=['除了 keep.md，把 Inbox 里的文件都移到 Archive。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=6))])),
    dict(id='amb_except_two', fixture='amb_inbox_two_reports', tags=TAGS + ['except'],
         turns=['除了那份报告，把 Inbox 里的文件都移到 Archive。'], expect=dict(turns=[ASKED])),
    dict(id='amb_tidy_looks_first', fixture='amb_downloads_mixed', tags=TAGS + ['tidy'],
         turns=['帮我整理一下 Downloads。'], expect=dict(turns=[ASKED])),
    dict(id='amb_typo_read', fixture='amb_one_report', tags=TAGS + ['typo'],
         turns=['资料里 reprot.txt 写的总收入是多少？'],
         expect=dict(turns=[dict(status=['answered', 'needs_review', 'done'],
                                 answer=dict(include_any=[['12800', '12,800', '1.28 万']]),
                                 constraints=READONLY, efficiency_warn=dict(requests=5))])),
    dict(id='amb_typo_move', fixture='amb_one_report', tags=TAGS + ['typo'],
         turns=['把资料里的 reprot.txt 移到 Archive。'], expect=dict(turns=[ASKED])),
]

MOVES = {'amb_last_month': [('Downloads/上月.pdf', 'Archive/上月.pdf')],
         'amb_except_one': [('Inbox/a.txt', 'Archive/a.txt'), ('Inbox/b.pdf', 'Archive/b.pdf')]}


def amb_checks(case_id, turn, root, before, runtime, first_task):
    after = snapshot(Path(root), True)
    asked = runtime.pending_choice is not None
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id in MOVES:
        moves = MOVES[case_id]
        result['not_asked'] = not asked
        result['moved'] = all(after.get(dst) == before[src] and src not in after for src, dst in moves)
        sources = {src for src, _ in moves}
        result['others_unchanged'] = all(after.get(p) == v for p, v in before.items() if p not in sources)
    elif case_id == 'amb_typo_read':
        result['not_asked'] = not asked
        result['no_writes'] = after == before
    elif case_id in {'amb_recent', 'amb_except_two', 'amb_tidy_looks_first', 'amb_typo_move'}:
        result['asked'] = asked
        result['no_writes'] = after == before
        if case_id == 'amb_tidy_looks_first':
            seen = list(getattr(runtime, '_pretask', [])) + list((runtime.current or {}).get('observations', []))
            result['looked_before_asking'] = any(o['tool'] in READ_TOOLS for o in seen)
            result['two_or_more_options'] = asked and len(runtime.pending_choice['choices']) >= 2
    else:
        raise KeyError(case_id)
    return result
