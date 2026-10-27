"""Controlled retrieval evaluation: one factor at a time, one library, paired statistics.

    python -m eval.retrieval_ablation --set eval/retrieval_set_<date>_dev.yaml --out eval/results/<new dir>
    python -m eval.retrieval_ablation --set eval/retrieval_set_<date>_heldout.yaml --out eval/results/<new dir>
    python -m eval.retrieval_ablation --set <set> --validate          # checks only, scores nothing

No API calls. Every question is searched in the same merged library (eval/retrieval_library.py).
Factors of tools.retrieval.PaperIndex, everything else fixed (tokenizer, k1, b, top k, extractor):
  formats  pdf | all        which files are indexed
  pages    2 | all          how much of a PDF is indexed (other formats are always read whole)
  chunk    none | <size>    whole-file scoring or the file's best segment
The gain from the baseline (pdf, 2, none) to the target (all, all, <size>) is split among the
three factors with Shapley values, per question, so the parts add up to the total; the run
also separates reachability (the gold text was indexed at all) from ranking. Results are
reported per stratum, derived from where each gold file's evidence is, never pooled into a
"share of the gain", because that share depends on how many questions of each kind the set has.

A dev set may try several chunk sizes; the size is then picked by the rule committed in
eval/retrieval_choice.yaml before the run. A held-out set runs once, with the committed size,
only when the set and the choice are committed and unchanged; a second run needs --rerun-reason.
Any difference in extracted text between configs, or from the committed text manifest, stops
the run: the comparison would no longer hold the text fixed.
"""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from eval import retrieval_library as library
from eval import retrieval_stats as stats
from eval.retrieval_library import normalize
from safety.sandbox import Sandbox

ROOT = library.ROOT
K = 5
SEED = 20261027
CHOICE = ROOT / 'eval/retrieval_choice.yaml'
DEFAULT_MINIMUMS = {'front_pdf': 15, 'late_pdf': 15, 'nonpdf': 15, 'no_relevant': 8}
STRATA = ('front_pdf', 'late_pdf', 'nonpdf', 'no_relevant')
PLAYERS = ('formats', 'pages', 'chunk')


class EvalError(RuntimeError):
    """The run cannot be trusted as a controlled comparison; nothing is scored."""


# --------------------------------------------------------------------------- sets and configs

def load_set(path):
    path = Path(path)
    spec = yaml.safe_load(path.read_text(encoding='utf-8'))
    queries = spec.get('queries') or []
    ids = [q['id'] for q in queries]
    if len(ids) != len(set(ids)):
        raise EvalError(f'{path.name}: duplicate question id')
    for q in queries:
        q.setdefault('cluster', q['id'])
        q.setdefault('relevant', [])
        for g in q['relevant']:
            if not {'path', 'page', 'evidence'} <= set(g):
                raise EvalError(f"{q['id']}: every relevant file needs path, page and evidence")
    if spec.get('role') not in ('dev', 'heldout'):
        raise EvalError(f'{path.name}: role must be dev or heldout')
    if not spec.get('library'):
        raise EvalError(f'{path.name}: library parts are missing')
    return spec


def config(formats, pages, chunk):
    return dict(formats=formats, pdf_pages=pages, chunk=chunk)


def name(c):
    return f"{c['formats']}_p{c['pdf_pages'] or 'all'}_c{c['chunk'] or 'none'}"


def cell(on, chunk):
    """The config with the given factors switched to the target level."""
    return config('all' if 'formats' in on else 'pdf', None if 'pages' in on else 2, chunk if 'chunk' in on else None)


def grid(chunks):
    return [config(f, p, c) for f in ('pdf', 'all') for p in (2, None) for c in (None, *chunks)]


# --------------------------------------------------------------------------- the controlled text

def check_evidence(sb, queries):
    from agent.extract import units
    for q in queries:
        for g in q['relevant']:
            path = sb.root / g['path']
            if not path.is_file():
                raise EvalError(f"{q['id']}: 相关文件不存在：{g['path']}")
            pages = units(sb, path, pages=None)
            if not 1 <= g['page'] <= len(pages) or normalize(g['evidence']) not in normalize(pages[g['page'] - 1]):
                raise EvalError(f"{q['id']}: 证据不匹配：{g['path']} 第 {g['page']} 页")


def page_hashes(pages):
    return [hashlib.sha256(p.encode('utf-8')).hexdigest() for p in pages]


def full_text(sb, files):
    """Every library file read whole: {rel: pages}; files that cannot be read map to None."""
    from agent.extract import READABLE_EXTS, units
    out = {}
    for rel in files:
        p = sb.root / rel
        if p.suffix.lower() not in READABLE_EXTS:
            continue
        try:
            out[rel] = units(sb, p, pages=None)
        except Exception:
            out[rel] = None
    return out


def check_pdf_prefix(sb, texts):
    """A PDF's first two pages must read the same whether two pages or the whole file is asked."""
    from agent.extract import units
    bad = []
    for rel, pages in texts.items():
        if pages is None or not rel.lower().endswith('.pdf'):
            continue
        if units(sb, sb.root / rel, pages=2) != pages[:2]:
            bad.append(rel)
    if bad:
        raise EvalError('PDF 前两页的文字与全文的前两页不一致：' + '、'.join(bad))


def manifest(texts):
    return {rel: page_hashes(pages) if pages is not None else None for rel, pages in sorted(texts.items())}


def check_manifest(texts, path):
    saved = json.loads(Path(path).read_text(encoding='utf-8'))
    now = manifest(texts)
    diff = sorted(set(saved) ^ set(now)) + sorted(r for r in set(saved) & set(now) if saved[r] != now[r])
    if diff:
        raise EvalError('抽取出的文字与已提交的文字清单不一致（语料或抽取器变了）：' + '、'.join(diff[:20]))


def check_config_text(cfg, papers, states, texts):
    """The text a config indexed must be the full text, or its first two pages for (PDF, 2)."""
    pending = [r for r, s in states.items() if s[1] == 'pending_ocr' or (len(s) > 3 and s[3])]
    if pending:
        raise EvalError(f"{name(cfg)}: OCR 没有完成或识别失败：" + '、'.join(pending[:10]))
    for rel, paper in papers.items():
        full = texts.get(rel)
        if full is None:
            raise EvalError(f'{name(cfg)}: {rel} 建索引时读到了文字，单独读取却失败')
        limited = cfg['pdf_pages'] is not None and rel.lower().endswith('.pdf')
        want = full[:cfg['pdf_pages']] if limited else full
        if paper.pages != want:
            raise EvalError(f'{name(cfg)}: {rel} 建索引用的文字与单独读取的不一致')


# --------------------------------------------------------------------------- strata

def gold_place(sb, rel, page, evidence, texts):
    """front_pdf / late_pdf / nonpdf for one gold file, and whether its text came from OCR."""
    from agent import ocr
    from agent.extract import IMAGE_EXTS
    ext = Path(rel).suffix.lower()
    if ext != '.pdf':
        return 'nonpdf', ext in IMAGE_EXTS
    from tools.fs_read import _pdf_pages
    raw = _pdf_pages(sb.root / rel, start=page, pages=1)
    scanned = not raw or len(raw[0].strip()) < ocr.MIN_TEXT
    front = normalize(evidence) in normalize('\n'.join((texts.get(rel) or [])[:2]))
    return ('front_pdf' if front else 'late_pdf'), scanned


def stratify(sb, queries, texts):
    out = {}
    for q in queries:
        if not q['relevant']:
            out[q['id']] = dict(stratum='no_relevant', ocr=False, gold=[])
            continue
        places = [gold_place(sb, g['path'], g['page'], g['evidence'], texts) for g in q['relevant']]
        kinds = {p for p, _ in places}
        stratum = 'nonpdf' if 'nonpdf' in kinds else 'late_pdf' if 'late_pdf' in kinds else 'front_pdf'
        out[q['id']] = dict(stratum=stratum, ocr=any(o for _, o in places), gold=[p for p, _ in places])
    return out


# --------------------------------------------------------------------------- one run

def run(spec, root, chunks, *, manifest_path=None):
    """Score every config of grid(chunks) on every question; returns the report dict."""
    from tools.retrieval import PaperIndex, tokenize
    sb = Sandbox(root)
    queries = spec['queries']
    check_evidence(sb, queries)
    texts = full_text(sb, library.files(root))  # also warms the extraction cache before any timing
    check_pdf_prefix(sb, texts)
    if manifest_path:
        check_manifest(texts, manifest_path)
    strata = stratify(sb, queries, texts)
    configs = grid(chunks)
    report = dict(set=dict(title=spec.get('title'), role=spec['role']), top_k=K, seed=SEED, chunks=list(chunks),
                  configs={}, questions=[dict(id=q['id'], cluster=q['cluster'], kind=q.get('kind'), query=q['query'],
                                                **strata[q['id']], relevant=[g['path'] for g in q['relevant']])
                                           for q in queries])
    rows = {r['id']: r for r in report['questions']}
    for cfg in configs:
        index = PaperIndex(**cfg)
        start = time.perf_counter()
        papers, coverage = index.collect(sb)
        seconds = time.perf_counter() - start
        check_config_text(cfg, papers, index.states, texts)
        times = []
        for q in queries:
            gold = {g['path'] for g in q['relevant']}
            start = time.perf_counter()
            ranked = [(h['path'], h['score']) for h in index.search(sb, q['query'], top_k=10 ** 6)[0]] \
                if tokenize(q['query']) else []
            times.append(time.perf_counter() - start)
            recall, success, straddle = stats.tie_aware(ranked, gold, K)
            top = [p for p, _ in ranked[:K]]
            first = next((i for i, (p, _) in enumerate(ranked[:K], 1) if p in gold), None)
            reach = {g['path']: g['path'] in papers and normalize(g['evidence']) in normalize('\n'.join(papers[g['path']].pages))
                     for g in q['relevant']}
            rows[q['id']][name(cfg)] = dict(
                recall=recall, success=success, rr=(1 / first if first else 0.0) if gold else None, straddle=straddle,
                top=top, false_positive=[p for p in top if p not in gold],
                outcome={p: 'hit' if p in top else 'missed' if reach[p] else 'unreachable' for p in gold},
                floor=dict(zip(('recall', 'success'), stats.random_floor(len(gold), len(papers), K))) if gold else None)
        report['configs'][name(cfg)] = dict(**cfg, files=len(papers), segments=sum(len(p.segments) for p in papers.values()),
                                            build_seconds=seconds, query_ms_mean=1000 * sum(times) / len(times),
                                            query_ms_max=1000 * max(times), coverage=coverage)
    report['analysis'] = {str(c): analyse(report, c) for c in chunks}
    return report


def analyse(report, chunk):
    """Baseline -> target for one chunk size: totals, Shapley parts and factorial effects per stratum."""
    base, target = name(cell(frozenset(), chunk)), name(cell(frozenset(PLAYERS), chunk))
    out = {}
    for stratum in (*STRATA[:3], 'all_with_gold'):
        rows = [r for r in report['questions'] if r['relevant'] and (stratum == 'all_with_gold' or r['stratum'] == stratum)]
        if not rows:
            continue
        clusters = [r['cluster'] for r in rows]
        value = lambda r, on: r[name(cell(on, chunk))]['recall']
        parts = [stats.shapley(lambda on, r=r: value(r, on), PLAYERS) for r in rows]
        mean = lambda f: sum(f(r) for r in rows) / len(rows)
        entry = dict(questions=len(rows), clusters=len(set(clusters)),
                     baseline=mean(lambda r: r[base]['recall']), target=mean(lambda r: r[target]['recall']),
                     total=stats.paired([r[target]['recall'] - r[base]['recall'] for r in rows], clusters, seed=report['seed']),
                     shapley={p: stats.paired([s[p] for s in parts], clusters, seed=report['seed']) for p in PLAYERS},
                     effects={'×'.join(t): v for t, v in stats.factorial(
                         lambda on: mean(lambda r: value(r, on)), PLAYERS).items()},
                     floor_target=mean(lambda r: r[target]['floor']['recall']))
        # conditional ranking: gold reachable under both baseline and target
        both = [(r, p) for r in rows for p in r['relevant']
                if r[base]['outcome'][p] != 'unreachable' and r[target]['outcome'][p] != 'unreachable']
        entry['ranking_on_reachable'] = dict(gold=len(both),
                                             baseline_hits=sum(r[base]['outcome'][p] == 'hit' for r, p in both),
                                             target_hits=sum(r[target]['outcome'][p] == 'hit' for r, p in both))
        out[stratum] = entry
    return out


# --------------------------------------------------------------------------- dev choice and held-out guard

def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True)


def committed(path):
    """Tracked and identical to HEAD."""
    rel = Path(path).resolve().relative_to(ROOT).as_posix()
    return git('ls-files', '--error-unmatch', rel).returncode == 0 and not git('status', '--porcelain', '--', rel).stdout.strip()


def choose_chunk(report, candidates):
    """The committed rule: at formats=all and whole PDFs, the highest macro Recall@5;
    ties go to the higher MRR, then to the faster mean query."""
    rows = [r for r in report['questions'] if r['relevant']]
    def key(c):
        n = name(config('all', None, c))
        return (round(sum(r[n]['recall'] for r in rows) / len(rows), 9), round(sum(r[n]['rr'] for r in rows) / len(rows), 9),
                -report['configs'][n]['query_ms_mean'])
    return max(candidates, key=key)


def guard_heldout(set_path, choice_path, chunk, results_dir, set_sha, rerun_reason):
    for path in (set_path, choice_path):
        if not committed(path):
            raise EvalError(f'{Path(path).name} 没有提交或有改动；留出集只在冻结后运行')
    choice = yaml.safe_load(Path(choice_path).read_text(encoding='utf-8')) or {}
    if choice.get('chosen_chunk') != chunk:
        raise EvalError(f"分段大小 {chunk} 与已提交的选择 {choice.get('chosen_chunk')} 不一致")
    earlier = [p.parent.name for p in Path(results_dir).glob('*/report.json')
               if json.loads(p.read_text(encoding='utf-8')).get('environment', {}).get('set_sha256') == set_sha]
    if earlier and not rerun_reason:
        raise EvalError(f"这套留出集已经跑过（{'、'.join(earlier)}）；确需重跑请写明 --rerun-reason")


def environment(set_path, extra=None):
    from agent import extract, ocr
    from tools.retrieval import INDEX_REVISION
    versions = {}
    for package in ('pymupdf', 'pypdf', 'python-pptx', 'openpyxl', 'PyYAML'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'absent'
    head = git('rev-parse', 'HEAD').stdout.strip()
    return dict(macos=platform.mac_ver()[0], python=sys.version.split()[0], packages=versions,
                ocr=ocr.version(), extract=extract.VERSION, index=INDEX_REVISION,
                git_head=head, git_dirty=bool(git('status', '--porcelain').stdout.strip()),
                set=str(set_path), set_sha256=hashlib.sha256(Path(set_path).read_bytes()).hexdigest(),
                k1=1.2, b=0.75, top_k=K, seed=SEED, **(extra or {}))


# --------------------------------------------------------------------------- validation of a set

def validate(spec, root, dev_spec=None):
    """Checks a set without scoring it. Returns (errors, notes)."""
    from tools.retrieval import tokenize
    sb = Sandbox(root)
    errors, notes = [], []
    try:
        check_evidence(sb, spec['queries'])
    except EvalError as e:
        errors.append(str(e))
        return errors, notes
    texts = full_text(sb, library.files(root))
    strata = stratify(sb, spec['queries'], texts)
    for q in spec['queries']:
        for g, place in zip(q['relevant'], strata[q['id']]['gold']):
            if g['page'] >= 3 and place == 'front_pdf':
                errors.append(f"{q['id']}: {g['path']} 标在第 {g['page']} 页，但证据在前两页也出现，不算后页题")
        if not q['relevant'] and not q.get('reviewed'):
            errors.append(f"{q['id']}: 无相关题需要用关键词查过全库并人工确认（reviewed: true）")
    clusters = {}
    for q in spec['queries']:
        clusters.setdefault(strata[q['id']]['stratum'], set()).add(q['cluster'])
    # minimum sizes bind the held-out set, which carries the final numbers; a dev set only lists its counts
    for stratum, need in {**DEFAULT_MINIMUMS, **(spec.get('minimums') or {})}.items():
        have = len(clusters.get(stratum, ()))
        short = have < need and spec['role'] == 'heldout'
        (errors if short else notes).append(f'{stratum}: {have} 个信息需求' + (f'（至少 {need}）' if spec['role'] == 'heldout' else ''))
    if dev_spec:
        words = lambda s: set(tokenize(s))
        for q in spec['queries']:
            for d in dev_spec['queries']:
                a, b = words(q['query']), words(d['query'])
                if a and b and len(a & b) / len(a | b) > 0.8:
                    notes.append(f"{q['id']} 与开发集 {d['id']} 的查询很接近（词重合 > 0.8）")
    return errors, notes


# --------------------------------------------------------------------------- report

def pct(x):
    return '—' if x is None else f'{x:.1%}'


def summary(report):
    env, k = report['environment'], report['top_k']
    lines = [f"# 检索控制变量评测（{report['set']['role']}，k={k}）", '',
             f"题集 `{env['set']}`（sha256 {env['set_sha256'][:12]}）；git {env['git_head'][:8]}{'（有未提交改动）' if env['git_dirty'] else ''}；"
             f"macOS {env['macos']}、OCR {env['ocr']}、抽取 {env['extract']}；k1={env['k1']}、b={env['b']}、种子 {env['seed']}。",
             '无 API 调用。所有题在同一个合并库里检索；配置之间只改一个因子，文字逐页核对一致。题目和标注由同一人写成。', '']
    names = list(report['configs'])
    rows = [r for r in report['questions'] if r['relevant']]
    lines += ['## 各配置', '', '| 配置 | 文件数 | 段数 | ' + ' | '.join(f'{s} Recall@{k}' for s in STRATA[:3]) + f' | 全部 Recall@{k} / Success@{k} / MRR | 查询平均 |',
              '|---|---|---|' + '---|' * 3 + '---|---|']
    for n in names:
        c = report['configs'][n]
        per = []
        for s in STRATA[:3]:
            rs = [r for r in rows if r['stratum'] == s]
            per.append(pct(sum(r[n]['recall'] for r in rs) / len(rs)) if rs else '—')
        m = lambda key: sum(r[n][key] for r in rows) / len(rows)
        lines.append(f"| {n} | {c['files']} | {c['segments']} | " + ' | '.join(per) +
                     f" | {pct(m('recall'))} / {pct(m('success'))} / {m('rr'):.3f} | {c['query_ms_mean']:.1f}ms |")
    for chunk, analysis in report['analysis'].items():
        lines += ['', f'## 改进前 → 现行（分段 {chunk}）：按分层的总差与 Shapley 归因', '',
                  '每题的三个 Shapley 值加起来正好等于这道题的总差；CI 为按信息需求簇的 bootstrap，p 为簇级精确符号检验。', '',
                  '| 分层 | 题（簇） | 改进前 | 现行 | 总差 [95% CI] 变好/变差 p | 格式 | 页数 | 分段 | 随机下限（现行库） | 都可达的金标：命中 改进前→现行 |',
                  '|---|---|---|---|---|---|---|---|---|---|']
        for stratum, a in analysis.items():
            fmt = lambda d: f"{d['mean']:+.1%} [{d['ci'][0]:+.1%}, {d['ci'][1]:+.1%}] {d['better']}/{d['worse']} p={d['p']:.3g}"
            rk = a['ranking_on_reachable']
            lines.append(f"| {stratum} | {a['questions']}（{a['clusters']}） | {pct(a['baseline'])} | {pct(a['target'])} | {fmt(a['total'])} | "
                         + ' | '.join(fmt(a['shapley'][p]) for p in PLAYERS) +
                         f" | {pct(a['floor_target'])} | {rk['baseline_hits']}→{rk['target_hits']} / {rk['gold']} |")
        lines += ['', '因子效应（2³ 对比，均值上的主效应和交互项）：', '']
        for stratum, a in analysis.items():
            lines.append(f"- {stratum}：" + '；'.join(f"{t} {v:+.1%}" for t, v in a['effects'].items()))
    lines += ['', '## 可达性（每个金标文件：不可达 / 可达未进前 5 / 命中）', '', '| 配置 | ' + ' | '.join(STRATA[:3]) + ' |', '|---|---|---|---|']
    for n in names:
        cells = []
        for s in STRATA[:3]:
            outs = [o for r in rows if r['stratum'] == s for o in r[n]['outcome'].values()]
            cells.append(f"{outs.count('unreachable')} / {outs.count('missed')} / {outs.count('hit')}")
        lines.append(f'| {n} | ' + ' | '.join(cells) + ' |')
    none = [r for r in report['questions'] if not r['relevant']]
    if none:
        lines += ['', '无相关题前 5 名里的误候选数：' + '；'.join(
            f"{r['id']} " + ' / '.join(f"{n}={len(r[n]['false_positive'])}" for n in names) for r in none)]
    straddles = {n: sum(r[n]['straddle'] for r in rows) for n in names}
    lines += ['', '同分跨过第 5 名的题数（已按随机顺序取期望）：' + ('；'.join(f'{n}={v}' for n, v in straddles.items() if v) or '无')]
    lines += ['', '## 逐题', '', '| 题 | 簇 | 分层 | OCR | ' + ' | '.join(names) + ' |', '|---|---|---|---|' + '---|' * len(names)]
    for r in report['questions']:
        cell_text = lambda n: f"{len(r[n]['false_positive'])} 误" if not r['relevant'] else pct(r[n]['recall'])
        lines.append(f"| {r['id']} | {r['cluster']} | {r['stratum']} | {'是' if r['ocr'] else ''} | " + ' | '.join(cell_text(n) for n in names) + ' |')
    lines += ['', '## 局限', '',
              '- 题目和相关性标注由同一人写成，不是独立盲测；混合格式文件是合成的。',
              '- 格式因子会改变库的文件数和平均长度（更多干扰项），这是该因子的一部分。',
              '- 现行配置的格式、全文两项是在看过开发集之后定的；留出集验证现行系统，不负责选择它。',
              '- 分段在开发集上只改变了少数题，按可行的题量测不出这么小的效应，只报 CI，不下结论。',
              '- 计时在抽取缓存预热之后，只作参考，不参与质量比较。']
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--set', type=Path, required=True)
    ap.add_argument('--out', type=Path, help='新的结果目录（不覆盖已有目录）')
    ap.add_argument('--validate', action='store_true', help='只校验题集，不打分')
    ap.add_argument('--dev', type=Path, help='--validate 时对照的开发集（查近似重复）')
    ap.add_argument('--manifest', type=Path, default=ROOT / 'eval/retrieval_textmanifest.json')
    ap.add_argument('--write-manifest', action='store_true', help='写出当前抽取文字的清单（冻结开发集时用）')
    ap.add_argument('--chunks', default='250,500,1000', help='开发集比较的分段大小')
    ap.add_argument('--rerun-reason', default='')
    args = ap.parse_args(argv)
    spec = load_set(args.set)
    set_sha = hashlib.sha256(args.set.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='filebutler-ablation-') as folder:
        root = library.build(folder, spec['library'])
        if args.validate:
            errors, notes = validate(spec, root, load_set(args.dev) if args.dev else None)
            print('\n'.join(['校验通过' if not errors else '校验没有通过：', *errors, '', *notes]))
            return 1 if errors else 0
        if args.write_manifest:
            texts = full_text(Sandbox(root), library.files(root))
            args.manifest.write_text(json.dumps(manifest(texts), ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
            print(f'wrote {args.manifest} ({len(texts)} files)')
            return 0
        if not args.out or args.out.exists():
            raise SystemExit('需要一个新的 --out 目录')
        if spec['role'] == 'heldout':
            choice = yaml.safe_load(CHOICE.read_text(encoding='utf-8')) if CHOICE.exists() else {}
            chunks = [choice.get('chosen_chunk')]
            guard_heldout(args.set, CHOICE, chunks[0], args.out.parent, set_sha, args.rerun_reason)
        else:
            if not CHOICE.exists() or not committed(CHOICE) or 'rule' not in (yaml.safe_load(CHOICE.read_text(encoding='utf-8')) or {}):
                raise SystemExit('跑开发集之前，先把分段大小的选择规则写进 eval/retrieval_choice.yaml 并提交')
            chunks = [int(c) for c in args.chunks.split(',')]
        report = run(spec, root, chunks, manifest_path=args.manifest if args.manifest.exists() else None)
    report['environment'] = environment(args.set, dict(rerun_reason=args.rerun_reason or None,
                                                       manifest=str(args.manifest) if args.manifest.exists() else None))
    if spec['role'] == 'dev':
        report['chosen_chunk'] = choose_chunk(report, chunks)
    args.out.mkdir(parents=True)
    (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    text = summary(report)
    (args.out / 'summary.md').write_text(text, encoding='utf-8')
    print(text)
    if spec['role'] == 'dev':
        print(f"按已提交的规则，分段大小选 {report['chosen_chunk']}；写进 eval/retrieval_choice.yaml 的 chosen_chunk 并提交后，才能跑留出集。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
