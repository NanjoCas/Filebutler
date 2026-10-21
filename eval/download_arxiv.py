"""Download a version-pinned, balanced arXiv corpus with provenance and PDF QA.

Usage: .venv/bin/python -m eval.download_arxiv
One network connection at a time, at least 6 seconds between requests.
Original PDFs remain unchanged; metadata/category labels are not semantic gold.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import signal
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
NS = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
DOMAINS = {
    '01_machine_learning': ('机器学习', ['cs.LG', 'stat.ML']),
    '02_language': ('自然语言处理', ['cs.CL']),
    '03_vision': ('计算机视觉', ['cs.CV']),
    '04_systems': ('计算机系统与分布式计算', ['cs.DC', 'cs.OS']),
    '05_security': ('安全与密码学', ['cs.CR']),
    '06_mathematics': ('数学与优化', ['math.PR', 'math.OC']),
    '07_physics': ('量子与凝聚态物理', ['quant-ph', 'cond-mat.stat-mech']),
    '08_astrophysics': ('天体物理', ['astro-ph.GA', 'astro-ph.CO']),
    '09_biology': ('定量生物学', ['q-bio.BM', 'q-bio.QM']),
    '10_finance_economics': ('金融与计量经济', ['q-fin.ST', 'econ.EM']),
}
ERAS = {
    'earlier': ('201501010000', '202012312359'),
    'recent': ('202301010000', '202512312359'),
}
CURATED = {
    '01_machine_learning': ['2101.00082', '2101.00079', '2101.07206', '2101.00074',
                            '2601.00907', '2601.00116', '2601.00090', '2601.00088'],
    '02_language': ['2101.00078', '2101.00063', '2101.00056', '2101.00036',
                    '2601.02404', '2601.11580', '2601.00100', '2601.00097'],
    '03_vision': ['1512.03385', '1409.1556', '1506.02640', '2010.11929',
                  '2103.00020', '2304.02643', '2304.07193', '2304.09854', '2211.06004'],
    '04_systems': ['1712.05889', '1810.08744', '2112.12921', '1812.03651',
                   '1803.06354', '2001.07704', '1912.11144', '2406.05546'],
    '05_security': ['1911.09777', '2103.09008', '1912.11328', '1806.01246',
                    '2202.12219', '1901.09546', '2305.15594', '1802.08908', '2203.06580'],
    '06_mathematics': ['2306.04283', '1605.08527', '2402.01337', '2305.04056',
                       '2405.02810', '1001.0498', '2206.02684', '2404.11700',
                       '2012.11568', '0810.2324', '1401.4498', '1803.00567'],
    '07_physics': ['1512.03388', '2405.10899', '1508.02595', '1709.02779',
                   '2402.11418', '1603.02275', '2505.14571', '1907.04043'],
    '08_astrophysics': ['1211.7090', '1001.0316', '2109.05854', '1502.03821',
                        '1001.3447', '0910.3538', '1503.07675', '2212.12804', '0903.4849', '1409.7174'],
    '09_biology': ['1304.6570', '1508.02945', '1912.04337', '1409.3499',
                   '1108.3589', '1405.0929', '2201.10024', '2510.17587', '2308.16805'],
    '10_finance_economics': ['2509.18837', '1203.1399', '2305.12539', '1012.1535',
                            '2406.02297', '2012.05088', '2504.05743', '2206.02854', '0709.2830'],
}
LAST_REQUEST = 0.0
MAX_BYTES = 40 * 1024 * 1024


def read_response(request):
    # urllib's socket timeout alone does not bound a response that trickles data.
    # SIGALRM is available on the macOS/Linux hosts supported by this downloader.
    def deadline(signum, frame):
        raise TimeoutError('total request deadline exceeded (90 seconds)')
    old = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(90)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_BYTES:
                raise ValueError('response exceeds 40 MiB limit')
            return response.read(MAX_BYTES + 1)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def fetch(url: str) -> bytes:
    global LAST_REQUEST
    error = None
    for attempt in range(3):
        time.sleep(max(0.0, 6.0 - (time.monotonic() - LAST_REQUEST)))
        LAST_REQUEST = time.monotonic()
        request = urllib.request.Request(url, headers={
            'User-Agent': 'FileButler-ResearchCorpus/1.0 (personal research; serial downloader)',
        })
        try:
            data = read_response(request)
            if len(data) > MAX_BYTES:
                raise ValueError('response exceeds 40 MiB limit')
            return data
        except urllib.error.HTTPError as exc:
            error = exc
            if exc.code in (429, 503):
                # Respect server throttling. Back off on the same endpoint;
                # do not rotate hosts or connections to bypass the limit.
                retry_after = exc.headers.get('Retry-After', '')
                cooldown = max(60, int(retry_after) if retry_after.isdigit() else 60 * (attempt + 1))
                if attempt == 2:
                    print(f'  server unavailable ({exc.code}); stop after three attempts', flush=True)
                    break
                print(f'  server requested backoff ({exc.code}); waiting {cooldown}s', flush=True)
                time.sleep(cooldown)
            else:
                print(f'  retry {attempt + 1}: HTTP {exc.code}', flush=True)
                time.sleep(8 * (attempt + 1))
        except Exception as exc:
            error = exc
            print(f'  retry {attempt + 1}: {type(exc).__name__}: {exc}', flush=True)
            if attempt < 2:
                time.sleep(8 * (attempt + 1))
    raise RuntimeError(f'failed to fetch {url}: {error}')


def candidates(categories, era, raw_path):
    if raw_path.exists():
        xml = raw_path.read_bytes()
    else:
        start, end = ERAS[era]
        # Only the newest candidates in each window are selected. Query its
        # final month to avoid asking the public API to sort several years.
        start = end[:4] + '12010000'
        query = '(' + ' OR '.join('cat:' + c for c in categories) + ')'
        query += f' AND submittedDate:[{start} TO {end}]'
        params = urllib.parse.urlencode(dict(search_query=query, start=0, max_results=12,
                                             sortBy='submittedDate', sortOrder='descending'))
        xml = fetch('https://export.arxiv.org/api/query?' + params)
        # Validate before caching so a proxy/error response is not saved as metadata.
        ET.fromstring(xml)
        raw_path.write_bytes(xml)
    result = []
    for entry in ET.fromstring(xml).findall('a:entry', NS):
        identifier = entry.findtext('a:id', '', NS).rsplit('/abs/', 1)[-1]
        if not re.fullmatch(r'\d{4}\.\d{4,5}v\d+', identifier):
            continue
        clean = lambda s: ' '.join(s.split())
        primary = entry.find('x:primary_category', NS)
        result.append(dict(
            arxiv_id=identifier, family_id=re.sub(r'v\d+$', '', identifier),
            title=clean(entry.findtext('a:title', '', NS)),
            abstract=clean(entry.findtext('a:summary', '', NS)),
            authors=[clean(a.findtext('a:name', '', NS)) for a in entry.findall('a:author', NS)],
            categories=[c.attrib['term'] for c in entry.findall('a:category', NS)],
            primary_category=primary.attrib.get('term') if primary is not None else None,
            published=entry.findtext('a:published', '', NS),
            updated=entry.findtext('a:updated', '', NS),
            abstract_url=f'https://arxiv.org/abs/{identifier}',
            pdf_url=f'https://arxiv.org/pdf/{identifier}',
            license=None, license_note='Consult the linked arXiv abstract page; not supplied by Atom API.',
        ))
    if not result:
        raise RuntimeError(f'No valid entries for {categories}, {era}')
    return result


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('name') or attrs.get('property')
            if key:
                self.meta.setdefault(key, []).append(attrs.get('content', ''))


def abstract_metadata(seed, directory):
    raw = directory / f'{seed}.html'
    if not raw.exists():
        data = fetch(f'https://arxiv.org/abs/{seed}')
        if b'citation_title' not in data:
            raise ValueError('abstract page lacks citation metadata')
        raw.write_bytes(data)
    body = raw.read_text(encoding='utf-8')
    parser = PageMetadata(); parser.feed(body)
    meta = parser.meta
    first = lambda key: meta.get(key, [''])[0]
    identifier = first('og:url').rsplit('/abs/', 1)[-1]
    if not re.fullmatch(re.escape(seed) + r'v\d+', identifier):
        raise ValueError('abstract page does not expose the requested versioned identifier')
    subject = re.search(r'class="primary-subject">(.*?)</span>', body, re.S)
    category = re.search(r'\(([^()]+)\)', html.unescape(subject[1]))[1] if subject else None
    license_match = re.search(r'class="abs-license">\s*<a href="([^"]+)"', body)
    published = first('citation_date').replace('/', '-')
    return dict(arxiv_id=identifier, family_id=seed, title=first('citation_title'),
                abstract=first('citation_abstract'), authors=meta.get('citation_author', []),
                categories=[category] if category else [], primary_category=category,
                categories_scope='primary category only; consult cached abstract HTML for cross-listings',
                published=published, updated=None, metadata_source='arxiv_abstract_html',
                abstract_url=f'https://arxiv.org/abs/{identifier}',
                pdf_url=f'https://arxiv.org/pdf/{identifier}',
                license=html.unescape(license_match[1]) if license_match else None,
                license_note='License link extracted from official abstract page when available.')


def inspect_pdf(path):
    with fitz.open(path) as doc:
        if doc.needs_pass or not len(doc):
            raise ValueError('encrypted or empty PDF')
        chars = [len(page.get_text().strip()) for page in doc]
        first_two = sum(chars[:2])
        return dict(pages=len(doc), page_text_chars=chars, text_chars=sum(chars),
                    first_two_page_chars=first_two,
                    pages_under_40_chars=sum(n < 40 for n in chars),
                    text_extraction_status='ok' if sum(chars) >= 100 else 'low_text',
                    # Actual visual review is performed separately, not inferred here.
                    visual_layout_review='not_annotated')


def save_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'data/real/arxiv_multidomain_v1')
    parser.add_argument('--selection', choices=['curated', 'api'], default='curated',
                        help='Curated IDs use abstract HTML; API mode uses category/date search.')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'metadata').mkdir(exist_ok=True)
    manifest_path = out / 'manifest.json'
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    rows = old.get('papers', [])
    # Revalidate existing completed downloads on resume; never trust the manifest alone.
    for row in rows:
        row.setdefault('metadata_source', 'arxiv_atom_api')
        p = out / row['path']
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError(f'Existing download missing or changed: {p}')
    families = {r['family_id'] for r in rows}
    hashes = {r['sha256'] for r in rows}
    failures = old.get('failures', [])
    def persist():
        save_json(manifest_path, dict(dataset='arxiv_multidomain_v1', target_papers=80,
                  generated_at_utc=datetime.now(timezone.utc).isoformat(),
                  sampling='10 buckets with 8 papers each; curated arXiv IDs or cached category/date API; '
                           'globally unique arXiv families; inspect per-paper era/source fields',
                  split_policy='6 development / 2 holdout per bucket; '
                               'no family overlap within this corpus; query gold labels not yet provided',
                  papers=rows, failures=failures))
    for domain, (label, categories) in DOMAINS.items():
        folder = out / 'papers' / domain
        folder.mkdir(parents=True, exist_ok=True)
        for era in (['curated'] if args.selection == 'curated' else ERAS):
            done = sum(r['domain'] == domain and (era == 'curated' or r['era'] == era) for r in rows)
            target = 8 if era == 'curated' else 4
            if done >= target:
                continue
            print(f'Metadata: {domain} / {era}', flush=True)
            if era == 'curated':
                if domain not in CURATED:
                    raise RuntimeError('This corpus uses cached API downloads for the first two buckets; '
                                       'use --selection api to initially populate those buckets.')
                pool = [dict(family_id=seed) for seed in CURATED[domain]]
            else:
                pool = candidates(categories, era, out / 'metadata' / f'{domain}_{era}.xml')
            for item in pool:
                if done >= target:
                    break
                if item['family_id'] in families:
                    continue
                try:
                    if era == 'curated':
                        item = abstract_metadata(item['family_id'], out / 'metadata')
                    filename = item['arxiv_id'] + '.pdf'
                    path = folder / filename
                    print(f'[{len(rows)+1}/80] {item["arxiv_id"]} {item["title"][:85]}', flush=True)
                    if not path.exists():
                        data = fetch(item['pdf_url'])
                        if not data.startswith(b'%PDF-'):
                            raise ValueError('response is not a PDF')
                        part = path.with_suffix('.pdf.part')
                        part.write_bytes(data)
                        inspect_pdf(part)
                        part.replace(path)
                    qa = inspect_pdf(path)
                    sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    if sha in hashes:
                        raise ValueError('duplicate PDF content')
                    row = dict(**item, domain=domain, domain_label=label, era=era,
                               split='development' if done < (6 if era == 'curated' else 3) else 'holdout',
                               path=path.relative_to(out).as_posix(), bytes=path.stat().st_size,
                               sha256=sha, downloaded_at_utc=datetime.now(timezone.utc).isoformat(),
                               qa=qa, semantic_labels_status='not_human_verified')
                    rows.append(row)
                    families.add(item['family_id']); hashes.add(sha)
                    done += 1
                    persist()
                except Exception as exc:
                    failures.append(dict(arxiv_id=item.get('arxiv_id', item['family_id']), error=str(exc),
                                         domain=domain, era=era))
                    persist()
                    print(f'  skipped: {exc}', flush=True)
            if done < target:
                raise RuntimeError(f'Insufficient papers for {domain}/{era}: {done}/{target}; rerun to resume')
    fields = ['arxiv_id', 'title', 'domain_label', 'era', 'split', 'primary_category',
              'published', 'updated', 'pages', 'bytes', 'sha256', 'path', 'abstract_url', 'pdf_url']
    with (out / 'manifest.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row['qa']['pages'] if k == 'pages' else row.get(k, '') for k in fields})
    counts = Counter(r['domain_label'] for r in rows)
    summary = dict(papers=len(rows), unique_families=len(families), unique_sha256=len(hashes),
                   bytes=sum(r['bytes'] for r in rows), pages=sum(r['qa']['pages'] for r in rows),
                   min_pages=min(r['qa']['pages'] for r in rows),
                   max_pages=max(r['qa']['pages'] for r in rows),
                   domains=dict(counts), splits=dict(Counter(r['split'] for r in rows)),
                   low_text_papers=[r['arxiv_id'] for r in rows if r['qa']['text_extraction_status'] != 'ok'],
                   failures=len(failures))
    save_json(out / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
