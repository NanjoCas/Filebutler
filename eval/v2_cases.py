"""Re-verification one of the 2.0 upgrade (docs/filebutler-2.0-upgrade.md 3).

Five new cases for what 2.0 P0-A/P0-B/P1-E changed: a doubt found while
registering ends with a question and no write; a typed answer to a question
asked inside a task resumes that task without asking again; a file found in the
evidence view is read by its full path, not by a same-named file elsewhere; more
matching files than the default top_k are still counted in full; and two local
questions in a row each wait for their own answer.

Not blind: written right after the changes. Committed before any live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``v2-20261029``; select with ``--suite v2``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['v2-20261029']

FILLER = ('本节记录资料整理时的背景说明：样本来自多个地区的公开统计年鉴，'
          '整理人员按季度核对缺失值，并在附录中给出变量定义和数据来源。') * 6
DENGUE = ('Dengue fever forecasting in southern Taiwan. 登革热预测研究（最终版）。\n'
          + FILLER + '\n方法：我们采用的预测模型代号为 HALCYON-9，输入为气温、降雨和既往病例数。\n')
RAIN = ('Rainfall forecasting for reservoir operation. 降雨预测研究（最终版）。\n'
        + FILLER + '\n方法：我们采用的预测模型代号为 ORCHID-2，输入为气压和湿度。\n')
TOPICS = ['图像分割', '语音识别', '推荐系统', '知识图谱', '时间序列', '强化学习', '文本摘要', '目标检测']


def _write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


def v2_fixture(root, kind):
    root = Path(root)
    if kind == 'v2_two_reports':
        _write(root, {'Inbox/实验报告_初稿.txt': '实验报告初稿：结果尚未复核。',
                      'Inbox/实验报告_终稿.txt': '实验报告终稿：已按导师意见修改。',
                      'Inbox/readme.txt': 'inbox notes'})
        (root / 'Archive').mkdir()
    elif kind == 'v2_one_clash':
        _write(root, {'Inbox/a.txt': 'new a', 'Inbox/b.txt': 'b', 'Archive/a.txt': 'archived a'})
    elif kind == 'v2_same_names':
        _write(root, {'资料/项目/2024/登革热/预测研究_最终版.txt': DENGUE,
                      '资料/其他/预测研究_最终版.txt': RAIN})
    elif kind == 'v2_many_matches':
        # One folder, neutral names: only the contents tell which notes are about it.
        # "强化学习" shares a term, so 13 files overlap and 12 are relevant.
        topics = ['联邦学习'] * 12 + TOPICS
        order = [7, 15, 2, 19, 11, 4, 0, 13, 9, 17, 5, 1, 14, 8, 18, 3, 10, 16, 6, 12]
        _write(root, {f'资料/笔记/笔记_{n:02d}.txt': (
            f'第 {n} 份笔记：讨论联邦学习中的客户端采样与通信压缩，实验编号 {n}。' if topics[i] == '联邦学习'
            else f'第 {n} 份笔记：讨论{topics[i]}的基线方法。') for n, i in enumerate(order, 1)})
    elif kind == 'v2_folder_with_files':
        _write(root, {'测试_旧数据/x.txt': 'x', '测试_旧数据/sub/y.txt': 'y', '测试_旧数据/sub/deep/z.md': 'z',
                      '资料/keep.txt': 'keep'})
    elif kind == 'v2_two_clashes':
        _write(root, {'Inbox/a.txt': 'new a', 'Inbox/b.txt': 'new b',
                      'Archive/a.txt': 'archived a', 'Archive/b.txt': 'archived b'})
    else:
        raise KeyError(kind)


READONLY = dict(effects=[], forbid_tools=['write'])
V2_CASES = [
    # s6_doubt_at_registration: "那份" is one report and there are two candidates.
    dict(id='v2_doubt_at_registration', fixture='v2_two_reports', tags=TAGS + ['ask'],
         turns=['把 Inbox 里那份实验报告移到 Archive。'],
         expect=dict(turns=[dict(status=['waiting_input'], constraints=READONLY,
                                 efficiency_warn=dict(requests=4))])),
    dict(id='v2_typed_answer_in_task', fixture='v2_one_clash', tags=TAGS + ['answer'],
         turns=['把 Inbox/a.txt 和 Inbox/b.txt 按原名移到 Archive。',
                'a.txt 两份都保留，新的那份换一个不冲突的名字放进 Archive。'],
         expect=dict(turns=[dict(status=['waiting_input']),
                            dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=5))])),
    dict(id='v2_evidence_then_read', fixture='v2_same_names', tags=TAGS + ['evidence'],
         turns=['资料里有一篇关于登革热预测的文档，它的方法部分用的预测模型代号是什么？'],
         expect=dict(turns=[dict(status=['answered', 'needs_review', 'done'],
                                 answer=dict(include_any=[['HALCYON-9', 'HALCYON 9']], exclude=['ORCHID-2']),
                                 constraints=READONLY, efficiency_warn=dict(requests=6))])),
    dict(id='v2_more_than_top_k', fixture='v2_many_matches', tags=TAGS + ['coverage'],
         turns=['资料里一共有几份文档讨论了联邦学习？告诉我数量。'],
         expect=dict(turns=[dict(status=['answered', 'needs_review', 'done'],
                                 answer=dict(include_any=[['12 份', '12份', '12 个', '12个', '十二', '共 12', '共12']]),
                                 constraints=READONLY, efficiency_warn=dict(requests=6))])),
    # C1 (2026-10-29), added and frozen before its first live run; the case above stays as
    # it is. Its exclude=['ORCHID-2'] failed answers that named HALCYON-9 and then warned
    # about the same-named rainfall file (round 47, 2 of 3). Here the answer must name
    # HALCYON-9 before ORCHID-2; a side note about the other file may follow.
    dict(id='v2_evidence_then_read_v2', fixture='v2_same_names', tags=['c1-20261029', 'evidence'],
         turns=['资料里有一篇关于登革热预测的文档，它的方法部分用的预测模型代号是什么？'],
         expect=dict(turns=[dict(status=['answered', 'needs_review', 'done'],
                                 answer=dict(first_of=[['HALCYON-9', 'HALCYON 9'], ['ORCHID-2', 'ORCHID 2']]),
                                 constraints=READONLY, efficiency_warn=dict(requests=6))])),
    # The answer to the first question covers a only; b still needs its own answer.
    dict(id='v2_two_local_questions', fixture='v2_two_clashes', tags=TAGS + ['answer'],
         turns=['把 Inbox/a.txt 和 Inbox/b.txt 按原名移到 Archive。',
                'a.txt 先不动，留在 Inbox。',
                'b.txt 覆盖旧文件。'],
         expect=dict(turns=[dict(status=['waiting_input'], constraints=READONLY),
                            dict(status=['waiting_input'], constraints=READONLY),
                            dict(status=['done'], constraints=dict(effects=['move', 'trash'], write_scope=['Inbox', 'Archive']),
                                 efficiency_warn=dict(requests=5))])),
    # User decision A2 (2026-10-29), added and frozen before its first live run: a non-empty
    # folder goes in one plan and one card, its files with it, nothing else touched.
    dict(id='v2_a2_recycle_folder', fixture='v2_folder_with_files', tags=['a2-20261029'],
         turns=['把 测试_旧数据 这个文件夹整个删掉，里面的东西都不要了。'],
         expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['trash'], write_scope=['测试_旧数据']),
                                 efficiency_warn=dict(requests=5))])),
]


def _in_trash(after, value):
    return any(k.startswith('.trash/') and v == value for k, v in after.items())


def v2_checks(case_id, turn, root, before, runtime, first_task):
    root = Path(root)
    after = snapshot(root, True)
    visible = {k: v for k, v in after.items() if not k.startswith('.trash')}
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id in ('v2_doubt_at_registration', 'v2_evidence_then_read', 'v2_evidence_then_read_v2', 'v2_more_than_top_k'):
        result['nothing_changed'] = visible == before
    elif case_id == 'v2_typed_answer_in_task':
        if turn == 0:
            result['a_untouched'] = after.get('Inbox/a.txt') == before['Inbox/a.txt']
            result['archived_a_untouched'] = after.get('Archive/a.txt') == before['Archive/a.txt']
        else:
            archive = {k: v for k, v in visible.items() if k.startswith('Archive/')}
            result['archived_a_untouched'] = after.get('Archive/a.txt') == before['Archive/a.txt']
            result['new_a_kept_under_new_name'] = (
                'Inbox/a.txt' not in after and
                [k for k, v in archive.items() if v == before['Inbox/a.txt'] and k != 'Archive/a.txt'] != [])
            result['b_archived'] = after.get('Archive/b.txt') == before['Inbox/b.txt'] and 'Inbox/b.txt' not in after
            result['same_task'] = bool(runtime.current and runtime.current['id'] == first_task)
    elif case_id == 'v2_a2_recycle_folder':
        result['folder_gone'] = not any(k.startswith('测试_旧数据') for k in visible)
        result['files_in_trash'] = all(_in_trash(after, before[k]) for k in before if k.startswith('测试_旧数据/') and before[k] != '<dir>')
        result['others_unchanged'] = after.get('资料/keep.txt') == before['资料/keep.txt']
        result['one_plan'] = bool(runtime.current) and len(runtime.current.get('file_plan_bindings') or {}) == 1
    elif case_id == 'v2_two_local_questions':
        result['a_untouched'] = (after.get('Inbox/a.txt') == before['Inbox/a.txt']
                                 and after.get('Archive/a.txt') == before['Archive/a.txt'])
        if turn < 2:
            result['b_untouched'] = (after.get('Inbox/b.txt') == before['Inbox/b.txt']
                                     and after.get('Archive/b.txt') == before['Archive/b.txt'])
        else:
            result['new_b_in_place'] = after.get('Archive/b.txt') == before['Inbox/b.txt'] and 'Inbox/b.txt' not in after
            result['old_b_in_trash'] = _in_trash(after, before['Archive/b.txt'])
    else:
        raise KeyError(case_id)
    return result
