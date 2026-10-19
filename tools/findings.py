"""Finish a read-only topic task from validated local evidence, without another API call."""
import re
from typing import Literal

from pydantic import BaseModel, Field
from tools.registry import ToolError, tool
from agent.artifacts import label


class Finding(BaseModel):
    file_id: str = Field(description='工具原样返回的 F 标识（F 加 10 位字符），不填写文件路径')
    evidence_ids: list[str] = Field(min_length=1,max_length=5,description='支持这条判断的完整 E 标识（E 加 10 位字符），逐字复制，不删 E 或末尾字符')
    relation: Literal['method','survey','mention','uncertain'] = Field(description='方法相关/综述/仅提及/尚不确定')
    reason: str = Field(min_length=1,max_length=240,description='仅依据所引用片段说明与用户主题的关系；不作全文/字节等价判断')


class FindingsArgs(BaseModel):
    findings: list[Finding] = Field(max_length=10)
    note: str = Field('',max_length=200,description='必要的任务说明或用户要求的固定回复标记；不得补充未核实结论')


def unsupported_equivalence(text):
    # Enforce a narrow, explicit fact boundary, not a general semantic verifier.
    phrases = r'(?:全文|字节|内容|文字|文本).{0,8}(?:完全相同|一致|相同|等价)|一模一样|确定重复|重复副本'
    for m in re.finditer(phrases,text):
        prefix = text[max(0,m.start()-12):m.start()]
        if not re.search(r'不能|无法|未能|不足|不证明|不代表|未核实|尚未|是否',prefix):
            return True
    return False


@tool('report_findings',
      '提交论文主题查找的最终结果并结束本轮。每项提供 file_id、evidence_ids、relation、reason；本地校验证据版本并生成带真实文件名的表格。'
      '可直接依据 search_content 的证据卡片判断；不必为了每项都再次 read_file。只提交已获片段支持的判断；未知候选用 uncertain，不能因低分排除。'
      '全文/字节相同只能由查重工具确认，本工具不接受此类判断。用户固定回复要求放 note。无需随后再生成总结。', FindingsArgs)
def report_findings(a, sb):
    artifacts = getattr(sb,'artifacts',None)
    runtime = getattr(sb,'runtime',None)
    if not artifacts or not runtime:
        raise ToolError('当前任务未启用证据登记')
    if unsupported_equivalence(a.note) or any(unsupported_equivalence(f.reason) for f in a.findings):
        raise ToolError('片段证据不能支持全文/字节等价结论；删除该判断，不要为普通查找额外启动去重。')
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
    lines=['| 文件 | 与主题的关系 | 片段依据 |','| --- | --- | --- |']
    kinds={'method':'方法相关','survey':'综述入口','mention':'片段中提及','uncertain':'尚未核实相关性'}
    seen=set()
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
        reason = '当前片段不足以确认与主题的关系，尚不能据此排除。' if finding.relation == 'uncertain' else finding.reason
        lines.append(f'| {link} | {kinds[finding.relation]} | {label(reason)} {sources} |')
    if not a.findings:
        lines=['当前证据不足以确认相关论文。未命中或低分不能证明不相关。']
    if a.note: lines+=['',label(a.note)]
    lines+=['','范围：本次检索仅覆盖 PDF 前两页；以上判断基于已取得的片段，不能保证穷尽，也不证明全文或文件字节相同。']
    runtime.final_answer='\n'.join(lines)
    return 'OK: 已校验证据并生成最终表格，本轮结束。'
