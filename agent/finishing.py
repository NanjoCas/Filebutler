"""Ending a run: finish_turn (reply, question or task), finish_task's local
verification, the chosen option's scope, inspect_run, a plain final answer and an
interrupted run (a mixin of TaskRuntime).

Routing redesign S5 (2026-10-25): with strict_terminal on (default) every turn ends
by finish_turn. ask_user and finish_task are no longer shown to the model; calls to
them from older histories still run through the same code (_ask, _finish).
"""
from __future__ import annotations

import json
from pathlib import Path

from typing import Literal

from pydantic import Field

from agent import choices
from agent.asking import AskUser, Choice, Draft, Scope
from agent.intent import InputResolution, parts_text
from agent.reporting import build_report
from agent.task import StrictModel, task_state
from agent.verifier import verify_task
from tools import registry


class Unfinished(StrictModel):
    part_id: str = Field(description='没有做到的部分的 ID')
    reason: str = Field(min_length=1, max_length=300, description='哪些对象没处理、为什么')


class FinishTask(StrictModel):
    summary: str = Field('', max_length=12000, description='默认简短补充；用户要求详细时按 policy.output 提供必要解释，不替代本地核验')
    reply: str = Field('', max_length=2000, description='用户在请求中要你告诉他的内容（如哪些文件、数量、无法执行的原因），'
                       '会显示在主回复里；没有就留空，不要复述已执行的操作')
    unfinished: list[Unfinished] = Field(default_factory=list, max_length=10,
        description='用户要求了、但没有做到的部分（被拒绝、无法完成或你决定不做）；这些部分会标为未完成。全部做到时留空')
    not_needed: list[Unfinished] = Field(default_factory=list, max_length=10,
        description='因为没有对象而不需要操作、又无法用空选择集计划证明的部分；结果只会是待核实，不会是完成。'
                    '能用 prepare_file_plan 的选择集证明为空时优先用它')


class EvidenceBinding(StrictModel):
    part: str = Field(description='查找部分的 ID')
    refs: list[str] = Field(min_length=1, max_length=20, description='支持该部分结论的 E 标识')


class FinishTurn(StrictModel):
    mode: Literal['reply', 'ask', 'task'] = Field(
        description='reply 回答或说明；ask 提问并等待用户选择；task 已登记任务时收尾，本地逐部分核验')
    reply: str = Field('', max_length=12000, description='给用户的主回复；mode=task 时只写用户要你告诉他的内容')
    # mode=ask: the question's fields sit here, not in a nested object (round 40, 2026-10-25:
    # 11 calls put them at the top level or invented question_reason against the nested form).
    question: str = Field('', max_length=300, description='mode=ask 必填：问用户的一句话')
    reason: AskUser.model_fields['reason'].annotation | None = Field(
        None, description=AskUser.model_fields['reason'].description)
    options: list[Choice] = Field(default_factory=list, max_length=4, description='mode=ask 必填：' + AskUser.model_fields['options'].description)
    allow_other: bool = AskUser.model_fields['allow_other']
    subject: list[str] = AskUser.model_fields['subject']
    scope: Scope | None = AskUser.model_fields['scope']
    drafts: list[Draft] = AskUser.model_fields['drafts']
    summary: str = FinishTask.model_fields['summary']
    unfinished: list[Unfinished] = Field(default_factory=list, max_length=10, description='没有做到的部分，会标为未完成')
    not_needed: list[Unfinished] = Field(default_factory=list, max_length=10, description='没有对象而无需操作的部分，结果待核实')
    input_resolution: InputResolution | None = Field(None, description='消息回应上一问、要暂停或取消时必填')
    evidence_bindings: list[EvidenceBinding] = Field(default_factory=list, max_length=10,
        description='多个查找部分时写明各自的证据')


TERMINAL_REQUIRED = ('[本地协议] TERMINAL_REQUIRED：刚才的回复没有调用 finish_turn，用户还看不到。'
                     '请调用 finish_turn 重新给出：直接回答用 mode=reply；要用户选择或确认用 mode=ask 并给出选项；'
                     '已登记任务用 mode=task。')


class InspectRun(StrictModel):
    run_id: str | None = Field(None,description='当前会话的运行 ID；留空查看最近一次非查询运行')
    finish: bool = Field(True, description='仅查询时结束；查询后还需处理其他请求时设 false，不会自动恢复任务')


class Finishing:
    """Needs the TaskRuntime state: store, current, operations, run_id, report fields."""

    WRITE_CONDITIONS = {'file_plan_verified', 'receipt_verified', 'plan_applied'}

    def _record_chosen_scope(self):
        """User decision 2026-10-22: after a choice, completion is judged by the chosen scope.

        When every action the chosen option declared was executed in this task, write
        parts that nothing was done for are excluded by the choice, and the model's
        "unfinished" notes become explanations. Matched by structure, not by text;
        if any declared action is missing, nothing changes.
        """
        chosen = self.chosen_option
        if not chosen or not self.current: return
        scoped = self.scope_goals()
        if scoped is not None:
            return self._record_scoped_choice(chosen, scoped)
        excluded = self._excluded_by_choice(chosen, [g['id'] for g in self.current['goals']])
        if excluded is not None:
            choices.record_choice(self.current, chosen['key'], chosen['label'], excluded)

    def _touched(self):
        return {g for b in self.current.get('file_plan_bindings', {}).values() for g in b['goal_ids']} | \
               {g for o in self.current.get('operation_history', []) + self.operations for g in (o.get('goal_ids') or [])}

    def _excluded_by_choice(self, chosen, candidates):
        """Parts among ``candidates`` the choice leaves out, or None when a declared action is missing."""
        ops = [o for o in self.current.get('operation_history', []) + self.operations if o.get('run_id') == self.run_id]
        def executed(a):
            path = self._resolve(a['path'])
            dst = self._resolve(a['destination']) if a.get('destination') else None
            kinds = {'trash'} if a['effect'] == 'trash' else {'move', 'rename'}
            return any(o['op'] in kinds and o['src'] == path and
                       (a['effect'] == 'trash' or not dst or o['dst'] == dst or Path(o['dst']).parent == Path(dst)) for o in ops)
        if not all(executed(a) for a in chosen['actions'] if a['effect'] != 'read'):
            return None
        touched = self._touched()
        # "Do nothing" was the choice (no actions): a part that only records it (an answer
        # part, reviewed by hand) is settled by the choice too, not left for review.
        nothing = not [a for a in chosen['actions'] if a['effect'] != 'read']
        # Empty actions cannot tell "do nothing at all" from "leave b alone", so a write
        # part is left out by such a choice only when the model names it as not done.
        named = {u['part_id'] for key in ('items', 'not_needed') for u in self.current['declared_unfinished'][key]}
        return [g['id'] for g in self.current['goals'] if g['id'] in candidates and g['id'] not in touched
                and (any(c['kind'] in self.WRITE_CONDITIONS for c in g['conditions']) and (not nothing or g['id'] in named)
                     or nothing and all(c['kind'] == 'manual_review' for c in g['conditions']))]

    def _record_scoped_choice(self, chosen, scoped):
        """A choice about some parts settles only those (routing redesign S3, 5.1): skip
        leaves them out when nothing was done for them; any other option settles them by
        the unscoped rule above, limited to the scope, so skipping b never settles an
        untouched a. Round 42: models mostly mark "move only a" or "leave it" as continue."""
        if chosen.get('disposition') == 'skip':
            excluded = [g for g in scoped if g not in self._touched()]
        else:
            excluded = self._excluded_by_choice(chosen, scoped) or []
        choices.record_choice(self.current, chosen['key'], chosen['label'], excluded,
                              question_id=(self.answered_choice or {}).get('question_id'), scope=scoped)

    def interrupt(self, reason):
        if self.current and self.final_answer is None:
            self.current['status'] = 'interrupted'
            self.current['interruption_reason'] = reason
            self.store.checkpoint(self.current)

    ASK_FIELDS = ('question', 'reason', 'options', 'allow_other', 'subject', 'scope', 'drafts')

    def _finish_turn(self, raw, sb):
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get('question'), dict):
            # The nested form of 2026-10-25 S5, still in some histories: unfold it (not in the schema).
            data = {**{k: v for k, v in data.items() if k != 'question'}, **data['question']}
        args = FinishTurn.model_validate(data)
        if args.input_resolution and self.input_relation is None:
            self.declare_relation(args.input_resolution, terminal=True)
            if self.finished:  # paused or cancelled: the local report is the turn's answer
                return registry.ToolResult(status='ok', text=f'OK: 已{"暂停" if self.stop_reason == "paused" else "取消"}这个请求，本轮结束')
        given = args.model_fields_set & set(self.ASK_FIELDS)
        if args.mode == 'ask':
            if not args.question or not args.reason or not args.options:
                raise ValueError('mode=ask 需要 question、reason 和 2–4 个 options')
            # Asking pauses the turn; it settles nothing, so fields that settle are refused, not dropped
            # (S5 audit 2026-10-25, P2-A). reply is shown before the question.
            settling = [k for k in ('summary', 'unfinished', 'not_needed', 'evidence_bindings') if getattr(args, k)]
            if settling:
                raise ValueError('mode=ask 只提问并等待用户，不能带 ' + '、'.join(settling) + '；收尾时再用 mode=task 写')
            return self._ask(AskUser(**{k: getattr(args, k) for k in self.ASK_FIELDS}), sb, preface=args.reply)
        if given - {'allow_other', 'subject'} and any(getattr(args, k) for k in given):
            raise ValueError('只有 mode=ask 才填 question / options 等提问字段；要用户选择请用 mode=ask')
        if args.evidence_bindings:
            self._bind_evidence(args.evidence_bindings)
        if self.current:
            # A registered task is verified whichever of reply / task ends it, and reply stays the
            # user's main reply in both (S5 audit 2026-10-25, P1-A: it was moved into the details).
            if len(args.reply) > 2000:
                raise ValueError('已登记任务时 reply 最多 2000 字（显示在主回复）；更长的补充放 summary')
            return self._finish(FinishTask(summary=args.summary, reply=args.reply,
                                           unfinished=args.unfinished, not_needed=args.not_needed), sb)
        if args.mode == 'task':
            raise ValueError('尚未登记任务，不能用 mode=task 收尾；直接回答用 mode=reply')
        if args.unfinished or args.not_needed:
            raise ValueError('未登记任务时没有部分可写 unfinished / not_needed')
        if not args.reply.strip():
            raise ValueError('mode=reply 需要 reply')
        self._reply(args.reply)
        if self.stop_reason == 'answered' and self._nothing_chosen():
            from agent.reporting import by_choice_report
            self.report = by_choice_report(self.answered_choice['chosen'], args.reply)
            self.final_answer = self.report['summary']
            self._settle_request()
        return registry.ToolResult(status='ok', text='OK: 已回复用户，本轮结束')

    def _bind_evidence(self, bindings):
        """Claims of evidence for find parts (routing redesign S4, 6.3). Each claim is
        checked locally: the part verifies evidence, and the evidence is this session's,
        still at the version it was taken from. A valid claim does not make the answer
        right; the semantic review stays."""
        if not self.current:
            raise ValueError('未登记任务时没有部分可写 evidence_bindings')
        from agent.verifier import CONSUMERS
        goals = {g['id']: g for g in self.current['goals']}
        artifacts = self.sb.artifacts
        claims = []
        for b in bindings:
            goal = goals.get(b.part)
            if not goal:
                raise ValueError(f'evidence_bindings 引用了不存在的部分：{b.part}；当前部分：' + parts_text(self.current))
            if not any(c['kind'] in CONSUMERS['evidence'] for c in goal['conditions']):
                raise ValueError(f'部分 {b.part} 不由证据核验，不能绑定证据')
            for ref in b.refs:
                eid = ref.removeprefix('evidence:')
                try: artifacts.resolve_evidence(eid)
                except ValueError as exc:
                    raise ValueError(f'证据 {ref} 不能用于部分 {b.part}：{exc}') from exc
                claims.append(dict(collection='evidence', id=eid, goal_ids=[b.part], claimed=True, run_id=self.run_id))
        known = {(r['collection'], r['id'], tuple(r.get('goal_ids', []))) for r in self.current.get('references', [])}
        self.current.setdefault('references', []).extend(
            c for c in claims if (c['collection'], c['id'], tuple(c['goal_ids'])) not in known)

    def _nothing_chosen(self):
        """The user pressed an option that does nothing, and this run changed nothing."""
        chosen = (self.answered_choice or {}).get('chosen') or {}
        return bool(chosen) and chosen.get('actions') == [] and chosen.get('disposition') != 'cancel_request' \
            and not self.operations

    def _settle_request(self):
        rid = (self.current or {}).get('request_id') or self.cfg.get('request_id')
        a = self.answered_choice or {}
        if rid and a.get('chosen'):
            self._interactions().settle(rid, dict(question_id=a.get('question_id'), option_id=a['chosen'].get('option_id'),
                                                  key=a['chosen']['key'], label=a['chosen']['label'], result='by_choice'))

    def unstructured(self, content):
        """Strict terminal: the model ended with bare text twice. The draft is kept, the
        turn is not an answer, and nothing more runs (routing redesign 4.1)."""
        if self.current:
            self.interrupt('unstructured_terminal')
        draft = (content or '').strip()
        self.final_answer = ((draft + '\n\n') if draft else '') + '这一轮的回复没有形成完整的交互（可能缺少需要你选择的选项），请重试或换个说法。'
        self.stop_reason = 'unstructured_terminal'
        self.report = dict(version=1, state='unstructured_terminal', summary=self.final_answer, sections=[])

    def finish_plain(self, content):
        if self.current:
            detail = self.current.get('policy', {}).get('output', {}).get('detail')
            self.dispatch('finish_task', json.dumps({'summary': (content or '')[:12000 if detail == 'full' else 1200]}),
                          self.sb, None, None)
        if self.final_answer is None:
            self._reply(content or '本轮没有返回内容。')

    def _reply(self, content):
        """An answer without a task: the reply, with the local facts of any blocked call."""
        if self.blocked and self.operations:
            self.final_answer = '本轮存在被拦截的操作，未形成可核验的任务记录，不能确认完成。'
            self.stop_reason = 'unverified_answer'
            return
        self.final_answer = content
        if self.blocked:
            # Without a task no write was opened. Keep the reply (often a
            # question for the user) and state the local facts beside it.
            reason = self.blocked_reasons[-1].splitlines()[0][:200] if self.blocked_reasons else ''
            self.final_answer += (f'\n\n本轮未执行任何文件操作；{self.blocked} 次调用被拦截'
                                  + ('，最近原因：' + reason if reason else '') + '。')
        self.stop_reason = 'answered'
        self.report = dict(version=1, state='answered', summary=self.final_answer, sections=[])
        self.finished = True  # later calls in the same response are TURN_CLOSED and leave this reply alone

    def _inspect_run(self, raw):
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
        return registry.ToolResult(status='ok',text='OK: '+explanation)

    def _finish_task(self, raw, sb):
        return self._finish(FinishTask.model_validate_json(raw), sb)

    def _finish(self, args, sb):
        if not self.current:
            raise ValueError('尚未登记任务，不能声称完成')
        unknown = [u.part_id for u in args.unfinished + args.not_needed if u.part_id not in {g['id'] for g in self.current['goals']}]
        if unknown:
            raise ValueError('unfinished 引用了不存在的部分：' + '、'.join(unknown) + '；当前部分：' + parts_text(self.current))
        # The model's own "not done" only lowers the state; local checks still decide "done".
        self.current['declared_unfinished'] = dict(revision=self.current['revision'],
                                                   items=[u.model_dump() for u in args.unfinished],
                                                   not_needed=[u.model_dump() for u in args.not_needed])
        self._record_chosen_scope()
        if getattr(self, 'coverage', None) and not self.current.get('coverage'):
            self.current['coverage'] = self.coverage  # searched before the task existed (S7)
        self.current['checks'] = verify_task(self.current, sb)
        state = task_state(self.current)
        self.current['status'] = state
        self.current['model_summary'] = args.summary
        self.current['operations'] = self.operations
        # A revision can change inside one run; report all of this
        # run's actions without duplicating the persistent ledger.
        run_ops = [row for row in self.current.get('operation_history', [])
                   if row.get('run_id') == self.run_id] + self.operations
        self.current['model_reply'] = args.reply
        self.report = build_report(dict(self.current, operations=run_ops), args.summary, self.output_records,
                                   reply=args.reply)
        self.current['report'] = self.report
        if self.report.get('result') == 'by_choice':
            self._settle_request()
        self.store.checkpoint(self.current)
        self.stop_reason = 'done' if state == 'completed' else state
        self.final_answer = self.report['summary']
        self.finished = True
        return registry.ToolResult(status='ok', text='OK: 已核验并保存任务状态：' + state)
