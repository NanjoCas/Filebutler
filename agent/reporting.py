"""One user-facing answer; audit, statistics and verification stay in details."""
from collections import Counter
import json

from agent.artifacts import label
from agent.task import open_blockers


STATE_TEXT = {'completed':'本地核验通过', 'partial':'尚未全部完成', 'needs_review':'部分结果待核实',
              'waiting_input':'等待补充信息', 'cancelled':'任务已取消'}


def incomplete_reason(task):
    reasons=[]
    for g in task.get('checks',[]):
        if not g['passed']:
            details='；'.join(c['detail'] for c in g['conditions'] if not c['passed'] and c['detail'])
            reasons.append(f"{g['description'][:70]}：{details[:180] or '完成条件未满足'}")
    reasons += task.get('constraint_failures', [])
    errors=[o for o in task.get('observations',[]) if o['status']!='ok']
    if errors and reasons:
        reasons.append(errors[-1]['tool']+'：'+(errors[-1].get('summary') or errors[-1].get('error_code') or '调用未完成')[:180])
    return '；'.join(reasons[:3]) or '尚缺少可核验的完成依据'


def build_report(task, model_summary, records):
    state = task['status']
    operations = task.get('operations', [])
    counts = Counter((o['op'], o.get('kind', '项')) for o in operations)
    actions = []
    verbs = {'move':'已移动', 'rename':'已重命名', 'trash':'已移入回收区', 'make_dir':'已创建'}
    for op in ('move','rename','trash','make_dir'):
        for kind in ('文件','文件夹','项'):
            if counts[(op,kind)]: actions.append(f'{verbs[op]} {counts[(op,kind)]} 个{kind}')
    sections = []
    lines = ['| 目标 | 核验结果 |', '| --- | --- |']
    for check in task['checks']:
        result = '未满足' if not check['passed'] else '需要人工判断' if check['needs_review'] else '通过'
        lines.append(f"| {label(check['description'])} | {result} |")
        for c in check['conditions']:
            if c['detail']: lines.append(f"| 核验依据 | {label(c['detail'])} |")
    sections.append(dict(title='目标与核验依据', markdown='\n'.join(lines)))
    for record in records:
        title = '最终统计' if record['tool']=='inventory_files' else '文件与证据'
        if record['tool']=='inventory_files' and record.get('samples',1)>1: title += f"（同一请求 {record['samples']} 次，展示最后一次）"
        sections.append(dict(title=title, markdown=record['text']))
    errors = [o for o in task['observations'] if o['status']!='ok']
    if errors:
        sections.append(dict(title=f'过程提示（{len(errors)} 次失败或拦截）', markdown='\n\n'.join(
            f"{label(o['tool'])}：{label(o.get('summary') or o.get('error_code') or '调用未完成')}" for o in errors)))
    # Recovered blockers are process history: kept in details, never in the answer.
    recovered = [b for b in task.get('blockers', []) if b.get('status', 'open') != 'open']
    if recovered:
        sections.append(dict(title=f'已恢复的阻塞（{len(recovered)} 项）', markdown='\n\n'.join(
            f"{label(b.get('source') or b.get('tool') or '计划')}：{label(b['message'])}（{label(b.get('resolved_by') or '已恢复')}）"
            for b in recovered)))
    if model_summary:
        sections.append(dict(title='模型补充说明', markdown=model_summary))
    if operations:
        summary = '；'.join(actions)+'。'
        if state=='completed': summary += '\n\n本地核验通过，可撤销本轮文件操作。'
        elif state=='partial': summary += '\n\n尚未全部完成。原因：'+incomplete_reason(task)+'。'
        elif state=='needs_review': summary += '\n\n文件操作已执行，部分结果待核实；请查看核验详情。'
        else: summary += '\n\n'+STATE_TEXT.get(state,state)+'。'
    else:
        # Preserve actual requested data, once. Do not append a second model
        # paraphrase of a locally generated table.
        if records:
            summary = '\n\n'.join(r['text'] for r in records)
            if state=='partial': summary = '尚未全部完成。原因：'+incomplete_reason(task)+'。'
        elif state=='completed':
            facts = [c['detail'] for g in task['checks'] for c in g['conditions'] if c['detail']]
            summary = '本地核验通过。'+(' '.join(facts) if facts else '')
        elif state=='partial': summary = '尚未全部完成。原因：'+incomplete_reason(task)+'。'
        else: summary = model_summary or STATE_TEXT.get(state,state)+'。'
        if state=='needs_review': summary += '\n\n结果待核实，可在“任务与复核”中查看依据。'
        if state=='waiting_input' and task.get('allowed_effects') and not task.get('operation_history'):
            summary += '\n\n本轮未执行任何文件操作，等待你的决定。'
    if state in {'partial', 'waiting_input'} and model_summary and model_summary[:1200] not in summary:
        summary += '\n\n补充原因（模型说明）：'+model_summary[:1200]
    # Unresolved blockers (control rejections, plan failures and failed writes)
    # are current reasons. A failed write stays visible even when the task is
    # otherwise complete, until the same call or object later succeeds.
    from tools.registry import TOOLS
    current = open_blockers(task)
    shown = [b for b in current if state != 'completed']
    if shown:
        summary += '\n\n阻塞原因：'+'；'.join(b['message'] for b in shown[-3:])
    failed = [b for b in current if b not in shown and TOOLS.get(b.get('source')) and TOOLS[b['source']].writes]
    if failed:
        reasons='；'.join(b['source']+'：'+b['message'][:180] for b in failed[:2])
        if reasons not in summary:summary+='\n\n文件操作未成功。原因：'+reasons+'。'
    if task['unresolved_questions']:
        summary += '\n\n待确认：'+'；'.join(task['unresolved_questions'])
    report = dict(version=1, state=state, summary=summary, sections=sections)
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
