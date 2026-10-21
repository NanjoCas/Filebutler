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


def project_task_history(full, limit, before):
    """Evict whole previous exchanges when recent turns alone exceed the budget."""
    boundary=max((i for i,m in enumerate(full) if m.get('role')=='user'),default=0)
    projected=copy.deepcopy(full); removed=0
    # Work backwards so replacement does not invalidate the earlier indices.
    groups=[]
    for i,m in enumerate(full[:boundary]):
        if m.get('tool_calls'):
            end=i+1
            ids={tc['id'] for tc in m['tool_calls']}
            while end<boundary and full[end].get('role')=='tool':end+=1
            results=full[i+1:end]
            if {r.get('tool_call_id') for r in results}==ids:groups.append((i,end))
    selected=[]
    size=len(json.dumps(projected,ensure_ascii=False))
    for start,end in groups:
        if size<=limit:break
        m=full[start];result_by_id={r['tool_call_id']:r for r in full[start+1:end]}
        notes=[]
        from tools.registry import TOOLS
        for tc in m['tool_calls']:
            tool=TOOLS.get(tc['function']['name'])
            if tool and tool.writes:
                raw=result_by_id[tc['id']].get('content','')
                try:
                    outcome=json.loads(raw);text=outcome['text'];ok=outcome.get('status')=='ok'
                except (ValueError,KeyError,TypeError):text=raw;ok=raw.startswith('OK')
                status='成功（历史结果；当前位置需核实）' if ok else (text.splitlines() or ['调用未完成'])[0][:250]
                notes.append('[历史工具执行记录] '+tool.name+' '+tc['function']['arguments']+'\n'+status)
        replacement=([{'role':'assistant','content':m['content']}] if m.get('content') else [])
        replacement += [dict(role='assistant',content=n) for n in notes]
        delta=len(json.dumps(full[start:end],ensure_ascii=False))-len(json.dumps(replacement,ensure_ascii=False))
        if delta<=0:continue
        selected.append((start,end,replacement));size-=delta;removed+=end-start
    for start,end,replacement in reversed(selected):projected[start:end]=replacement
    if removed:projected.insert(1,dict(role='system',content=ARCHIVE_NOTE))
    after=len(json.dumps(projected,ensure_ascii=False))
    return projected,dict(before_chars=before,after_chars=after,archived_messages=removed,
                          projection='task_budget',over_budget=after>limit)


def prepare_history(history, cfg, artifacts=None):
    """Archive only completed old tool exchanges; never split call/result pairs.

    User constraints and assistant prose remain verbatim. Work once at turn entry,
    not on every LLM round. This is separate from the experimental compressor.
    """
    full = copy.deepcopy(history)
    before = len(json.dumps(full, ensure_ascii=False))
    if cfg.get('task_runtime'):
        full = [m for m in full if not (m.get('role') == 'system' and
                m.get('content', '').startswith('【任务控制 V2，优先于旧版流程说明】'))]
    if artifacts and cfg.get('local_runtime') and not cfg.get('task_runtime') and full and full[-1].get('role')=='user':
        from agent.runtime import classification_followup
        from tools.classification import latest_plan
        pid=latest_plan(artifacts)
        if pid and classification_followup(full[-1]['content']):
            # A deterministic executor needs the stored plan, not old excerpts.
            # Keep all user/system constraints; the UI still retains everything.
            projected=[m for m in full[:-1] if m['role'] in {'user','system'}]
            plan=artifacts.data['classification_plans'][pid]
            projected += [{'role':'system','content':f'本地分类计划 {pid}：{len(plan["assignments"])} 篇，{len(plan["categories"])} 类；执行时逐项校验版本和路径。'},full[-1]]
            return projected,dict(before_chars=before,after_chars=len(json.dumps(projected,ensure_ascii=False)),
                                  archived_messages=len(full)-len(projected)+1,projection='classification_plan')
    limit = cfg.get("conversation_history_max_chars", 40000)
    if cfg.get('task_runtime') and limit and len(json.dumps(full,ensure_ascii=False))>limit:
        return project_task_history(full,limit,before)
    users = [i for i, m in enumerate(full) if m.get("role") == "user"]
    keep = max(0, int(cfg.get("conversation_keep_full_turns", 2)))
    if not limit or before <= limit or len(users) <= keep + 1:
        return full, {"before_chars": before, "after_chars": before, "archived_messages": 0}
    boundary = users[-(keep + 1)]  # current user plus recent completed turns
    projected, removed = [], 0
    writes = {}
    for i, m in enumerate(full):
        if i < boundary and m.get("role") == "tool":
            removed += 1
            # Preserve local mutation outcomes even if a final reply was interrupted.
            if m.get("tool_call_id") in writes:
                projected.append({"role": "assistant", "content": "[历史工具执行记录] " + writes[m["tool_call_id"]] + "\n" + m.get("content", "")})
            continue
        if i < boundary and m.get("tool_calls"):
            writes = {tc["id"]: tc["function"]["name"] + " " + tc["function"]["arguments"]
                           for tc in m["tool_calls"] if tc["function"]["name"] in {"apply_dedup", "deduplicate", "apply_classification", "trash", "move", "rename", "make_dir"}}
            removed += 1
            if m.get("content"):
                projected.append({"role": "assistant", "content": m["content"]})
            continue
        projected.append(m)
    if removed:
        projected.insert(1, {"role": "system", "content": ARCHIVE_NOTE})
    after = len(json.dumps(projected, ensure_ascii=False))
    if after >= before:
        return full, {"before_chars": before, "after_chars": before, "archived_messages": 0}
    return projected, {"before_chars": before, "after_chars": after, "archived_messages": removed}
