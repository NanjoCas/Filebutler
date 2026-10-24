"""Reading scanned PDFs and images by local OCR (2026-10-24, feature plan stage 2).

Each answer is only in pixels: a scanned PDF page without a text layer, a
screenshot, a phone photo stored sideways (EXIF orientation), an image among
several that nobody named, and a scanned paper that only search_content can
reach once its page is recognised. All cases are read-only. Fixtures need macOS
(Arial Unicode font) and the OCR program the cases test.

Not blind: written right after the change. Committed before the first live run;
expectations are not changed afterwards. Only ``turns`` reach the model. A find
question may end as needs_review (displayed as answered, 2026-10-24), so those
cases accept it. Tag ``ocr-20261024``; select with ``--suite ocr``.
"""
from pathlib import Path

from eval.checker import files_preserved, snapshot

TAGS = ['ocr-20261024']
READ_ONLY = dict(effects=[], forbid_tools=['write'])
FONT = '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'  # Hiragino's d reads as a in English (2026-10-24)
MARK = '蓝鲸计划'


def _picture(path, lines, size=(1200, 520), turn=False):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 52)
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 100), line, fill='black', font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    if turn:  # stored sideways as a phone does; EXIF 6 = rotate 90° clockwise to view
        exif = Image.Exif(); exif[0x0112] = 6
        image.transpose(Image.Transpose.ROTATE_90).save(path, exif=exif)
    else:
        image.save(path)


def _scanned_pdf(path, lines):
    import pymupdf
    image = path.with_suffix('.png')
    _picture(image, lines, size=(1240, 700))
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(30, 30, 565, 330), filename=str(image))
        document.save(path)
    image.unlink()


def _text_pdf(path, text):
    import pymupdf
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        document.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=11)
        document.save(path)


def ocr_fixture(root, kind):
    root = Path(root)
    if kind == 'ocr_docs':
        _scanned_pdf(root / '资料/入学通知.pdf', ['新生入学通知', '报到截止：11 月 3 日', '报到地点：图书馆一楼大厅'])
        _picture(root / '资料/截图.png', ['线上组会', '会议号：482 9917 3305', '时间：周四 19:30'])
        _picture(root / '资料/白板.jpg', ['本周进展：完成基线实验', '下一步：补充消融实验'], turn=True)
    elif kind == 'ocr_images':
        _picture(root / '资料/海报.png', ['实验室开放日', f'项目代号：{MARK}'])
        _picture(root / '资料/课表.png', ['周一 机器学习', '周三 信息检索'])
        _picture(root / '资料/风景.jpg', [''], size=(800, 600))
    elif kind == 'ocr_papers':
        _scanned_pdf(root / '文献/scan_0042.pdf', ['Federated Learning for Hospital Records',
                                                 'Abstract: we train models across hospitals', 'without sharing patient data.'])
        _text_pdf(root / '文献/gnn_traffic.pdf', 'Graph neural networks for traffic forecasting. Abstract: spatio-temporal graphs.')
        _text_pdf(root / '文献/vision_transformer.pdf', 'Vision transformers for image classification. Abstract: patches as tokens.')
    else:
        raise KeyError(kind)


def _ask(case_id, fixture, question, include, status=('done', 'answered'), requests=5):
    return dict(id=case_id, fixture=fixture, tags=TAGS, turns=[question],
                expect=dict(turns=[dict(status=list(status), answer=dict(include_any=include),
                                        constraints=READ_ONLY, efficiency_warn=dict(requests=requests))]))


FIND = ('done', 'answered', 'needs_review')
OCR_CASES = [
    _ask('ocr_scanned_notice', 'ocr_docs', '资料/入学通知.pdf 里写的报到截止日期和地点是什么？',
         [['11 月 3', '11月3'], ['图书馆']]),
    _ask('ocr_screenshot', 'ocr_docs', '资料/截图.png 里的会议号是多少？', [['482 9917 3305', '48299173305', '482-9917-3305']]),
    _ask('ocr_sideways_photo', 'ocr_docs', '资料/白板.jpg 上写的下一步是什么？', [['消融实验']]),
    _ask('ocr_find_image', 'ocr_images', f'资料 里哪张图片上写着“{MARK}”？', [['海报']], status=FIND, requests=8),
    _ask('ocr_search_scan', 'ocr_papers', '文献 里哪篇论文讲联邦学习？', [['scan_0042', 'Federated Learning for Hospital']],
         status=FIND, requests=8),
]


def ocr_checks(case_id, turn, root, before, runtime, first_task):
    after = snapshot(Path(root), True)
    if case_id not in {c['id'] for c in OCR_CASES}:
        raise KeyError(case_id)
    return dict(contents_preserved=files_preserved(before, after), readonly=after == before)
