"""One user-facing answer; audit, statistics and verification stay in details."""
from collections import Counter
import json
import re

from agent.artifacts import label
from agent.task import open_blockers


STATE_TEXT = {'completed':'已完成', 'partial':'尚未全部完成', 'needs_review':'部分结果待核实',
              'waiting_input':'等待补充信息', 'cancelled':'任务已取消'}


def display_state(state, task=None):
    """What the user should do next (user decision 2026-10-24); the task state itself
    is unchanged. A read-only task's review is its cited fragments, so it reads as
    answered; only a task that changed files asks the user to confirm.
    Keys: done / answered / confirm / waiting / unfinished / cancelled."""
    if state == 'needs_review':
        changed = task and (task.get('operation_history') or task.get('operations'))
        return 'confirm' if changed else 'answered'
    return {'completed': 'done', 'answered': 'done', 'explained': 'done', 'waiting_input': 'waiting', 'by_choice': 'chosen',
            'cancelled': 'cancelled', 'paused': 'cancelled'}.get(state, 'unfinished')


def _clause(text, limit=180):
    """One clause without trailing punctuation, so joining never yields "。。"."""
    text = text.strip().rstrip('。.；;，, ')
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


# Tool names as the user should read them (the page has the same list, web/app.js TOOL_LABELS).
TOOL_NAMES = {
    'prepare_file_plan': '准备文件操作', 'execute_file_plan': '执行文件操作', 'inspect_file_plan': '查看文件操作',
    'ask_user': '向你提问', 'start_task': '登记任务', 'revise_task': '修改任务',
    'finish_task': '核验任务', 'inspect_run': '查询运行记录', 'inventory_files': '统计文件', 'list_dir': '查看文件夹',
    'search_files': '查找文件', 'search_content': '检索文件内容', 'read_file': '阅读文件', 'file_info': '查看文件信息',
    'find_duplicates': '查找重复文件', 'apply_dedup': '处理重复文件', 'deduplicate': '查重与清理',
    'report_findings': '整理查找结果', 'paper_profiles': '读取论文片段', 'submit_classification': '保存分类方案'}
# Internal identifiers: plans, tasks, catalogs, receipts, scans, file and evidence handles.
# ASCII look-arounds, not \b: next to a Chinese character \b does not separate words.
_A = r'(?<![A-Za-z0-9_])'
_Z = r'(?![A-Za-z0-9_])'
_IDS = re.compile(rf'(?:file:F|evidence:E)[A-Za-z0-9_]+|{_A}[XTPCR][0-9a-f]{{10,12}}{_Z}|{_A}scan_id=[A-Za-z0-9_]+|{_A}[0-9a-f]{{12}}{_Z}')
_PLAIN = [('本目标尚无已执行的文件计划', '还没有执行'), ('已准备的计划尚未执行完成', '准备好的操作还没有执行完'),
          ('完成条件未满足', '没有完成'), ('计划 部分执行后未完成', '只执行了一部分'), ('部分执行后未完成', '只执行了一部分')]


def plain(text):
    """A local reason as the user reads it: the first sentence (the rest tells the model
    what to do next), tool names in words, internal identifiers left out. The original
    text stays in the details for developers."""
    text = re.split(r'(?<=[。；;])', text.strip(), maxsplit=1)[0]
    text = re.sub(r'[ 　]+', ' ', _IDS.sub('', text))
    for name, words in TOOL_NAMES.items():
        text = re.sub(_A + name + _Z, words, text)
    for before, after in _PLAIN:
        text = text.replace(before, after)
    return text.strip().rstrip('。；;：:，,、 ')


NOTE_BY = {'model': '模型说明', 'choice': '你的选择', 'local': '核验依据'}


def _unmet(g):
    """(local reasons, the model's declared reasons) for one unmet part."""
    local = [_clause(c['detail'], 120) for c in g['conditions'] if not c['passed'] and c['detail']]
    local += [_clause(n['text'], 120) for n in g.get('notes', []) if n['by'] == 'local']
    model = [_clause(n['text'], 120) for n in g.get('notes', []) if n['by'] == 'model']
    return local, model


def incomplete_reason(task, reply=False):
    """尚未全部完成。原因：one line per unmet part, with its local reason, else the model's.

    With a reply, the model has spoken in its own words below; a part that only it
    declared unfinished is then listed by name, its declared reason kept in details,
    so one reason is not said twice. Local reasons always stay.
    """
    lines=[]
    for g in task.get('checks',[]):
        if not g['passed']:
            local, model = _unmet(g)
            name = _clause(g['description'], 70)
            if local:
                lines.append(f"- {name}：{'；'.join(_clause(plain(x), 120) for x in local)}")
            elif model and not reply:
                lines.append(f"- {name}（模型说明）：{'；'.join(plain(m) for m in model)}")
            else:
                lines.append(f"- {name}" + ('' if model else '：没有完成'))
    lines += ['- '+_clause(plain(f)) for f in task.get('constraint_failures', [])]
    return '尚未全部完成。原因：\n'+'\n'.join(lines[:5]) if lines else '尚未全部完成。'


def _all_explained(task):
    """The model declared a reason for every unmet part (finish_task.unfinished)."""
    unmet = [g for g in task.get('checks', []) if not g['passed']]
    return bool(unmet) and all(any(n['by'] == 'model' for n in g.get('notes', [])) for g in unmet)


def action_phrases(operations):
    """"已移动 2 个文件" and the like, for the operations given."""
    counts = Counter((o['op'], o.get('kind', '项')) for o in operations)
    actions = []
    verbs = {'move':'已移动', 'rename':'已重命名', 'trash':'已移入回收区', 'make_dir':'已创建'}
    for op in ('move','rename','trash','make_dir'):
        for kind in ('文件','文件夹','项'):
            if counts[(op,kind)]: actions.append(f'{verbs[op]} {counts[(op,kind)]} 个{kind}')
    return actions


def chosen_nothing(task):
    """The note of the option the user chose in this task revision when it left every part out
    (nothing done by choice), else ''."""
    from agent.choices import chosen_scope
    note, skipped = chosen_scope(task or {})
    goals = {g['id'] for g in (task or {}).get('goals', [])}
    return note if note and goals and goals <= skipped else ''


def by_choice_report(chosen, reply=''):
    """No task, and the user picked an option that does nothing: the same words as a task
    settled that way (S5 audit 4.3), so the result does not depend on the model's path."""
    summary = f"已按你的选择保持原样（{chosen['key']}（{chosen['label']}）），没有改动。"
    reply = (reply or '').strip()
    if reply and reply not in summary:
        summary += '\n\n' + (reply if len(reply) <= 1500 else reply[:1500] + '…')
    return dict(version=1, state='answered', result='by_choice', display='chosen', summary=summary, sections=[])


def stopped_report(task, operations, state):
    """The user cancelled or paused the request (routing redesign S3, 5.3). What ran stays
    done and undoable; what did not is named, and nothing is called complete."""
    done = action_phrases(operations)
    if state == 'cancelled':
        head = '已取消' + (f"；此前{'；'.join(done)}，可撤销" if done else '，没有执行任何文件操作')
    else:
        head = '已暂停' + (f"；此前{'；'.join(done)}，可撤销" if done else '，没有执行任何文件操作') + '。需要继续时告诉我'
    touched = {g for o in operations for g in (o.get('goal_ids') or [])}
    left = [g['description'] for g in (task or {}).get('goals', []) if g['id'] not in touched]
    summary = head + ('。未处理：' + '；'.join(_clause(d, 60) for d in left[:5]) if left and task else '') + '。'
    return dict(version=1, state=state, summary=summary.replace('。。', '。'), sections=[])


def build_report(task, model_summary, records, reply=''):
    state = task['status']
    operations = task.get('operations', [])
    actions = action_phrases(operations)
    sections = []
    lines = ['| 目标 | 核验结果 |', '| --- | --- |']
    for check in task['checks']:
        result = '未满足' if not check['passed'] else '需要人工判断' if check['needs_review'] else '通过'
        lines.append(f"| {label(check['description'])} | {result} |")
        for c in check['conditions']:
            if c['detail']: lines.append(f"| 核验依据 | {label(c['detail'])} |")
        for n in check.get('notes', []):
            lines.append(f"| {NOTE_BY[n['by']]} | {label(n['text'])} |")
    # audience 'dev': internal detail, shown on the page only in developer mode.
    sections.append(dict(title='目标与核验依据', markdown='\n'.join(lines), audience='dev'))
    for record in records:
        title = '最终统计' if record['tool']=='inventory_files' else '文件与证据'
        if record['tool']=='inventory_files' and record.get('samples',1)>1: title += f"（同一请求 {record['samples']} 次，展示最后一次）"
        sections.append(dict(title=title, markdown=record['text']))
    errors = [o for o in task['observations'] if o['status']!='ok']
    if errors:
        sections.append(dict(title=f'过程提示（{len(errors)} 次失败或拦截）', markdown='\n\n'.join(
            f"{label(o['tool'])}：{label(o.get('summary') or o.get('error_code') or '调用未完成')}" for o in errors), audience='dev'))
    # Recovered blockers are process history: kept in details, never in the answer.
    recovered = [b for b in task.get('blockers', []) if b.get('status', 'open') != 'open']
    if recovered:
        sections.append(dict(title=f'已恢复的阻塞（{len(recovered)} 项）', markdown='\n\n'.join(
            f"{label(b.get('source') or b.get('tool') or '计划')}：{label(b['message'])}（{label(b.get('resolved_by') or '已恢复')}）"
            for b in recovered), audience='dev'))
    declared = [(g['description'], m) for g in task['checks'] if not g['passed'] for m in _unmet(g)[1]]
    if declared:
        sections.append(dict(title='未完成原因', markdown='\n'.join(
            f"- {label(_clause(d, 70))}：{label(plain(m))}" for d, m in declared)))
    if model_summary:
        sections.append(dict(title='补充说明', markdown=model_summary))
    if operations:
        summary = '；'.join(actions)+'。'
        if state=='completed': summary += '\n\n已完成并核对无误，可撤销本轮操作。'
        elif state=='partial': summary += '\n\n'+incomplete_reason(task, bool(reply.strip()))
        elif state=='needs_review': summary += '\n\n文件操作已执行，部分结果需要你看一下。'
        else: summary += '\n\n'+STATE_TEXT.get(state,state)+'。'
    else:
        # Preserve actual requested data, once. Do not append a second model
        # paraphrase of a locally generated table.
        if records:
            summary = '\n\n'.join(r['text'] for r in records)
            if state=='partial': summary = incomplete_reason(task, bool(reply.strip()))
        elif state=='completed' and chosen_nothing(task):
            # The user chose to change nothing: say that, not that the request was met (S5 audit 4.3).
            summary = f"已按你的选择保持原样（{chosen_nothing(task)}），没有改动。"
        elif state=='completed':
            # Nothing to change: say so; the checks behind it are in the developer details.
            summary = '已完成：文件已经符合要求，没有改动。' if task.get('allowed_effects') else '已完成。'
        elif state=='partial': summary = incomplete_reason(task, bool(reply.strip()))
        elif state=='needs_review': summary = model_summary or ''
        else: summary = model_summary or STATE_TEXT.get(state,state)+'。'
        # No file changed: the cited fragments are the review, so no "待核实" line (display_state).
        if state=='waiting_input' and task.get('allowed_effects') and not task.get('operation_history'):
            summary += '\n\n本轮未执行任何文件操作，等待你的决定。'
    # What the user asked to be told, stated by the model; local facts lead.
    # Main answer: local facts, unmet parts one line each, then the model's reply.
    reply = reply.strip()
    if reply and reply not in summary:
        summary += '\n\n' + (reply if len(reply) <= 1500 else reply[:1500] + '…')
    if state in {'partial', 'waiting_input'} and model_summary and not reply and model_summary[:1200] not in summary:
        summary += '\n\n说明：'+_clause(model_summary, 1200)+'。'
    # Unresolved blockers (control rejections, plan failures and failed writes)
    # are current reasons. A failed write stays visible even when the task is
    # otherwise complete, until the same call or object later succeeds. When the
    # model declared a reason for every unmet part, they are process detail.
    from tools.registry import TOOLS
    current = open_blockers(task)
    shown = [b for b in current if state != 'completed']
    if shown:
        text = '；'.join(_clause(b['message'], 300) for b in shown[-3:]) + '。'
        sections.append(dict(title='阻塞原因', markdown=text, audience='dev'))
        if not _all_explained(task):
            reasons = list(dict.fromkeys(plain(b['message']) for b in shown[-3:]))
            summary += '\n\n没能完成：' + '；'.join(r for r in reasons if r) + '。'
    failed = [b for b in current if b not in shown and TOOLS.get(b.get('source')) and TOOLS[b['source']].writes]
    if failed:
        sections.append(dict(title='未成功的文件操作', markdown='；'.join(
            label(b['source']+'：'+_clause(b['message'])) for b in failed[:2]) + '。', audience='dev'))
        reasons='；'.join(dict.fromkeys(plain(b['message']) for b in failed[:2]))
        summary+='\n\n有文件操作没有成功：'+reasons+'。'  # always in the answer
    if task['unresolved_questions']:
        summary += '\n\n待确认：'+'；'.join(task['unresolved_questions'])
    report = dict(version=1, state=state, display=display_state(state, task), summary=summary, sections=sections)
    if state == 'completed' and not operations and chosen_nothing(task):
        report.update(result='by_choice', display='chosen')
    policy = task.get('policy', {}).get('output', {})
    if policy.get('format') == 'json':
        report['summary'] = json.dumps(dict(state=state, summary=summary, checks=task['checks'],
            results=records, operations=operations, model_interpretation=model_summary), ensure_ascii=False, indent=2)
    elif policy.get('detail') == 'full':
        extra = [s for s in sections if s['markdown'] and s['markdown'] not in summary]
        report['summary'] = summary + ''.join('\n\n'+s['title']+'\n\n'+s['markdown'] for s in extra)
    if policy.get('format') == 'text':
        # Rendering transformation only; no inference about business meaning.
        import re
        report['summary'] = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', report['summary'])
        report['summary'] = report['summary'].replace('**', '').replace('`', '')
    return report


def legacy_report(final, runtime):
    """Read-only presentation of old structured reports. Never upgrades state."""
    marker='模型说明（不替代上述核验）：\n'
    if runtime.get('task')!='structured' or marker not in final: return None
    summary=final.rsplit(marker,1)[1].strip()
    if runtime.get('task_status')!='completed':
        summary += '\n\n历史任务缺少完整核验依据，原有待核实状态保留。'
    return dict(version=1, state=runtime.get('task_status'), summary=summary,
                sections=[dict(title='原始核验与过程记录',markdown=final)])
