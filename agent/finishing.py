"""Ending a run: finish_task's local verification, the chosen option's scope,
inspect_run, a plain final answer and an interrupted run (a mixin of TaskRuntime)."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from agent import choices
from agent.intent import parts_text
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
        ops = [o for o in self.current.get('operation_history', []) + self.operations if o.get('run_id') == self.run_id]
        def executed(a):
            path = self._resolve(a['path'])
            dst = self._resolve(a['destination']) if a.get('destination') else None
            kinds = {'trash'} if a['effect'] == 'trash' else {'move', 'rename'}
            return any(o['op'] in kinds and o['src'] == path and
                       (a['effect'] == 'trash' or not dst or o['dst'] == dst or Path(o['dst']).parent == Path(dst)) for o in ops)
        if not all(executed(a) for a in chosen['actions'] if a['effect'] != 'read'):
            return
        touched = {g for b in self.current.get('file_plan_bindings', {}).values() for g in b['goal_ids']} | \
                  {g for o in self.current.get('operation_history', []) + self.operations for g in (o.get('goal_ids') or [])}
        # "Do nothing" was the choice (no actions): a part that only records it (an answer
        # part, reviewed by hand) is settled by the choice too, not left for review.
        nothing = not [a for a in chosen['actions'] if a['effect'] != 'read']
        excluded = [g['id'] for g in self.current['goals'] if g['id'] not in touched
                    and (any(c['kind'] in self.WRITE_CONDITIONS for c in g['conditions'])
                         or nothing and all(c['kind'] == 'manual_review' for c in g['conditions']))]
        choices.record_choice(self.current, chosen['key'], chosen['label'], excluded)

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
            if self.blocked and self.operations:
                self.final_answer = '本轮存在被拦截的操作，未形成可核验的任务记录，不能确认完成。'
                self.stop_reason = 'unverified_answer'
                return
            self.final_answer = content or '本轮没有返回内容。'
            if self.blocked:
                # Without a task no write was opened. Keep the reply (often a
                # question for the user) and state the local facts beside it.
                reason = self.blocked_reasons[-1].splitlines()[0][:200] if self.blocked_reasons else ''
                self.final_answer += (f'\n\n本轮未执行任何文件操作；{self.blocked} 次调用被拦截'
                                      + ('，最近原因：' + reason if reason else '') + '。')
            self.stop_reason = 'answered'
            self.report = dict(version=1, state='answered', summary=self.final_answer, sections=[])

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
        args = FinishTask.model_validate_json(raw)
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
        self.store.checkpoint(self.current)
        self.stop_reason = 'done' if state == 'completed' else state
        self.final_answer = self.report['summary']
        self.finished = True
        return registry.ToolResult(status='ok', text='OK: 已核验并保存任务状态：' + state)
