"""Sensitive files: their contents are not sent to the model without the user's grant.

User decision 2026-10-22: by default a sensitive file is not read; the user grants
reading per file, valid for the current task only. "Sensitive" is decided here by
fixed local rules, never by the model:

- file names: credential files (the secrets_scan list), identity documents,
  contracts, finance and medical records (keywords below);
- contents, scanned locally before anything is returned: credentials
  (secrets_scan rules), mainland ID numbers with a valid check digit and birth
  date, and card numbers that pass the Luhn check.

Names stay visible and files can still be moved; only contents are withheld
(read_file, search snippets, classification cards).

User decision 2026-10-24: off by default (config sensitive_files: false); what the
model may read is left to the user, who chooses the library folder. With it off,
no file is treated as sensitive and the task guidance leaves out the rule.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import PurePath

from agent.secrets_scan import scan_text, sensitive_name

# Substrings of a file name (lower-cased). Narrowed where a broader word is common
# in paper titles: 诊断书/诊断证明 (not 故障诊断), 协议书 (not 通信协议); no English
# "contract" (smart contract, contract theory).
NAME_KEYWORDS = [
    ('身份证', '证件'), ('护照', '证件'), ('passport', '证件'), ('驾驶证', '证件'), ('驾照', '证件'),
    ('户口', '证件'), ('社保', '证件'), ('银行卡', '证件'),
    ('合同', '合同'), ('协议书', '合同'),
    ('病历', '医疗记录'), ('体检', '医疗记录'), ('诊断书', '医疗记录'), ('诊断证明', '医疗记录'),
    ('工资', '财务记录'), ('薪资', '财务记录'), ('银行流水', '财务记录'), ('纳税', '财务记录'),
    ('个税', '财务记录'), ('完税', '财务记录'), ('征信', '财务记录'),
]

ID_NUMBER = re.compile(r'(?<![0-9A-Za-z])(\d{17}[\dXx])(?![0-9A-Za-z])')
CARD_NUMBER = re.compile(r'(?<![\d-])((?:\d[ -]?){15,18}\d)(?![\d-])')
ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_valid(number):
    try:
        born = date(int(number[6:10]), int(number[10:12]), int(number[12:14]))
    except ValueError:
        return False
    if not 1900 <= born.year <= date.today().year:
        return False
    check = '10X98765432'[sum(int(d) * w for d, w in zip(number[:17], ID_WEIGHTS)) % 11]
    return number[17].upper() == check


def _luhn(digits):
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def content_kinds(text):
    """Kinds of sensitive content in text, in order of first appearance."""
    kinds = [f['kind'] for f in scan_text(text)]
    if any(_id_valid(m.group(1)) for m in ID_NUMBER.finditer(text)):
        kinds.append('身份证号')
    for m in CARD_NUMBER.finditer(text):
        digits = re.sub(r'[ -]', '', m.group(1))
        if 16 <= len(digits) <= 19 and digits[0] in '3456' and _luhn(digits):
            kinds.append('银行卡号')
            break
    return list(dict.fromkeys(kinds))


def name_kind(name):
    lower = name.lower()
    return sensitive_name(name) or next((kind for word, kind in NAME_KEYWORDS if word in lower), None)


def reasons(rel, text=None):
    """Why this file is sensitive (empty: it is not)."""
    found = []
    kind = name_kind(PurePath(rel).name)
    if kind: found.append(f'文件名像{kind}')
    if text:
        found += [f'内容含{k}' for k in content_kinds(text)]
    return found


def enabled(sb):
    """The checks run only when the run's config turns them on (sensitive_files)."""
    runtime = getattr(sb, 'runtime', None)
    return bool(runtime is not None and runtime.cfg.get('sensitive_files'))


def granted(sb, rel):
    """The user granted reading this file (or a folder holding it) in the current task."""
    runtime = getattr(sb, 'runtime', None)
    if runtime is None:
        return False
    path = PurePath(rel)
    return any(path == PurePath(g) or path.is_relative_to(g) for g in runtime.sensitive_grants())


def refusal(rel, why):
    return (f"{rel} 是敏感文件（{'；'.join(why)}），默认不读取内容。需要读取时先 ask_user 请用户授权："
            f"同意的选项 actions 写 {{\"effect\": \"read\", \"path\": \"{rel}\"}}，授权只在本任务内有效；"
            '不需要内容时，按文件名整理即可，不要换工具或路径重试')


def file_reasons(sb, rel, pdf_pages=2):
    """reasons() for a file on disk: name, then text extracted locally (PDF: first pages).
    Cached per file version (size, mtime): about 25 ms per PDF otherwise."""
    if not enabled(sb):
        return []
    found = reasons(rel)
    path = sb.path(rel, exact=True)
    try: st = path.stat()
    except OSError: return found
    cache = sb.__dict__.setdefault('_sensitive_cache', {})
    key = (rel, st.st_size, st.st_mtime_ns, pdf_pages)
    if key not in cache:
        cache[key] = found + [f'内容含{k}' for k in content_kinds(_text(sb, path, pdf_pages))]
    return cache[key]


def _text(sb, path, pdf_pages):
    from agent.extract import READABLE_EXTS, TEXT_EXTS, units
    try:
        # Documents through the shared extractor (a docx is a zip: its raw bytes hold no text).
        if path.suffix.lower() in READABLE_EXTS - TEXT_EXTS:
            text = '\n'.join(units(sb, path, pages=pdf_pages))
        else:
            with path.open('rb') as stream:
                data = stream.read(64 * 1024)
            text = '' if b'\x00' in data else data.decode('utf-8', errors='replace')
    except Exception:
        return ''
    return text
