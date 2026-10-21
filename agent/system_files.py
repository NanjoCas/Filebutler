"""Which system-generated files a recycled folder may contain, from a reviewed list.

The list (system_files.json) is curated from github/gitignore, sindresorhus/junk,
Nextcloud's exclude list and Dropbox's documentation; see that file for commits
and licenses. A name match alone never qualifies a file: metadata must also
pass its content check and size limit, or it falls back to a higher tier.
"""
from __future__ import annotations

import fnmatch
import json
from functools import lru_cache
from pathlib import Path

LIST = Path(__file__).with_name('system_files.json')
TEXT_BYTES = 64 * 1024


@lru_cache(maxsize=1)
def entries():
    return json.loads(LIST.read_text(encoding='utf-8'))['entries']


def match(name, *, is_dir):
    """The list entry a name matches (case-insensitive), for its kind of object."""
    lower = name.lower()
    for entry in entries():
        if entry['kind'] == 'file' and is_dir or entry['kind'] == 'dir' and not is_dir:
            continue
        if any(fnmatch.fnmatchcase(lower, p) for p in entry['patterns']):
            return entry
    return None


def _text(data):
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return data.decode('utf-16', errors='replace')  # desktop.ini is often UTF-16
    return data.decode('utf-8', errors='replace')


def verified(path: Path, entry):
    """Content and size agree with what the system writes under this name."""
    try:
        size = path.stat().st_size
        if size > entry.get('max_bytes', 0):
            return False
        check = entry.get('check') or {}
        if not check:
            return True
        with path.open('rb') as stream:
            data = stream.read(TEXT_BYTES)
        if 'magic' in check:
            return data.startswith(bytes.fromhex(check['magic']))
        if 'text' in check:
            text = _text(data).lower()
            return any(marker.lower() in text for marker in check['text'])
    except OSError:
        return False
    return False


def metadata_dir_ok(folder: Path, entry):
    """A generated folder holds only what its system writes there."""
    members = [m.lower() for m in entry.get('members') or []]
    total = 0
    for p in folder.rglob('*'):
        if p.is_symlink(): return False
        if p.is_dir(): continue
        total += p.stat().st_size
        if members and not any(fnmatch.fnmatchcase(p.name.lower(), m) for m in members): return False
    return total <= entry.get('max_bytes', 0)
