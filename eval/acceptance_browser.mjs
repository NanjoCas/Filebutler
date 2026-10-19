// Native Chrome DevTools protocol; Node 22+ and installed Chrome, no npm dependencies.
import fs from 'node:fs';
const [debugPort, appUrl, out, mode = 'search', arg = ''] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
let target = targets.find(t => t.type === 'page' && t.url.startsWith(appUrl));
if (!target) target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(appUrl)}`, {method: 'PUT'})).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = reject;});
let seq = 0; const pending = new Map(), errors = [];
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id) {const p = pending.get(m.id); if (p) {pending.delete(m.id); m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result);}}
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
};
function call(method, params = {}) {return new Promise((resolve, reject) => {const id = ++seq; pending.set(id, {resolve,reject}); ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression) {
  const r = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function waitFor(expression, seconds = 180) {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {const v = await evaluate(expression); if (v) return v; await new Promise(r => setTimeout(r,100));}
  throw Error(`Timeout: ${expression}`);
}
async function screenshot(name) {const r = await call('Page.captureScreenshot', {format:'png'}); fs.writeFileSync(`${out}/${name}.png`,Buffer.from(r.data,'base64'));}
await call('Page.enable'); await call('Runtime.enable');
const report = {mode, errors};
try {
  await waitFor(`typeof currentConversation !== 'undefined' && currentConversation && !document.querySelector('#send-button').disabled`,30);
  if (mode === 'search') {
    const old = await evaluate('currentConversation');
    await evaluate(`document.querySelector('#new-chat').click()`);
    report.cid = await waitFor(`currentConversation !== ${JSON.stringify(old)} && !document.querySelector('#send-button').disabled && currentConversation`,20);
    const prompt = '帮我找出与半监督假新闻检测相关的论文。用表格列出论文、简短依据和可点击的文件引用。';
    await evaluate(`document.querySelector('#message-input').value=${JSON.stringify(prompt)};document.querySelector('#send-button').click()`);
    report.tid = await waitFor('currentTurn',30);
    report.live = await waitFor(`document.querySelector('#agent-activity.working') && ({title:document.querySelector('.activity-title').textContent,animation:getComputedStyle(document.querySelector('.activity-symbol')).animationName})`,30);
    await screenshot('search-running');
    await waitFor(`!currentTurn && document.querySelector('#agent-activity.finished') && document.querySelector('.message.assistant')`,210);
    report.tables = await evaluate(`document.querySelectorAll('.message.assistant table').length`);
    report.refs = await evaluate(`[...document.querySelectorAll('.message.assistant .file-reference')].map(x=>({path:x.dataset.filePath,page:x.dataset.filePage,color:getComputedStyle(x).color}))`);
    if (!report.refs.length) throw Error('No clickable file reference');
    await evaluate(`document.querySelector('.message.assistant .file-reference[data-file-kind="file"][data-file-path$=".pdf"]').click()`);
    report.preview = await waitFor(`document.querySelector('#preview iframe')?.getAttribute('src')`,15);
    report.scroll = await evaluate(`({overflow:getComputedStyle(document.querySelector('#messages')).overflowY, height:document.querySelector('#messages').clientHeight, noHorizontalOverflow:document.documentElement.scrollWidth<=innerWidth})`);
    await new Promise(r=>setTimeout(r,2000)); // Let Chrome's embedded PDF viewer paint before visual QA.
    await screenshot('search-result-preview');
    if (!report.tables || !report.scroll.noHorizontalOverflow) throw Error('Missing table or horizontal page overflow');
  } else if (mode === 'review') {
    await evaluate(`openConversation(${JSON.stringify(arg)})`);
    await waitFor(`document.querySelector('#confirmation button')`,20);
    await evaluate(`document.querySelector('#confirmation button').click()`);
    await waitFor(`document.querySelector('#confirmation-review').open`,10);
    report.groups = await evaluate(`[...document.querySelectorAll('#review-plan .confirmation-group')].map(g=>({keep:g.querySelectorAll('.confirmation-file-row.retain').length,trash:g.querySelectorAll('.confirmation-file-row.trash').length}))`);
    report.dom = await evaluate(`document.querySelector('#review-plan').innerText`);
    report.trash = await evaluate(`[...document.querySelectorAll('#review-plan .confirmation-file-row.trash')].map(x=>({class:x.className,color:getComputedStyle(x).backgroundColor}))`);
    await evaluate(`document.querySelector('#review-plan .confirmation-file-row.trash button')?.click()`);
    report.clock1 = await evaluate(`document.querySelector('.activity-clock').textContent`);
    await new Promise(r=>setTimeout(r,2200));
    report.clock2 = await evaluate(`document.querySelector('.activity-clock').textContent`);
    report.preview = await waitFor(`document.querySelector('#review-preview iframe')?.getAttribute('src')`,15);
    await screenshot('confirmation-preview');
    if (report.clock1 !== report.clock2) throw Error('Agent clock advanced while waiting for user');
    if (!report.groups.length || !report.groups.every(g=>g.keep===1 && g.trash>=1)) throw Error('Duplicate families are not mixed keep/trash groups');
    if (!report.trash.length || !report.trash.every(x=>{const c=x.color.match(/[0-9]+/g).map(Number);return c[0]>c[1] && c[0]>c[2];})) throw Error('Pending trash lacks light red distinction');
  } else if (mode === 'reload') {
    await call('Page.reload');
    await waitFor(`typeof currentConversation !== 'undefined' && currentConversation === ${JSON.stringify(arg)} && !document.querySelector('#send-button').disabled`,30);
    report.cid = await evaluate('currentConversation');
    report.text = await evaluate(`document.querySelector('#messages').innerText`);
    report.messages = await evaluate(`document.querySelectorAll('.message').length`);
    await screenshot('long-conversation-reloaded');
    if (!report.text.includes('验收标记：松柏')) throw Error('Missing retained constraint marker');
  }
  report.ok = true;
} catch (e) {report.ok = false; report.error = String(e); await screenshot(`${mode}-failure`).catch(()=>{});}
fs.writeFileSync(`${out}/browser-${mode}.json`,JSON.stringify(report,null,2));
console.log(JSON.stringify(report));
ws.close();
process.exitCode = report.ok ? 0 : 1;
