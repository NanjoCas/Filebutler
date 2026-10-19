"""Real-model acceptance through an isolated local HTTP server and headless Chrome.

python -m eval.acceptance --offline --out eval/results/acceptance_<name>
Real API requires explicit --live instead of --offline.
Uses existing model credentials; estimated price cap defaults to CNY 1.20.
Only the temporary corpus copy is mutated. Results/DB/traces/screenshots are retained.
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

try:
    import httpx
except ImportError:
    import httpx2 as httpx
import uvicorn

from agent.history import has_undo
from agent.llm import LLM
from eval.checker import snapshot, files_preserved
from main import ROOT, load_config
import webapp

TEXT_GROUPS = [
    ['Wang.pdf', '强化学习/Weak Supervision Reinforcement Learning.pdf'],
    ['Hu.pdf', 'review/Deep learning for fake_news_detection_A_comprehensive_survey.pdf'],
    ['review/A systematic literature review of machine learning algorithms and datasets.pdf',
     'review/Fake news detection a systematic literature review of machine learning algorithms and datasets.pdf'],
]
GOLD_SEARCH = ['Dong.pdf', 'Guacho.pdf', 'covid-19/Cross_SEAN_A_cross_stitch_semi_supervise.pdf']


class ProtocolFixtureClient:
    """Local protocol/UI fixture; never measures model quality or contacts an API."""
    def __init__(self):
        from types import SimpleNamespace as NS
        self.NS = NS
        self.chat = NS(completions=NS(create=self.create))
        self.count = 0

    def create(self, **kw):
        import re
        messages = kw["messages"]
        start = max(i for i,m in enumerate(messages) if m["role"] == "user")
        prompt = messages[start]["content"]
        results = [m for m in messages[start+1:] if m["role"] == "tool"]
        calls, content = [], ""
        local = any(s['function']['name'] in {'report_findings','deduplicate'} for s in kw.get('tools',[]))
        if local:
            if '内容去重' in prompt or '按论文文字内容查重' in prompt:
                calls=[('deduplicate',dict(path='文献',mode='text'))]
            elif not results:
                calls=[('search_content',dict(query='domain adaptive reinforcement REAL FND' if 'REAL-FND' in prompt else 'semi supervised fake news detection'))]
            else:
                paths = ['2202.08159v1.pdf','强化学习/Domain Adaptive Fake News Detection via Reinforcement Learning.pdf'] if 'REAL-FND' in prompt else GOLD_SEARCH
                matches=re.findall(r'file:(F[a-f0-9]+) evidence:(E[a-f0-9]+).*?名称=(.*)',results[0]['content'])
                findings=[dict(file_id=fid,evidence_ids=[eid],relation='method',reason='本地协议展示样例')
                          for fid,eid,name in matches if any(Path(p).stem[:65] == name for p in paths)]
                note='验收标记：松柏' if any('持续约定' in m.get('content','') for m in messages) else ''
                calls=[('report_findings',dict(findings=findings,note=note))]
            self.count+=1
            NS=self.NS
            tc=[NS(id=f'fixture_{self.count}_{i}',function=NS(name=n,arguments=json.dumps(a,ensure_ascii=False))) for i,(n,a) in enumerate(calls)]
            return NS(usage=NS(prompt_tokens=0,completion_tokens=0,prompt_cache_hit_tokens=0),choices=[NS(message=NS(content='',tool_calls=tc))])
        if "内容去重" in prompt or "按论文文字内容查重" in prompt:
            if not results:
                calls = [("find_duplicates", dict(path="文献",mode="text"))]
            elif len(results) == 1:
                sid = re.search(r"scan_id=([a-z0-9]+)",results[-1]["content"]).group(1)
                calls = [("apply_dedup", dict(scan_id=sid))]
            else:
                content = "已按内容去重，回收3份确定副本，保留不同版本；本地复核通过。"
        else:
            realfnd = "REAL-FND" in prompt
            paths = (["2202.08159v1.pdf", "强化学习/Domain Adaptive Fake News Detection via Reinforcement Learning.pdf"]
                     if realfnd else GOLD_SEARCH)
            if not results:
                calls = [("search_content",dict(query="domain adaptive reinforcement REAL FND" if realfnd else "semi supervised fake news detection",path=".",top_k=5))]
            elif len(results) == 1:
                calls = [("read_file",dict(path="文献/"+p)) for p in paths]
            else:
                content = "| 论文 | 依据 |\n| --- | --- |\n" + "\n".join(f"| [{Path(p).stem}](sandbox:/文献/{p.replace(' ', '%20')}) | 本地协议展示样例 |" for p in paths)
                content += "\n\n仅检索前两页，未执行OCR，不能保证穷尽。"
                if any("持续约定" in m.get("content","") and "松柏" in m.get("content","") for m in messages):
                    content += "\n\n验收标记：松柏"
        self.count += 1
        NS = self.NS
        tc = [NS(id=f"fixture_{self.count}_{i}",function=NS(name=n,arguments=json.dumps(a,ensure_ascii=False))) for i,(n,a) in enumerate(calls)]
        return NS(usage=NS(prompt_tokens=0,completion_tokens=0,prompt_cache_hit_tokens=0),
                  choices=[NS(message=NS(content=content,tool_calls=tc or None))])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline',action='store_true',help='Local scripted protocol/UI only; no API, no model-quality claim')
    mode.add_argument('--live',action='store_true',help='Send test excerpts/history to configured external API and incur charges')
    parser.add_argument('--baseline',action='store_true',help='Current code with local_runtime disabled and v5 prompt, for paired comparison')
    parser.add_argument('--price-cap', type=float, default=1.20)
    parser.add_argument('--history-id', default='5a0106b837b24be9a68d846c13f4d9e7')
    args = parser.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    cfg = load_config(ROOT / 'config.yaml')
    if args.baseline: cfg.update(local_runtime=False,prompt='v5_latency')
    if not args.offline and not os.getenv(cfg['api_key_env']):
        parser.error('configured API key environment variable is unset')
    source, user_sb = ROOT / 'data/real/papers', Path(cfg['sandbox'])
    source_before, user_before = snapshot(source, True), snapshot(user_sb, True)
    # Copy prior history read-only; never use the live UI database for test writes.
    with sqlite3.connect(f'file:{ROOT / ".filebutler-ui/state.sqlite3"}?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT history_json FROM conversations WHERE id=?', (args.history_id,)).fetchone()
        old_history = json.loads(row[0]) if row else []
    report = dict(variant='baseline' if args.baseline else 'runtime', offline=args.offline, model='scripted-protocol-fixture' if args.offline else cfg['model'], prompt=cfg['prompt'], price_cap=args.price_cap,
                  cases=[], checks=[], browser=[], estimated_cost=0.0, actual_api_requests=0,
                  limitations=['Single-run acceptance, not a randomized A/B latency experiment.',
                               'Long history is replayed from saved real history with one early test constraint.'])
    def save():
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(name, ok, **detail):
        report['checks'].append(dict(name=name, ok=bool(ok), **detail)); save()
    class BudgetLLM(LLM):
        def __init__(self, config):
            super().__init__(config, client=ProtocolFixtureClient() if args.offline else None)

        def chat(self, *a, **kw):
            if report['estimated_cost'] >= args.price_cap:
                raise RuntimeError('acceptance API price cap reached')
            before = self.cost
            observer = kw.get('on_request')
            def observe(event):
                if event.get('phase') == 'start' and not args.offline:
                    report['actual_api_requests'] += 1
                if observer: observer(event)
            kw['on_request'] = observe
            try:
                return super().chat(*a, **kw)
            finally:
                report['estimated_cost'] += self.cost - before
    started = time.time()
    server = chrome = server_thread = None
    with tempfile.TemporaryDirectory(prefix='filebutler-acceptance-') as tmp:
        tmp = Path(tmp); sandbox = tmp / 'sandbox'
        shutil.copytree(source, sandbox / '文献', symlinks=True)
        for p in user_sb.rglob('*'):
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in {'.jpg', '.jpeg', '.png'} and not any(x.startswith('.') for x in p.relative_to(user_sb).parts):
                dst = sandbox / '图片' / p.relative_to(user_sb); dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p,dst)
        initial = snapshot(sandbox, True)
        report['corpus'] = dict(pdf=sum(p.suffix.lower()=='.pdf' for p in sandbox.rglob('*') if p.is_file()), files=sum(v!='<dir>' for v in initial.values()))
        webapp.STATE_DIR = out / 'state'; webapp.STATE_DIR.mkdir()
        webapp.DB_PATH = webapp.STATE_DIR / 'state.sqlite3'; webapp.LOG_DIR = out / 'traces'
        webapp._config = lambda: {**cfg, 'sandbox': str(sandbox)}
        webapp.LLM = BudgetLLM
        sock = socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(128)
        port = sock.getsockname()[1]; url = f'http://127.0.0.1:{port}'
        server = uvicorn.Server(uvicorn.Config(webapp.app, host='127.0.0.1', port=port, log_level='warning'))
        server_thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); server_thread.start()
        client = httpx.Client(base_url=url, timeout=20)
        def api(method, path, **kw):
            r = client.request(method,path,**kw); r.raise_for_status(); return r.json()
        deadline = time.monotonic()+15
        while not server.started:
            if time.monotonic()>deadline: raise TimeoutError('local server startup')
            time.sleep(.05)
        chrome_log = (out/'chrome.log').open('w')
        profile = tmp/'chrome-profile'
        try:
            chrome = subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                '--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking',
                '--disable-sync','--disable-component-update','--window-size=1500,1050',
                f'--user-data-dir={profile}','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','about:blank'], stdout=chrome_log,stderr=chrome_log)
            deadline = time.monotonic()+25
            while not (profile/'DevToolsActivePort').exists():
                if chrome.poll() is not None or time.monotonic()>deadline: raise RuntimeError('headless Chrome startup failed')
                time.sleep(.1)
            debug_port = (profile/'DevToolsActivePort').read_text().splitlines()[0]
            def browser(mode, arg=''):
                p = subprocess.run(['node',str(ROOT/'eval/acceptance_browser.mjs'),debug_port,url,str(out),mode,arg],capture_output=True,text=True,timeout=260)
                result = json.loads((out/f'browser-{mode}.json').read_text())
                report['browser'].append(result); check(f'browser_{mode}',result['ok'],error=result.get('error'))
                return result
            def history(cid):
                with webapp._db() as db:
                    return json.loads(db.execute('SELECT history_json FROM conversations WHERE id=?',(cid,)).fetchone()[0])
            def record(name, tid):
                turn = api('GET',f'/api/turns/{tid}')
                rows = [json.loads(x) for x in (webapp.LOG_DIR/f'{turn["run_id"]}.trace.jsonl').read_text().splitlines()]
                tools = [r for r in rows if 'tool' in r]
                requests = [r for r in rows if r.get('event')=='llm_request' and r.get('phase')=='end']
                item = dict(name=name, tid=tid, run_id=turn['run_id'], status=turn['status'], final=turn['final'],
                            error=turn['error'],result=turn['result'],tools=tools,requests=requests,
                            tool_counts=dict(Counter(r['tool'] for r in tools)),
                            prompt_tokens=sum(r.get('prompt_tokens',0) for r in requests),
                            completion_tokens=sum(r.get('completion_tokens',0) for r in requests),
                            cache_hit_tokens=sum(r.get('cache_hit_tokens',0) for r in requests))
                from web_render import FileCatalog, render_markdown
                catalog=FileCatalog(webapp.Sandbox(str(sandbox)),webapp._artifacts(turn['conversation_id']),turn['conversation_id'])
                rendered=render_markdown(turn['final'],catalog)
                item['unavailable_references']=rendered.count('class="unavailable-reference"')
                check(name+'_file_references',item['unavailable_references']==0)
                report['cases'].append(item); check(name+'_completed',turn['status']=='completed' and turn['result'].get('stop_reason')=='done')
                print(json.dumps({k:item[k] for k in ('name','result','tool_counts','error')},ensure_ascii=False),flush=True)
                return item
            def run(cid,prompt,name,allow_write=False,review=False):
                tid = api('POST',f'/api/conversations/{cid}/messages',json={'content':prompt})['turn_id']
                deadline = time.monotonic()+240; answered=set()
                while time.monotonic()<deadline:
                    t = api('GET',f'/api/turns/{tid}')
                    if t['status'] not in {'queued','running','waiting_confirmation'}: return record(name,tid)
                    pending=t.get('pending_confirmation')
                    if pending and pending['id'] not in answered:
                        if review: browser('review',cid); review=False
                        rows=[x for g in pending['action'].get('groups',[]) for x in g.get('items',[])]
                        expected={f'文献/{p}' for g in TEXT_GROUPS for p in g}
                        actual={x['path'] for x in rows}
                        safe=allow_write and actual and actual<=expected
                        check(name+'_confirmation_scope',safe,paths=sorted(actual))
                        api('POST',f'/api/turns/{tid}/confirm',json={'answer':'y' if safe else 'n','confirmation_id':pending['id']});answered.add(pending['id'])
                    time.sleep(.1)
                raise TimeoutError(name)
            # Browser performs new conversation -> real query -> table -> reference click.
            ui = browser('search'); cid=ui['cid']; search=record('topic_search',ui['tid'])
            refs={r['path'] for r in ui.get('refs',[])}
            check('fixture_reference_paths' if args.offline else 'topic_gold_recall',all(f'文献/{p}' in refs for p in GOLD_SEARCH),expected=GOLD_SEARCH,actual=sorted(refs))
            raw=client.get(ui['preview'].split('#')[0]) if ui.get('preview') else None
            check('preview_raw_pdf',raw is not None and raw.status_code==200 and raw.content.startswith(b'%PDF'))
            check('search_no_change',snapshot(sandbox,True)==initial)
            dcid=api('POST','/api/conversations')['id']
            prompt='对文献按论文文字内容查重，每组只保留一份，把副本移入回收区。不同版本先保留，不按相似度合并。'
            first=run(dcid,prompt,'content_dedup',allow_write=True,review=True)
            after=snapshot(sandbox,True)
            expected_removed={f'文献/{g[1]}' for g in TEXT_GROUPS}
            removed={p for p,v in initial.items() if v!='<dir>' and p not in after}
            check('content_expected_removed',removed==expected_removed,expected=sorted(expected_removed),actual=sorted(removed))
            check('content_preserved',files_preserved(initial,after))
            check('content_no_unsolicited_near',all(json.loads(t['args']).get('mode')=='text' for t in first['tools'] if t['tool']=='find_duplicates' and not json.loads(t['args']).get('scan_id')))
            target={'deduplicate':1} if cfg.get('local_runtime') else {'find_duplicates':1,'apply_dedup':1}
            check('content_minimal_calls',first['tool_counts']==target,expected=target,actual=first['tool_counts'])
            undone=api('POST',f'/api/turns/{first["tid"]}/undo')
            check('undo_exact_restore',snapshot(sandbox,True)==initial,report=undone['report'])
            check('undo_in_model_history',has_undo(history(dcid),first['run_id']))
            again=run(dcid,'按刚才相同的规则，再做一次内容去重。','dedup_after_undo',allow_write=True)
            check('redo_expected_removed',{p for p,v in initial.items() if v!='<dir>' and p not in snapshot(sandbox,True)}==expected_removed)
            import re
            if cfg.get('local_runtime'):
                stats=again['result']['runtime']['dedup_scan']
                parsed,hits=stats['parsed'],stats['text_cache_hits']
            else:
                redo_scan = next(t for t in again['tools'] if t['tool']=='find_duplicates')['result']
                cache = re.search(r'全文解析 (\d+)，缓存复用 (\d+)',redo_scan)
                parsed,hits=int(cache.group(1)),int(cache.group(2))
            check('redo_cache_reused',hits>=48,parsed=parsed,hits=hits)
            check('redo_no_false_previous_failure',not any(s in again['final'] for s in ['上次失败','之前失败','上次未成功','之前未成功']))
            api('POST',f'/api/turns/{again["tid"]}/undo'); check('redo_undo_restore',snapshot(sandbox,True)==initial)
            # Replay existing long history plus an early stable formatting constraint.
            lcid=api('POST','/api/conversations')['id']
            long_history=json.loads(json.dumps(old_history,ensure_ascii=False))
            long_history.insert(1,{'role':'user','content':'本次验收的持续约定：以后每次答复末尾都写“验收标记：松柏”，用中文回复。'})
            with webapp._db() as db:
                db.execute('UPDATE conversations SET history_json=? WHERE id=?',(json.dumps(long_history,ensure_ascii=False),lcid))
            old_tools=sum(m.get('role')=='tool' for m in long_history)
            long=run(lcid,'继续查找使用 REAL-FND 做领域自适应强化学习假新闻检测的论文，给出简短依据和文件引用，遵守开头的回复约定。','long_conversation')
            check('long_constraint_retained','验收标记：松柏' in long['final'])
            ctx=long['result'].get('context',{});check('long_history_projected',ctx.get('archived_messages',0)>0 and ctx.get('after_chars',0)<ctx.get('before_chars',0),context=ctx)
            check('long_full_history_retained',sum(m.get('role')=='tool' for m in history(lcid))>=old_tools)
            check('long_no_change',snapshot(sandbox,True)==initial)
            browser('reload',lcid)
        except Exception as exc:
            report['error']=f'{type(exc).__name__}: {exc}';check('harness_completed',False,error=report['error']);raise
        finally:
            if chrome is not None:
                chrome.terminate()
                try: chrome.wait(timeout=10)
                except subprocess.TimeoutExpired: chrome.kill();chrome.wait()
            chrome_log.close(); client.close()
            server.should_exit=True; server_thread.join(timeout=10)
            webapp.POOL.shutdown(wait=True)
            report['elapsed_seconds']=time.time()-started
            check('source_unchanged',snapshot(source,True)==source_before)
            check('user_sandbox_unchanged',snapshot(user_sb,True)==user_before)
            report['passed']=all(x['ok'] for x in report['checks']);save()
    print(json.dumps({'out':str(out),'passed':report['passed'],'estimated_cost':report['estimated_cost'],'checks':report['checks']},ensure_ascii=False),flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__': main()
