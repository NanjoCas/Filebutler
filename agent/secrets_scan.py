"""Local secret detection for hidden files a recycle would take along.

Purpose: a stronger, factual warning ("第 3 行疑似 GitHub 令牌") on the
confirmation and in the question to the user. It never lowers a risk tier and
never lets a file through: hidden files still require the user's consent.

Rules follow the common practice of secret scanners such as gitleaks and
detect-secrets (provider token formats, private key headers, credential
assignments checked for entropy); the expressions are written here, not copied.
Contents stay local: callers show the model only the kind and line, and the
card only a masked prefix. Nothing read here is logged.
"""
from __future__ import annotations

import fnmatch
import math
import re
from collections import Counter
from pathlib import Path

SCAN_BYTES = 64 * 1024

# File names that conventionally hold credentials (lower-cased match).
SENSITIVE_NAMES = [
    ('.env', '环境变量文件'), ('.env.*', '环境变量文件'), ('*.env', '环境变量文件'),
    ('id_rsa', 'SSH 私钥'), ('id_dsa', 'SSH 私钥'), ('id_ecdsa', 'SSH 私钥'), ('id_ed25519', 'SSH 私钥'),
    ('*.pem', '证书或私钥'), ('*.key', '私钥'), ('*.p12', '证书包'), ('*.pfx', '证书包'),
    ('.netrc', '登录凭据'), ('.git-credentials', 'Git 凭据'), ('.npmrc', 'npm 令牌配置'), ('.pypirc', 'PyPI 凭据'),
    ('.htpasswd', '密码文件'), ('credentials', '凭据文件'), ('credentials.*', '凭据文件'), ('*.kdbx', '密码库'),
]

# (kind, expression, group holding the secret value or 0 for the whole match)
CONTENT_RULES = [
    ('私钥', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----'), 0),
    ('AWS 访问密钥', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), 0),
    ('AWS 私有访问密钥', re.compile(r'(?i)aws_secret_access_key\s*[=:]\s*["\']?([A-Za-z0-9/+=]{40})'), 1),
    ('GitHub 令牌', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})'), 0),
    ('OpenAI 类 API 密钥', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}'), 0),
    ('Slack 令牌', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}'), 0),
    ('Google API 密钥', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'), 0),
    ('Stripe 密钥', re.compile(r'\b(?:sk|rk)_live_[0-9a-zA-Z]{20,}'), 0),
    ('JWT 令牌', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), 0),
    ('带密码的连接地址', re.compile(r'\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]{3,})@'), 1),
]
# key = value assignments; reported only when the value looks generated.
ASSIGNMENT = re.compile(r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth)\w*\s*[=:]\s*["\']?([^\s"\'#]{12,})')
MIN_ENTROPY = 3.5  # bits per character


def entropy(text):
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def mask(value):
    return value[:4] + '****' if len(value) > 4 else '****'


def sensitive_name(name):
    lower = name.lower()
    return next((label for pattern, label in SENSITIVE_NAMES if fnmatch.fnmatchcase(lower, pattern)), None)


def scan_text(text):
    findings = []
    for line_no, line in enumerate(text.splitlines(), 1):
        hit = False
        for kind, expr, group in CONTENT_RULES:
            m = expr.search(line)
            if m:
                findings.append(dict(kind=kind, line=line_no, masked=mask(m.group(group))))
                hit = True
                break
        if hit: continue
        m = ASSIGNMENT.search(line)
        if m and entropy(m.group(2)) >= MIN_ENTROPY:
            findings.append(dict(kind='疑似凭据（' + m.group(1).lower() + '）', line=line_no, masked=mask(m.group(2))))
    return findings


def scan_file(path: Path):
    """Name rule plus findings in the first SCAN_BYTES; binary content is not read as text."""
    result = dict(name_rule=sensitive_name(path.name), findings=[], truncated=False, binary=False)
    try:
        with path.open('rb') as stream:
            data = stream.read(SCAN_BYTES + 1)
    except OSError:
        return result
    result['truncated'] = len(data) > SCAN_BYTES
    data = data[:SCAN_BYTES]
    if b'\x00' in data:
        result['binary'] = True
        return result
    result['findings'] = scan_text(data.decode('utf-8', errors='replace'))
    return result


def is_sensitive(report):
    return bool(report and (report['name_rule'] or report['findings']))


def describe(rel, report, *, masked):
    """One line about a file. ``masked`` adds the masked prefix (cards, never the model)."""
    parts = []
    if report['name_rule']: parts.append('文件名表明是' + report['name_rule'])
    for f in report['findings'][:3]:
        parts.append(f"第 {f['line']} 行疑似{f['kind']}" + (f"（{f['masked']}）" if masked else ''))
    if len(report['findings']) > 3: parts.append(f"另有 {len(report['findings']) - 3} 处")
    return f"{rel}：" + '；'.join(parts)
