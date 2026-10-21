const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "running", "waiting_confirmation"]);
let currentConversation = null, currentTurn = null, currentFilePath = ".";
let currentMessages = [], pollTimer = null, viewEpoch = 0, submitting = false, latestActivity = null;
let previewEpoch = 0, fileListEpoch = 0;
let activeReview = null;
const api = async (url, options = {}) => {
  const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `请求失败 (${response.status})`);
  return data;
};
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function setStatus(text, kind = "") {
  $("run-status").textContent = text; $("run-status").className = `status ${kind}`;
}
function scrollIfNearBottom() {
  const area = $("messages");
  if (area.scrollHeight - area.scrollTop - area.clientHeight < 180) area.scrollTop = area.scrollHeight;
}
function bindReferences(container) {
  container.querySelectorAll('.file-reference[data-file-name]').forEach(ref => {
    if (!ref.dataset.evidence) ref.textContent = ref.dataset.fileName;
    if (ref.dataset.fileStatus === 'trash') ref.title += '（已回收，可预览）';
  });
  container.addEventListener("click", (event) => {
    const ref = event.target.closest(".file-reference[data-file-path]");
    if (!ref || !container.contains(ref)) return;
    if (ref.dataset.fileKind === "directory") { openReferenceDirectory(ref.dataset.filePath); return; }
    preview({path: ref.dataset.filePath, name: ref.textContent.trim(), artifactId: ref.dataset.artifactId, conversationId: ref.dataset.conversationId}, Number(ref.dataset.filePage) || null, true);
  });
}
const messageKeys = new WeakMap();
let renderedConversation = null;
function messageKey(message) {
  const sections = (message.report?.sections || []).map(section => `${section.title}\u0001${section.html ?? section.markdown}`).join("\u0002");
  return `${message.role}\u0000${message.html ?? message.content}\u0000${sections}`;
}
function wrapTables(container) {
  container.querySelectorAll("table").forEach(table => { const wrap = node("div", "table-scroll"); table.replaceWith(wrap); wrap.append(table); });
}
function buildMessage(message) {
  const item = node("article", `message ${message.role}`);
  item.append(node("div", "message-label", message.role === "user" ? "你" : message.role === "event" ? "本地文件状态更新" : "✦ FileButler"));
  const body = node("div", `message-body${message.role === "assistant" ? " markdown-body" : ""}`);
  // html is produced only by the server's HTML-disabled Markdown renderer and verified file catalog.
  if (message.role === "assistant" && typeof message.html === "string") body.innerHTML = message.html;
  else body.textContent = message.content;
  wrapTables(body); bindReferences(body); item.append(body);
  if (message.report?.sections?.length) {
    const details=node('details','result-details');
    details.append(node('summary','','查看核验与过程详情'));
    for (const section of message.report.sections) {
      const part=node('section','result-detail-section'); part.append(node('h4','',section.title));
      const content=node('div','markdown-body');
      if (typeof section.html==='string') content.innerHTML=section.html; else content.textContent=section.markdown;
      wrapTables(content); bindReferences(content);part.append(content);details.append(part);
    }
    item.append(details);
  }
  if (message.role === "assistant") {
    const actions = node("div", "message-actions");
    const copy = node("button", "text-button", "复制回复"); copy.type = "button";
    copy.onclick = async () => { try { await navigator.clipboard.writeText(message.content); copy.textContent = "已复制"; setTimeout(() => copy.textContent = "复制回复", 1600); } catch { copy.textContent = "复制不可用"; } };
    actions.append(copy); item.append(actions);
  }
  messageKeys.set(item, messageKey(message));
  return item;
}
function renderWelcome(area) {
  const welcome = node("div", "welcome");
  welcome.innerHTML = '<div class="welcome-icon">✦</div><h2>你想整理什么？</h2><p>查找一篇论文，或把整个文件夹整理好。</p><div class="suggestions"><button type="button">帮我找出有关强化学习的论文</button><button type="button">列出文献目录里的 PDF</button><button type="button">查看当前文件夹里有哪些文件</button></div>';
  area.append(welcome);
  welcome.querySelectorAll("button").forEach(button => button.onclick = () => { $("message-input").value = button.textContent; sendMessage(); });
}
// Messages are append-only in practice: keep every unchanged <article> (and its open
// details/scroll state) and rebuild only from the first message that differs.
function renderMessages(messages, animateLast = false, conversationId = currentConversation) {
  const area = $("messages"), same = renderedConversation === conversationId;
  const nearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 180;
  currentMessages = messages; renderedConversation = conversationId; latestActivity = null;
  if (!same) area.replaceChildren();
  else for (const child of [...area.children]) if (!messageKeys.has(child)) child.remove();
  if (!messages.length) { area.replaceChildren(); renderWelcome(area); return; }
  const existing = [...area.children];
  let index = 0;
  while (index < messages.length && index < existing.length && messageKeys.get(existing[index]) === messageKey(messages[index])) index++;
  existing.slice(index).forEach(child => child.remove());
  let firstNew = null;
  for (let i = index; i < messages.length; i++) {
    const item = buildMessage(messages[i]);
    if (animateLast && i === messages.length - 1) item.classList.add("message-enter");
    firstNew ||= item; area.append(item);
  }
  if (!same) area.scrollTop = area.scrollHeight;
  else if (firstNew && nearBottom) {
    // Show the start of a long new reply rather than jumping past it to the end.
    const last = area.lastElementChild;
    if (animateLast && last.offsetHeight > area.clientHeight * 0.8) area.scrollTop += last.getBoundingClientRect().top - area.getBoundingClientRect().top - 12;
    else area.scrollTop = area.scrollHeight;
  }
}
function appendMessage(message) {
  const area = $("messages");
  area.querySelector(":scope > .welcome")?.remove();
  currentMessages = [...currentMessages, message];
  const item = buildMessage(message); item.classList.add("message-enter"); area.append(item);
  area.scrollTop = area.scrollHeight;
}
let conversationOrder = [];
// Sidebar titles show a fixed-length preview; the full title stays in the tooltip.
const TITLE_PREVIEW_CHARS = 16;
function previewTitle(title) {
  const chars = [...(title || "").replace(/\s+/g, " ").trim()];
  return chars.length > TITLE_PREVIEW_CHARS ? chars.slice(0, TITLE_PREVIEW_CHARS).join("") + "…" : chars.join("");
}
async function loadConversations() {
  const {items} = await api("/api/conversations");
  conversationOrder = items.map(conv => conv.id);
  const nav = $("conversation-list"); nav.replaceChildren();
  if (!items.length) nav.append(node("div", "conversation-empty", "还没有对话"));
  for (const conv of items) {
    const row = node("div", `conversation-row ${conv.id === currentConversation ? "active" : ""}`); row.dataset.conversationId = conv.id;
    const button = node("button", "conversation-item", previewTitle(conv.title)); button.type = "button";
    button.title = conv.title; if (conv.id === currentConversation) button.setAttribute("aria-current", "true");
    button.onclick = () => { closeSidebar(); if (conv.id !== currentConversation) openConversation(conv.id).catch(err => showError(err.message)); };
    const remove = node("button", "conversation-delete", "✕"); remove.type = "button";
    remove.title = "删除对话"; remove.setAttribute("aria-label", `删除对话「${conv.title}」`);
    remove.onclick = () => requestDeleteConversation(conv);
    row.append(button, remove); nav.append(row);
  }
  return items;
}
function requestDeleteConversation(conv) {
  const dialog = $("delete-dialog"), confirm = $("delete-confirm"), feedback = $("delete-feedback");
  $("delete-conversation-title").textContent = conv.title; feedback.textContent = "";
  const running = conv.id === currentConversation && (currentTurn || submitting);
  confirm.disabled = !!running;
  if (running) feedback.textContent = "该对话正在运行或等待确认，请先处理确认并等待本轮结束。";
  confirm.onclick = async () => {
    confirm.disabled = true; feedback.textContent = "正在删除…";
    try {
      const result = await api(`/api/conversations/${encodeURIComponent(conv.id)}`, {method: "DELETE"});
      dialog.close(); await afterConversationDeleted(conv, result);
    } catch (err) { feedback.textContent = err.message; confirm.disabled = false; }
  };
  if (!dialog.open) dialog.showModal();
  $("delete-cancel").focus();
}
async function afterConversationDeleted(conv, result) {
  const index = conversationOrder.indexOf(conv.id), wasCurrent = conv.id === currentConversation;
  const runs = result.undoable_run_ids || [];
  showToast(`已删除「${conv.title}」。` + (runs.length ? `其中 ${runs.length} 次运行的操作日志仍保留，可在终端运行 python main.py undo <运行编号> 撤销：${runs.join("、")}` : ""));
  if (!wasCurrent) { await loadConversations(); return; }
  // Invalidate any in-flight load or poll for the removed conversation before switching.
  viewEpoch++; clearTimeout(pollTimer); currentConversation = null; currentTurn = null; renderedConversation = null;
  const items = await loadConversations();
  const next = items[Math.min(Math.max(index, 0), items.length - 1)];
  if (next) await openConversation(next.id); else await newConversation();
}
let toastTimer = null;
function showToast(text) {
  const toast = $("toast"); toast.querySelector(".toast-text").textContent = text; toast.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.add("hidden"), text.length > 60 ? 12000 : 4000);
}
function closeSidebar() { document.body.classList.remove("sidebar-open"); }
function resetConfirmation() {
  if ($("confirmation-review").open) $("confirmation-review").close();
  activeReview = null;
  $("confirmation").replaceChildren(); $("confirmation").classList.add("hidden"); delete $("confirmation").dataset.question;
}
async function openConversation(id, animateLast = false) {
  const epoch = ++viewEpoch;
  const refresh = id === renderedConversation;
  clearTimeout(pollTimer); currentConversation = id; currentTurn = null; submitting = false;
  resetConfirmation(); $("send-button").disabled = true;
  if (!refresh) { setStatus("加载中…"); $("messages").classList.add("loading"); markActiveConversation(id); }
  let conv;
  try { conv = await api(`/api/conversations/${id}`); }
  finally { if (epoch === viewEpoch) $("messages").classList.remove("loading"); }
  if (epoch !== viewEpoch) return;
  $("conversation-title").textContent = conv.title; renderMessages(conv.messages, animateLast, id);
  const latest = conv.latest_turn;
  if (latest) {
    updateActivity(latest);
    if (ACTIVE.has(latest.status)) {
      currentTurn = latest.id; updateConfirmation(latest);
      setStatus(latest.status === "waiting_confirmation" ? "等待确认" : "正在处理", "running");
      pollTimer = setTimeout(() => pollTurn(id, latest.id, epoch), 100);
    } else {
      setStatus(latest.status === "failed" ? "运行失败" : outcomeLabel(latest.result), latest.status === "failed" ? "failed" : "done");
      if (latest.status === "failed" && !latest.final) showError(latest.error || "任务中断");
      addRunActions(latest, epoch);
    }
  } else setStatus("");
  $("send-button").disabled = !!currentTurn;
  loadConversations().catch(err => showError(err.message));
}
function markActiveConversation(id) {
  document.querySelectorAll(".conversation-row").forEach(row => row.classList.toggle("active", row.dataset.conversationId === id));
}
async function newConversation() {
  const conv = await api("/api/conversations", {method: "POST", body: "{}"});
  await openConversation(conv.id); $("message-input").focus();
}
async function sendMessage() {
  const input = $("message-input"), content = input.value.trim();
  if (!content || !currentConversation || currentTurn || submitting) return;
  const cid = currentConversation, epoch = viewEpoch;
  submitting = true; $("send-button").disabled = true; input.value = ""; autoGrow();
  appendMessage({role: "user", content});
  updateActivity({id: "submitting", status: "queued", events: [], result: {}}); setStatus("提交中…", "running");
  try {
    const response = await api(`/api/conversations/${cid}/messages`, {method: "POST", body: JSON.stringify({content})});
    if (epoch !== viewEpoch) return;
    currentTurn = response.turn_id; submitting = false;
    await pollTurn(cid, currentTurn, epoch);
  } catch (err) {
    if (epoch !== viewEpoch) return;
    submitting = false; currentTurn = null; $("send-button").disabled = false;
    updateActivity({id: "submitting", status: "failed", events: [], error: err.message});
    input.value = content; autoGrow(); showError(err.message); setStatus("发送失败", "failed");
  }
}
function showError(text) { $("messages").append(node("div", "run-card error-card", text)); }
async function pollTurn(cid, turnId, epoch) {
  if (epoch !== viewEpoch || cid !== currentConversation || turnId !== currentTurn) return;
  try {
    const turn = await api(`/api/turns/${turnId}`);
    if (epoch !== viewEpoch || turnId !== currentTurn) return;
    updateActivity(turn); updateConfirmation(turn);
    if (ACTIVE.has(turn.status)) {
      setStatus(turn.status === "waiting_confirmation" ? "等待确认" : "正在处理", "running");
      pollTimer = setTimeout(() => pollTurn(cid, turnId, epoch), 650);
    } else {
      currentTurn = null; $("send-button").disabled = false;
      await openConversation(cid, true);
      loadFiles(currentFilePath);
    }
  } catch (err) {
    if (epoch !== viewEpoch) return;
    setStatus("连接中断，正在重试…", "running");
    pollTimer = setTimeout(() => pollTurn(cid, turnId, epoch), 2200);
  }
}
const TOOL_LABELS = {
  prepare_file_plan: "生成文件操作清单并预检", execute_file_plan: "本地执行并核验文件计划", inspect_file_plan: "查看文件计划和阻塞原因",
  set_task: "登记目标、范围与约束", finish_task: "逐项核验任务完成条件", inspect_run: "只读查询运行原因", inventory_files: "本地统计文件",
  list_dir: "查看文件夹", search_files: "查找文件", search_content: "检索论文内容", read_file: "阅读文件",
  file_info: "核对文件信息", find_duplicates: "扫描重复文件", apply_dedup: "处理重复文件", deduplicate: "本地查重与清理", report_findings: "核对证据并生成答复",
  paper_profiles: "批量获取论文片段", submit_classification: "保存分类计划并生成表格", apply_classification: "本地批量执行分类计划",
  begin_step: "准备当前任务步骤", make_dir: "创建文件夹", move: "移动文件", rename: "重命名文件", trash: "移入回收区",
};
function outcomeLabel(result) {
  return ({done: "已完成", answered: "已回复", partial: "部分完成", needs_review: "结果待核实", waiting_input: "等待补充信息", cancelled: "已取消", cost_budget:'费用达上限，已中止', token_budget:'Token 达上限，已中止', max_steps:'执行轮数达上限', repeated_call:'重复调用，已中止', repair_limit:'连续失败无进展，已中止', exception:'运行失败', unverified_answer: "未形成可核验结果"})[result?.stop_reason] || "已停止";
}
function eventSteps(events) {
  const steps = [], indexes = new Map();
  for (const [index, event] of events.entries()) {
    if (event.type === "llm_start") {
      const key = `llm:${event.step}`;
      indexes.set(key, steps.length); steps.push({title: event.step === 1 ? "分析你的请求" : "分析工具结果，准备下一步", detail: `第 ${event.step} 轮`, state: "active"});
    } else if (event.type === "llm_end") {
      const entry = steps[indexes.get(`llm:${event.step}`)]; if (entry) entry.state = "done";
    } else if (event.type === "llm_retry") {
      steps.push({title: "模型请求暂时失败，正在重试", detail: `${event.error_type} · 第 ${event.attempt} 次请求 · 等待 ${event.backoff_seconds} 秒`, state: "done"});
    } else if (event.type === "tool_start") {
      const args = event.args || {}, key = event.call_id || `tool:${index}`;
      indexes.set(key, steps.length);
      steps.push({title: TOOL_LABELS[event.tool] || "执行文件工具", detail: args.description || args.query || args.path || args.src || args.scan_id || "", state: "active"});
    } else if (event.type === "tool_end") {
      const entry = steps[indexes.get(event.call_id)];
      if (entry) { entry.state = event.ok ? "done" : "error"; entry.note = event.ok ? "" : event.summary; }
    } else if (event.type === "local_step") {
      steps.push({title: event.message, detail: "本地执行", state: "done"});
    } else if (event.type === "confirmation_required") {
      indexes.set("confirmation", steps.length); steps.push({title: "等待你确认文件操作", detail: "确认后继续执行", state: "waiting"});
    } else if (event.type === "confirmation_answered") {
      const entry = steps[indexes.get("confirmation")]; if (entry) { entry.state = "done"; entry.detail = event.answer === "n" ? "你已拒绝该操作" : "你已允许该操作"; }
    }
  }
  return steps;
}
const stepKeys = new WeakMap();
let expandedTurn = null;
function updateActivity(turn) {
  let card = $("agent-activity");
  if (!card) {
    card = node("section", "agent-activity"); card.id = "agent-activity";
    const heading = node("button", "activity-heading"); heading.type = "button"; heading.setAttribute("aria-expanded", "false");
    heading.append(node("span", "activity-symbol", "✦"), node("span", "activity-title"), node("span", "activity-clock"), node("span", "activity-chevron", "⌄"));
    const current = node("div", "activity-current"); current.setAttribute("aria-live", "polite");
    const details = node("ol", "activity-steps hidden");
    const toggle = (open) => { heading.setAttribute("aria-expanded", String(open)); details.classList.toggle("hidden", !open); if (open && card.turn) renderSteps(card, card.turn); };
    heading.onclick = () => { const open = heading.getAttribute("aria-expanded") !== "true"; expandedTurn = open ? card.turn?.id : null; toggle(open); };
    card.append(heading, current, details);
    // Keep the list open across the reload that follows completion.
    if (expandedTurn && expandedTurn === turn.id) { card.turn = turn; toggle(true); }
    const last = $("messages").lastElementChild;
    if (turn.status === "completed" && last?.classList.contains("assistant")) last.before(card); else $("messages").append(card);
  }
  const events = turn.events || [], steps = eventSteps(events), working = ACTIVE.has(turn.status);
  card.className = `agent-activity ${working ? turn.status === "waiting_confirmation" ? "waiting" : "working" : turn.status === "failed" ? "failed" : "finished"}`;
  const activeStep = [...steps].reverse().find(step => step.state === "active" || step.state === "waiting");
  const toolCount = events.filter(event => event.type === "tool_end").length;
  const title = working ? turn.status === "waiting_confirmation" ? "等待你的确认" : activeStep?.title || (turn.id === "submitting" ? "正在提交请求" : "Agent 正在准备") : turn.status === "failed" ? "本轮处理已中断" : turn.result?.stop_reason === "done" ? `处理完成 · ${toolCount || turn.result?.tool_calls || 0} 次工具调用` : outcomeLabel(turn.result);
  const heading = card.querySelector(".activity-title"); if (heading.textContent !== title) heading.textContent = title;
  const localStep = [...events].reverse().find(event => event.type === "local_step");
  const detail = working ? ["本地查重与清理", "本地批量执行分类计划"].includes(activeStep?.title) && localStep ? localStep.message : activeStep?.detail || "" : turn.error || "展开查看执行步骤";
  card.querySelector(".activity-current").textContent = detail;
  // The step list is built only while expanded; collapsed cards update just the heading.
  card.turn = turn;
  if (card.querySelector(".activity-heading").getAttribute("aria-expanded") === "true") expandedTurn = turn.id;
  if (!card.querySelector(".activity-steps").classList.contains("hidden")) renderSteps(card, turn);
  turn.clockReceivedAt = performance.now();
  latestActivity = turn; updateClock(); scrollIfNearBottom();
}
function renderSteps(card, turn) {
  const events = turn.events || [], signature = `${turn.id}:${events.length}:${turn.status}`;
  if (card.dataset.signature !== signature) {
    const steps = eventSteps(events), working = ACTIVE.has(turn.status);
    // Patch only the steps whose state or text changed; earlier finished steps stay as they are.
    const list = card.querySelector(".activity-steps"), items = list.children;
    steps.forEach((step, index) => {
      const state = !working && (step.state === "active" || step.state === "waiting") ? "error" : step.state;
      const key = `${state}\u0000${step.title}\u0000${step.detail || ""}\u0000${step.note || ""}`;
      if (items[index] && stepKeys.get(items[index]) === key) return;
      const li = node("li", `activity-step ${state}`);
      li.append(node("span", "step-dot", state === "done" ? "✓" : state === "error" ? "!" : ""));
      const content = node("div", "step-content"); content.append(node("div", "step-title", step.title));
      if (step.detail) content.append(node("div", "step-detail", step.detail));
      if (step.note) content.append(node("div", "step-detail", step.note));
      li.append(content); stepKeys.set(li, key);
      if (items[index]) items[index].replaceWith(li); else list.append(li);
    });
    while (items.length > steps.length) list.lastElementChild.remove();
    card.dataset.signature = signature;
  }
}
function updateClock() {
  const clock = document.querySelector("#agent-activity .activity-clock"); if (!clock || !latestActivity) return;
  const turn = latestActivity;
  const metrics = ACTIVE.has(turn.status) ? turn.clock : turn.result;
  if (metrics?.active_seconds != null) {
    const ticking = ACTIVE.has(turn.status) && turn.status !== "waiting_confirmation";
    const extra = ticking ? (performance.now() - (turn.clockReceivedAt || performance.now())) / 1000 : 0;
    clock.textContent = `运行 ${Math.round(metrics.active_seconds + extra)} 秒`;
    clock.title = `确认等待 ${Number(metrics.wait_seconds || 0).toFixed(1)} 秒（单独统计）`;
  } else {
    clock.textContent = turn.result?.seconds ? `总计 ${Math.round(turn.result.seconds)} 秒（含确认等待）` : "";
  }
}
setInterval(updateClock, 1000);
function updateConfirmation(turn) {
  const box = $("confirmation"), pending = turn.pending_confirmation;
  if (!pending) { resetConfirmation(); return; }
  const key = `${turn.id}:${pending.id || pending.question}`;
  if (box.dataset.question === key) return;
  resetConfirmation();
  box.dataset.question = key; box.replaceChildren(); box.classList.remove("hidden");
  const action = pending.action || {title: "确认文件操作", summary: pending.question, confirm_label: "确认执行", groups: [], notes: []};
  const state = {turnId: turn.id, id: pending.id, action, allowAll: false, submitted: false, viewed: new Set(), previewEpoch: 0};
  activeReview = state;
  const header = node("div", "confirmation-card-header");
  header.append(node("span", `confirmation-badge ${action.tone || "neutral"}`, "等待确认"), node("strong", "confirmation-title", action.title));
  const review = node("button", "confirmation-expand", "展开核对 ↗"); review.type = "button"; review.onclick = () => openConfirmationReview(state); header.append(review);
  const body = node("div", "confirmation-body"); body.append(node("p", "confirmation-summary", action.summary));
  renderConfirmationGroups(body, state, true);
  const status = node("div", "confirmation-feedback"); status.setAttribute("role", "status");
  box.append(header, body, status, confirmationActions(state));
}
function renderConfirmationGroups(container, state, compact = false) {
  for (const group of state.action.groups || []) {
    if (!group.items.length) continue;
    const section = node("section", `confirmation-group ${group.tone}`);
    const heading = node("h3", "confirmation-group-heading", group.title);
    heading.append(node("span", "confirmation-group-count", `${group.items.length} 项`)); section.append(heading);
    if (group.description) section.append(node("p", "confirmation-group-description", group.description));
    const items = node("div", "confirmation-group-items");
    for (const item of compact ? group.items.slice(0, 2) : group.items) {
      const row = node("div", `confirmation-file-row ${item.disposition || ""}`); row.dataset.reviewPath = item.path;
      if (item.disposition) row.append(node("span", `file-disposition ${item.disposition}`, item.disposition === "trash" ? "待回收" : "保留"));
      const button = node("button", "confirmation-file-button"); button.type = "button"; button.disabled = !item.previewable;
      const text = node("span", "confirmation-file-text"); text.append(node("span", "confirmation-file-name", item.name), node("span", "confirmation-file-path", item.path));
      button.append(node("span", "confirmation-file-icon", item.kind === "directory" ? "▰" : "▤"), text, node("span", "confirmation-file-open", item.previewable ? "预览 ↗" : item.planned ? "待创建" : "不可预览"));
      button.title = item.previewable ? `预览 ${item.path}` : item.message;
      button.onclick = () => compact ? openConfirmationReview(state, item) : showReviewPreview(state, item);
      row.append(button);
      const meta = node("div", "confirmation-file-meta", [item.size_text, item.message].filter(Boolean).join(" · "));
      if (item.previewable) { const viewed = node("span", "confirmation-viewed", state.viewed.has(item.path) ? "已打开" : ""); viewed.dataset.viewedPath = item.path; meta.append(viewed); }
      row.append(meta);
      if (item.destination) row.append(node("div", "confirmation-destination", `→ ${item.destination}`));
      items.append(row);
    }
    section.append(items);
    if (compact && group.items.length > 2) {
      const more = node("button", "confirmation-more", `还有 ${group.items.length - 2} 项，查看全部`); more.type = "button"; more.onclick = () => openConfirmationReview(state); section.append(more);
    }
    container.append(section);
  }
  for (const note of state.action.notes || []) container.append(node("p", "confirmation-note", note));
}
function confirmationActions(state) {
  const footer = node("div", "confirmation-actions");
  const option = node("label", "confirmation-allow-all");
  const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.allowAll; checkbox.disabled = state.submitted;
  checkbox.onchange = () => { state.allowAll = checkbox.checked; document.querySelectorAll(".confirmation-allow-all input").forEach(input => input.checked = state.allowAll); };
  option.title = "勾选后，本轮后续文件操作将不再逐次询问"; option.append(checkbox, node("span", "", "本轮后续操作免确认"));
  const buttons = node("div", "confirmation-action-buttons");
  const reject = node("button", "confirmation-reject", "拒绝此次操作"); reject.type = "button";
  const allow = node("button", `confirmation-approve ${state.action.tone || "neutral"}`, state.action.confirm_label || "确认执行"); allow.type = "button";
  reject.onclick = () => submitConfirmation(state, "n"); allow.onclick = () => submitConfirmation(state, state.allowAll ? "a" : "y");
  buttons.append(reject, allow); footer.append(option, buttons); return footer;
}
async function submitConfirmation(state, answer) {
  if (state !== activeReview || state.submitted) return;
  state.submitted = true;
  document.querySelectorAll(".confirmation-actions button, .confirmation-allow-all input").forEach(button => button.disabled = true);
  const feedback = text => document.querySelectorAll(".confirmation-feedback").forEach(el => el.textContent = text);
  feedback("正在提交你的选择…");
  try {
    await api(`/api/turns/${state.turnId}/confirm`, {method: "POST", body: JSON.stringify({answer, confirmation_id: state.id})});
    if (state !== activeReview) return;
    feedback(answer === "n" ? "已拒绝，等待 Agent 继续。" : "已确认，等待 Agent 执行。");
    if ($("confirmation-review").open) $("confirmation-review").close();
  } catch (err) {
    if (state !== activeReview) return;
    state.submitted = false; feedback(err.message);
    document.querySelectorAll(".confirmation-actions button, .confirmation-allow-all input").forEach(button => button.disabled = false);
  }
}
function openConfirmationReview(state, selected = null) {
  if (state !== activeReview || state.submitted) return;
  const dialog = $("confirmation-review");
  $("review-title").textContent = state.action.title;
  const plan = $("review-plan"); plan.replaceChildren(node("p", "confirmation-summary", state.action.summary));
  renderConfirmationGroups(plan, state);
  const feedback = node("div", "confirmation-feedback"); feedback.setAttribute("role", "status");
  $("review-footer").replaceChildren(feedback, confirmationActions(state));
  if (!dialog.open) dialog.showModal();
  const item = selected || state.action.groups.flatMap(group => group.items).find(item => item.previewable);
  if (item) showReviewPreview(state, item);
  else $("review-preview").replaceChildren(node("div", "review-preview-empty", state.action.operation === 'task_edit' ? "请核对左侧修改前后的目标、约束和范围。本次确认只更新任务。" : "此操作尚无可预览的文件，请核对左侧路径。"));
}
async function showReviewPreview(state, item) {
  const epoch = ++state.previewEpoch, panel = $("review-preview");
  const valid = () => state === activeReview && epoch === state.previewEpoch && $("confirmation-review").open;
  panel.replaceChildren(node("div", "review-preview-empty", "正在打开文件…"));
  document.querySelectorAll("[data-review-path]").forEach(row => row.classList.toggle("selected", row.dataset.reviewPath === item.path));
  try {
    const data = await api(item.kind === "directory" ? `/api/files?path=${encodeURIComponent(item.path)}` : `/api/files/preview?path=${encodeURIComponent(item.path)}`);
    if (!valid()) return;
    const related = state.action.groups.flatMap(group => group.items).find(file => file.path === item.path || (file.kind === "directory" && item.path.startsWith(`${file.path}/`)));
    const disposition = item.disposition || related?.disposition;
    const heading = node("div", `review-preview-heading ${disposition || ""}`);
    if (disposition) heading.append(node("span", `file-disposition ${disposition}`, disposition === "trash" ? "待回收 · 确认后移入回收区" : "保留 · 本次不改动"));
    heading.append(node("strong", "", item.name), node("span", "", item.path));
    const body = node("div", "review-preview-body"); panel.replaceChildren(heading, body);
    if (item.kind === "directory") {
      if (data.parent !== null) {
        const up = node("button", "confirmation-more", "↑ 返回上级文件夹"); up.onclick = () => showReviewPreview(state, {path: data.parent, name: data.parent, kind: "directory"}); body.append(up);
      }
      if (!data.items.length) body.append(node("div", "review-preview-empty", "此文件夹为空"));
      for (const child of data.items) {
        const button = node("button", "review-directory-item"); button.type = "button";
        button.append(node("span", "", child.type === "directory" ? "▰" : "▤"), node("span", "", child.name), node("small", "", child.size_text));
        button.onclick = () => showReviewPreview(state, {...child, kind: child.type}); body.append(button);
      }
    } else {
      const url = `/api/files/raw?path=${encodeURIComponent(item.path)}`;
      const external = node("a", "review-open-original", "打开 / 下载原文件 ↗"); external.href = url; external.target = "_blank"; external.rel = "noopener noreferrer"; heading.append(external);
      if (data.type === "pdf") { const frame = node("iframe"); frame.src = url; frame.title = `预览 ${item.name}`; body.append(frame); }
      else if (data.type === "image") { const image = node("img"); image.src = url; image.alt = item.name; image.onerror = () => body.replaceChildren(node("div", "review-preview-empty", "浏览器无法显示此图片，可打开原文件查看。")); body.append(image); }
      else if (data.type === "text") body.append(node("pre", "", data.content + (data.truncated ? `\n\n…预览已截断，共 ${data.chars} 字。可打开原文件查看完整内容。` : "")));
      else body.append(node("div", "review-preview-empty", `此格式暂不支持内嵌预览（${data.size_text}），可通过上方链接下载查看。`));
    }
    state.viewed.add(item.path);
    document.querySelectorAll("[data-viewed-path]").forEach(label => { if (state.viewed.has(label.dataset.viewedPath)) label.textContent = "已打开"; });
  } catch (err) { if (valid()) panel.replaceChildren(node("div", "review-preview-empty", `无法预览：${err.message}`)); }
}
function addRunActions(turn, epoch) {
  if (!turn.result?.seconds && turn.status !== "failed") return;
  const card = node("div", "run-actions");
  const result = turn.result || {};
  const timing = result.active_seconds != null ? `运行 ${Number(result.active_seconds).toFixed(1)} 秒 · 确认等待 ${Number(result.wait_seconds || 0).toFixed(1)} 秒 · 模型 ${Number(result.llm_seconds || 0).toFixed(1)} 秒 · 本地工具 ${Number(result.tool_seconds || 0).toFixed(1)} 秒` : result.seconds ? `总计 ${Number(result.seconds).toFixed(1)} 秒（含确认等待）` : "";
  const metrics = node("span", "run-metrics", `${timing} · ¥${Number(result.cost || 0).toFixed(4)}`);
  const trace = node("button", "text-button", "详细记录"); trace.type = "button";
  trace.onclick = async () => {
    trace.disabled = true;
    try {
      const {items, requests = []} = await api(`/api/turns/${turn.id}/trace`); if (epoch !== viewEpoch) return;
      const details = node("pre", "raw-trace"); details.textContent = [items.map(item => `${item.tool} ${JSON.stringify(item.args)}\n${item.result}`).join("\n\n"), requests.length ? "模型请求诊断（毫秒 / Token）\n" + requests.map(item => JSON.stringify(item)).join("\n") : ""].filter(Boolean).join("\n\n") || "本轮没有工具执行记录";
      card.after(details); trace.remove();
    } catch (err) { trace.disabled = false; showError(err.message); }
  };
  const undoButton = node("button", "text-button", turn.result?.undo ? "已执行撤销" : "撤销文件操作"); undoButton.type = "button"; undoButton.disabled = !!turn.result?.undo;
  undoButton.onclick = async () => {
    undoButton.disabled = true;
    try {
      const data = await api(`/api/turns/${turn.id}/undo`, {method: "POST", body: "{}"}); if (epoch !== viewEpoch) return;
      await openConversation(currentConversation); loadFiles();
    } catch (err) { undoButton.disabled = false; showError(err.message); }
  };
  card.append(metrics, trace, undoButton); $("messages").append(card); scrollIfNearBottom();
}
async function loadFiles(path = currentFilePath) {
  const epoch = ++fileListEpoch;
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    if (epoch !== fileListEpoch) return;
    currentFilePath = data.path;
    $("current-path").textContent = data.path; $("file-up").disabled = !data.parent; $("file-up").onclick = () => data.parent && loadFiles(data.parent);
    const list = $("file-list"); list.replaceChildren();
    if (!data.items.length) list.append(node("div", "file-empty", "此文件夹为空"));
    for (const item of data.items) {
      const button = node("button", `file-row ${item.type === "directory" ? "dir" : ""}`);
      const name = node("span", "file-name", item.name); name.title = item.name;
      button.append(node("span", "", item.type === "directory" ? "▰" : fileIcon(item.name)), name, node("span", "file-size", item.size_text));
      button.onclick = () => item.type === "directory" ? loadFiles(item.path) : preview(item);
      button.dataset.path = item.path; list.append(button);
    }
    return data;
  } catch (err) { if (epoch === fileListEpoch) $("file-list").replaceChildren(node("div", "file-empty", err.message)); }
}
function revealFilePanel() {
  $("file-panel").classList.remove("closed"); $("file-panel").classList.add("file-panel-reveal");
  setTimeout(() => $("file-panel").classList.remove("file-panel-reveal"), 450);
}
async function openReferenceDirectory(path) {
  const epoch = ++previewEpoch;
  revealFilePanel();
  const data = await loadFiles(path);
  if (epoch !== previewEpoch || !data) return;
  $("preview-type").textContent = "文件夹"; $("preview-type").title = path;
  $("preview").replaceChildren(node("div", "empty-preview", "已打开文件夹，在上方列表中选择文件即可预览。"));
}
function fileIcon(name) { const ext = name.split(".").pop().toLowerCase(); return ext === "pdf" ? "▤" : ["png", "jpg", "jpeg", "gif", "webp"].includes(ext) ? "▧" : "≡"; }
async function preview(item, page = null, reveal = false) {
  const epoch = ++previewEpoch;
  const pane = $("preview"); pane.replaceChildren(node("div", "empty-preview preview-loading", "正在打开文件…"));
  if (reveal) revealFilePanel();
  $("preview-type").textContent = item.path.split("/").pop(); $("preview-type").title = item.path;
  try {
    const artifactBase = item.artifactId ? `/api/conversations/${encodeURIComponent(item.conversationId)}/files/${encodeURIComponent(item.artifactId)}` : null;
    const data = await api(artifactBase ? `${artifactBase}/preview` : `/api/files/preview?path=${encodeURIComponent(item.path)}`);
    if (epoch !== previewEpoch) return;
    pane.replaceChildren();
    if (reveal && data.status !== 'trash') { const parent = item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "."; await loadFiles(parent); }
    if (epoch !== previewEpoch) return;
    document.querySelectorAll(".file-row").forEach(row => row.classList.toggle("selected", row.dataset.path === item.path));
    const url = artifactBase ? `${artifactBase}/raw` : `/api/files/raw?path=${encodeURIComponent(item.path)}`;
    if (data.type === "text") pane.append(node("pre", "", data.content + (data.truncated ? `\n\n…预览已截断，文件共 ${data.chars} 字` : "")));
    else if (data.type === "image") { const img = node("img"); img.src = url; img.alt = item.name; pane.append(img); }
    else if (data.type === "pdf") { const frame = node("iframe"); frame.src = url + (page ? `#page=${page}` : ""); frame.title = item.name; pane.append(frame); }
    else { const link = node("a", "download-link", `下载 ${item.name}（${data.size_text}）`); link.href = url; pane.append(link); }
  } catch (err) { if (epoch === previewEpoch) pane.replaceChildren(node("div", "empty-preview", `无法打开文件：${err.message}。文件可能已移动或改名，请刷新对话后重试。`)); }
}
function autoGrow() { const input = $("message-input"); input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 150)}px`; }
$("new-chat").onclick = () => {
  closeSidebar();
  // An untouched empty chat is already a new conversation; do not pile up blank entries.
  if (currentConversation && !currentMessages.length && !currentTurn && !submitting) { $("message-input").focus(); return; }
  newConversation().catch(err => showError(err.message));
};
$("toggle-sidebar").onclick = () => {
  // On phones both panels are overlays; show one at a time.
  if (!document.body.classList.contains("sidebar-open")) $("file-panel").classList.add("closed");
  document.body.classList.toggle("sidebar-open");
};
$("sidebar-backdrop").onclick = closeSidebar;
$("delete-cancel").onclick = () => $("delete-dialog").close();
$("toast-close").onclick = () => $("toast").classList.add("hidden");
$("composer").onsubmit = (event) => { event.preventDefault(); sendMessage(); };
$("message-input").oninput = autoGrow;
$("message-input").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } };
$("refresh-files").onclick = () => loadFiles();
$("toggle-files").onclick = () => { closeSidebar(); $("file-panel").classList.toggle("closed"); };
$("review-close").onclick = () => $("confirmation-review").close();
$("confirmation-review").addEventListener("close", () => { if (activeReview) activeReview.previewEpoch++; $("review-preview").replaceChildren(); });
if (matchMedia("(max-width: 1050px)").matches) $("file-panel").classList.add("closed");

// Draggable panel borders. Sizes are a per-browser convenience, so storage may be unavailable.
const layoutEl = document.querySelector(".layout");
const SIZE_VARS = {sidebar: "--sidebar-w", files: "--files-w", filelist: "--filelist-h"};
function panelSize(name) {
  return name === "sidebar" ? $("sidebar").offsetWidth : name === "files" ? $("file-panel").offsetWidth : $("file-list").offsetHeight;
}
function clampSize(name, px) {
  // Keep the chat column at least 420px wide and both file-panel halves usable.
  const width = layoutEl.clientWidth, files = $("file-panel").classList.contains("closed") ? 0 : panelSize("files");
  if (name === "sidebar") return Math.max(160, Math.min(px, 420, width - files - 420));
  if (name === "files") return Math.max(260, Math.min(px, 720, width - panelSize("sidebar") - 420));
  const panel = $("file-panel").clientHeight;
  return panel ? Math.max(80, Math.min(px, panel - 200)) : px;
}
function applySize(name, px, save = true) {
  px = Math.round(clampSize(name, px));
  layoutEl.style.setProperty(SIZE_VARS[name], `${px}px`);
  if (save) try { localStorage.setItem(`filebutler.size.${name}`, String(px)); } catch {}
}
function resetSize(name) {
  layoutEl.style.removeProperty(SIZE_VARS[name]);
  try { localStorage.removeItem(`filebutler.size.${name}`); } catch {}
}
function bindResize(handle, name, axis) {
  // The file panel grows leftwards, so its drag delta is inverted.
  const sign = name === "files" ? -1 : 1, coord = event => axis === "x" ? event.clientX : event.clientY;
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault(); handle.setPointerCapture(event.pointerId);
    const start = coord(event), base = panelSize(name);
    document.body.classList.add("resizing", axis === "y" ? "resizing-row" : "resizing-col"); handle.classList.add("active");
    const move = e => applySize(name, base + sign * (coord(e) - start), false);
    const end = () => {
      handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", end); handle.removeEventListener("pointercancel", end);
      document.body.classList.remove("resizing", "resizing-row", "resizing-col"); handle.classList.remove("active");
      applySize(name, panelSize(name));
    };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", end); handle.addEventListener("pointercancel", end);
  });
  handle.addEventListener("keydown", event => {
    const step = event.shiftKey ? 48 : 16;
    const delta = axis === "x" ? {ArrowLeft: -step, ArrowRight: step}[event.key] : {ArrowUp: -step, ArrowDown: step}[event.key];
    if (delta) { event.preventDefault(); applySize(name, panelSize(name) + sign * delta); }
  });
  handle.ondblclick = () => resetSize(name);
}
bindResize($("resize-sidebar"), "sidebar", "x");
bindResize($("resize-files"), "files", "x");
bindResize($("resize-filelist"), "filelist", "y");
for (const name of Object.keys(SIZE_VARS)) {
  let saved = null;
  try { saved = Number(localStorage.getItem(`filebutler.size.${name}`)); } catch {}
  if (saved) applySize(name, saved, false);
}
(async () => {
  try {
    const [health, items] = await Promise.all([api("/api/health"), loadConversations(), loadFiles(".")]);
    $("health").textContent = `${health.model} · 本地服务已连接`; $("sandbox-label").textContent = `沙盒：${health.sandbox}`;
    if (items.length) await openConversation(items[0].id); else await newConversation();
  } catch (err) { $("health").textContent = "本地服务连接失败"; showError(err.message); }
})();
