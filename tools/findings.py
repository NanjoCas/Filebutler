"""Finish a read-only topic task from validated local evidence, without another API call."""
from typing import Literal

from pydantic import BaseModel, Field
from tools.registry import ToolError, tool
from tools.scopes import finding_paths
from agent.artifacts import label


class Finding(BaseModel):
    file_id: str = Field(description='工具原样返回的 F 标识（F 加 10 位字符），不填写文件路径')
    evidence_ids: list[str] = Field(min_length=1,max_length=5,description='支持这条判断的完整 E 标识（E 加 10 位字符），逐字复制，不删 E 或末尾字符')
    relation: str = Field(min_length=1, max_length=60, description='由任务决定的关系标签；method/survey/mention/uncertain 仍兼容')
    reason: str = Field(min_length=1,max_length=240,description='仅依据所引用片段说明与用户主题的关系；不作全文/字节等价判断')
    claim_type: Literal['interpretation', 'byte_identity'] = Field('interpretation',
        description='interpretation 是待复核的模型解释；byte_identity 必须提供比较文件并由本地计算，不能靠措辞认定')
    related_file_ids: list[str] = Field(default_factory=list, max_length=20)


class FindingsArgs(BaseModel):
    findings: list[Finding] = Field(max_length=500)
    note: str = Field('',max_length=200,description='必要的任务说明或用户要求的固定回复标记；不得补充未核实结论')


@tool('report_findings',
      '提交有原文依据的任务判断。每项提供 file_id、evidence_ids、任务关系标签 relation 和 reason；本地校验版本并生成文件链接表格。'
      '已有片段足够时不重复读取，未知候选用 uncertain，不能因检索低分排除。解释标为待复核；'
      'byte_identity 需 related_file_ids，由本地实际比较字节。片段不能证明全文等价。V2 中仅完成子步骤，整项任务仍须 finish_task。', FindingsArgs, read_scope=finding_paths)
def report_findings(a, sb):
    artifacts = getattr(sb,'artifacts',None)
    runtime = getattr(sb,'runtime',None)
    if not artifacts or not runtime:
        raise ToolError('当前任务未启用证据登记')
    # Return exact, already-issued IDs for every bad pairing in one reply.
    # Do not guess/repair a model typo, or silently cite a different fragment.
    issues=[]
    for finding in a.findings:
        finding.file_id=finding.file_id.removeprefix('file:')
        finding.evidence_ids=[eid.removeprefix('evidence:') for eid in finding.evidence_ids]
        artifacts.resolve(finding.file_id,allow_trash=False)
        bad=[eid for eid in finding.evidence_ids
             if artifacts.data['evidence'].get(eid,{}).get('file_id') != finding.file_id]
        if bad:
            valid=[]
            for eid,e in artifacts.data['evidence'].items():
                if e['file_id']==finding.file_id and e['kind']=='fragment':
                    try: artifacts.resolve_evidence(eid)
                    except ValueError: continue
                    valid.append(f'evidence:{eid}')
            issues.append(f"file:{finding.file_id} 的证据 {','.join(bad)} 不存在或不属于该文件；"
                          f"本文件已提供的有效片段：{'、'.join(valid[-5:]) or '无'}。")
    if issues:
        raise ToolError(' '.join(issues)+' 请原样复制完整 E 标识，仅选支持判断的片段；已有片段无需重新读取。')
    lines=['| 文件 | 任务判断 | 片段依据 |','| --- | --- | --- |']
    kinds={'method':'方法相关','survey':'综述入口','mention':'片段中提及','uncertain':'尚未核实相关性'}
    seen=set()
    scopes=set()
    for finding in a.findings:
        if finding.file_id in seen:
            raise ToolError('相同文件请合并为一行')
        seen.add(finding.file_id)
        artifacts.resolve(finding.file_id,allow_trash=False)
        evs=[artifacts.resolve_evidence(eid) for eid in finding.evidence_ids]
        if any(e['kind'] != 'fragment' for e in evs):
            raise ToolError('主题判断需要原文片段证据，不能用文件哈希或操作回执替代正文')
        if any(e['file_id'] != finding.file_id for e in evs):
            raise ToolError('证据标识不属于这份文件')
        link=artifacts.file_link(finding.file_id,evs[0].get('page'))
        sources=' '.join(f'[片段{i+1}](evidence:{eid})' for i,eid in enumerate(finding.evidence_ids))
        scopes.update(e.get('scope') or ('指定页原文片段' if e.get('page') else '指定原文片段') for e in evs)
        reason = '当前片段不足以确认与主题的关系，尚不能据此排除。' if finding.relation == 'uncertain' else finding.reason
        if finding.claim_type == 'byte_identity':
            from agent.artifacts import digest
            if not finding.related_file_ids: raise ToolError('字节一致判断必须提供 related_file_ids')
            original, _ = artifacts.resolve(finding.file_id, allow_trash=False)
            expected = digest(original)
            for other in finding.related_file_ids:
                target, _ = artifacts.resolve(other.removeprefix('file:'), allow_trash=False)
                if target == original or digest(target) != expected:
                    raise ToolError('本地字节一致核验未通过')
                artifacts.resolve(other.removeprefix('file:'), allow_trash=False)
            artifacts.resolve(finding.file_id, allow_trash=False)
            reason = '本地 SHA256 比对通过：'+str(len(finding.related_file_ids))+' 个其他文件与本文件字节一致。'
        else:
            reason = '模型判断：'+reason
        lines.append(f'| {link} | {label(kinds.get(finding.relation, finding.relation))} | {label(reason)} {sources} |')
    if not a.findings:
        lines=['当前证据不足以确认相关论文。未命中或低分不能证明不相关。']
    if a.note: lines+=['',label(a.note)]
    lines+=['','证据范围：'+'；'.join(sorted(scopes))+'。判断只依据所引片段，不代表已查遍全部文件。']
    runtime.final_answer='\n'.join(lines)
    return 'OK: 已校验证据并生成结果表格。'
