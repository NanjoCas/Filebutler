"""Read-only follow-up audit of a completed acceptance report; no API calls.

python -m eval.acceptance_audit eval/results/<run>
Checks every explicit file link against a reconstructed pre-cleanup corpus, so a
bad model path is distinguished from a correctly named file currently in trash.
"""
import argparse
import json
import shutil
import sqlite3
import re
import copy
import tempfile
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt
from agent.artifacts import ArtifactStore, digest, signature
from main import ROOT
from safety.sandbox import Sandbox
from web_render import FileCatalog


def audit(base: Path):
    report = json.loads((base / 'report.json').read_text())
    md = MarkdownIt('commonmark')
    original_validate=md.validateLink
    md.validateLink=lambda value: bool(re.fullmatch(r'(file:F|evidence:E)[a-f0-9]+(?:#page=[1-9]\d*)?',value)) or original_validate(value)
    cases = []
    with tempfile.TemporaryDirectory(prefix='filebutler-reference-audit-') as tmp:
        root = Path(tmp).resolve()
        shutil.copytree(ROOT / 'data/real/papers', root / '文献', symlinks=True)
        for case in report['cases']:
            data={}
            with sqlite3.connect(f'file:{base}/state/state.sqlite3?mode=ro',uri=True) as conn:
                columns={r[1] for r in conn.execute('PRAGMA table_info(conversations)')}
                if 'artifacts_json' in columns:
                    found=conn.execute('SELECT artifacts_json FROM conversations WHERE id=(SELECT conversation_id FROM turns WHERE id=?)',(case['tid'],)).fetchone()
                    if found:data=json.loads(found[0])
            data=copy.deepcopy(data)
            data['root']=str(root)
            # The original temp root has been deleted. Rebind only audit copies
            # with the exact recorded content digest, without changing the DB.
            for row in data.get('files',{}).values():
                path=root/row['path']
                if row.get('status') == 'trash' and not path.exists():
                    # Reconstruct only registered, byte-identical victims from
                    # the untouched source corpus. Hidden files are never read
                    # from the user's sandbox or the live UI database.
                    matches=[p for p in (root/'文献').rglob('*')
                             if p.is_file() and p.name==row['name'] and digest(p)==row['version']]
                    if matches:
                        path.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copy2(matches[0],path)
                if path.is_file() and digest(path)==row['version']:
                    row['stat']=signature(path)
            store=ArtifactStore(Sandbox(root),data)
            catalog = FileCatalog(Sandbox(root),store)
            links = []
            for block in md.parse(case['final']):
                for token in block.children or []:
                    if token.type == 'link_open' and (href := token.attrGet('href') or '').startswith(('sandbox:/','file:','evidence:')):
                        resolved = catalog.resolve(href)
                        links.append(dict(href=href, decoded=unquote(href), valid=bool(resolved),
                                          resolved=resolved[0] if resolved else None))
            method_paths=[]
            for tool in case['tools']:
                if tool['tool']=='report_findings' and tool['ok']:
                    for finding in json.loads(tool['args']).get('findings',[]):
                        fid=finding['file_id'].removeprefix('file:')
                        if finding['relation']=='method' and fid in store.data['files']:
                            method_paths.append(store.data['files'][fid]['path'])
            requests = case['requests']
            cases.append(dict(name=case['name'], method_paths=method_paths, links=links, invalid_links=[l for l in links if not l['valid']],
                              model_share=case['result']['llm_seconds'] / case['result']['active_seconds'],
                              prompt_tokens=case['prompt_tokens'], output_tokens=case['completion_tokens'],
                              cache_hit_tokens=case['cache_hit_tokens'],
                              cache_hit_ratio=case['cache_hit_tokens'] / case['prompt_tokens'],
                              final_request_ms=requests[-1]['ms'] if requests else None))
    totals = dict(prompt_tokens=sum(c['prompt_tokens'] for c in report['cases']),
                  output_tokens=sum(c['completion_tokens'] for c in report['cases']),
                  cache_hit_tokens=sum(c['cache_hit_tokens'] for c in report['cases']),
                  active_seconds=sum(c['result']['active_seconds'] for c in report['cases']),
                  model_seconds=sum(c['result']['llm_seconds'] for c in report['cases']),
                  tool_seconds=sum(c['result']['tool_seconds'] for c in report['cases']))
    totals['total_tokens'] = totals['prompt_tokens'] + totals['output_tokens']
    totals['model_share'] = totals['model_seconds'] / totals['active_seconds']
    result = dict(offline=report['offline'], model=report['model'], estimated_cost=report['estimated_cost'],
                  actual_api_requests=report['actual_api_requests'], checks_passed=sum(x['ok'] for x in report['checks']),
                  checks_total=len(report['checks']), failed_checks=[x for x in report['checks'] if not x['ok']],
                  request_retries=sum(bool(x.get('retry')) for c in report['cases'] for x in c['requests']),
                  totals=totals,cases=cases,
                  reference_audit_scope='Original corpus copied into a temporary directory; registered trash is reconstructed only from matching original basename and SHA256. IDs are read from the test database, not inferred from model labels.')
    (base / 'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('result',type=Path)
    args=parser.parse_args();result=audit(args.result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
