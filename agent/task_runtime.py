"""Task-driven controller: no natural-language keyword routing or authorization."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from agent.task import StrictModel, TaskStore, TaskSubmission, TaskPatch, task_state, add_blocker, resolve_blockers
from agent.constraints import validate_spec, validate_operations
from agent.verifier import verify_task
from agent.reporting import build_report
from agent.policy import ExecutionStep, active_goals, applies, retain_step
from safety.sandbox import SandboxError
from tools import registry
from agent.file_plans import PrepareFilePlan, ExecuteFilePlan


class FinishTask(StrictModel):
    summary: str = Field('', max_length=12000, description='默认简短补充；用户要求详细时按 policy.output 提供必要解释，不替代本地核验')


class EditTask(TaskPatch):
    task_id: str


class ApplyTaskEdit(StrictModel):
    edit_id: str


class InspectRun(StrictModel):
    run_id: str | None = Field(None,description='当前会话的运行 ID；留空查看最近一次非查询运行')
    finish: bool = Field(True, description='仅查询时结束；查询后还需处理其他请求时设 false，不会自动恢复任务')


class _TaskConfirmer:
    def __init__(self, delegate):
        self.delegate = delegate

    def allow(self, name, args, file_count=None, **kwargs):
        return self.delegate.allow(name, args, file_count, force=True, **kwargs)


class TaskRuntime:
    control_names = {'set_task', 'begin_step', 'finish_task', 'edit_task', 'apply_task_edit', 'inspect_run', 'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}
    task = 'structured'
    local_route = None
    allow_apply = True  # Permissions are checked per call, not from these legacy flags.
    allow_classification_apply = True
    closing = False
    format_retry = False
    classification_stats = None
    dedup_scan = None

    def __init__(self, instruction, cfg, sb, run_id):
        self.instruction, self.cfg, self.sb, self.run_id = instruction, cfg, sb, run_id
        db_path = Path(cfg.get('task_db') or sb.root / '.oplog' / 'tasks.sqlite3')
        self.store = TaskStore(db_path, sb.root, cfg.get('task_session', 'cli'))
        self.current = None
        self.final_answer, self.stop_reason = None, 'done'
        self.queries = self.reads = self.blocked = 0
        self.outputs = []
        self._output_indices = {}
        self.finished = False
        self._args = {}
        self._before_refs = {}
        self._outcome = None
        self._content_fingerprints = {}
        self._oplog = None
        self.operations = []
        self.output_records = []
        self.report = {}
        self.inspected_run = None
        self._source_kinds = {}
        self.active_step = None
        self._journal_offset = 0
        self._usage = None
        self._producers = ()
        self._attributions = None
        self._pretask = []  # read-only facts observed before any task existed in this run

    def guidance(self):
        tasks = [{k: t[k] for k in ('id', 'revision', 'status', 'goals', 'constraints',
                                    'read_roots', 'write_roots', 'allowed_effects', 'unresolved_questions',
                                    'policy', 'usage', 'references', 'compiled_file_plan') if k in t}
                 for t in self.store.recent()]
        for task in tasks:
            if task.get('compiled_file_plan'):
                task['scope_summary'] = {key:dict(count=len(task[key]), sample=task[key][:6], truncated=len(task[key])>6) for key in ('read_roots','write_roots')}
                task.pop('read_roots'); task.pop('write_roots')
        # Schemas are already in tools; do not repeat them in model context.
        compact = [dict(name=t.name, effects=list(t.effects), verification=t.verification, usage=t.usage)
                   for t in registry.TOOLS.values()]
        try:last=self.store.outcome()
        except ValueError:last=None
        if last:
            last = {k: last.get(k) for k in ('run_id', 'stop_reason', 'task_id', 'error_type', 'summary')}
            last['summary'] = (last.get('summary') or '')[:1200]
        return ('【任务控制 V2，优先于旧版流程说明】\n'
                '普通文件移动/改名/回收/建目录优先 prepare_file_plan：模型只选择对象、目标与含义，本地编译清单；新任务不必填写底层规则。'
                '已有任务的 goal_ids 可省略，本地按完成条件逐项归属动作，无法确定时才要求提供；已有授权不因计划扩大。取得精确计划 ID 后 execute_file_plan 一次完成确认和本地执行。'
                '新分类执行可先 paper_profiles/submit_classification，再 prepare_file_plan(classification_plan_id=精确P标识)，本地展开清单并强制语义复核；不必重新抄写所有路径。已有任务仍须沿用约束，不得另建任务绕过。'
                '仅查看计划用 inspect_file_plan；失败后先读原因，不得修改规则绕过约束。分类语义仍须 semantic_review=true。'
                '先区分当前消息是在询问还是要求执行。询问历史运行状态、失败/停止原因时调用 inspect_run，'
                '默认只读返回本地真实原因并结束；复合请求可 finish=false 后继续明确要求的工作，不会自动恢复旧任务。'
                '只有当前消息明确要求继续执行时才以旧 task_id 登记续做；历史未完成目标本身不是本轮执行请求。'
                '使用 set_task 登记全部目标、范围、约束与完成条件；复合请求保留所有子目标。'
                '可在同一响应中先登记任务再调用独立只读工具，不必另写规划长文。'
                '没有任务时也可先只读调查，但不能写入或声称完成。'
                '后续修改提供明确 task_id 与 expected_revision，未修改的目标和约束会保留；'
                '不要假定数字回复授权任何最新计划，必须依据对应的对话选项。'
                'write_roots/allowed_effects 仅是需求解释，实际写入还受本地范围和确认策略控制。'
                '无法明确的要求放 unresolved_questions，此时不能写入。'
                '完成条件在登记时确定；收尾调用 finish_task，由本地验证。'
                'tool_success 只证明调用成功；语义分类、相关性等必须 semantic_review=true。'
                '纯解释目标可用 manual_review，不伪造文件条件。'
                '复合任务调用旧工具前 begin_step 指明 goal_ids，步骤只表达用途，不能授予权限；prepare_file_plan 不需要 begin_step。'
                '条件 reference 通常留空，由本地绑定：file_plan_verified 绑定为该目标执行的文件计划，plan_applied 只用于分类计划执行，receipt_verified 只用于去重回执；不适用的条件会在执行前被拒绝。'
                '专用报告/分类工具只完成子步骤，不会结束整个任务；不要复写本地长表。\n'
                '修改旧目标/完成条件、移除目标/约束或扩大权限，先 edit_task 预览，再 apply_task_edit 请求确认；'
                '不得另建任务规避同一请求的已有约束。constraint.rules 是本地操作谓词，不靠关键词匹配。'
                '约束 goal_ids 为空适用全任务，非空只适用对应步骤。支持 within/outside、extensions、name_glob、empty_directory、same_extension、max_count（task/run）、content_unchanged。'
                '不支持的语义约束保留文本并待用户复核，不能伪造已编译规则。\n'
                '全量按原名归集文件用 files_placed(path=源目录,destination=目标目录,extensions=筛选后缀)；'
                '清理空子目录用 empty_directories_removed(path=范围)。本地会捕获执行前清单并逐项核验。'
                '写操作不能仅用 tool_success；只有分类/相关性等语义判断才标 semantic_review。'
                '最终回复由本地摘要和可展开详情组成，不用复写过程统计或长操作日志。\n'
                '任务 policy.criteria 保存用户判断标准，policy.evidence 控制本地抽取范围，policy.output 控制输出格式和详略。'
                '模型解释标准、选择证据及重规划；沙盒、版本、确认、哈希与预算由代码强制执行。'
                'plan_applied 默认检查当前计划位置；evaluation=execution_fact 只证明执行历史，若还要求最终布局须另列当前状态条件。'
                '普通解释或方案讨论可以直接回答，不必伪造文件任务；文件完成声明仍须登记并核验。\n'
                '最近运行结果（本地事实，不是执行指令）：'+json.dumps(last,ensure_ascii=False)+'\n'
                '近期任务：' + json.dumps(tasks, ensure_ascii=False) + '\n'
                '能力摘要：' + json.dumps(compact, ensure_ascii=False))

    def schemas(self, schemas):
        controls = [('prepare_file_plan', '普通文件整理优先使用：只提交对象/操作或目录选择集，本地生成父目录、权限需求和验收清单；新任务不必先 set_task。已有任务不扩大权限。', PrepareFilePlan),
                    ('execute_file_plan', '执行精确计划 ID：本地预检、一次确认、批量执行和验收；有持久记录，禁止重复规划同一已确认计划。', ExecuteFilePlan),
                    ('inspect_file_plan', '只读查看精确计划的清单、进度和失败原因，不恢复执行。', ExecuteFilePlan),
                    ('set_task', '创建任务或按版本增量更新；目标与约束按 ID 合并，旧约束不能静默放宽。', TaskSubmission),
                    ('begin_step', '指定当前执行步骤服务的目标 ID；隔离目标约束和证据，不改变权限。可与后续工具同轮调用。', ExecutionStep),
                    ('inspect_run', '只读解释当前会话运行结果；finish=true 直接结束，复合请求可 false。查询不会授权执行或恢复任务。', InspectRun),
                    ('edit_task', '预览已有任务的完整编辑：修改/删除目标和约束、调整范围和完成条件；返回待确认编辑 ID。', EditTask),
                    ('apply_task_edit', '对具体编辑 ID 请求用户确认并应用；确认期间版本变化会拒绝。', ApplyTaskEdit),
                    ('finish_task', '收尾并本地验证全部目标；未满足条件不能标记完成。', FinishTask)]
        result = list(schemas)
        result += [dict(type='function', function=dict(name=name, description=desc,
                                                      parameters=model.model_json_schema()))
                   for name, desc, model in controls]
        return result

    def direct_call(self, sb):
        return None

    def _within(self, path, roots):
        p = self.sb.path(path or '.', exact=True)
        return any(p.is_relative_to(self.sb.path(root, exact=True)) for root in roots)

    def _resolve(self, value):
        artifacts = self.sb.artifacts
        if value.startswith('file:') or value in artifacts.data['files']:
            p, _ = artifacts.resolve(value.removeprefix('file:'), allow_trash=False)
            return self.sb.rel(p)
        return value

    def prepare(self, name, raw, sb):
        self._args, self._outcome = {}, None
        self._content_fingerprints = {}
        self._source_kinds = {}
        self._usage = None
        self._producers = ()
        if self._oplog and self._oplog.path.exists():
            self._journal_offset = len(self._oplog.path.read_text().splitlines())
        self._before_refs = {k: set(v) for k, v in sb.artifacts.data.items()
                             if isinstance(v, dict) and k not in {'files', 'paper_profiles'}}
        if self.final_answer is not None:
            return raw, 'ERROR: 任务已经收尾，此调用未执行'
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError('工具参数必须是对象')
            self._args = args
            if name in self.control_names:
                return raw, None
            tool = registry.TOOLS.get(name)
            if not tool:
                return raw, None  # registry provides the error, never dispatch unknown code.
            validated = tool.args_model.model_validate(args)
            args = validated.model_dump(mode='json')
            for key in tool.file_fields:
                if isinstance(args.get(key), str): args[key] = self._resolve(args[key])
            # Keep missing distinct from explicit null. Capability policies may
            # use null for full coverage while omission inherits task defaults.
            self._args = {key: args[key] for key in validated.model_fields_set}
            raw = json.dumps(self._args, ensure_ascii=False)
            if self.current:
                if self.store.get(self.current['id'])['revision'] != self.current['revision']:
                    raise ValueError('任务版本已改变，必须重新登记任务后继续')
                if tool.read_scope is None:
                    raise ValueError('能力未注册读取范围适配器')
                paths = [self._resolve(p) for p in tool.read_scope(args, sb)]
                if any(not self._within(path, self.current['read_roots']) for path in paths):
                    raise ValueError('工具路径超出任务读取范围')
            if tool.usage:
                limits = {'queries': self.cfg.get('task_query_limit', 12), 'reads': self.cfg.get('task_read_limit', 40)}
                used = (self.current or {}).get('usage', {}).get(tool.usage, 0)
                used = max(used, getattr(self, tool.usage, 0))
                if tool.usage in limits and used >= limits[tool.usage]:
                    raise ValueError('已达到任务'+('检索' if tool.usage == 'queries' else '阅读')+'预算，请报告证据缺口')
                self._usage = tool.usage
            if tool.writes:
                self._producers = tool.produces
                self._check_write(tool, args)
            return raw, None
        except (ValueError, SandboxError, KeyError, registry.ToolError) as exc:
            self.blocked += 1
            return raw, 'ERROR: ' + str(exc)

    def _check_write(self, tool, args):
        if not self.current:
            raise ValueError('写操作前必须 set_task 登记范围与约束')
        if self.current.get('cancelled'):
            raise ValueError('任务已取消，不能执行写操作')
        if self.current['unresolved_questions']:
            raise ValueError('任务仍有待确认问题，未开放写操作')
        if self.current.get('constraint_failures'):
            raise ValueError('此前约束核验失败，必须先检查并修订任务')
        from agent.completion import require_valid_snapshots
        require_valid_snapshots(self.current)
        effects = set(tool.effects)
        goal_ids = active_goals(self)
        if not goal_ids:
            raise ValueError('复合任务写入前必须 begin_step 指明对应目标')
        # Goal-scoped prohibitions are checked per attributed action below.
        forbidden = {e for c in self.current['constraints'] if not c.get('goal_ids') for e in c['forbidden_effects']}
        if effects & forbidden or not effects.issubset(self.current['allowed_effects']):
            raise ValueError('操作副作用不符合任务约束或允许范围')
        roots = self.current['write_roots']
        paths = [self._resolve(p) for p in tool.write_scope(args, self.sb)] if tool.write_scope else []
        if tool.write_scope is None or not paths:
            raise ValueError('能力缺少可验证的写入范围适配器')
        missing = [p for p in paths if not self._within(p, roots)]
        if missing:
            raise ValueError('写入范围不包含以下对象：'+'、'.join(missing[:8])+'；需检查来源和目标，不能自动扩大权限')
        if not tool.batch and tool.operation_preview is None:
            raise ValueError('写能力缺少可验证的操作预览适配器')
        if tool.operation_preview:
            self.validate_operations(tool.operation_preview(args, self.sb))

    def validate_operations(self, operations):
        operations = list(operations)
        bound = validate_operations(self, operations)
        for op in operations:
            if op.get('source'):
                p=self.sb.path(op['source'],exact=True)
                self._source_kinds[op['source']]='文件夹' if p.is_dir() else '文件' if p.is_file() else '项'
        return bound

    def adopt_observations(self):
        """Facts read before registration belong to the task registered in this run."""
        if self.current and self._pretask:
            self.current['observations'] = self._pretask + self.current['observations']
            self._pretask = []

    def _check_references(self, goals, previous):
        """Explicit references must bind to one existing object before registration."""
        data = self.sb.artifacts.data if getattr(self.sb, 'artifacts', None) else {}
        bound_plans = (previous or {}).get('file_plan_bindings', {})
        for goal in goals:
            for c in goal.conditions:
                ref = c.reference.removeprefix('evidence:').removeprefix('receipt:')
                if not ref: continue
                collection = {'file_plan_verified': None, 'plan_ready': 'classification_plans',
                              'plan_applied': 'classification_plans', 'receipt_verified': 'receipts',
                              'evidence_valid': 'evidence'}.get(c.kind, '')
                if collection is None:
                    if ref not in bound_plans:
                        raise ValueError(f'目标 {goal.id} 的 file_plan_verified.reference={ref} 不是本任务已准备的计划；'
                                         '请留空，由本地绑定为该目标执行的文件计划')
                elif collection and data and ref not in data.get(collection, {}):
                    raise ValueError(f'目标 {goal.id} 的 {c.kind}.reference={ref} 在当前会话中不存在；请留空由本地绑定，或填写已有的精确标识')

    def _blocker_object(self, name, raw):
        try: args = json.loads(raw)
        except (json.JSONDecodeError, TypeError): return ''
        if not isinstance(args, dict): return ''
        if name in self.control_names:
            return str(args.get('plan_id') or args.get('edit_id') or args.get('task_id')
                       or ','.join(sorted(args.get('goal_ids') or [])) or '')
        return json.dumps(args, sort_keys=True, ensure_ascii=False)

    def _resolve_blockers(self, name, raw):
        if not self.current: return
        obj = self._blocker_object(name, raw)
        resolve_blockers(self.current, lambda b: b.get('source') == name and b.get('object', '') in {'', obj},
                         '后续同类调用成功：'+name)
        if name == 'execute_file_plan' and obj:
            # A plan's later success, or a later plan for the same goals, supersedes it.
            bindings = self.current.get('file_plan_bindings', {})
            goals = set(bindings.get(obj, {}).get('goal_ids', []))
            resolve_blockers(self.current, lambda b: b.get('object') == obj or (
                b.get('source') == 'execute_file_plan' and b.get('object') in bindings and
                set(bindings[b['object']]['goal_ids']) <= goals), '计划 '+obj+' 已执行并核验')

    def dispatch(self, name, raw, sb, oplog, confirmer):
        try:
            if name in {'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}:
                from agent import file_plans
                if name == 'prepare_file_plan':
                    plan = file_plans.prepare(self, PrepareFilePlan.model_validate_json(raw))
                    self.capture_output(name, file_plans.markdown(plan), plan['id'])
                    outcome = registry.ToolResult(status='ok', text=f"OK: plan_id={plan['id']}；已预检 {len(plan['payload']['steps'])} 项操作；尚未执行。下一步 execute_file_plan 仅需此 ID。")
                elif name == 'inspect_file_plan':
                    plan = self.store.file_plan(ExecuteFilePlan.model_validate_json(raw).plan_id)
                    outcome = registry.ToolResult(status='ok', text='OK: '+file_plans.markdown(plan))
                else:
                    pid = ExecuteFilePlan.model_validate_json(raw).plan_id
                    outcome = registry.ToolResult(status='ok', text=file_plans.execute(self, pid, oplog, confirmer))
                    self.capture_output('prepare_file_plan', file_plans.markdown(self.store.file_plan(pid)), pid)
            elif name == 'inspect_run':
                args=InspectRun.model_validate_json(raw)
                if args.finish and (self.current or self.operations):
                    raise ValueError('本轮已进入执行流程，请用 finish=false 查询并保留当前任务')
                try:previous=self.store.outcome(args.run_id)
                except ValueError:previous=None
                self.inspected_run=previous['run_id'] if previous else args.run_id or 'not_found'
                explanation=('运行 '+previous['run_id']+' 的记录：\n\n'+previous['summary']) if previous else '当前会话没有对应的运行记录，无法确定停止原因。请在原对话中查询或核对运行 ID。'
                if args.finish:
                    self.final_answer=explanation
                    self.report=dict(version=1,state='explained',summary=explanation,sections=[])
                    self.finished=True
                    self.stop_reason='done'
                outcome=registry.ToolResult(status='ok',text='OK: '+explanation)
            elif name == 'begin_step':
                step = ExecutionStep.model_validate_json(raw).model_dump()
                if not self.current or set(step['goal_ids']) - {g['id'] for g in self.current['goals']}:
                    raise ValueError('步骤必须引用当前任务中的目标')
                existing = next((s for s in self.current.get('steps', []) if s['id'] == step['id']), None)
                if existing and existing != step:
                    raise ValueError('步骤 ID 已存在；重新规划请使用新的步骤 ID，旧证据绑定不变')
                if not existing: self.current.setdefault('steps', []).append(step)
                self.active_step = step
                self.store.checkpoint(self.current)
                outcome = registry.ToolResult(status='ok', text='OK: 当前步骤 '+step['id']+'，目标 '+','.join(step['goal_ids']))
            elif name == 'set_task':
                spec = TaskSubmission.model_validate_json(raw)
                previous = self.store.get(spec.task_id) if spec.task_id else None
                read_roots = (previous['read_roots'] if previous and 'read_roots' not in spec.model_fields_set
                              else spec.read_roots)
                write_roots = (previous['write_roots'] if previous and 'write_roots' not in spec.model_fields_set
                               else spec.write_roots)
                for path in read_roots + write_roots:
                    sb.path(path, exact=True)
                merged_goals = {g.id: g for g in spec.goals}
                if spec.task_id:
                    from agent.task import Goal
                    for old in previous['goals']:
                        merged_goals.setdefault(old['id'], Goal.model_validate(old))
                for goal in merged_goals.values():
                    for c in goal.conditions:
                        if c.kind in {'path_exists', 'path_absent', 'directory_empty', 'sha256'}:
                            if not c.path or not any(sb.path(c.path, exact=True).is_relative_to(sb.path(r, exact=True))
                                                     for r in read_roots):
                                raise ValueError('完成条件路径必须位于任务读取范围')
                        if c.kind == 'tool_success' and c.tool not in registry.TOOLS:
                            raise ValueError('完成条件引用了不存在的能力')
                validate_spec(dict(goals=[g.model_dump() for g in merged_goals.values()],
                                   read_roots=read_roots, write_roots=write_roots,
                                   constraints=[c.model_dump() for c in spec.constraints]), sb)
                self._check_references(spec.goals, previous)
                before_task = self.current
                self.current = self.store.submit(spec, self.instruction, self.run_id)
                self.adopt_observations()
                self.active_step = retain_step(before_task, self.current, self.active_step)
                self.operations = []
                self.outputs = []
                self._output_indices = {}
                self.output_records = []
                text = (f"OK: task_id={self.current['id']} revision={self.current['revision']}；"
                        f"保留 {len(self.current['goals'])} 项目标、{len(self.current['constraints'])} 项约束。")
                outcome = registry.ToolResult(status='ok', text=text)
            elif name == 'edit_task':
                args = EditTask.model_validate_json(raw)
                patch = TaskPatch.model_validate(args.model_dump(exclude={'task_id'}, exclude_unset=True))
                self._check_references(patch.upsert_goals, self.store.get(args.task_id))
                edit = self.store.preview_edit(args.task_id, patch)
                outcome = registry.ToolResult(status='ok', text='OK: 任务编辑尚未应用：'+json.dumps(
                    {k: edit[k] for k in ('id','task_id','revision','reason','changes')}, ensure_ascii=False))
            elif name == 'apply_task_edit':
                args = ApplyTaskEdit.model_validate_json(raw)
                edit = self.store.edit(args.edit_id)
                if not confirmer:
                    raise ValueError('模型提出的任务编辑必须通过用户确认策略')
                if not confirmer.allow('task_edit', {k: edit[k] for k in
                        ('id','task_id','revision','reason','changes')}, file_count=0, force=True):
                    raise ValueError('用户拒绝任务编辑，旧任务不变')
                before_task = self.current
                self.current = self.store.apply_edit(args.edit_id, self.instruction, self.run_id, actor='confirmed_model_edit')
                self.active_step = retain_step(before_task, self.current, self.active_step)
                self.operations = []
                self.outputs, self._output_indices = [], {}
                self.output_records = []
                outcome = registry.ToolResult(status='ok', text=f"OK: task_id={self.current['id']} revision={self.current['revision']} 编辑已应用")
            elif name == 'finish_task':
                args = FinishTask.model_validate_json(raw)
                if not self.current:
                    raise ValueError('尚未登记任务，不能声称完成')
                self.current['checks'] = verify_task(self.current, sb)
                state = task_state(self.current)
                self.current['status'] = state
                self.current['model_summary'] = args.summary
                self.current['operations'] = self.operations
                # A revision can change inside one run; report all of this
                # run's actions without duplicating the persistent ledger.
                run_ops = [row for row in self.current.get('operation_history', [])
                           if row.get('run_id') == self.run_id] + self.operations
                self.report = build_report(dict(self.current, operations=run_ops), args.summary, self.output_records)
                self.current['report'] = self.report
                self.store.checkpoint(self.current)
                self.stop_reason = 'done' if state == 'completed' else state
                self.final_answer = self.report['summary']
                self.finished = True
                outcome = registry.ToolResult(status='ok', text='OK: 已核验并保存任务状态：' + state)
            else:
                tool = registry.TOOLS.get(name)
                self._oplog = oplog
                before = oplog.path.read_text().splitlines() if oplog and oplog.path.exists() else []
                self._journal_offset = len(before)
                if tool and self._usage:
                    setattr(self, self._usage, getattr(self, self._usage, 0) + 1)
                    if self.current:
                        usage = self.current.setdefault('usage', {})
                        usage[self._usage] = usage.get(self._usage, 0) + 1
                        self.store.checkpoint(self.current)
                if tool and tool.writes:
                    if not oplog or not confirmer:
                        raise ValueError('V2 写入需要操作日志和明确的确认策略')
                    confirmer = _TaskConfirmer(confirmer)
                    self._producers, self._attributions = tool.produces, {}
                try: outcome = registry.dispatch_result(name, raw, sb, oplog, confirmer)
                finally: self._producers = ()
                attributions, self._attributions = self._attributions or {}, None
                if tool and tool.writes and oplog and oplog.path.exists():
                    for line in oplog.path.read_text().splitlines()[len(before):]:
                        row = json.loads(line)
                        from agent.constraints import op_key
                        effect = 'create' if row['op'] == 'make_dir' else row['op']
                        key = op_key(effect, None if effect == 'create' else row['src'], row['dst'])
                        row.update(run_id=self.run_id, goal_ids=attributions.get(key, active_goals(self)),
                                   step_id=(self.active_step or {}).get('id'))
                        row['kind']='文件夹' if row['op']=='make_dir' else self._source_kinds.get(row['src'],'项')
                        self.operations.append(row)
                        if row['op'] == 'make_dir': continue
                        for src, expected in self._content_fingerprints.items():
                            if Path(src).is_relative_to(Path(row['src'])):
                                from agent.artifacts import digest
                                target = sb.path((Path(row['dst']) / Path(src).relative_to(row['src'])).as_posix(), exact=True)
                                if not target.is_file() or digest(target) != expected:
                                    self.current.setdefault('constraint_failures', []).append('文件内容保持核验失败：'+src)
                                    raise ValueError('文件内容保持核验失败；已执行操作有日志，可撤销')
        except (ValueError, SandboxError, registry.ToolError) as exc:
            self.blocked += 1
            if self.current:
                add_blocker(self.current, name, self._blocker_object(name, raw), str(exc), self.run_id)
                try: self.store.checkpoint(self.current)
                except ValueError: pass  # A stale task cannot overwrite the newer version.
            outcome = registry.ToolResult(status='error', text='ERROR: ' + str(exc),
                                           error_code='CONTROL_REJECTED')
        if outcome.status == 'ok' and name in self.control_names and self.current:
            self._resolve_blockers(name, raw)
            try: self.store.checkpoint(self.current)
            except ValueError: pass
        self._outcome = outcome
        return outcome.text

    def capture_output(self, name, text, reference=None):
        identity = reference if reference is not None else json.dumps(self._args,sort_keys=True,ensure_ascii=False)
        key = ('classification', identity) if reference and name in {
            'submit_classification', 'apply_classification'} else (name, identity)
        if key in self._output_indices:
            index=self._output_indices[key]
            self.outputs[index] = text
            self.output_records[index].update(tool=name,text=text,samples=self.output_records[index]['samples']+1)
        else:
            self._output_indices[key] = len(self.outputs)
            self.outputs.append(text)
            self.output_records.append(dict(tool=name,text=text,samples=1))

    def after(self, name, result, sb):
        if self.finished:
            return
        # Legacy report tools may set final_answer. They finish a sub-step only.
        if name not in self.control_names and self.final_answer:
            self.capture_output(name, self.final_answer)
            self.final_answer = None
        if name in self.control_names:
            return
        if not self.current:
            outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code='CONTROL_REJECTED')
            if outcome.status == 'ok' and name in registry.TOOLS and not registry.TOOLS[name].writes:
                self._pretask.append(dict(tool=name, arguments=self._args, status='ok', error_code=None,
                                          summary=result.splitlines()[0][:300] if result else '',
                                          references=[], goal_ids=[], step_id=None, run_id=self.run_id))
            return
        refs = []
        for collection, before in self._before_refs.items():
            ids = set(sb.artifacts.data[collection]) - before
            tool = registry.TOOLS.get(name)
            for field, target in tool.reference_inputs if tool else ():
                if target == collection and self._args.get(field) in sb.artifacts.data[collection]:
                    ids.add(self._args[field])
            refs.extend(dict(collection=collection, id=value, goal_ids=self._consumers(collection),
                             step_id=(self.active_step or {}).get('id')) for value in sorted(ids))
        outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code='CONTROL_REJECTED')
        tool = registry.TOOLS.get(name)
        if tool and tool.display_result and outcome.status == 'ok':
            self.capture_output(name, result.removeprefix('OK: '))
        self.current['observations'].append(dict(tool=name, arguments=self._args,
                                                status=outcome.status, error_code=outcome.error_code,
                                                summary=result.splitlines()[0][:300] if result else '',
                                                references=refs, goal_ids=active_goals(self),
                                                step_id=(self.active_step or {}).get('id'), run_id=self.run_id))
        if tool and tool.writes:
            if outcome.status == 'ok':
                self._resolve_blockers(name, json.dumps(self._args))
            else:
                add_blocker(self.current, name, self._blocker_object(name, json.dumps(self._args)),
                            result.removeprefix('ERROR: ').splitlines()[0][:300] if result else '调用未完成', self.run_id)
        for ref in refs:
            if ref['collection'] == 'receipts':
                self.capture_output(name, sb.artifacts.receipt_markdown(ref['id']), ref['id'])
        self.current['outputs'] = self.outputs
        self.current['output_records'] = self.output_records
        self.current['operations'] = self.operations
        self.store.checkpoint(self.current)

    def _consumers(self, collection):
        """Bind a new object to the step goals whose conditions verify that collection."""
        kinds = {'evidence': {'evidence_valid'}, 'classification_plans': {'plan_ready', 'plan_applied'},
                 'receipts': {'receipt_verified'}}.get(collection, set())
        goals = {g['id']: g for g in self.current['goals']}
        active = active_goals(self)
        return [g for g in active if any(c['kind'] in kinds for c in goals[g]['conditions'])] or active

    def interrupt(self, reason):
        if self.current and self.final_answer is None:
            self.current['status'] = 'interrupted'
            self.current['interruption_reason'] = reason
            self.store.checkpoint(self.current)

    def finish_plain(self, content):
        if self.current:
            detail = self.current.get('policy', {}).get('output', {}).get('detail')
            self.dispatch('finish_task', json.dumps({'summary': (content or '')[:12000 if detail == 'full' else 1200]}),
                          self.sb, None, None)
        if self.final_answer is None:
            if self.blocked:
                self.final_answer = '本轮存在被拦截的操作，未形成可核验的任务记录，不能确认完成。'
                self.stop_reason = 'unverified_answer'
                return
            self.final_answer = content or '本轮没有返回内容。'
            self.stop_reason = 'answered'
            self.report = dict(version=1, state='answered', summary=self.final_answer, sections=[])

    def stats(self):
        return dict(task='structured', route='task_controller', execution='model_and_local',
                    queries=self.queries, reads=self.reads, blocked_calls=self.blocked,
                    task_id=self.current['id'] if self.current else None,
                    task_revision=self.current['revision'] if self.current else None,
                    task_status=self.current['status'] if self.current else 'not_registered',
                    document_cache=getattr(self.sb, '_document_cache_stats', {}),
                    step=self.active_step, usage=self.current.get('usage', {}) if self.current else {},
                    checks=self.current.get('checks', []) if self.current else [])
