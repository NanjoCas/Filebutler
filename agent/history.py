"""Local, per-turn model-history projection. The UI keeps the complete transcript."""
import copy
import json

ARCHIVE_NOTE = "[本地历史归档] 较早的工具调用明细已归档；用户指令、助手文字结论与文件状态更新仍保留。旧文件快照不能代表当前状态；需要原文细节时使用工具核实。"
UNDO_MARK = "[本地撤销状态 run="


def undo_note(run_id, report, *, at=None, historical=False):
    # Actual reports, including skips/conflicts, are data rather than new user instructions.
    when = f"时间：{at}。" if at else ""
    context = "历史撤销事件补录；此后可能已有新的文件操作，本记录不是当前文件快照。" if historical else "用户已在界面执行撤销。"
    return {"role": "system", "content": f"{UNDO_MARK}{run_id}]\n{when}{context}以下是本地实际结果（不是新任务）；先前成功操作的结论只描述撤销之前。不得把恢复的文件误判为之前操作失败。跳过项不代表已还原。\n" + json.dumps(report, ensure_ascii=False)}


def has_undo(history, run_id):
    marker = f"{UNDO_MARK}{run_id}]"
    return any(m.get("role") == "system" and m.get("content", "").startswith(marker) for m in history)


# Tools retired with protocol v2 (tag archive-v2-legacy-20261023). Their old calls
# are folded into notes: the model never sees a call to a tool it no longer has.
LEGACY_TOOLS = {'set_task', 'begin_step', 'edit_task', 'apply_task_edit',
                'move', 'rename', 'make_dir', 'trash', 'apply_classification'}
LEGACY_WRITES = {'move', 'rename', 'make_dir', 'trash', 'apply_classification'}


def _exchanges(full, boundary):
    """Complete (assistant tool_calls, tool results) pairs before ``boundary``."""
    groups = []
    for i, m in enumerate(full[:boundary]):
        if m.get('tool_calls'):
            end = i + 1
            ids = {tc['id'] for tc in m['tool_calls']}
            while end < boundary and full[end].get('role') == 'tool': end += 1
            if {r.get('tool_call_id') for r in full[i+1:end]} == ids: groups.append((i, end))
    return groups


def _fold(full, start, end, legacy=False):
    """Replace one exchange by its prose plus a note per write (or per call, for legacy tools)."""
    from tools.registry import TOOLS
    m = full[start]; result_by_id = {r['tool_call_id']: r for r in full[start+1:end]}
    notes = []
    for tc in m['tool_calls']:
        name = tc['function']['name']
        tool = TOOLS.get(name)
        if not (legacy or name in LEGACY_WRITES or (tool and tool.writes)):
            continue
        raw = result_by_id[tc['id']].get('content', '')
        try:
            outcome = json.loads(raw); text = outcome['text']; ok = outcome.get('status') == 'ok'
        except (ValueError, KeyError, TypeError): text = raw; ok = raw.startswith('OK')
        status = '成功（历史结果；当前位置需核实）' if ok else (text.splitlines() or ['调用未完成'])[0][:250]
        prefix = '[旧版本工具记录] ' if name in LEGACY_TOOLS else '[历史工具执行记录] '
        notes.append(prefix + name + ' ' + tc['function']['arguments'] + '\n' + status)
    return ([{'role': 'assistant', 'content': m['content']}] if m.get('content') else []) + \
           [dict(role='assistant', content=n) for n in notes]


def project_task_history(full, limit, before):
    """Evict whole previous exchanges when recent turns alone exceed the budget."""
    boundary=max((i for i,m in enumerate(full) if m.get('role')=='user'),default=0)
    projected=copy.deepcopy(full); removed=0
    selected=[]
    size=len(json.dumps(projected,ensure_ascii=False))
    for start,end in _exchanges(full, boundary):
        if size<=limit:break
        replacement=_fold(full,start,end)
        delta=len(json.dumps(full[start:end],ensure_ascii=False))-len(json.dumps(replacement,ensure_ascii=False))
        if delta<=0:continue
        selected.append((start,end,replacement));size-=delta;removed+=end-start
    for start,end,replacement in reversed(selected):projected[start:end]=replacement
    if removed:projected.insert(1,dict(role='system',content=ARCHIVE_NOTE))
    after=len(json.dumps(projected,ensure_ascii=False))
    return projected,dict(before_chars=before,after_chars=after,archived_messages=removed,
                          projection='task_budget',over_budget=after>limit)


def prepare_history(history, cfg, artifacts=None):
    """Fold retired-tool exchanges, then archive old tool exchanges only over budget;
    never split call/result pairs. User constraints and assistant prose remain
    verbatim. Works once at turn entry, not on every LLM round.
    """
    full = copy.deepcopy(history)
    before = len(json.dumps(full, ensure_ascii=False))
    full = [m for m in full if not (m.get('role') == 'system' and
            m.get('content', '').startswith('【任务控制 V2，优先于旧版流程说明】'))]
    boundary = max((i for i, m in enumerate(full) if m.get('role') == 'user'), default=0)
    legacy = [(s, e) for s, e in _exchanges(full, boundary)
              if any(tc['function']['name'] in LEGACY_TOOLS for tc in full[s]['tool_calls'])]
    for start, end in reversed(legacy):
        full[start:end] = _fold(full, start, end, legacy=True)
    limit = cfg.get("conversation_history_max_chars", 40000)
    if limit and len(json.dumps(full, ensure_ascii=False)) > limit:
        return project_task_history(full, limit, before)
    after = len(json.dumps(full, ensure_ascii=False))
    stats = {"before_chars": before, "after_chars": after, "archived_messages": 0}
    if legacy: stats["legacy_folded"] = len(legacy)
    return full, stats
