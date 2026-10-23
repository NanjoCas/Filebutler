"""Authoritative stop reasons, generated locally without another model request."""
import json
import os
from collections import Counter


def stop_reason_text(reason, cfg, llm, error=None):
    if reason=='cost_budget':return f'本轮费用 ¥{llm.cost:.5f} 已超过上限 ¥{cfg["cost_budget"]:.2f}'
    if reason=='token_budget':return f'本轮已使用 {llm.total_tokens} tokens，超过上限 {cfg["token_budget"]}'
    if reason=='max_steps':return f'已达到本轮执行轮数上限 {cfg.get("max_steps",15)}'
    if reason=='repair_limit':return f'同类失败在没有文件执行或新证据进展时累计达到上限 {cfg.get("task_repair_limit",6)}；已停止继续消耗模型请求'
    if reason=='repeated_call':return '相同工具和参数连续调用 3 次，触发重复调用保护'
    if isinstance(error,KeyboardInterrupt):return '运行被用户中断'
    if getattr(error,'code',None)=='MISSING_API_KEY':return f'环境变量 {error.variable} 未设置，模型服务尚未配置'
    kind=type(error).__name__ if error else ''
    known={'APIConnectionError':'无法连接模型服务，请检查网络或服务地址',
           'APITimeoutError':'模型请求超时，已用尽配置的重试次数',
           'RateLimitError':'模型服务限流，已用尽配置的重试次数',
           'AuthenticationError':'模型服务身份验证失败，请检查 API 凭据',
           'PermissionDeniedError':'模型服务拒绝访问，请检查账号权限',
           'BadRequestError':'模型服务拒绝了请求参数',
           'InternalServerError':'模型服务内部错误，重试后仍未成功'}
    # The exception class is developer detail (stop_report puts it in a dev section).
    if kind in known:return known[kind]
    if isinstance(error,OSError) and error.errno:return f'本地文件操作失败：{os.strerror(error.errno)}'
    if error:return '程序内部出错，后续步骤已停止'
    return {'unverified_answer':'未形成可核验的任务记录','operation_error':'文件操作未成功，请核对执行记录'}.get(reason,reason)


def stop_report(reason, cfg, llm, runtime=None, oplog=None, error=None, stage=None):
    cause=stop_reason_text(reason,cfg,llm,error)
    if error and stage:cause=stage+'失败：'+cause
    records=[]
    journal_error=False
    try:
        if oplog and oplog.path.exists():
            for line in oplog.path.read_text().splitlines():
                if line.strip():records.append(json.loads(line))
    except (OSError,ValueError):journal_error=True
    counts=Counter(r['op'] for r in records)
    effects='；'.join(f'{verb} {counts[op]} 项' for op,verb in
                     [('move','已记录移动'),('rename','已记录改名'),('trash','已记录回收'),('make_dir','已记录创建目录')]
                     if counts[op])
    if effects:effects+='；已执行的操作保留，可按本轮日志撤销。'
    else:effects='本轮没有已记录的文件写入操作。'
    if journal_error:effects='操作日志读取不完整，无法确认全部已执行范围；请核对文件状态。'
    task=getattr(runtime,'current',None)
    if task and any(r['collection']=='classification_plans' for o in task['observations']
                    if o['status']=='ok' for r in o.get('references',[])) and not records:
        effects='已保存分类方案，尚未执行文件移动。'+effects
    summary=('任务失败。原因：' if error and not isinstance(error,KeyboardInterrupt) else '任务已中止。原因：')+cause+'。\n\n'+effects
    sections=[]
    if runtime:
        sections=[dict(title='已取得的结果',markdown=r['text']) for r in getattr(runtime,'output_records',[])]
    if error and not isinstance(error,KeyboardInterrupt):
        # Class name only: an exception's text can carry credentials (e.g. a request URL with a key).
        sections.append(dict(title='错误类型',markdown=type(error).__name__,audience='dev'))
    if reason=='repair_limit' and getattr(runtime,'last_failure',None):
        from agent.reporting import plain
        summary += '\n\n最近失败原因：'+plain(runtime.last_failure)
        sections.append(dict(title='最近失败原因',markdown=runtime.last_failure,audience='dev'))
    return dict(version=1,state='failed' if error and not isinstance(error,KeyboardInterrupt) else 'interrupted',
                reason=reason,summary=summary,sections=sections)


def close_messages(messages, final):
    """Close unfinished call/result pairs before persisting a failed turn."""
    pending={}
    for m in messages:
        for tc in m.get('tool_calls',[]):pending[tc['id']]=tc
        if m.get('role')=='tool':pending.pop(m.get('tool_call_id'),None)
    for cid in pending:
        messages.append(dict(role='tool',tool_call_id=cid,content=json.dumps(dict(status='error',
            error_code='RUN_INTERRUPTED',text='运行中止，调用未取得完整结果；请以本地操作记录为准'),ensure_ascii=False)))
    if not messages or messages[-1].get('role')!='assistant' or messages[-1].get('tool_calls') or messages[-1].get('content')!=final:
        messages.append(dict(role='assistant',content=final))


def save_outcome(runtime, reason, final, cfg, llm, *, error=None):
    if not runtime:return None
    data=dict(run_id=runtime.run_id,stop_reason=reason,summary=final,cost=llm.cost,tokens=llm.total_tokens,
              task_id=runtime.current['id'] if runtime.current else None,error_type=type(error).__name__ if error else None,
              purpose='inspection' if runtime.inspected_run and not runtime.current and not runtime.operations else 'action',
              pending_choice=getattr(runtime,'pending_choice',None))
    runtime.store.save_outcome(data)
    return data
