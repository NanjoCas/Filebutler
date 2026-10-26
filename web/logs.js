// The log page: every turn, step by step (data from /api/logs, web_logs.py).
// The server sends facts and codes; words come from labels.js, shared with the chat page.
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const fmtS = s => s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}分${Math.round(s % 60)}秒` : `${(+s).toFixed(1)}秒`;
const fmtMs = ms => ms == null ? "" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
const fmtK = n => n == null ? "—" : n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n.toLocaleString();
const ICONS = {look: "🔍", write: "📂", ask: "💬", task: "📋"};
const HANDLE_KINDS = {F: "文件", E: "证据片段", C: "目录快照", P: "分类计划", X: "文件计划", T: "任务"};
const STATE_TONE = {completed: "ok", partial: "warn", needs_review: "warn", waiting_input: "wait", cancelled: "wait", interrupted: "bad"};
const ANSWERS = {y: "允许", a: "本轮全部允许", n: "拒绝"};
const PART_KINDS = {organize: "整理", find: "查找", answer: "回答", classify: "分类", dedup: "查重"};
const CLAIMS = {done: "说已完成", unfinished: "说没做到", not_needed: "说无需操作"};
const VERDICTS = {pass: "核验通过", fail: "核验未通过", review: "需要你复核"};
// stop reasons where the run gave up (agent/states.py RETRY, plus answers it could not prove)
const GAVE_UP = new Set(["cost_budget", "token_budget", "max_steps", "repeated_call", "repair_limit", "exception", "unverified_answer", "unstructured_terminal"]);
const toolLabel = name => TOOL_LABELS[name] || name;
const opLabel = op => EFFECT_NAMES[op === "make_dir" ? "create" : op] || op;
const taskStateLabel = s => TASK_STATES[s] || s || "—";
const api = async url => { const r = await fetch(url); const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(data.detail || `请求失败 (${r.status})`); return data; };

// The verdict uses the chat page's own words (labels.js outcomeLabel); colour is this page's.
function outcome(t) {
  if (!t.ended) return {label: "没有结束记录（可能中途崩溃）", tone: "bad"};
  const label = outcomeLabel({stop_reason: t.stop_reason, report: {display: t.display}});
  const d = t.display, s = t.stop_reason;
  const tone = d ? ({done: "ok", answered: "ok", chosen: "ok", confirm: "warn", waiting: "wait", cancelled: "wait"})[d] || (GAVE_UP.has(s) ? "bad" : "warn")
    : GAVE_UP.has(s) ? "bad" : ["partial", "needs_review"].includes(s) ? "warn" : ["waiting_input", "cancelled", "paused"].includes(s) ? "wait" : "ok";
  return {label, tone};
}
const dotTone = t => { const o = outcome(t).tone; return o !== "ok" ? o : t.issues ? "warn" : "ok"; };

function anomalyText(x) {
  switch (x.code) {
    case "slow_request": return `第 ${x.step} 步模型请求用了 ${(x.ms / 1000).toFixed(1)} 秒，慢于 95% 的历史步骤`;
    case "request_failed": return `第 ${x.step} 步模型请求失败（${x.error}），${x.retry ? "已自动重试" : "未重试"}`;
    case "call_failed": return `第 ${x.step} 步 ${x.tool}（${toolLabel(x.tool)}）失败：${x.text}`;
    case "repeat": return `第 ${x.step} 步 ${x.tool} 与之前的调用重复（第 ${x.count} 次）`;
    case "violations": return `触发了 ${x.count} 次安全规则`;
    case "rejections": return `有 ${x.count} 个操作被拒绝`;
  }
  return JSON.stringify(x);
}

// --- task forms: the server sends the fields as written and stored ---
const paths = xs => (xs || []).map(x => x === "." || x === "" ? "全部文件" : `「${x}」`).join("、");
const effects = xs => (xs || []).map(e => EFFECT_NAMES[e] || e).join("、");
function conditionText(c) {
  let text = c.kind === "tool_success" && c.tool ? `${toolLabel(c.tool)}成功` : CONDITION_NAMES[c.kind] || c.kind;
  if (c.path) text += " " + c.path;
  if (c.destination) text += " → " + c.destination;
  if (c.placement) text += `（${c.placement}）`;
  return text;
}
function limitLines(limits) {
  return Object.entries(limits || {}).map(([k, v]) => k === "only_within" ? `只在 ${paths(v)} 内操作` : k === "no_effects" ? `不允许：${effects(v)}`
    : k === "only_extensions" ? `只处理 ${v.map(x => "." + x).join("、")} 文件` : `${k}：${JSON.stringify(v)}`);
}
function ruleLine(r) {
  const who = {source: "原文件", destination: "目标位置"}[r.selector] || "操作对象", vals = r.values || [];
  const text = {within: `${who}必须在 ${paths(vals)} 内`, outside: `${who}不能在 ${paths(vals)} 内`, extensions: `${who}只能是 ${vals.map(v => "." + v).join("、")} 文件`,
    content_unchanged: "文件内容不能变", same_extension: "扩展名不能变", empty_directory: "只能是空文件夹"}[r.predicate] || `${r.predicate} ${JSON.stringify(vals)}`;
  return `${effects(r.effects)}时：${text}`;
}

let state = {index: null, group: null, conv: null, turn: null, detail: null, limits: null};
const details = new Map();

function readHash() { const p = new URLSearchParams(location.hash.slice(1)); return {c: p.get("c"), r: p.has("r"), t: +p.get("t") || null}; }
function writeHash() { history.replaceState(null, "", state.group === "runs" ? `#r=1&t=${state.turn}` : `#c=${state.group}&t=${state.turn}`); }

function renderConvs() {
  const nav = $("convs"), idx = state.index; nav.replaceChildren(el("div", "section-label", `对话 · ${idx.conversations.length}`));
  const entry = (id, title, sub, turns) => {
    const b = el("button", "lv-conv" + (id === state.group ? " active" : "")); b.type = "button"; b.append(el("strong", null, title));
    const bad = turns.filter(t => t.issues || outcome(t).tone === "bad").length;
    const meta = el("div", "meta"); meta.append(el("span", null, sub)); if (bad) meta.append(el("span", "pill warn", `⚠ ${bad} 轮有异常`));
    const dots = el("div", "dots"); for (const t of turns) { const d = el("i", "dot " + dotTone(t)); d.title = `第 ${t.index} 轮：${outcome(t).label}${t.issues ? ` · ${t.issues} 处异常` : ""}`; dots.append(d); }
    b.append(meta, dots); b.onclick = () => selectGroup(id, 1); return b;
  };
  for (const c of idx.conversations) nav.append(entry(c.id, c.title, `${c.turns.length} 轮 · ${c.updated_at.slice(5)}`, c.turns));
  if (idx.runs.length) { nav.append(el("div", "section-label", "不在网页对话里的运行")); nav.append(entry("runs", `评测与命令行运行 · ${idx.runs.length} 次`, "没有对话历史，只有运行记录", idx.runs)); }
}

function renderChat() {
  const c = state.conv, box = $("chat"); box.replaceChildren();
  $("chat-title").textContent = c.title; $("chat-sub").textContent = `${c.turns.length} 轮 · 点击任意一轮查看右侧日志`;
  $("back").disabled = state.group === "runs";
  let block = null;
  for (const m of c.messages) {
    if (m.role === "event") { const note = el("div", "event-note", "↺ " + m.content.split("\n")[0].replace(/[:：]\s*$/, "")); note.title = m.content; box.append(note); block = null; continue; }
    if (m.role === "user") {
      const t = c.turns[m.turn - 1];
      block = el("div", "turn-block" + (m.turn === state.turn ? " active" : "")); block.dataset.turn = m.turn;
      const tag = el("div", "turn-tag"); tag.append(el("i", "dot " + dotTone(t)), el("span", null, `第 ${m.turn} 轮 · ${outcome(t).label} · ${fmtS(t.seconds)}${t.issues ? ` · ${t.issues} 处异常` : ""}`));
      block.append(tag);
      const turnNo = m.turn; block.onclick = () => selectTurn(turnNo);
      box.append(block);
    }
    const msg = el("div", "message " + m.role);
    msg.append(el("div", "message-label", m.role === "user" ? "你" : "FileButler"));
    const body = el("div", "message-body" + (m.html ? " markdown-body" : "")); if (m.html) body.innerHTML = m.html; else body.textContent = m.content;
    msg.append(body); (block || box).append(msg);
  }
}

function card(title) { const c = el("div", "card"); if (title) c.append(el("h3", null, title)); return c; }

const show = v => typeof v === "string" ? v : JSON.stringify(v, null, 2);
function fold(title, content, open) {
  const d = el("details", "fold"); if (open) d.open = true; d.append(el("summary", null, title));
  d.append(content instanceof Node ? content : el("pre", null, content)); return d;
}
function kvList(pairs) {
  const kv = el("dl", "kv");
  for (const [k, v] of pairs) if (v != null && v !== "" && !(Array.isArray(v) && !v.length)) kv.append(el("dt", null, k), el("dd", null, typeof v === "string" ? v : JSON.stringify(v)));
  return kv;
}

// arguments as the model asked for them, next to what actually ran when the runtime changed them
function argsNode(c) {
  const actual = c.args && typeof c.args === "object" ? c.args : null;
  if (!actual) return el("pre", null, show(c.args));
  const asked = c.requested && typeof c.requested === "object" ? c.requested : null;
  const tb = el("table", "mini-table args");
  tb.innerHTML = asked ? "<tr><th>参数</th><th>模型请求</th><th>实际执行</th></tr>" : "<tr><th>参数</th><th>值</th></tr>";
  const keys = [...new Set([...Object.keys(asked || {}), ...Object.keys(actual)])];
  if (!keys.length) { const tr = el("tr"); const td = el("td", null, "（没有参数）"); td.colSpan = 3; tr.append(td); tb.append(tr); }
  for (const k of keys) {
    const tr = el("tr"); tr.append(el("td", null, k));
    if (asked) { const a = show(asked[k] ?? "—"), b = show(actual[k] ?? "—"); tr.append(el("td", null, a), el("td", null, b)); if (a !== b) tr.className = "diff"; }
    else tr.append(el("td", null, show(actual[k])));
    tb.append(tr);
  }
  return tb;
}

function callNode(c, maxMs, t) {
  const d = el("details", "item call" + (c.ok === false ? " fail" : "")); d.id = `call-${t.index}-${c.i}`;
  const s = el("summary"); const what = el("div", "what"); const right = el("div", "right"); const tags = el("div", "tags");
  const line = el("span"); line.append(el("code", "tool", c.tool), document.createTextNode(" " + (c.sentence || toolLabel(c.tool)))); what.append(line);
  if (c.outcome) what.append(el("small", null, (c.ok === false ? "失败原因：" : "→ ") + c.outcome));
  tags.append(el("span", "pill " + (c.ok === false ? "bad" : "ok"), c.ok === false ? "失败" : "成功"));
  if (c.executed === false) tags.append(el("span", "pill warn", "未执行"));
  if (c.requested) tags.append(el("span", "pill warn", "参数被改写"));
  if (c.repeat >= 2) tags.append(el("span", "pill warn", `重复 ${c.repeat} 次`));
  if (c.slow) tags.append(el("span", "pill slow", "较慢"));
  if (c.uses.length) tags.append(el("span", "pill link", `引用 ${c.uses.length} 个句柄`));
  right.append(tags, el("span", "num", fmtMs(c.ms)));
  const bar = el("div", "msbar tool"); const fill = el("i"); fill.style.width = Math.max(2, 100 * (c.ms || 0) / maxMs) + "%"; bar.append(fill); right.append(bar);
  s.append(el("span", "ico", c.ok === false ? "⚠️" : ICONS[c.group] || ICONS.task), what, right); d.append(s);

  const body = el("div", "call-body");
  body.append(el("h5", null, c.requested ? "① 参数：模型请求的和实际执行的不一样（标黄的行）" : "① 模型请求的参数"), argsNode(c));
  const A = c.args || {};
  if (c.tool === "ask_user" && A.options) {
    const tb = el("table", "mini-table"); tb.innerHTML = "<tr><th>选项</th><th>说明</th></tr>";
    for (const o of A.options) { const tr = el("tr"); tr.append(el("td", null, o.label), el("td", null, o.detail || "")); tb.append(tr); }
    body.append(tb);
  } else if (c.tool === "prepare_file_plan" && A.actions) {
    const tb = el("table", "mini-table"); tb.innerHTML = `<tr><th>操作</th><th>原位置</th><th>目标位置</th></tr>`;
    for (const x of A.actions.slice(0, 40)) { const tr = el("tr"); tr.append(el("td", null, EFFECT_NAMES[x.effect] || x.effect), el("td", null, x.source || "—"), el("td", null, x.destination || "—")); tb.append(tr); }
    if (A.actions.length > 40) { const tr = el("tr"); const td = el("td", null, `… 还有 ${A.actions.length - 40} 项，完整列表见上面的参数`); td.colSpan = 3; tr.append(td); tb.append(tr); }
    body.append(tb);
  }
  const hn = handlesNode(c, t);
  if (hn) body.append(el("h5", null, "句柄：这次调用引用了哪些之前拿到的 ID，又产生了哪些新 ID（点击跳到出处）"), hn);
  body.append(el("h5", null, "② 运行时怎么处理"), kvList([
    ["工具", `${c.tool}（${toolLabel(c.tool)}）`],
    ["是否执行", c.executed === false ? "否：被运行时拦下或因重复保护没有执行" : c.executed ? "是" : null],
    ["耗时", c.ms != null ? `${fmtMs(c.ms)}` + (c.wait_ms ? `，其中等你确认 ${fmtMs(c.wait_ms)}` : "") : null],
    ["相同调用第几次", c.repeat > 1 ? String(c.repeat) : null],
    ["错误码", c.error_code], ["错误类别", c.error_class],
    ["引用的对象", c.references], ["文件操作数", c.operation_refs ? String(c.operation_refs) : null], ["进度", c.progress],
    ["result_id", c.result_id], ["调用 id", c.call_id],
  ]));
  body.append(el("h5", null, c.result_source === "history"
    ? `③ 模型收到的结果（全文 ${c.result.length} 字，来自对话历史）`
    : `③ 结果（运行记录只存了前 300 字${c.truncated ? "，这里不是全文" : ""}）`), el("pre", null, c.result || "（无输出）"));
  if (c.envelope && Object.keys(c.envelope).length) body.append(kvList(Object.entries(c.envelope).map(([k, v]) => [`结果字段 ${k}`, v])));
  if (c.raw_result) body.append(fold("模型实际收到的原始消息（JSON，原样）", c.raw_result));
  d.append(body);
  return d;
}

function eventNode(x) {
  if (x.kind === "ops") {
    const w = el("div", "inline-ops"); w.append(el("div", "hint", `📁 改动了 ${x.ops.length} 个文件` + (x.inferred ? `（${x.inferred}；没有时间记录，按顺序推断）` : ""))); w.append(opsNode(x.ops)); return w;
  }
  const d = el("details", "item " + x.kind); const s = el("summary"); const what = el("div", "what"); const right = el("div", "right"); const tags = el("div", "tags");
  let ico = "•", detail = null;
  if (x.kind === "confirm") {
    ico = "✋"; what.append(el("span", null, "请你确认操作"), el("small", null, x.question.split("\n").find(l => l.trim()) || ""));
    tags.append(el("span", "pill " + (x.answer === "n" ? "bad" : x.answer ? "ok" : "wait"), x.answer ? `你选了「${ANSWERS[x.answer] || x.answer}」` : "等待中"));
    right.append(tags, el("span", "num", x.wait_s != null ? `等了 ${fmtS(x.wait_s)}` : "")); detail = x.question;
  } else if (x.kind === "state") {
    ico = "📌"; what.append(el("span", null, `任务状态变为「${taskStateLabel(x.status)}」`), el("small", null, `第 ${x.revision} 版 · ${x.task_id} · ${x.status}` + (x.blocked ? ` · 已拦截 ${x.blocked} 次越界调用` : "")));
    tags.append(el("span", "pill " + (STATE_TONE[x.status] || "gray"), taskStateLabel(x.status))); right.append(tags, el("span", "num", x.at));
  } else if (x.kind === "context") {
    ico = "🗜"; what.append(el("span", null, x.archived ? `整理上下文：归档了 ${x.archived} 条早期消息` : "整理上下文：之前的对话全部带上，没有归档"),
      el("small", null, `历史 ${fmtK(x.before)} → ${fmtK(x.after)} 字`)); detail = show(x.detail);
  } else if (x.kind === "local") {
    ico = "⚙️"; what.append(el("span", null, x.message), el("small", null, "本地步骤，不经过模型"));
  } else if (x.kind === "protocol") {
    ico = "↩"; what.append(el("span", null, "运行时要求模型重新收尾"), el("small", null, `原因：${x.reason}（模型直接输出了文字，没有用收尾工具；这算一步）`));
  } else if (x.kind === "failed") {
    ico = "✕"; what.append(el("span", null, "运行出错")); detail = show(x.detail);
  } else if (x.kind === "injected") {
    ico = x.source === "undo" ? "↺" : "📨";
    what.append(el("span", null, (x.source === "undo" ? "撤销记录写入对话：" : "系统消息写入对话：") + x.title),
      el("small", null, `${x.chars} 字 · 写进对话历史后，之后每次请求都会带给模型`)); detail = x.content;
  } else {
    ico = "？"; what.append(el("span", null, `页面不认识的事件：${x.type}`)); detail = show(x.detail);
  }
  s.append(el("span", "ico", ico), what, right); d.append(s);
  if (detail) d.append(el("pre", null, detail));
  return d;
}

function requestsNode(s) {
  if (!s.requests.length) return el("div", "hint", "这次运行的记录里没有请求明细（较早的版本只记了耗时和 token）。");
  const tb = el("table", "mini-table"); tb.innerHTML = "<tr><th>第几次</th><th>发出消息</th><th>输入字数</th><th>输入 token</th><th>其中缓存命中</th><th>输出 token</th><th>耗时</th><th>结果</th></tr>";
  for (const a of s.requests) { const tr = el("tr");
    for (const v of [a.attempt, a.messages, fmtK(a.input_chars), fmtK(a.prompt_tokens), fmtK(a.cache_hit_tokens), fmtK(a.completion_tokens), fmtMs(a.ms),
      a.ok === false ? `失败 ${a.error_type || ""}${a.retry ? "，重试" : ""}` : a.ok ? "成功" : "—"]) tr.append(el("td", null, v == null ? "—" : String(v)));
    tb.append(tr); }
  return tb;
}

function stepNode(s, maxMs, t) {
  const step = el("div", "step"); step.append(el("span", "step-num", s.step));
  const sh = el("div", "step-head"); const th = el("div", "think");
  th.append(el("span", null, `🧠 第 ${s.step} 步 · 请求模型`));
  const mb = el("div", "msbar"); const f = el("i"); f.style.width = Math.max(2, 100 * (s.ms || 0) / maxMs) + "%"; mb.append(f); th.append(mb);
  sh.append(th); if (s.slow) sh.append(el("span", "pill slow", "较慢"));
  if (s.requests.length > 1) sh.append(el("span", "pill warn", `请求了 ${s.requests.length} 次`));
  sh.append(el("span", "num", fmtMs(s.ms) + (s.tokens ? ` · ${fmtK(s.tokens)} tok` : ""))); step.append(sh);

  const out = el("div", "model-out");
  out.append(fold("发给模型的请求：消息条数、字数和 token", requestsNode(s)));
  if (s.has_history) {
    if (s.reasoning) out.append(fold(`模型的思考内容 · ${s.reasoning.length} 字`, s.reasoning));
    else out.append(el("div", "hint", "这一步没有思考内容（模型没返回 reasoning_content）。"));
    if (s.text) { out.append(el("h5", null, "模型输出的文字")); out.append(el("div", "said", s.text)); }
    out.append(el("div", "decided", s.decided.length
      ? `→ 模型在这一次回复里决定调用 ${s.decided.length} 个工具：${s.decided.join("、")}`
      : "→ 模型没有调用工具，这一步直接给出文字"));
  } else {
    out.append(el("div", "hint", `没有对话历史，看不到模型这一步的思考和输出${s.items.length ? `；只知道它调用了 ${s.items.length} 个工具` : ""}。`));
  }
  step.append(out);

  const items = el("div", "items");
  for (const c of s.items) {
    items.append(callNode(c, maxMs, t));
    const inner = el("div", "inner");
    if (c.form) inner.append(formBox(c.form));
    if (c.finish) { const w = el("div", "inline-form"); w.append(finishNode(c.finish)); inner.append(w); }
    for (const x of c.inner) inner.append(eventNode(x));
    if (inner.childNodes.length) items.append(inner);
  }
  if (s.after.length) { items.append(el("div", "hint", "工具结果回到模型之后、下一次请求之前：")); for (const x of s.after) items.append(eventNode(x)); }
  if (items.childNodes.length) step.append(items);
  return step;
}

function formBox(f) {
  const w = el("div", "inline-form");
  w.append(el("div", "hint", "📝 任务登记表：上半部分是模型填的，下半部分是系统校验后正式登记的；之后模型申报完成时，系统按这里的完成条件逐项核验。"), formNode(f));
  return w;
}

function prepNode(t, conv) {
  const step = el("div", "step"); step.append(el("span", "step-num", "0"));
  const sh = el("div", "step-head"); sh.append(el("span", null, "准备阶段 · 第一次请求模型之前")); step.append(sh);
  const items = el("div", "items");
  const r = t.prep.request || {};
  items.append(el("div", "hint", r.messages != null
    ? `第一次请求一共发出 ${r.messages} 条消息（按 JSON 计 ${fmtK(r.input_chars)} 字，不含同时发出的工具定义）：对话开头的系统提示、之前各轮的全部消息、这一轮你的输入，加上运行时附的任务快照。`
    : "这次运行的记录里没有请求明细。"));
  if (conv.system_prompt) items.append(fold(`对话开头的系统提示 · ${conv.system_prompt.length} 字（这个对话每次请求都带着它，保存的是对话创建时的版本）`, conv.system_prompt));
  for (const x of t.prep.items) items.append(eventNode(x));
  for (const x of t.prep.system) items.append(eventNode(x));
  items.append(el("div", "hint", "运行时每轮还会附上一条任务快照（runtime.guidance），它只在本轮请求里出现，不存进对话历史，所以这里看不到原文。"));
  step.append(items);
  return step;
}

function formNode(f) {
  const box = el("div", "form"); const M = f.model || {};
  const head = el("div", "form-head"); const ht = el("span"); ht.append(el("b", null, `第 ${f.step} 步 · ${toolLabel(f.tool)}`), document.createTextNode(`（${f.tool}）`));
  head.append(ht, el("span", "pill " + (f.ok === false ? "bad" : "ok"), f.task_id ? `${f.ok === false ? "未通过" : "已登记"} · 第 ${f.revision} 版` : f.ok === false ? "未通过" : "已提交"));
  box.append(head);
  // model side, as written
  const m = el("div", "form-sec"); m.append(el("h4", null, "🧠 模型填写的内容"));
  const fields = M.raw != null ? [["原始参数", M.raw]] : [["任务摘要", M.summary], ["修改原因", M.reason], ["基于版本", M.expected_revision && `第 ${M.expected_revision} 版`]];
  for (const [k, v] of fields) if (v) { const r = el("div", "frow"); r.append(el("span", null, k), el("span", null, v)); m.append(r); }
  const parts = [...(M.parts || []).map(p => ({id: p.id, kind: PART_KINDS[p.kind] || p.kind || "—", description: p.description || "", scope: paths(p.scope)})),
                 ...(M.goals || []).map(g => ({id: g.id, kind: "目标", description: g.description || "", scope: (g.conditions || []).map(conditionText).join("；")}))];
  if (parts.length) {
    const r = el("div", "frow"); r.append(el("span", null, `拆成 ${parts.length} 部分`)); const col = el("div");
    for (const p of parts) { const d = el("div", "part"); const top = el("div", "part-top"); top.append(el("span", "pill gray", p.kind)); if (p.id) top.append(el("code", null, p.id));
      d.append(top, el("div", null, p.description)); if (p.scope) d.append(el("small", null, "范围：" + p.scope)); col.append(d); }
    r.append(col); m.append(r);
  }
  const lims = [...limitLines(M.limits && typeof M.limits === "object" && !Array.isArray(M.limits) ? M.limits : {}),
                ...(M.write_roots ? [`写入范围：${paths(M.write_roots)}`] : []), ...(M.allowed_effects ? [`允许的操作：${effects(M.allowed_effects)}`] : []),
                ...(M.constraints || []).map(c => c.description || c.id)];
  const lr = el("div", "frow"); lr.append(el("span", null, "限制"));
  if (lims.length) { const ul = el("ul", "rules"); ul.style.margin = "0"; for (const x of lims) ul.append(el("li", null, x)); lr.append(ul); } else lr.append(el("span", null, "没写限制"));
  m.append(lr); box.append(m);
  // the runtime's side, as stored
  const g = f.registered;
  box.append(el("div", "arrow-row", g ? "↓ 系统校验后登记为" : f.ok === false ? "↓ 系统没有接受这次登记" : "↓ 没有找到对应的登记记录"));
  if (g) {
    const sys = el("div", "form-sec sys"); const h4 = el("h4", null, "📋 系统登记的任务 "); h4.append(el("i", null, `${f.task_id} · 第 ${f.revision} 版`)); sys.append(h4);
    const gr = el("div", "frow"); gr.append(el("span", null, `${g.goals.length} 个目标`)); const col = el("div");
    for (const x of g.goals) { const d = el("div", "part"); const top = el("div", "part-top"); top.append(el("code", null, x.id)); d.append(top);
      d.append(el("div", null, x.description)); const chips = el("div", "chips");
      for (const c of x.conditions) chips.append(el("span", "chip", "完成条件：" + conditionText(c)));
      if (x.semantic_review) chips.append(el("span", "chip review", "结果需语义复核"));
      d.append(chips); col.append(d); }
    gr.append(col); sys.append(gr);
    const write = g.write_roots;
    for (const [k, v] of [["可以读取", paths(g.read_roots) || "—"],
                          ["可以写入", write.length ? paths(write.slice(0, 6)) + (write.length > 6 ? ` 等 ${write.length} 处` : "") : "不写入任何文件"],
                          ["允许操作", effects(g.allowed_effects) || "无（只读）"]]) { const r = el("div", "frow"); r.append(el("span", null, k), el("span", null, v)); sys.append(r); }
    if (g.constraints.length) { const r = el("div", "frow"); r.append(el("span", null, "约束")); const col2 = el("div");
      for (const c of g.constraints) { col2.append(el("div", null, c.description)); if (c.forbidden_effects.length) col2.append(el("div", null, "禁止：" + effects(c.forbidden_effects)));
        const ul = el("ul", "rules"); for (const x of c.rules) ul.append(el("li", null, ruleLine(x))); col2.append(ul); }
      r.append(col2); sys.append(r); }
    box.append(sys);
  }
  box.append(el("div", "receipt", "工具回执：" + (f.receipt || "—")));
  return box;
}


function opsNode(list) {
  const ops = el("div", "ops");
  for (const o of list) { const r = el("div", "op"); r.append(el("span", "pill " + (o.undone ? "gray" : o.op === "trash" ? "warn" : "ok"), opLabel(o.op) + (o.undone ? "（已撤销）" : "")));
    const p = el("div", "path"); p.append(document.createTextNode(o.src || "")); if (o.dst && o.op !== "make_dir") { p.append(el("em", null, "  →  " + o.dst)); }
    r.append(p, el("span", "num", o.time)); ops.append(r); }
  return ops;
}

// the model's finish declaration, filed at the step it was made
function finishNode(sf) {
  const sec = el("div", "form"); const head = el("div", "form-head"); const ht = el("span"); ht.append(el("b", null, `第 ${sf.step} 步 · 模型申报完成情况`), document.createTextNode(`（${sf.tool}）`));
  head.append(ht, el("span", "pill " + (sf.ok === false ? "bad" : "ok"), sf.ok === false ? "申报被拒" : "已提交")); sec.append(head);
  const m = el("div", "form-sec"); m.append(el("h4", null, "🧠 模型填写的内容"));
  for (const [kk, vv] of [["给你的话", sf.reply || "（留空）"], ["总结", sf.summary || "（留空）"]]) { const r = el("div", "frow"); r.append(el("span", null, kk), el("span", null, vv)); m.append(r); }
  sec.append(m);
  sec.append(el("div", "arrow-row", "↓ 系统按登记的完成条件逐个目标核验"));
  const sys = el("div", "form-sec sys"); sys.append(el("h4", null, `📋 逐个目标对照（${sf.rows.length} 个）`));
  for (const r of sf.rows) sys.append(goalRow(r));
  if (!sf.rows.length) sys.append(el("div", "hint", "没有核验记录。"));
  sec.append(sys, el("div", "receipt", "工具回执：" + (sf.receipt || "—")));
  return sec;
}

// --- handles: where an ID first appeared, and jumping there ---
function jumpTo(from) {
  const go = () => { const n = document.getElementById(`call-${from.turn}-${from.i}`); if (!n) return;
    n.open = true; n.scrollIntoView({behavior: "smooth", block: "center"}); n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); };
  if (state.group !== "runs" && from.turn !== state.turn) selectTurn(from.turn).then(() => setTimeout(go, 30)).catch(failed); else go();
}
function handleChip(u, here) {
  const chip = el("span", "handle" + (u.from ? " linked" : ""));
  chip.append(el("code", null, u.id), el("small", null, HANDLE_KINDS[u.kind] || u.kind));
  if (u.from) {
    const where = u.from.turn === here ? `第 ${u.from.step} 步` : `第 ${u.from.turn} 轮第 ${u.from.step} 步`;
    chip.append(el("small", null, `← ${where} ${u.from.tool}`)); chip.title = `${where}：${u.from.sentence}（点击跳转）`;
    chip.onclick = e => { e.preventDefault(); e.stopPropagation(); jumpTo(u.from); };
  } else chip.title = state.group === "runs" ? "出处不在记录里：这次运行的结果只存了前 300 字" : "没找到产生它的调用（可能是碰巧长得像句柄的文字）";
  return chip;
}
function handlesNode(c, t) {
  if (!c.produced.length && !c.uses.length) return null;
  const box = el("div", "handles");
  if (c.uses.length) { const r = el("div", "handle-row"); r.append(el("span", null, "引用了"));
    const list = el("div"); for (const u of c.uses) list.append(handleChip(u, t.index)); r.append(list); box.append(r); }
  if (c.produced.length) { const r = el("div", "handle-row"); r.append(el("span", null, "新产生"));
    const list = el("div"); for (const u of c.produced) { const chip = el("span", "handle new"); chip.append(el("code", null, u.id), el("small", null, HANDLE_KINDS[u.kind] || u.kind)); list.append(chip); }
    r.append(list); box.append(r); }
  return box;
}

// --- states and ending, from the product's reducer (agent/states.py) ---
const ST = {
  flow: {active: "进行中", waiting_input: "等你回答", ended: "已结束", interrupted: "被中断", cancelled: "已取消"},
  result: {answered: "已回答（没有要交付的改动或结果）", fulfilled: "完成了要求", partial: "只完成了一部分", no_result: "没有结果"},
  mechanical: {not_applicable: "不适用：没有可由本地逐项核对的条件", pending: "待核验", passed: "通过", failed: "未通过", unknown: "不确定（有执行中断的计划）"},
  semantic: {not_required: "不需要", unreviewed: "待你复核", accepted: "你已认可", rejected: "你已否定"},
  next: {none: "无", answer_question: "回答问题", confirm_result: "确认结果", recover: "恢复中断的计划", retry: "重试"},
  part: {done: "完成", review: "待复核", unfinished: "未完成", not_needed: "无需操作", by_choice: "按你的选择跳过"},
};
const TONE = {passed: "ok", failed: "bad", pending: "warn", unknown: "warn", unreviewed: "warn", rejected: "bad", accepted: "ok", not_applicable: "gray", not_required: "gray"};

function endingText(t) {
  const e = t.ending, st = t.states;
  if (st.registered) return `登记了任务，模型用 ${e ? e.tool + (e.mode ? `(mode=${e.mode})` : "") : "收尾工具"} 收尾，本地按登记的完成条件逐部分核验（见上面的「逐个目标对照」）。`;
  if (!e) return "没有登记任务，也没有收尾调用（较早版本的协议或运行中途停止），本地没有可核验的完成条件。";
  if (e.tool === "finish_turn" && e.mode === "reply" || e.tool === "inspect_run")
    return `没有登记任务：模型没有调用 start_task，用 ${e.tool}${e.mode ? `(mode=${e.mode})` : ""} 直接回答。按协议「只有用户要求执行时才 start_task」（agent/prompts.py），询问类请求不登记任务，所以本地没有完成条件可核验，回答内容没有经过本地核对。`;
  if (e.tool === "ask_user" || e.mode === "ask") return "没有登记任务：模型提出问题等你回答，本轮不写入，也不做核验。";
  return `没有登记任务：模型用 ${e.tool}${e.mode ? `(mode=${e.mode})` : ""} 结束本轮，本地没有可核验的完成条件。`;
}

function statesNode(t) {
  const st = t.states, box = el("div", "states");
  box.append(el("div", "ending", endingText(t)));
  if (!st.registered && t.evidence_calls && !t.findings)
    box.append(el("div", "alert warn", `⚠  这一轮有 ${t.evidence_calls} 次读取拿到了证据片段（evidence:E…），但没有调用 report_findings 把结论和证据绑定，所以本地不会检查回答里的结论是否有证据支持。`));
  const tb = el("table", "mini-table states-table");
  const row = (k, v, raw, tone) => { const tr = el("tr"); const td = el("td"); td.append(el("span", "pill " + (tone || "gray"), v)); tr.append(el("td", null, k), td, el("td", null, raw)); tb.append(tr); };
  row("登记了任务", st.registered ? "是" : "否", `registered = ${st.registered}`, st.registered ? "ok" : "gray");
  row("流程", ST.flow[st.flow_state] || st.flow_state, `flow_state = ${st.flow_state}`);
  row("结果", ST.result[st.result_state] || st.result_state, `result_state = ${st.result_state}`, {fulfilled: "ok", answered: "ok", partial: "warn", no_result: "bad"}[st.result_state]);
  row("本地机械核验", ST.mechanical[st.verification.mechanical] || st.verification.mechanical, `mechanical = ${st.verification.mechanical}`, TONE[st.verification.mechanical]);
  row("语义复核", ST.semantic[st.verification.semantic] || st.verification.semantic, `semantic = ${st.verification.semantic}`, TONE[st.verification.semantic]);
  row("下一步", ST.next[st.next_action] || st.next_action, `next_action = ${st.next_action}`);
  box.append(tb);
  if (st.parts?.length) {
    const pt = el("table", "mini-table"); pt.innerHTML = "<tr><th>部分</th><th>结果</th><th>机械核验</th><th>语义复核</th></tr>";
    for (const p of st.parts) { const tr = el("tr");
      tr.append(el("td", null, p.id + (p.skipped_by_user ? "（你选择跳过）" : "")), el("td", null, ST.part[p.outcome] || p.outcome),
        el("td", null, ST.mechanical[p.mechanical]?.split("：")[0] || p.mechanical), el("td", null, ST.semantic[p.semantic] || p.semantic)); pt.append(tr); }
    box.append(pt);
  }
  box.append(el("div", "hint", t.states_source === "saved"
    ? "以上来自运行结束时保存的 report.states（agent/states.py 推导）。"
    : "这轮运行没有保存 report.states，以上由同一个推导函数（agent/states.py，经 eval/judge.py 的 turn_states）按保存的结果现算。"));
  return box;
}

// closing node of the timeline: the system's final verdict for this turn
function endNode(t) {
  const sf = t.status_form, box = el("div", "turn-end");
  const vr = el("div", "verdict-row"); vr.append(el("span", null, "🏁 本轮结束 · 系统最终判定"));
  const o = outcome(t); vr.append(el("span", "pill " + o.tone, o.label), el("span", "num", `stop_reason = ${t.stop_reason ?? "—"}`));
  if (sf?.task_id) vr.append(el("span", "num", `${sf.task_id} · 第 ${sf.revision} 版 · ${taskStateLabel(sf.state)}`));
  const mism = sf ? sf.rows.filter(r => !r.agree).length : 0;
  if (mism) vr.append(el("span", "pill warn", `${mism} 个目标模型和系统说法不一致`));
  box.append(vr, statesNode(t));
  if (sf && !sf.has_call && sf.rows.length) {
    box.append(el("div", "hint", `本轮模型没有申报完成，任务停在「${taskStateLabel(sf.state)}」；系统这边的核验：`));
    for (const r of sf.rows) box.append(goalRow(r, true));
  }
  return box;
}

// the report the chat page shows for this turn, generated locally at the end
function reportNode(t) {
  const r = t.report; if (!r) return null;
  const rh = t.report_html || {sections: {}};
  const d = el("details", "item report"); d.open = true;
  const s = el("summary"); const what = el("div", "what");
  what.append(el("span", null, "本轮报告：聊天页里你看到的回复"), el("small", null, `由本地根据任务、核验和证据生成 · report.state = ${r.state}${r.sections?.length ? ` · ${r.sections.length} 段` : ""}`));
  s.append(el("span", "ico", "📄"), what, el("div", "right")); d.append(s);
  const body = el("div", "report-body");
  const sum = el("div", "markdown-body"); if (rh.summary) sum.innerHTML = rh.summary; else sum.textContent = r.question || r.summary || "（空）"; body.append(sum);
  if (r.choices?.length) { const ul = el("ul", "rules"); for (const c of r.choices) ul.append(el("li", null, c.label)); body.append(ul); }
  for (const sec of r.sections || []) {
    const box = el("div", "report-sec"); const h = el("h5", null, sec.title);
    if (sec.audience === "dev") h.append(el("span", "pill gray", "开发者段落：聊天页默认隐藏"));
    box.append(h);
    const html = rh.sections?.[sec.title]; const content = el("div", "markdown-body"); if (html) content.innerHTML = html; else content.textContent = sec.markdown; box.append(content);
    if (sec.uses?.length) { const r2 = el("div", "handle-row"); r2.append(el("span", null, "引用了")); const list = el("div"); for (const u of sec.uses) list.append(handleChip(u, t.index)); r2.append(list); box.append(r2); }
    body.append(box);
  }
  d.append(body); return d;
}

function goalRow(r, systemOnly) {
  const box = el("div", "goal-row" + (r.agree || systemOnly ? "" : " mismatch"));
  const title = el("div", "goal-title"); const tt = el("span", null, r.description); title.append(tt);
  title.append(el("code", "num", r.id));
  if (!r.agree && !systemOnly) title.append(el("span", "pill warn", "说法不一致"));
  box.append(title);
  const cols = el("div", "goal-cols");
  if (!systemOnly) { const a = el("div"); a.append(el("h5", null, "🧠 模型申报"));
    a.append(el("span", "pill " + ({done: "ok", unfinished: "bad", not_needed: "wait"})[r.claim], CLAIMS[r.claim]));
    if (r.claim_reason) a.append(el("p", null, r.claim_reason)); cols.append(a); }
  const b = el("div"); b.append(el("h5", null, "📋 系统核验"));
  b.append(el("span", "pill " + ({pass: "ok", fail: "bad", review: "warn"})[r.verdict], ({pass: "✓ ", fail: "✕ ", review: "⚠ "})[r.verdict] + VERDICTS[r.verdict]));
  if (r.detail) b.append(el("p", null, r.detail)); cols.append(b);
  if (systemOnly) cols.style.gridTemplateColumns = "1fr";
  box.append(cols); return box;
}

function renderLog(t) {
  const c = state.conv, log = $("log"); log.replaceChildren();
  const head = el("div", "log-head"); const h = el("div");
  h.append(el("h2", null, `第 ${t.index} 轮 · ${t.instruction.length > 30 ? t.instruction.slice(0, 30) + "…" : t.instruction}`));
  h.append(el("p", null, `${t.created_at} · 模型 ${t.model || "—"} · 运行 ${t.run_id}`));
  const nav = el("div", "nav");
  for (const [txt, delta] of [["↑ 上一轮", -1], ["下一轮 ↓", 1]]) { const b = el("button", "icon-button", txt); b.type = "button"; b.disabled = !c.turns[state.turn - 1 + delta]; b.onclick = () => selectTurn(state.turn + delta).catch(failed); nav.append(b); }
  head.append(h, nav); log.append(head);
  const body = el("div", "log-body"); log.append(body);

  // overview: verdict, stats, time split
  const v = card(); const verdict = el("div", "verdict");
  const o = outcome(t);
  verdict.append(el("span", "big " + o.tone, ({ok: "✓ ", warn: "⚠ ", bad: "✕ ", wait: "… "})[o.tone] + o.label));
  verdict.append(el("span", "pill gray", `stop_reason = ${t.stop_reason ?? "—"}`));
  v.append(verdict);
  const st = t.stats, stats = el("div", "stats");
  for (const [b, s] of [[fmtS(st.seconds), "总耗时"], [st.steps ?? "—", "请求模型"], [st.tool_calls ?? "—", "工具调用"], [st.files_changed, "改动文件"],
                        [fmtK(st.tokens), "Token"], [st.cost != null ? `¥${st.cost.toFixed(4)}` : "—", "费用"]]) {
    const x = el("div", "stat"); x.append(el("b", null, b), el("span", null, s)); stats.append(x); }
  v.append(stats);
  const other = Math.max(0, st.seconds - st.llm - st.tool - st.wait);
  const segs = [["模型请求", st.llm, "--c-llm"], ["执行工具", st.tool, "--c-tool"], ["等你确认", st.wait, "--c-wait"], ["其他", other, "--c-other"]].filter(x => x[1] > 0.05);
  const total = segs.reduce((a, x) => a + x[1], 0) || 1;
  const bar = el("div", "timebar"), legend = el("div", "legend");
  for (const [name, sec, c2] of segs) { const i = el("i"); i.style.flex = String(sec / total); i.style.background = `var(${c2})`; i.title = `${name} ${fmtS(sec)}`; bar.append(i);
    const l = el("span", null, `${name} ${fmtS(sec)} · ${Math.round(100 * sec / total)}%`); l.style.setProperty("--c", `var(${c2})`); legend.append(l); }
  v.append(bar, legend); body.append(v);

  // what the records can and cannot show for this turn
  const rc = card("记录完整性 · 这一轮能看到什么"); const ra = el("div", "alerts");
  const shown = t.shown_calls;
  ra.append(el("div", "alert " + (shown === (st.tool_calls ?? shown) ? "none" : "bad"),
    `${shown === (st.tool_calls ?? shown) ? "✓" : "✕"}  运行一共调用工具 ${st.tool_calls ?? "—"} 次，下面显示了 ${shown} 次`));
  for (const x of t.completeness) ra.append(el("div", "alert " + x.level, ({bad: "✕", warn: "⚠", info: "ⓘ"})[x.level] + "  " + x.text));
  rc.append(ra); body.append(rc);

  const a = card("需要留意"); const alerts = el("div", "alerts");
  if (o.tone === "warn" || o.tone === "bad") alerts.append(el("div", "alert " + o.tone, ({bad: "✕", warn: "⚠"})[o.tone] + "  本轮结局：" + o.label));
  for (const x of t.anomalies) alerts.append(el("div", "alert " + x.level, ({bad: "✕", warn: "⚠", slow: "◷"})[x.level] + "  " + anomalyText(x)));
  if (!alerts.childNodes.length) alerts.append(el("div", "alert none", "✓ 这一轮没有失败、重复或异常慢的步骤"));
  a.append(alerts); body.append(a);

  // the chain, in order
  const tl = card("过程 · 每一步：发给模型什么 → 模型返回什么 → 运行时和工具做了什么（点开看全文）");
  const maxMs = Math.max(1, ...t.steps.map(s => s.ms || 0), ...t.steps.flatMap(s => s.items.map(i => i.ms || 0)));
  tl.append(prepNode(t, c));
  for (const s of t.steps) tl.append(stepNode(s, maxMs, t));
  if (t.runtime_final != null) {
    const d = el("details", "item injected"); const s = el("summary"); const what = el("div", "what");
    what.append(el("span", null, "写进对话历史的最终答复"), el("small", null, "由运行时根据收尾工具的结果写入，不是模型又请求了一次"));
    s.append(el("span", "ico", "📝"), what, el("div", "right")); d.append(s, el("pre", null, t.runtime_final || "（空）")); tl.append(d);
  }
  const rn = reportNode(t); if (rn) tl.append(rn);
  if (t.loose_forms.length) { tl.append(el("div", "hint", "以下登记表没能对应到过程中的某一步：")); for (const f of t.loose_forms) tl.append(formBox(f)); }
  tl.append(endNode(t));
  if (t.after_turn.length) { tl.append(el("div", "hint", "本轮结束之后写进对话历史的消息（下一轮会带给模型）：")); for (const x of t.after_turn) tl.append(eventNode(x)); }
  body.append(tl);

  const loose = t.file_ops.filter(o => !o.placed);
  if (loose.length) { const fo = card(`文件改动 · ${loose.length} 项没能对应到过程中的某一步`); fo.append(opsNode(loose)); body.append(fo); }

  const raw = el("details", "card raw"); raw.append(el("summary", null, `原始运行记录 · ${t.run_id}.trace.jsonl（${t.raw.length} 行）`));
  raw.append(el("pre", null, t.raw.map(x => JSON.stringify(x)).join("\n"))); body.append(raw);
  const L = state.limits;
  if (L?.llm_ms) body.append(el("div", "hint", `“较慢”= 慢于本机 95% 的历史同类步骤（请求模型 > ${(L.llm_ms / 1000).toFixed(1)}s，基于 ${L.llm_n} 次；工具 > ${(L.tool_ms / 1000).toFixed(1)}s，基于 ${L.tool_n} 次）。`));
  log.scrollTop = 0;
}


async function selectGroup(id, turn) {
  state.group = id; state.turn = null;
  if (id === "runs") {
    const runs = state.index.runs.map((r, i) => ({...r, index: i + 1}));
    state.conv = {id: "runs", title: `评测与命令行运行 · ${runs.length} 次`, turns: runs, system_prompt: "",
                  messages: runs.map(r => ({role: "user", content: r.instruction || "（没有记录指令）", turn: r.index}))};
  } else {
    $("log").replaceChildren(el("div", "empty-log", "正在读取…"));
    state.conv = await api(`/api/logs/conversations/${encodeURIComponent(id)}`);
    state.limits = state.conv.limits;
  }
  renderConvs(); renderChat();
  await selectTurn(turn);
}

async function selectTurn(turn) {
  const c = state.conv; state.turn = Math.min(Math.max(1, turn), c.turns.length || 1);
  document.querySelectorAll(".turn-block").forEach(b => b.classList.toggle("active", +b.dataset.turn === state.turn));
  document.querySelector(`.turn-block[data-turn="${state.turn}"]`)?.scrollIntoView({block: "nearest", behavior: "smooth"});
  writeHash();
  const summary = c.turns[state.turn - 1];
  if (!summary) { $("log").replaceChildren(el("div", "empty-log", "这个对话还没有运行记录")); return; }
  const key = state.group === "runs" ? `run:${summary.run_id}` : `${state.group}:${state.turn}`;
  if (!details.has(key)) {
    $("log").replaceChildren(el("div", "empty-log", "正在读取这一轮的记录…"));
    if (state.group === "runs") { const r = await api(`/api/logs/runs/${encodeURIComponent(summary.run_id)}`); state.limits = r.limits; details.set(key, r.turn); }
    else details.set(key, await api(`/api/logs/conversations/${encodeURIComponent(state.group)}/turns/${state.turn}`));
  }
  if (summary !== c.turns[state.turn - 1]) return;  // another turn was picked meanwhile
  renderLog(details.get(key));
}

const failed = err => $("log").replaceChildren(el("div", "empty-log", "读取失败：" + err.message));
$("back").onclick = () => { location.href = `/#c=${encodeURIComponent(state.group)}`; };
window.addEventListener("hashchange", () => {
  const h = readHash();
  if (h.c && h.c !== state.group) selectGroup(h.c, h.t || 1).catch(failed);
  else if (h.t && h.t !== state.turn) selectTurn(h.t).catch(failed);
});
(async () => {
  try {
    state.index = await api("/api/logs");
    const h = readHash();
    const first = h.r && state.index.runs.length ? "runs" : state.index.conversations.find(c => c.id === h.c)?.id || state.index.conversations[0]?.id || (state.index.runs.length ? "runs" : null);
    if (!first) { renderConvs(); $("log").replaceChildren(el("div", "empty-log", "还没有任何运行记录")); return; }
    await selectGroup(first, h.t || 1);
  } catch (err) { failed(err); }
})();
