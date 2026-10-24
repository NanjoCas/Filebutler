"""Reading Office documents and web pages (2026-10-24, feature plan stage 1).

Each answer is only in the file's contents, never in its name: the model must
read a docx, pptx, xlsx or html file to answer. off_find_marker also checks that
it reads documents nobody named (search_content still indexes PDFs only).
All cases are read-only.

Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model.
Tag ``office-20261024``; select with ``--suite office``.
"""
import subprocess
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['office-20261024']
READ_ONLY = dict(effects=[], forbid_tools=['write'])
MARK = '蓝鲸计划'


def _docx(path, text):
    source = path.with_suffix('.txt')
    source.write_text(text, encoding='utf-8')
    subprocess.run(['textutil', '-convert', 'docx', '-output', str(path), str(source)], check=True)
    source.unlink()


def _pptx(path):
    from pptx import Presentation
    from pptx.util import Inches
    deck = Presentation()
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = '检索模块阶段汇报'
    body = deck.slides.add_slide(deck.slide_layouts[5])
    body.shapes.title.text = '下阶段指标'
    table = body.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(1.5)).table
    for (r, c), v in {(0, 0): '指标', (0, 1): '目标', (1, 0): 'Recall@5', (1, 1): '92%',
                      (2, 0): '单次查询耗时', (2, 1): '300 毫秒以内'}.items():
        table.cell(r, c).text = v
    deck.save(path)


def _xlsx(path):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = '2026 下半年'
    for row in [['项目', '金额（元）', '备注'], ['云服务器', 3600, '按月'], ['GPU 租用', 12800, '两个月'],
                ['会议注册', 2400, ''], ['合计', '=SUM(B2:B4)', '']]:
        sheet.append(row)
    book.save(path)


def office_fixture(root, kind):
    root = Path(root)
    (root / '资料').mkdir(parents=True, exist_ok=True)
    if kind == 'off_docs':
        _docx(root / '资料/会议纪要.docx', '组会纪要\n讨论了检索模块的进度。\n下次评审定在 11 月 18 日（周二）下午三点，地点 B203。\n记录人：王五')
        _pptx(root / '资料/项目汇报.pptx')
        _xlsx(root / '资料/预算.xlsx')
        (root / '资料/公告.html').write_text(
            '<html><head><title>通知</title><style>p{margin:0}</style></head><body>'
            '<h1>学术报告报名</h1><p>报名截止时间：<b>10 月 31 日 17:00</b>。</p><p>名额 40 人。</p></body></html>',
            encoding='utf-8')
    elif kind == 'off_marker':
        _docx(root / '资料/立项说明.docx', f'立项说明\n本项目代号为“{MARK}”，目标是整理实验室论文库。')
        _docx(root / '资料/周报.docx', '周报\n本周完成了 BM25 检索的评测。')
        _xlsx(root / '资料/预算.xlsx')
        (root / '资料/公告.html').write_text('<html><body><p>学术报告报名截止 10 月 31 日。</p></body></html>', encoding='utf-8')
    else:
        raise KeyError(kind)


def _ask(case_id, fixture, question, include, requests=5):
    return dict(id=case_id, fixture=fixture, tags=TAGS, turns=[question],
                expect=dict(turns=[dict(status=['done', 'answered'], answer=dict(include_any=include),
                                        constraints=READ_ONLY, efficiency_warn=dict(requests=requests))]))


OFFICE_CASES = [
    _ask('off_docx_date', 'off_docs', '资料/会议纪要.docx 里定的下次评审是哪天、在哪里？',
         [['11 月 18', '11月18'], ['B203']]),
    _ask('off_pptx_table', 'off_docs', '资料/项目汇报.pptx 里 Recall@5 的目标是多少？', [['92%', '92 %', '百分之九十二']]),
    _ask('off_xlsx_value', 'off_docs', '资料/预算.xlsx 里 GPU 租用花多少钱？', [['12800', '12,800', '1.28 万', '12800 元']]),
    _ask('off_html_deadline', 'off_docs', '资料/公告.html 里报名截止是什么时候？', [['10 月 31', '10月31']]),
    _ask('off_find_marker', 'off_marker', f'资料 文件夹里哪份文件提到了“{MARK}”？', [['立项说明']], requests=8),
]


def office_checks(case_id, turn, root, before, runtime, first_task):
    after = snapshot(Path(root), True)
    if case_id not in {c['id'] for c in OFFICE_CASES}:
        raise KeyError(case_id)
    return dict(contents_preserved=files_preserved(before, after), readonly=after == before)
