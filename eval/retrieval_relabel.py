"""Relevance labelling for the merged library, blind to how the system ranks.

    python -m eval.retrieval_relabel dump  --set <set> --part mixed --out <file.md>   # every file of a part, read whole
    python -m eval.retrieval_relabel scan  --set <set> --out <file.md>                # every occurrence of each question's patterns
    python -m eval.retrieval_relabel draft-dev --out eval/retrieval_set_<date>_dev.yaml  # the 2026-10-24 set on the merged library

Candidates come from reading every file (dump) or from plain pattern matching over the full
text (scan), never from the retrieval code: this module must not import tools.retrieval
(tests/test_retrieval_ablation.py checks it). A person confirms each candidate and writes
the decision into the set's relabel_audit before the set is frozen.
"""
import argparse
import sys
import tempfile
from pathlib import Path

import yaml

from eval import retrieval_library as library
from eval.retrieval_library import normalize
from safety.sandbox import Sandbox

CONTEXT = 200
OLD_SET = library.ROOT / 'eval/retrieval_set_20261024.yaml'


def read_whole(sb, rel):
    from agent.extract import units
    try:
        return units(sb, sb.root / rel, pages=None)
    except Exception as e:  # unreadable files are listed, not skipped silently
        return [f'（读取失败：{type(e).__name__}: {e}）']


def dump(root, part):
    """Every file of one library part, page by page, for a person to read against the questions."""
    sb = Sandbox(root)
    out = []
    for rel in library.files(root):
        if rel.split('/', 1)[0] != part:
            continue
        out.append(f'## {rel}\n')
        for i, page in enumerate(read_whole(sb, rel), 1):
            out.append(f'### 第 {i} 页\n\n{page.strip()}\n')
    return '\n'.join(out)


def scan(root, queries):
    """{question id: [(path, page, pattern, context)]} for every occurrence of the question's patterns."""
    sb = Sandbox(root)
    pages = {rel: read_whole(sb, rel) for rel in library.files(root)}
    found = {}
    for q in queries:
        hits = []
        for pattern in q.get('patterns') or []:
            key = normalize(pattern)
            if not key:
                continue
            for rel, texts in pages.items():
                for i, text in enumerate(texts, 1):
                    folded = text.casefold()
                    at = folded.find(pattern.casefold())
                    if at < 0 and key not in normalize(text):
                        continue
                    lo = max(0, at - CONTEXT) if at >= 0 else 0
                    hits.append((rel, i, pattern, text[lo:lo + 2 * CONTEXT + len(pattern)].replace('\n', ' ')))
        found[q['id']] = hits
    return found


def draft_dev(old=OLD_SET):
    """The 2026-10-24 questions on the merged library: paths prefixed with their part, and
    paraphrases of one need (identical gold lists) in one cluster. Rules and audit are to be
    written by a person before freezing."""
    spec = yaml.safe_load(old.read_text(encoding='utf-8'))
    base = yaml.safe_load(old.with_name(spec['base']).read_text(encoding='utf-8'))
    queries = [dict(q, corpus='papers') for q in base['queries']] + spec['queries']
    clusters, out = {}, []
    for q in queries:
        relevant = [dict(g, path=f"{q['corpus']}/{g['path']}") for g in q['relevant']]
        key = tuple(sorted(g['path'] for g in relevant))
        cluster = clusters.setdefault(key, q['id']) if key else q['id']
        out.append(dict(id=q['id'], cluster=cluster, kind=q['kind'], query=q['query'], intent=q['intent'],
                        relevant=relevant, patterns=[]))
    return dict(title='检索开发集（2026-10-24 题目，合并库）', role='dev', derived_from=str(old.relative_to(library.ROOT)),
                # the same library as the held-out set, held-out files included, so term statistics match
                library=[dict(name='papers', source=spec['corpora']['papers']), dict(name='mixed', builder='eval.retrieval_mixed'),
                         dict(name='heldout', builder='eval.retrieval_mixed_heldout')],
                relevance_rules='待写：合并库后的相关性范围规则（先写规则并提交，再看候选）。',
                relabel_audit=[], queries=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=('dump', 'scan', 'draft-dev'))
    ap.add_argument('--set', type=Path)
    ap.add_argument('--part')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f'{args.out} 已存在，请换一个文件名')
    if args.command == 'draft-dev':
        text = yaml.safe_dump(draft_dev(), allow_unicode=True, sort_keys=False, width=200)
    else:
        spec = yaml.safe_load(args.set.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(prefix='filebutler-relabel-') as folder:
            root = library.build(folder, spec['library'])
            if args.command == 'dump':
                text = dump(root, args.part)
            else:
                found = scan(root, spec['queries'])
                text = '\n'.join(f"## {qid}（{len(hits)} 处）\n\n" + '\n'.join(
                    f'- `{rel}` 第 {page} 页 · 「{pattern}」：…{context}…' for rel, page, pattern, context in hits)
                    for qid, hits in found.items()) + '\n'
    args.out.write_text(text, encoding='utf-8')
    print(f'wrote {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
