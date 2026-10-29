"""Task-driven controller: no natural-language keyword routing or authorization."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from agent.asking import Asking, AskUser
from agent.finishing import Finishing, FinishTask, FinishTurn, InspectRun
from agent.prompts import FIELD_DOCS, SENSITIVE_RULE, TASK_GUIDANCE, TOOL_DESCRIPTIONS
from agent.task import LEGACY_NOTE, TaskStore, is_legacy, add_blocker, resolve_blockers
from agent.constraints import validate_operations
from agent.policy import active_goals
from safety.sandbox import SandboxError
from tools import registry
from agent.file_plans import PrepareFilePlan, ExecuteFilePlan
from agent import choices
from agent.intent import StartTask, ReviseTask, compile_start, compile_revision, project, parts_text, normalize_part_refs
from pydantic import ValidationError


def _with_docs(schema, docs):
    from copy import deepcopy
    schema = deepcopy(schema)
    for path, text in docs.items():
        owner, _, field = path.rpartition('.')
        node = schema['$defs'][owner] if owner else schema
        node['properties'][field]['description'] = text  # KeyError if a field was renamed
    return schema


def untitled(node):
    """A schema without pydantic's generated titles ("title": "Label" for field label).

    They repeat the field or model name and carry no rule; dropping them freed
    3,661 of the 31,311 model-visible characters (routing redesign capacity gate,
    2026-10-25). A property that is itself named title is kept (its value is a schema).
    """
    if isinstance(node, dict):
        return {k: untitled(v) for k, v in node.items() if not (k == 'title' and isinstance(v, str))}
    if isinstance(node, list):
        return [untitled(v) for v in node]
    return node


class _TaskConfirmer:
    def __init__(self, delegate, notes=()):
        self.delegate, self.notes = delegate, list(notes)

    def allow(self, name, args, file_count=None, **kwargs):
        return self.decide(name, args, file_count, **kwargs).approved

    def decide(self, name, args, file_count=None, **kwargs):
        if self.notes: kwargs['notes'] = self.notes
        kwargs['force'] = True
        return self.delegate.decide(name, args, file_count, **kwargs)


class TaskRuntime(Asking, Finishing):
    control_names = {'ask_user', 'start_task', 'revise_task', 'finish_task', 'finish_turn', 'inspect_run', 'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}

    def __init__(self, instruction, cfg, sb, run_id):
        self.instruction, self.cfg, self.sb, self.run_id = instruction, cfg, sb, run_id
        db_path = Path(cfg.get('task_db') or sb.root / '.oplog' / 'tasks.sqlite3')
        self.store = TaskStore(db_path, sb.root, cfg.get('task_session', 'cli'))
        self.current = None
        self.final_answer, self.stop_reason = None, 'done'
        self.queries = self.reads = self.blocked = 0
        self.blocked_reasons = []
        self.outputs = []
        self._output_indices = {}
        self.finished = False
        self._args = {}
        self._before_refs = {}
        self._outcome = None
        self._oplog = None
        self.operations = []
        self.output_records = []
        self.report = {}
        self.inspected_run = None
        self._source_kinds = {}
        self._journal_offset = 0
        self._usage = None
        self._producers = ()
        self._part = None  # the part a write tool's call names (routing redesign S4)
        self.input_relation = None  # how this message relates to the last question (S3, 4.5)
        self._epochs = {}  # request id -> cancel epoch this run started under
        self._attributions = None
        self._pretask = []  # read-only facts observed before any task existed in this run
        self.results = {}  # result_id -> typed result summary (S6b), recorded once
        self.coverage = None  # search coverage ledger before a task holds it (S7)
        self._last_observation = None
        self.pending_choice = None
        self.answered_choice = self._answered_choice(instruction)
        self._resume_asked_task()
        # Folders whose hidden files the user agreed, last turn, to recycle.
        # Consent to recycle hidden files: a chosen consent option, or (user choice
        # 2026-10-22, option C) a free-text reply, which the confirmation card must
        # then confirm. A chosen option without consent grants nothing.
        # What the user decided in that reply (hidden files, overwrites, reads): agent/choices.py.
        self.decisions = choices.from_answer(self.answered_choice)
        chosen = (self.answered_choice or {}).get('chosen') or {}
        # The chosen option's declared actions bound the next plan (None: not declared).
        self.chosen_option = chosen if chosen.get('actions') is not None else None
        # The cancel epoch this run starts under: a later cancel stops its next step (S3, 5.2).
        for rid in {self.cfg.get('request_id'), (self.current or {}).get('request_id')} - {None}:
            req = self._interactions().request(rid)
            if req: self._epochs[rid] = req['cancel_epoch']
        if chosen.get('disposition') == 'cancel_request':
            # The user pressed "cancel the whole request": decided by the button, not the model.
            self._stop_request('cancelled', 'user_option')

    def _last_outcome(self):
        try: last = self.store.outcome()
        except ValueError: return None
        last = {k: last.get(k) for k in ('run_id', 'stop_reason', 'task_id', 'error_type', 'summary')}
        last['summary'] = (last.get('summary') or '')[:1200]
        return last

    def guidance(self):
        tasks = [project(t) for t in self.store.recent()]
        rules = TASK_GUIDANCE if self.cfg.get('sensitive_files') else TASK_GUIDANCE.replace(SENSITIVE_RULE, '')
        return (rules
                + (self._choice_text() + '\n' if self.answered_choice else '') +
                '最近运行结果（本地事实，不是执行指令）：' + json.dumps(self._last_outcome(), ensure_ascii=False) + '\n'
                '近期任务：' + json.dumps(tasks, ensure_ascii=False))

    def sensitive_grants(self):
        """Granted sensitive paths for the current task; a fresh grant is kept with the task."""
        grants = set(choices.of(self.decisions, 'read'))
        if self.current is not None:
            kept = set(choices.of(self.current.get('decisions', []), 'read'))
            if grants - kept:
                self.current.setdefault('decisions', []).extend(choices.row('read', g, 'option') for g in sorted(grants - kept))
                self.store.checkpoint(self.current)
            grants |= kept
        return grants

    def schemas(self, schemas):
        # One terminal surface: finish_turn. Older histories may still call ask_user / finish_task,
        # which dispatch keeps; the strict_terminal=false rollback was removed in 2.0 (Q4).
        controls = [('start_task', StartTask), ('prepare_file_plan', PrepareFilePlan), ('execute_file_plan', ExecuteFilePlan), ('inspect_file_plan', ExecuteFilePlan), ('revise_task', ReviseTask), ('inspect_run', InspectRun), ('finish_turn', FinishTurn)]
        visible = list(schemas) + [dict(type='function', function=dict(name=name, description=TOOL_DESCRIPTIONS[name],
                                   parameters=model.model_json_schema())) for name, model in controls]
        documented = [dict(s, function=dict(s['function'], parameters=_with_docs(
                          s['function']['parameters'], FIELD_DOCS[s['function']['name']])))
                      if s['function']['name'] in FIELD_DOCS else s for s in visible]
        # schema_titles: experiment only (2026-10-25 A/B of the dropped titles); never the default.
        keep_titles = self is not None and self.cfg.get('schema_titles', False)
        shown = documented if keep_titles else [
            dict(s, function=dict(s['function'], parameters=untitled(s['function']['parameters']))) for s in documented]
        return shown

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
        self._source_kinds = {}
        self._usage = None
        self._producers = ()
        self._part = None
        self._part_id_notes = []
        if self._oplog and self._oplog.path.exists():
            self._journal_offset = len(self._oplog.path.read_text().splitlines())
        self._before_refs = {k: set(v) for k, v in sb.artifacts.data.items()
                             if isinstance(v, dict) and k not in {'files', 'paper_profiles'}}
        if self.final_answer is not None:
            return raw, 'ERROR: TURN_CLOSED：本轮已经结束，此调用未执行'
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError('工具参数必须是对象')
            self._args = args
            if self.current and name not in {'start_task', 'revise_task'}:
                args, self._part_id_notes = normalize_part_refs(args, self.current)
                self._args = args
                raw = json.dumps(args, ensure_ascii=False)
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
                self._part = self._named_part(args.get('part'), tool) if args.get('part') else None
                self._check_write(tool, args)
            return raw, None
        except (json.JSONDecodeError, ValidationError) as exc:
            self.blocked += 1
            return raw, registry.argument_error(exc) if name in self.control_names else 'ERROR: ' + str(exc)
        except (ValueError, SandboxError, KeyError, registry.ToolError) as exc:
            self.blocked += 1
            self.blocked_reasons.append(str(exc))
            return raw, 'ERROR: ' + str(exc)

    def _check_drafts(self, spec):
        """Part.draft_id names a draft of the question the user just answered, once each;
        a draft that question's option is about must be mapped (routing redesign S3, 4.2)."""
        a = self.answered_choice or {}
        drafts = {d['id'] for d in a.get('drafts') or []}
        named = list(spec.draft_map.values())
        if len(named) != len(set(named)):
            raise ValueError('draft_id 重复：一个草案部分只对应一个正式部分')
        unknown = [d for d in named if d not in drafts]
        if unknown:
            raise ValueError('draft_id 不属于用户刚回答的问题：' + '、'.join(unknown))
        scope = a.get('scope') or {}
        if a.get('chosen') and scope.get('drafts'):
            missing = [d for d in scope['part_ids'] if d not in named]
            if missing:
                raise ValueError('UNRESOLVED_QUESTION_SCOPE：用户的选择只关于草案部分 ' + '、'.join(missing) +
                                 '；请在对应的 Part 写 draft_id')

    # ----- how the message relates to the question, and the user stopping a request (S3) -----

    def _interactions(self):
        from agent.interactions import Interactions
        return Interactions(self.store)

    def relation_needed(self):
        """The message replied to a question in text: before writing, the run must say how."""
        a = self.answered_choice
        return bool(a and not a.get('chosen'))

    def _target_request(self):
        """The request a pause or cancel means: the task's, the question's, this message's."""
        rid = (self.current or {}).get('request_id')
        if not rid and self.answered_choice and self.cfg.get('receipt_id'):
            replied = self._interactions().replied_to(self.cfg['receipt_id'])
            rid = replied and replied.get('request_id')
        if not rid:
            latest = self._interactions().latest_request(exclude=self.cfg.get('request_id'))
            rid = latest and latest['request_id']
        return rid or self.cfg.get('request_id')

    def declare_relation(self, resolution, *, terminal):
        """Apply the model's statement of how this message relates to what came before."""
        kind = resolution.kind
        if kind in ('pause', 'cancel_request') and not terminal:
            raise ValueError(f'{kind} 用 finish_turn 的 input_resolution 声明，并结束本轮')
        if kind == 'answer' and not self.relation_needed():
            if (self.answered_choice or {}).get('chosen'):
                self.input_relation = kind  # an exact answer is an answer already: nothing to apply
                return
            raise ValueError('本轮消息没有回应待答问题，不能声明 answer')
        if self.relation_needed() and (self.answered_choice or {}).get('question_id'):
            self._interactions().text_relation(self.answered_choice['question_id'], kind)
        if kind in ('new_request', 'independent_query'):
            # Not an answer: the question waits, and none of its options or consents carry over.
            if self.relation_needed() and (self.answered_choice or {}).get('question_id'):
                self._interactions().suspend_question(self.answered_choice['question_id'])
            self.answered_choice, self.decisions, self.chosen_option = None, [], None
        elif kind == 'resume':
            self._resume_request(resolution.request_id)
        elif kind == 'pause':
            self._stop_request('paused', 'user_text')
        elif kind == 'cancel_request':
            self._stop_request('cancelled', 'user_text')
        self.input_relation = kind

    def _require_writable(self):
        if self.relation_needed() and self.input_relation is None:
            q = (self.answered_choice or {}).get('question', '')
            raise ValueError(f'INPUT_RELATION_REQUIRED：用户这条消息回应了上一问“{q[:60]}”，但没有选选项。写入前请在 '
                             'start_task 或 prepare_file_plan 的 input_resolution 写明它是 answer、revise 还是 new_request 等')
        if self.stop_requested():
            raise ValueError('USER_CANCELLED：用户已取消或暂停这个请求，不能再写入')

    def stop_requested(self):
        """The user paused or cancelled the request this run works for (checked by the executor
        before the confirmation, after it and before each step)."""
        rid = (self.current or {}).get('request_id') or self.cfg.get('request_id')
        if not rid:
            return False
        req = self._interactions().request(rid)
        return bool(req and (req['status'] in ('paused', 'cancelled') or req['cancel_epoch'] > self._epochs.get(rid, 0)))

    def stopped_by_user(self):
        """The request was cancelled or paused from outside this run: report what ran."""
        rid = (self.current or {}).get('request_id') or self.cfg.get('request_id')
        req = self._interactions().request(rid) if rid else None
        status = 'paused' if req and req['status'] == 'paused' else 'cancelled'
        if self.current and status == 'cancelled':
            self.current = self.store.cancel_from_user(self.current['id'], actor=(req or {}).get('status_actor') or 'user_button',
                                                       receipt_id=(req or {}).get('status_receipt'), run_id=self.run_id)
        task = self.current
        operations = list((task or {}).get('operation_history', [])) + list(self.operations)
        from agent.reporting import stopped_report
        self.report = stopped_report(task, operations, status)
        self.final_answer, self.stop_reason, self.finished = self.report['summary'], status, True

    def _resume_request(self, request_id):
        if not request_id:
            raise ValueError('resume 需要 request_id（见最近运行结果）')
        req = self._interactions().request(request_id)
        if not req or req['status'] != 'paused':
            raise ValueError('只有暂停的请求可以继续；已取消的请求请重新提出')
        self._interactions().set_status(request_id, 'active', actor='user_text', receipt_id=self.cfg.get('receipt_id'))
        if req.get('task_id'):
            task = self.store.get(req['task_id'])
            if task.get('status') == 'paused':
                task['status'] = 'running'
                self.store.checkpoint(task)
            self.current = task
        self._epochs[request_id] = req['cancel_epoch']

    def _stop_request(self, status, actor):
        """Pause or cancel the request: no new writes, its questions closed or suspended, the task
        cancelled or paused. What already ran stays done; nothing is called complete."""
        rid = self._target_request()
        req = self._interactions().set_status(rid, status, actor=actor, receipt_id=self.cfg.get('receipt_id')) if rid else None
        task_id = (self.current or {}).get('id') or (req or {}).get('task_id')
        task = None
        if task_id:
            if status == 'cancelled':
                task = self.store.cancel_from_user(task_id, actor=actor, receipt_id=self.cfg.get('receipt_id'), run_id=self.run_id)
            else:
                task = self.store.get(task_id)
                task['status'] = 'paused'
                self.store.checkpoint(task)
            self.current = task
        operations = ([o for o in (task or {}).get('operation_history', []) + (task or {}).get('operations', [])]
                      if task else []) + [o for o in self.operations if o not in ((task or {}).get('operations') or [])]
        from agent.reporting import stopped_report
        self.report = stopped_report(task, operations, status)
        self.final_answer, self.stop_reason, self.finished = self.report['summary'], status, True

    def _named_part(self, part, tool):
        """The part a write tool's call names: one of the task's, proved by this tool's receipt."""
        from agent.verifier import CONSUMERS
        goals = {g['id']: g for g in (self.current or {}).get('goals', [])}
        if part not in goals:
            raise ValueError(f'part 引用了不存在的部分：{part}；当前部分：' + parts_text(self.current or dict(goals=[])))
        kinds = {k for p in tool.produces for k in CONSUMERS.get(p, ())}
        if not any(c['kind'] in kinds for c in goals[part]['conditions']):
            raise ValueError(f'部分 {part} 的完成条件不能由 {tool.name} 的回执满足；请写明对应的去重部分')
        return part

    def _check_write(self, tool, args):
        if not self.current:
            raise ValueError('写操作前必须 start_task 登记用户的要求和限制')
        self._require_writable()
        if self.current.get('cancelled'):
            raise ValueError('任务已取消，不能执行写操作')
        if self.current.get('constraint_failures'):
            raise ValueError('此前约束核验失败，必须先检查并修订任务')
        effects = set(tool.effects)
        goal_ids = active_goals(self)
        if not goal_ids:
            raise ValueError('AMBIGUOUS_GOAL：有多个部分都可能由这次写入完成，本地无法确定归属；'
                             '请在参数 part 写明属于哪个部分（当前部分：' + parts_text(self.current) + '）')
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

    def validate_operations(self, operations):
        operations = list(operations)
        bound = validate_operations(self, operations)
        for op in operations:
            if op.get('source'):
                p=self.sb.path(op['source'],exact=True)
                self._source_kinds[op['source']]='文件夹' if p.is_dir() else '文件' if p.is_file() else '项'
        return bound

    def text_limits(self):
        """User limits with no local rule, shown on every confirmation until reviewed."""
        if not self.current: return []
        latest = {}  # the user's last decision on each limit in this revision
        for r in self.current.get('reviews', []):
            if r['revision'] == self.current['revision']:
                latest.update({c: r['decision'] for c in r.get('constraint_ids', [])})
        accepted = {c for c, decision in latest.items() if decision == 'accept'}
        return ['待你确认的限制（本地无法自动检查）：' + c['description'] for c in self.current['constraints']
                if not c['forbidden_effects'] and not c.get('rules') and c['id'] not in accepted]

    def _task_for(self, task_id):
        """(task, note) a plan's task_id names. A mistyped ID is accepted only when
        it cannot mean another task; otherwise the model gets the IDs to copy."""
        try:
            task = self.store.get(task_id)
        except ValueError:
            task = None
        if task:
            if is_legacy(task): raise ValueError(LEGACY_NOTE)
            return task, ''
        if self.current:
            # Only this run's task can take the plan, so an ID that names no task is a typo of it.
            return self.current, (f"task_id {task_id} 不存在，已按本轮任务 {self.current['id']} 处理；"
                                  '本轮已登记任务时可以不填 task_id。')
        recent = self.store.recent(10)
        matches = [t for t in recent if len(task_id) >= 10 and t['id'].startswith(task_id)]
        if len(matches) == 1:
            if is_legacy(matches[0]): raise ValueError(LEGACY_NOTE)
            return matches[0], f"task_id {task_id} 不完整，已按近期任务 {matches[0]['id']} 处理。"
        listing = '；'.join(f"{t['id']}（{(t['goals'][0]['description'] if t['goals'] else '')[:30]}）" for t in recent[:5])
        raise ValueError(f"task_id {task_id} 不存在" + ('，且同时匹配多个近期任务' if len(matches) > 1 else '')
                         + f"。近期任务：{listing or '无'}。请复制完整的任务 ID")

    def adopt_observations(self):
        """Facts read before registration belong to the task registered in this run."""
        if self.current and self._pretask:
            self.current['observations'] = self._pretask + self.current['observations']
            self._pretask = []

    def _blocker_object(self, name, raw):
        try: args = json.loads(raw)
        except (json.JSONDecodeError, TypeError): return ''
        if not isinstance(args, dict): return ''
        if name in self.control_names:
            return str(args.get('plan_id') or args.get('task_id')
                       or ','.join(sorted(args.get('goal_ids') or [])) or '')
        return json.dumps(args, sort_keys=True, ensure_ascii=False)

    def _resolve_blockers(self, name, raw):
        if not self.current: return
        obj = self._blocker_object(name, raw)
        resolve_blockers(self.current, lambda b: b.get('source') == name and b.get('object', '') in {'', obj},
                         '后续同类调用成功：'+name)
        try: args = json.loads(raw)
        except (json.JSONDecodeError, TypeError): args = {}
        if isinstance(args, dict) and args.get('goal_ids') and not (args.get('plan_id') or args.get('task_id')):
            # A rejection naming no goal of the task was a wrong ID, not a reason any goal
            # is unmet; the same call succeeding with real IDs is its correction.
            goals = {g['id'] for g in self.current.get('goals', [])}
            resolve_blockers(self.current, lambda b: b.get('source') == name and b.get('object')
                             and not set(b['object'].split(',')) & goals, '后续同类调用改用了正确的部分 ID')
        if name == 'execute_file_plan' and obj:
            # A plan's later success, or a later plan for the same goals, supersedes it.
            bindings = self.current.get('file_plan_bindings', {})
            goals = set(bindings.get(obj, {}).get('goal_ids', []))
            resolve_blockers(self.current, lambda b: b.get('object') == obj or (
                b.get('source') == 'execute_file_plan' and b.get('object') in bindings and
                set(bindings[b['object']]['goal_ids']) <= goals), '计划 '+obj+' 已执行并核验')

    def _register(self, spec, sb):
        """Register a new task (start_task)."""
        self._check_drafts(spec)
        self.current = self.store.submit(spec, self.instruction, self.run_id)
        if self.answered_choice and self.answered_choice.get('question_id') and (self.chosen_option or self.input_relation == 'answer'):
            self._interactions().bind_question_task(self.answered_choice['question_id'], self.current['id'])
        if self.cfg.get('request_id'):
            self.current['request_id'] = self.cfg['request_id']
            self._interactions().link_task(self.cfg['request_id'], self.current['id'])
            self.store.checkpoint(self.current)
        self.adopt_observations()
        self.operations = []
        self.outputs = []
        self._output_indices = {}
        self.output_records = []
        text = (f"OK: task_id={self.current['id']} revision={self.current['revision']}；"
                f"保留 {len(self.current['goals'])} 项目标、{len(self.current['constraints'])} 项约束。")
        return registry.ToolResult(status='ok', text=text)

    def dispatch(self, name, raw, sb, oplog, confirmer):
        try:
            if self.current and name not in {'start_task', 'revise_task'}:
                data, notes = normalize_part_refs(json.loads(raw), self.current)
                raw = json.dumps(data, ensure_ascii=False)
                self._part_id_notes = list(dict.fromkeys(getattr(self, '_part_id_notes', []) + notes))
            if name in {'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}:
                from agent import file_plans
                if name == 'prepare_file_plan':
                    args = PrepareFilePlan.model_validate_json(raw)
                    if args.input_resolution and self.input_relation is None:
                        self.declare_relation(args.input_resolution, terminal=False)
                    id_note = ''
                    if args.task_id:
                        # Continuing a recent task keeps its limits; it never switches tasks.
                        task, id_note = self._task_for(args.task_id)
                        if self.current and self.current['id'] != task['id']:
                            raise ValueError('计划只能属于当前任务 '+self.current['id']+'，不能切换任务绕过限制；本轮已登记任务时 task_id 可以不填')
                        if not self.current:
                            self.current = task
                            self.adopt_observations()
                    elif not self.current:
                        raise ValueError('请先 start_task 登记用户的要求和限制（可在同一响应中紧接着准备计划）；续做近期任务时填写 task_id')
                    self._require_writable()
                    plan = file_plans.prepare(self, args)
                    if plan['payload'].get('empty_selection'):
                        sel = plan['payload']['empty_selection']
                        outcome = registry.ToolResult(status='ok', text=(
                            f"OK: plan_id={plan['id']}；选择集 {sel['path']} 当前没有可处理的文件，已记录为部分 "
                            f"{'、'.join(plan['payload']['goal_ids'])} 的核验结果（无需操作），不需要执行"))
                    else:
                        self.capture_output(name, file_plans.markdown(plan), plan['id'])
                        text = f"OK: plan_id={plan['id']}；已预检 {len(plan['payload']['steps'])} 项操作；尚未执行。下一步 execute_file_plan 仅需此 ID。"
                        for part, sel in (plan['payload'].get('empty_parts') or {}).items():
                            text += f"\n选择集 {sel['path']} 当前没有可处理的文件，执行后部分 {part} 按无需操作核验。"
                        hints = file_plans.conflicts(self, plan)
                        # User decision 2026-10-22: ask before creating a target folder the user
                        # did not ask for. A judgement for the model, so a reminder at the point
                        # of decision, not a refusal; the card lists new folders (reversible).
                        fresh = plan['payload'].get('new_folders')
                        if fresh and not self.chosen_option and not plan['payload'].get('classification'):
                            text += (f"\n本计划会新建文件夹：{'、'.join(fresh)}。用户没有要求新建时，不要执行，先 finish_turn(mode=ask)："
                                     '有意思相近的已有文件夹就作为第一个选项问是否指它，其余写在哪里新建和取消。')
                        if hints:
                            text += '\n注意：' + '；'.join(hints) + '。若与用户要求不符，请先用 revise_task 修正对应部分；不要执行此计划。'
                        outcome = registry.ToolResult(status='ok', text=text)
                    if id_note:
                        outcome.text += '\n' + id_note
                elif name == 'inspect_file_plan':
                    plan = self.store.file_plan(ExecuteFilePlan.model_validate_json(raw).plan_id)
                    outcome = registry.ToolResult(status='ok', text='OK: '+file_plans.markdown(plan))
                else:
                    pid = ExecuteFilePlan.model_validate_json(raw).plan_id
                    self._require_writable()
                    outcome = registry.ToolResult(status='ok', text=file_plans.execute(self, pid, oplog, confirmer))
                    self.capture_output('prepare_file_plan', file_plans.markdown(self.store.file_plan(pid)), pid)
            elif name == 'inspect_run':
                outcome = self._inspect_run(raw)
            elif name == 'ask_user':
                outcome = self._ask_user(raw, sb)
            elif name == 'finish_turn':
                outcome = self._finish_turn(raw, sb)
            elif name == 'start_task':
                if self.current and any(m.get('run_id') == self.run_id for m in self.current.get('source_messages', [])):
                    raise ValueError(f"本轮已登记任务 {self.current['id']}；更正或补充请用 revise_task，不能另建任务")
                start = StartTask.model_validate_json(raw)
                if start.input_resolution:
                    self.declare_relation(start.input_resolution, terminal=False)
                outcome = self._register(compile_start(start), sb)
                outcome.text += '部分：' + parts_text(self.current) + '。修订时用这些 ID。'
            elif name == 'revise_task':
                args = ReviseTask.model_validate_json(raw)
                patch = compile_revision(args, self.store.get(args.task_id))
                edit = self.store.preview_edit(args.task_id, patch)
                if not confirmer:
                    raise ValueError('任务修改必须通过用户确认策略')
                from agent.file_plans import not_confirmed
                changes = {k: edit[k] for k in ('id', 'task_id', 'revision', 'reason', 'changes')}
                key = hashlib.sha256(json.dumps([args.task_id, edit['changes']], ensure_ascii=False, sort_keys=True,
                                                default=str).encode()).hexdigest()[:24]
                answer = confirmer.decide('task_edit', changes, file_count=0, force=True, manifest_key=key)
                if not answer.approved:
                    raise ValueError(not_confirmed(answer, '任务修改').replace('未执行剩余操作', '旧任务不变'))
                self.current = self.store.apply_edit(edit['id'], self.instruction, self.run_id, actor='confirmed_model_edit')
                self.operations = []
                self.outputs, self._output_indices = [], {}
                self.output_records = []
                outcome = registry.ToolResult(status='ok', text=f"OK: task_id={self.current['id']} revision={self.current['revision']}；"
                                              f"修改已确认并生效。部分：{parts_text(self.current)}")
            elif name == 'finish_task':
                outcome = self._finish_task(raw, sb)
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
                        raise ValueError('写入需要操作日志和明确的确认策略')
                    confirmer = _TaskConfirmer(confirmer, self.text_limits())
                    self._producers, self._attributions = tool.produces, {}
                try: outcome = registry.dispatch_result(name, raw, sb, oplog, confirmer)
                finally: self._producers = ()
                if outcome.status == 'ok' and '下一页用 page=' in outcome.text:
                    # logs/20261021-230636: six pages listed one by one before a selection plan.
                    outcome.text += ('\n批量移动、平铺或回收不必翻完所有页：prepare_file_plan 的 selection 由本地展开全部文件，'
                                     '预检时检查重名；只需要数量时用 inventory_files。')
                attributions, self._attributions = self._attributions or {}, None
                if tool and tool.writes and oplog and oplog.path.exists():
                    for line in oplog.path.read_text().splitlines()[len(before):]:
                        row = json.loads(line)
                        from agent.constraints import op_key
                        effect = 'create' if row['op'] == 'make_dir' else row['op']
                        key = op_key(effect, None if effect == 'create' else row['src'], row['dst'])
                        row.update(run_id=self.run_id, goal_ids=attributions.get(key, active_goals(self)))
                        row['kind']='文件夹' if row['op']=='make_dir' else self._source_kinds.get(row['src'],'项')
                        self.operations.append(row)
        except (json.JSONDecodeError, ValidationError) as exc:
            self.blocked += 1
            outcome = registry.ToolResult(status='error', text=registry.argument_error(exc), error_code='INVALID_ARGUMENTS')
        except (ValueError, SandboxError, registry.ToolError) as exc:
            self.blocked += 1
            self.blocked_reasons.append(str(exc))
            if self.current:
                add_blocker(self.current, name, self._blocker_object(name, raw), str(exc), self.run_id)
                try: self.store.checkpoint(self.current)
                except ValueError: pass  # A stale task cannot overwrite the newer version.
            outcome = registry.ToolResult(status='error', text='ERROR: ' + str(exc),
                                           error_code=registry.error_code(str(exc)))
            if name == 'execute_file_plan':
                self._plan_progress(raw, outcome)
        if outcome.status == 'ok' and name in self.control_names and self.current:
            self._resolve_blockers(name, raw)
            try: self.store.checkpoint(self.current)
            except ValueError: pass
        if outcome.status == 'ok' and getattr(self, '_part_id_notes', None):
            outcome.text += '\n部分 ID 已统一：' + '；'.join(self._part_id_notes)
            self._part_id_notes = []
        self._outcome = outcome
        return outcome.text

    def capture_output(self, name, text, reference=None):
        identity = reference if reference is not None else json.dumps(self._args,sort_keys=True,ensure_ascii=False)
        key = ('classification', identity) if reference and name == 'submit_classification' else (name, identity)
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
        self._last_observation = None
        typed = self._outcome if name in registry.TOOLS else None
        # A tool's table for the user comes in its result (S6b); a legacy tool that still
        # sets final_answer finishes a sub-step only.
        if typed is not None and typed.status == 'ok' and typed.data.get('display'):
            self.capture_output(name, typed.data['display'])
        if name not in self.control_names and self.final_answer:
            self.capture_output(name, self.final_answer)
            self.final_answer = None
        if name in self.control_names:
            return
        refs = []
        # The objects the call issued, as its result names them. A result's references are
        # the artifact-store difference across that one call (registry.dispatch_result),
        # so they are not an independent check of it; the old shadow comparison of the
        # two was removed in 2.0 P1-B. Untyped results fall back to the same difference.
        issued = {(r['kind'], r['id']) for r in typed.references} if typed is not None else None
        for collection, before in self._before_refs.items():
            ids = ({i for k, i in issued if k == collection} if issued is not None
                   else set(sb.artifacts.data[collection]) - before)
            tool = registry.TOOLS.get(name)
            for field, target in tool.reference_inputs if tool else ():
                if target == collection and self._args.get(field) in sb.artifacts.data[collection]:
                    ids.add(self._args[field])
            # Before registration no goal exists: the refs stay untagged and bind
            # later only where a single goal verifies that collection.
            refs.extend(dict(collection=collection, id=value,
                             goal_ids=self._consumers(collection) if self.current else []) for value in sorted(ids))
        if not self.current:
            outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code=registry.error_code(result))
            if outcome.status == 'ok' and name in registry.TOOLS and not registry.TOOLS[name].writes:
                self._pretask.append(dict(tool=name, arguments=self._args, status='ok', error_code=None,
                                          summary=result.splitlines()[0][:300] if result else '',
                                          references=refs, goal_ids=[], run_id=self.run_id))
            return
        outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code=registry.error_code(result))
        tool = registry.TOOLS.get(name)
        if tool and tool.display_result and outcome.status == 'ok':
            self.capture_output(name, result.removeprefix('OK: '))
        self.current['observations'].append(dict(tool=name, arguments=self._args,
                                                status=outcome.status, error_code=outcome.error_code,
                                                summary=result.splitlines()[0][:300] if result else '',
                                                references=refs, goal_ids=active_goals(self), run_id=self.run_id))
        self._last_observation = self.current['observations'][-1]
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

    def _plan_progress(self, raw, outcome):
        """A stopped plan's steps: done (journalled, kept, undoable), still owed, and in flight
        (outcome unknown: settled by the next execute's recovery, never replayed blindly)."""
        try: plan = self.store.file_plan(ExecuteFilePlan.model_validate_json(raw).plan_id)
        except ValueError: return
        steps = [s['id'] for s in plan['payload']['steps']]
        done = [s for s in steps if s in plan['progress']]
        unknown = [plan['inflight']] if plan.get('inflight') else []
        outcome.progress = dict(completed=done, remaining=[s for s in steps if s not in done and s not in unknown],
                                unknown=unknown)
        if unknown:
            outcome.error_class, outcome.recovery = 'OUTCOME_UNKNOWN', dict(action='reconcile', target=plan['id'])
        elif done:
            outcome.error_class, outcome.recovery = 'PARTIAL_EXECUTION', dict(action='reconcile', target=plan['id'])

    def _cover(self, name, outcome):
        """The coverage ledger (S7, 9.1): per scope, which files a search enumerated and
        which of them were reviewed (read, or cited), excluded (with why) or not reviewed.
        A changed library makes a new snapshot; unchanged files keep their review, marked
        inherited, and new or changed ones start unreviewed. Local facts only."""
        from agent.coverage import record_scan, record_review
        ledger = (self.current or {}).get('coverage') or self.coverage
        if name == 'search_content' and outcome.data.get('coverage'):
            ledger = record_scan(ledger, outcome.data['coverage'], self.current.get('request_id') if self.current
                                 else self.cfg.get('request_id'))
        elif name == 'read_file' and self._args.get('path'):
            ledger = record_review(ledger, self.sb, [self._args['path']], '读过正文')
        elif name == 'report_findings' and outcome.data.get('cited'):
            files = self.sb.artifacts.data['files']
            evidence = self.sb.artifacts.data['evidence']
            paths = [files[evidence[e]['file_id']]['path'] for e in outcome.data['cited']
                     if e in evidence and evidence[e]['file_id'] in files]
            ledger = record_review(ledger, self.sb, paths, '引用片段')
        else:
            return
        self.coverage = ledger
        if self.current is not None and ledger:
            self.current['coverage'] = ledger

    def record_result(self, name, outcome):
        """The typed result of one call, recorded once by its result_id (S6b): the
        observation this call made gets its ID, error class and recovery, and the run
        keeps a bounded ledger for the trace and reports."""
        if not outcome.result_id or outcome.result_id in self.results:
            return
        row = dict(tool=name, status=outcome.status, error_code=outcome.error_code, error_class=outcome.error_class,
                   recovery=(outcome.recovery or {}).get('action'), references=outcome.references[:20],
                   operations=len(outcome.operation_refs), progress=outcome.progress)
        self.results[outcome.result_id] = row
        if outcome.status == 'ok':
            self._cover(name, outcome)
        obs = getattr(self, '_last_observation', None)
        if obs is not None and self.current and obs.get('run_id') == self.run_id and 'result_id' not in obs:
            obs.update(result_id=outcome.result_id, error_class=outcome.error_class, recovery=row['recovery'],
                       **({'progress': outcome.progress} if outcome.progress else {}))
            try: self.store.checkpoint(self.current)
            except ValueError: pass  # a stale task cannot overwrite the newer version
        self._last_observation = None

    def _consumers(self, collection):
        """Bind a new object to the step goals whose conditions verify that collection."""
        kinds = {'evidence': {'evidence_valid'}, 'classification_plans': {'plan_ready', 'plan_applied'},
                 'receipts': {'receipt_verified'}}.get(collection, set())
        goals = {g['id']: g for g in self.current['goals']}
        active = active_goals(self)
        return [g for g in active if any(c['kind'] in kinds for c in goals[g]['conditions'])] or active

    def stats(self):
        return dict(task='structured', route='task_controller', execution='model_and_local',
                    queries=self.queries, reads=self.reads, blocked_calls=self.blocked,
                    task_id=self.current['id'] if self.current else None,
                    task_revision=self.current['revision'] if self.current else None,
                    task_status=self.current['status'] if self.current else 'not_registered',
                    document_cache=getattr(self.sb, '_document_cache_stats', {}),
                    usage=self.current.get('usage', {}) if self.current else {},
                    error_classes=dict(Counter(r['error_class'] for r in self.results.values() if r['error_class'])),
                    checks=self.current.get('checks', []) if self.current else [])
