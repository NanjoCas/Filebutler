"""Content-addressed text extraction cache shared across chats in one library.

Only parsed text is shared. Conversation IDs, evidence and authorizations are not.
Failed parses are never cached; source versions are checked before and after use.
"""
import importlib.metadata
import json
import sqlite3
from pathlib import Path

from agent.artifacts import digest, signature


def pdf_pages(sb, path, *, start=1, pages=2, extractor):
    versions=[]
    for package in ('PyMuPDF', 'pypdf'):
        try: versions.append(importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError: versions.append('absent')
    return cached(sb, path, [start, pages, versions, 1], lambda p: extractor(p, start=start, pages=pages))


def cached(sb, path, parts, extract):
    """extract(path) -> list[str], cached under the file's content hash plus parts
    (what was extracted and by which extractor version)."""
    path = sb.path(sb.rel(Path(path)), exact=True)
    if path.is_symlink() or any(x.startswith('.') for x in Path(sb.rel(path)).parts):
        raise ValueError('不能缓存隐藏文件或符号链接')
    cursor = sb.root
    for part in Path(sb.rel(path)).parts:
        cursor = cursor/part
        if cursor.is_symlink(): raise ValueError('不能缓存符号链接')
    before = signature(path)
    sha = digest(path)
    key = json.dumps([sha, *parts])
    folder = sb.root/'.oplog'
    database = folder/'document-cache.sqlite3'
    stats = getattr(sb, '_document_cache_stats', None)
    if stats is None: stats = sb._document_cache_stats = dict(hits=0, misses=0, unavailable=0)
    db = None
    try:
        # An unusable cache must never prevent a normal read or follow a symlink.
        if folder.is_symlink() or database.is_symlink(): raise OSError('cache path is symlink')
        folder.mkdir(exist_ok=True)
        db = sqlite3.connect(database, timeout=2)
        # The table keeps its first name so caches written before other formats stay valid.
        db.execute('CREATE TABLE IF NOT EXISTS pdf_text (key TEXT PRIMARY KEY, pages TEXT NOT NULL)')
        row = db.execute('SELECT pages FROM pdf_text WHERE key=?', (key,)).fetchone()
        if row:
            result = json.loads(row[0])
            if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
                raise ValueError('invalid cache record')
            if signature(path) != before: raise ValueError('读取缓存期间源文件变化')
            stats['hits'] += 1
            db.close(); db = None
            return result
    except (sqlite3.Error, OSError, ValueError):
        stats['unavailable'] += 1
        if db: db.close(); db = None
    try:
        stats['misses'] += 1
        result = extract(path)
        if signature(path) != before: raise ValueError('抽取期间源文件变化，请重新读取')
        if db:
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded) <= 16_000_000:
                try:
                    db.execute('INSERT OR REPLACE INTO pdf_text VALUES(?,?)', (key, encoded)); db.commit()
                except sqlite3.Error: stats['unavailable'] += 1
        return result
    finally:
        if db: db.close()
