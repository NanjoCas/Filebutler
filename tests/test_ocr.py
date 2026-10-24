"""Local OCR for images and scanned PDF pages (plan stage 2, 2026-10-24).

Tests marked ``vision`` run the real macOS Vision program; the others replace
recognition and check caching, time limits, failures and sensitive checks.
"""
import json
import shutil
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

from agent import extract, ocr
from agent.artifacts import ArtifactStore
from agent.task_runtime import TaskRuntime
from safety.sandbox import Sandbox
from tools import fs_read, registry, retrieval  # noqa: F401  (register the tools)
from tools.registry import ToolError

import helpers as H

vision = pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('swiftc'), reason='macOS Vision')
FONT = '/System/Library/Fonts/Hiragino Sans GB.ttc'


def picture(path, lines, size=(1200, 500), turn=False):
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 56)
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 110), line, fill='black', font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    if turn:  # stored sideways, as a phone does; EXIF 6 = rotate 90° clockwise to view
        exif = Image.Exif(); exif[0x0112] = 6
        image.transpose(Image.Transpose.ROTATE_90).save(path, exif=exif)
    else:
        image.save(path)


def scanned_pdf(path, image):
    import pymupdf
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(40, 40, 555, 300), filename=str(image))
        document.save(path)


def library(tmp_path, **cfg):
    sb = Sandbox(tmp_path); sb.artifacts = ArtifactStore(sb)
    sb.runtime = TaskRuntime('读取', dict(protocol='v3', **cfg), sb, 'one')
    return sb


def read(sb, path, **kw):
    return registry.dispatch('read_file', json.dumps(dict(path=path, **kw)), sb)


def fake(monkeypatch, text='报到截止 11 月 3 日'):
    calls = []
    monkeypatch.setattr(ocr, 'recognize', lambda image: calls.append(image) or text)
    return calls


@vision
def test_an_image_is_read_by_local_ocr(tmp_path):
    picture(tmp_path / '截图.png', ['会议号 482 9917 3305', 'Meeting ID for review'])
    out = read(library(tmp_path), '截图.png')
    assert '482 9917 3305' in out and 'Meeting ID' in out and '本地 OCR 识别' in out


@vision
def test_a_scanned_pdf_page_is_recognised_and_cached(tmp_path, monkeypatch):
    picture(tmp_path / 'scan.png', ['新生报到截止：11 月 3 日', '地点：图书馆一楼'])
    scanned_pdf(tmp_path / '通知.pdf', tmp_path / 'scan.png')
    (tmp_path / 'scan.png').unlink()
    out = read(library(tmp_path), '通知.pdf')
    assert '11 月 3 日' in out.replace('11月3日', '11 月 3 日') and '图书馆' in out
    monkeypatch.setattr(ocr, 'recognize', lambda image: pytest.fail('cached OCR must not run again'))
    assert '图书馆' in read(library(tmp_path), '通知.pdf')


@vision
def test_a_sideways_phone_photo_is_turned_before_recognition(tmp_path):
    picture(tmp_path / '白板.jpg', ['下一步：补充消融实验'], turn=True)
    assert '消融实验' in read(library(tmp_path), '白板.jpg')


def test_pages_with_a_text_layer_are_not_recognised(tmp_path, monkeypatch):
    calls = fake(monkeypatch)
    H.pdf(tmp_path / 'paper.pdf', 'Abstract: graph neural networks for traffic forecasting.')
    assert 'traffic' in read(library(tmp_path), 'paper.pdf') and calls == []


def test_search_finds_scanned_text_and_resumes_after_the_time_limit(tmp_path, monkeypatch):
    calls = fake(monkeypatch, 'Abstract: federated learning on hospital records')
    picture(tmp_path / 'blank.png', [''], size=(200, 200))
    for name in ('a', 'b'):
        scanned_pdf(tmp_path / f'文献/{name}.pdf', tmp_path / 'blank.png')
    sb = library(tmp_path)
    search = lambda: registry.dispatch('search_content', json.dumps(dict(query='federated learning', path='文献')), sb)
    monkeypatch.setattr(retrieval, 'OCR_SECONDS', 0)
    first = search()
    assert '2 个因时间上限未识别' in first and '候选 0 个' in first and calls == []
    monkeypatch.setattr(retrieval, 'OCR_SECONDS', 20)
    second = search()
    assert '2 个扫描页、0 张图片已识别' in second and '候选 2 个' in second and len(calls) == 2


def test_without_ocr_a_scan_says_so_and_an_image_gets_a_clear_error(tmp_path, monkeypatch):
    def unavailable(image): raise ToolError('本机无法识别图片文字：需要 macOS 和 Swift 编译器（xcode-select --install）')
    monkeypatch.setattr(ocr, 'recognize', unavailable)
    picture(tmp_path / 'scan.png', [''], size=(200, 200))
    scanned_pdf(tmp_path / 'scan.pdf', tmp_path / 'scan.png')
    sb = library(tmp_path)
    assert '有扫描页没能识别文字' in read(sb, 'scan.pdf')
    assert '无法识别图片文字' in read(sb, 'scan.png')


def test_with_sensitive_checks_on_recognised_text_is_checked(tmp_path, monkeypatch):
    from test_sensitive import id_number
    fake(monkeypatch, f'居民身份证 {id_number()}')
    picture(tmp_path / 'IMG_0001.png', [''], size=(200, 200))
    assert '敏感文件' in read(library(tmp_path, sensitive_files=True), 'IMG_0001.png')


def test_the_program_is_built_once_per_source_version(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, '_built', {})
    monkeypatch.setattr(ocr, 'BUILD_DIR', tmp_path)
    monkeypatch.setattr(ocr.shutil, 'which', lambda name: None)
    with pytest.raises(ToolError, match='Swift 编译器'):
        ocr._binary()
    target = tmp_path / f'ocr-vision-{ocr.version()}'
    target.write_text('built'); monkeypatch.setattr(ocr, '_built', {})
    assert ocr._binary() == target
