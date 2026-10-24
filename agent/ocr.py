"""Local OCR for images and PDF pages without a text layer (plan stage 2, 2026-10-24).

macOS Vision through a small Swift program (agent/ocr_vision.swift), compiled once
into .cache/ by swiftc: no Python package, nothing leaves the machine. Without
macOS or a Swift compiler OCR is unavailable and readers say so.
"""
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from tools.registry import ToolError

SOURCE = Path(__file__).with_name('ocr_vision.swift')
BUILD_DIR = Path(__file__).resolve().parents[1] / '.cache'
BUILD_TIMEOUT = 300  # seconds; the first build takes about a minute
RUN_TIMEOUT = 60     # seconds per image
DPI = 200            # PDF pages are rendered at this resolution before recognition
MIN_TEXT = 20        # a PDF page with fewer extracted characters is recognised

_built = {}  # source hash -> binary path, or the reason it is unavailable


def version():
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]


def _binary():
    key = version()
    if key in _built:
        found = _built[key]
        if isinstance(found, Path):
            return found
        raise ToolError(found)
    target = BUILD_DIR / f'ocr-vision-{key}'
    if not target.is_file():
        compiler = shutil.which('swiftc')
        if not compiler:
            _built[key] = '本机无法识别图片文字：需要 macOS 和 Swift 编译器（xcode-select --install）'
            raise ToolError(_built[key])
        BUILD_DIR.mkdir(exist_ok=True)
        partial = target.with_suffix('.building')
        try:
            done = subprocess.run([compiler, '-O', str(SOURCE), '-o', str(partial)],
                                  capture_output=True, timeout=BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            done = None
        if not done or done.returncode:
            _built[key] = '本机无法识别图片文字：OCR 程序编译失败'
            raise ToolError(_built[key])
        partial.replace(target)
    _built[key] = target
    return target


def recognize(image):
    """Text of one image file."""
    try:
        done = subprocess.run([str(_binary()), str(image)], capture_output=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ToolError(f'{Path(image).name} 文字识别超时（{RUN_TIMEOUT} 秒）') from None
    lines = done.stdout.decode('utf-8', errors='replace').splitlines()
    if done.returncode or not lines:
        raise ToolError(f'{Path(image).name} 文字识别失败')
    result = json.loads(lines[-1])
    if result.get('error'):
        raise ToolError(f"{Path(image).name} {result['error']}")
    return result.get('text') or ''


def image_text(path):
    return [recognize(path)]


def pdf_page_text(path, page):
    """Render one PDF page (1-based) and recognise it."""
    import pymupdf
    with pymupdf.open(path) as document, tempfile.TemporaryDirectory(prefix='filebutler-ocr-') as tmp:
        image = Path(tmp) / 'page.png'
        document[page - 1].get_pixmap(dpi=DPI).save(image)
        return [recognize(image)]


def budget(sb):
    """False once the caller's OCR deadline (sb._ocr_deadline, monotonic) has passed."""
    deadline = getattr(sb, '_ocr_deadline', None)
    return deadline is None or time.monotonic() < deadline


def stats(sb):
    found = getattr(sb, '_ocr_stats', None)
    if found is None:
        found = sb._ocr_stats = dict(pages=0, images=0, skipped=0, failed=0)
    return found
