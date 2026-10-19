"""Bound ordinary topic searches and finish deterministic tasks locally.

Explicit comprehensive/mixed tasks retain the general loop. This is not a
semantic confidence estimator: no BM25 threshold is used to declare relevance.
"""
import json
import re


def classification_followup(instruction):
    """Only unambiguous plan execution phrases can bypass the model."""
    value=instruction.strip().rstrip('。！! ')
    return bool(re.fullmatch(
        r'(?:请|帮我)?(?:把(?:这|上述|上面|刚才)(?:\s*\d+\s*类|些分类|个分类方案|个方案|个计划)?(?:落成实际文件夹|落成文件夹)|'
        r'(?:执行|应用)(?:刚才的|上述|上面的|这份|这个|该|之前的)?(?:分类方案|分类计划))',value))


class LocalRuntime:
    def __init__(self, instruction, cfg):
        self.instruction=instruction
        write=r'移动|归类|分类|整理|重命名|删除|回收|写入|修改|创建'
        dedup=bool(re.search(r'去重|查重|重复|副本',instruction))
        self.allow_apply=dedup and bool(re.search(r'清理|回收|删除|删掉|只留一份|保留一份|每组.*保留.*一|相同.*规则.*再',instruction))
        if re.search(r'(?:不要|不许|勿|不进行).{0,3}(?:清理|回收|删除|移动)|仅查重|只查重|只报告|先报告|先不.*(?:清理|处理)',instruction):
            self.allow_apply=False
        near=bool(re.search(r'近似|相似|版本',instruction)) and not bool(re.search(r'版本先保留|不合并|不按相似度合并|不同版本.*保留',instruction))
        topic=bool(re.search(r'论文|主题|REAL.FND|paper',instruction,re.I)) and bool(re.search(r'找|查|检索|搜索',instruction))
        comprehensive=bool(re.search(r'穷尽|找全|所有|全部|逐篇|阅读全文|详细解析|文件名',instruction))
        self.task='dedup_review' if dedup and near else 'dedup' if dedup else 'topic' if topic and not re.search(write,instruction) and not comprehensive else 'general'
        if dedup and re.search(r'分类|归类|重命名|改名|移动到|移到|写报告|保存报告|生成报告',instruction):
            self.task='general'
        if self.task=='topic' and cfg.get('retrieval') is False:
            self.task='general'
        self.allow_classification_apply=classification_followup(instruction) or bool(
            re.search(r'分类|方案|计划',instruction) and re.search(r'执行|应用|移动|移入|落成',instruction)
            and not re.search(r'不要|先不|不许|不准|不执行|不应用|不移动|不移入|不落成|只看|只报告|仅.*(?:查看|报告)',instruction))
        if classification_followup(instruction):
            self.task='classification_apply'
        elif (not dedup and re.search(r'文献|论文|PDF|paper',instruction,re.I)
              and re.search(r'核心方法|按方法|按照方法',instruction) and re.search(r'分类|归类',instruction)
              and not re.search(r'移动|移入|删除|回收|改名|重命名|生成.*报告|保存.*报告|阅读全文|逐篇通读|穷尽|方法章节|逐篇核实',instruction)):
            self.task='classification'
        self.classification_stats={}
        self.query_limit=max(1,int(cfg.get('topic_query_limit',2)))
        self.read_limit=max(1,int(cfg.get('topic_read_limit',5)))
        self.classification_read_limit=max(1,int(cfg.get('classification_read_limit',6)))
        self.queries=0
        self.reads=0
        self.blocked=0
        self.no_hits=False
        self.had_error=False
        self.closing=False
        self.final_answer=None
        self.dedup_scan={}
        self.format_retry=False
        self.failed_reports=0
        self.stop_reason="done"

    def schemas(self, schemas):
        if self.task in {'classification','classification_apply'}:
            allowed=({'paper_profiles','submit_classification'} if self.closing else {'paper_profiles','read_file','submit_classification'}) if self.task=='classification' else {'apply_classification'}
            return [s for s in schemas if s['function']['name'] in allowed]
        if self.task=='topic':
            allowed={'report_findings'} if self.closing else {'search_content','read_file','report_findings'}
            return [s for s in schemas if s['function']['name'] in allowed]
        if self.task.startswith('dedup'):
            allowed={'find_duplicates','read_file','file_info'}
            if self.allow_apply: allowed|={'apply_dedup','deduplicate'}
            if self.task=='dedup_review': allowed.discard('deduplicate')
            if self.had_error: allowed|={'list_dir','search_files'}
            return [s for s in schemas if s['function']['name'] in allowed]
        return [s for s in schemas if s['function']['name'] != 'report_findings'
                and (self.allow_apply or s['function']['name'] != 'deduplicate')
                and (self.allow_classification_apply or s['function']['name'] != 'apply_classification')]

    def direct_call(self,sb):
        if self.task=='classification_apply':
            return 'apply_classification',{'plan_id':'latest'}

    def prepare(self,name,raw,sb):
        try: args=json.loads(raw)
        except (ValueError,TypeError): return raw,None
        if not isinstance(args,dict): return raw,None
        if self.final_answer:
            return raw,'ERROR: 结果已经提交，此调用未执行。'
        if self.task in {'classification','classification_apply'}:
            allowed={'paper_profiles','read_file','submit_classification'} if self.task=='classification' else {'apply_classification'}
            if name not in allowed:
                self.blocked+=1
                return raw,'ERROR: 本轮使用结构化分类计划，不逐文件列目录、生成路径或移动。'
            if self.task=='classification' and name=='read_file' and (self.closing or self.reads>=self.classification_read_limit):
                self.blocked+=1
                return raw,'ERROR: 已达到分类补读预算；用已提供片段提交分类计划，证据不足项标 uncertain=true，不作猜测。'
        if name=='apply_classification' and not self.allow_classification_apply:
            return raw,'ERROR: 用户本轮未授权执行分类计划。'
        if self.task=='topic':
            if name not in {'search_content','read_file','report_findings'}:
                self.blocked+=1
                return raw,'ERROR: 本轮是普通主题查找，只接受检索、必要的片段阅读或 report_findings。'
            if name=='search_content':
                if self.closing or self.queries>=self.query_limit:
                    self.blocked+=1
                    return raw,'ERROR: 已达到普通查找的查询预算；使用现有证据 report_findings，未核实项如实说明，不作排除。'
                if self.queries and not (self.no_hits or self.had_error or args.get('retry_reason')=='insufficient_evidence'):
                    self.blocked+=1
                    return raw,'ERROR: 补充查询需说明 retry_reason=insufficient_evidence；已有证据足够时直接 report_findings。'
                try: top_k=int(args.get('top_k',5))
                except (ValueError,TypeError): return raw,None
                args.update(top_k=min(5,max(1,top_k)),view='evidence',page=1)
                self.queries+=1
            elif name=='read_file':
                if self.closing or self.reads>=self.read_limit:
                    self.blocked+=1;self.closing=True
                    return raw,'ERROR: 已达到片段阅读预算；用已有证据 report_findings，证据不足可报告 uncertain。'
                self.reads+=1
                if self.reads>=self.read_limit:self.closing=True
        elif self.task.startswith('dedup'):
            allowed={'find_duplicates','read_file','file_info'}
            if self.allow_apply: allowed|={'apply_dedup','deduplicate'}
            if self.task=='dedup_review': allowed.discard('deduplicate')
            if self.had_error: allowed|={'list_dir','search_files'}
            if name not in allowed:
                self.blocked+=1
                return raw,'ERROR: 当前查重任务未开放此工具；按本轮范围和授权执行。'
            if name=='list_dir' and not self.had_error:
                self.blocked+=1
                return raw,'ERROR: 范围由查重工具本地校验，请直接 find_duplicates 或 deduplicate；无需先列目录。'
            if name in {'apply_dedup','deduplicate','trash','move','rename'} and not self.allow_apply:
                self.blocked+=1
                return raw,'ERROR: 用户本轮仅要求查重/报告，未授权清理。'
            if name in {'trash','move','rename'}:
                self.blocked+=1
                return raw,'ERROR: 批量去重请使用本地计划或 deduplicate，不逐文件操作。'
        return json.dumps(args,ensure_ascii=False),None

    def after(self,name,result,sb):
        self.had_error=result.startswith('ERROR')
        if self.task=='classification' and name=='read_file' and result.startswith('OK'):
            self.reads+=1
            if self.reads>=self.classification_read_limit:self.closing=True
        if self.task=='classification_apply' and self.had_error and not self.final_answer:
            self.final_answer='分类计划未执行：'+result.removeprefix('ERROR: ')
            self.stop_reason='operation_error'
        if name=='search_content': self.no_hits='无命中' in result
        if name=='report_findings' and self.had_error:
            self.failed_reports+=1
            if self.failed_reports>=2:
                self.final_answer='本轮证据校验未通过，尚未发布可核实的主题结论。未核实不代表不相关。'
                self.stop_reason='unverified_answer'
        if self.task=='dedup' and name in {'apply_dedup','deduplicate'} and '用户拒绝' in result:
            self.final_answer='你已拒绝本次清理，未移动任何文件。'
        if self.task=='dedup' and name in {'apply_dedup','deduplicate'} and result.startswith('OK'):
            artifacts=getattr(sb,'artifacts',None)
            if artifacts:
                receipts=[rid for rid,r in artifacts.data['receipts'].items() if r['run_id']==artifacts.run_id]
                if receipts:self.final_answer=artifacts.receipt_markdown(receipts[-1])
                elif '没有需要' in result:self.final_answer='本次扫描未发现需要回收的确定重复副本。未执行近似版本合并，未改动文件。'

    def stats(self):
        return dict(task=self.task,queries=self.queries,reads=self.reads,blocked_calls=self.blocked,dedup_scan=self.dedup_scan,
                    classification=self.classification_stats)
