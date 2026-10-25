"""ask_user: the question, its options checked locally before the user sees them,
and the user's answer resolved for the next turn (a mixin of TaskRuntime)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from agent.intent import parts_text
from agent.task import StrictModel, is_legacy
from tools import registry


class ChoiceAction(StrictModel):
    effect: Literal['move', 'rename', 'trash', 'read'] = Field(description='此选项会做的操作')
    path: str = Field(description='操作对象的相对路径')
    destination: str | None = Field(None, description='move/rename 的目标；可写目录')


class Draft(StrictModel):
    id: str = Field(min_length=1, max_length=20, description='短 ID，如 d_b')
    description: str = Field(min_length=1, max_length=200, description='这部分要做什么')
    subjects: list[str] = Field(default_factory=list, max_length=20, description='涉及的路径')


class Scope(StrictModel):
    level: Literal['request', 'parts'] = Field(description='request 整个请求；parts 只关于 part_ids')
    part_ids: list[str] = Field(default_factory=list, max_length=10, description='任务的部分 ID；未登记时是 drafts 的 ID')


class Choice(StrictModel):
    label: str = Field(min_length=1, max_length=80, description='一个可以直接执行的具体选项，如“只移动 Projects/alpha/report.txt”')
    detail: str = Field('', max_length=200, description='选这个会怎样；可留空')
    consent: Literal['include_hidden'] | None = Field(None, description='仅当此选项表示“同意连同隐藏文件一起回收”时填 include_hidden')
    actions: list[ChoiceAction] | None = Field(None, max_length=20,
        description='此选项会执行的操作，本地在显示前检查；不做操作写空列表')
    disposition: Literal['skip', 'continue', 'cancel_request'] | None = Field(None,
        description='通常不填：按 actions 执行，只是先不动就写空 actions；skip 仅跳过 scope.level=parts 的部分；cancel_request 仅当取消整个请求（scope.level=request）')


class AskUser(StrictModel):
    question: str = Field(min_length=1, max_length=300, description='问用户的一句话，说清缺什么信息')
    reason: Literal['multiple_candidates', 'missing_criterion', 'conflict', 'scope_beyond_request', 'cannot_do_as_asked'] = Field(
        description='为什么必须问（任务协议列出的须问情况）')
    options: list[Choice] = Field(min_length=2, max_length=4, description='2–4 个互斥的具体选项，推荐的放第一个')
    allow_other: bool = Field(True, description='是否允许用户不选而自己补充说明')
    subject: list[str] = Field(default_factory=list, max_length=20, description='问题涉及的路径；询问能否回收含隐藏文件的文件夹时必填')
    scope: Scope | None = Field(None, description='只关于部分目标时必填，选项只影响这些部分')
    drafts: list[Draft] = Field(default_factory=list, max_length=10, description='未登记任务时 scope 涉及的部分；登记时 draft_id 对应')


CHOICE_KEYS = 'ABCDE'  # the model gives up to four; local may add one


class Asking:
    """Needs the TaskRuntime state: store, current, operations, sb paths (_resolve)."""

    def _answered_choice(self, instruction):
        """The user's reply to the last ask_user.

        Since routing redesign S2 an option is chosen only by an exact answer the
        message was received with (cfg answer_id: question id, version and option id,
        agent/interactions.py). Typed text, a bare letter included, chooses nothing:
        the run is told it may be the answer, and it grants no option's consent.
        """
        from agent.interactions import Interactions
        repo = Interactions(self.store)
        if self.cfg.get('answer_id'):
            question, option = repo.answered(self.cfg['answer_id'])
            asked = question['data']
            return dict(question=asked['question'], chosen=option, text=None, subject=asked.get('subject', []),
                        task_id=question['task_id'], question_id=question['question_id'],
                        scope=asked.get('scope'), drafts=asked.get('drafts', []))
        if self.cfg.get('receipt_id'):
            question = repo.replied_to(self.cfg['receipt_id'])
            if not question: return None
            asked = question['data']
        else:
            # Not received through a receipt (a library caller): the last run's question, unchosen.
            try: last = self.store.outcome()
            except ValueError: return None
            asked = last.get('pending_choice')
            if not asked: return None
            question = dict(task_id=last.get('task_id'), question_id=asked.get('question_id'))
        return dict(question=asked['question'], chosen=None, text=instruction.strip(), subject=asked.get('subject', []),
                    task_id=question['task_id'], question_id=question['question_id'])

    def _resume_asked_task(self):
        """A chosen option goes back to the task its question was asked in, with its
        progress, limits and budget; typed text may be a new request and resumes nothing."""
        a = self.answered_choice
        if not a or not a['chosen'] or not a.get('task_id'): return
        try: task = self.store.get(a['task_id'])
        except ValueError: return
        if is_legacy(task) or task.get('cancelled') or not task.get('pending_choice'): return
        task.pop('pending_choice')
        # Earlier runs' operations join the history (as file_plans._reconcile does), so
        # this run's own ledger, written at finish, does not replace them.
        history = task.setdefault('operation_history', [])
        history += [row for row in task.get('operations', []) if row not in history]
        task['operations'] = []
        self.current = task

    def _subject_trees(self, subject, sb):
        from agent.file_plans import scan_tree
        for path in subject:
            try: folder = sb.path(self._resolve(path), exact=True)
            except Exception: continue
            if folder.is_dir() and not folder.is_symlink():
                yield path, folder, scan_tree(folder)

    def _ground_choices(self, choices, subject, sb):
        """Check each option's declared actions against what the tools can do.

        Runs before the user sees the question: an option the tools cannot carry
        out is sent back to the model to rewrite. An option whose actions recycle
        a folder with other hidden files means consent, and gets the one warning
        line the user needs. Options without declared actions are shown as given.
        """
        problems = []
        for c in choices:
            actions = c.get('actions') or []
            moved = {self._resolve(a['path']) for a in actions if a['effect'] in {'move', 'rename'}}
            hidden = []
            for a in actions:
                why, found = self._action_check(a, moved, sb)
                if why: problems.append(f"选项 {c['key']}（{c['label'][:30]}）：{why}")
                hidden += found
            why, replaced = self._overwrite_check(actions, sb)
            if why: problems.append(f"选项 {c['key']}（{c['label'][:30]}）：{why}")
            if replaced: c['overwrite'] = replaced
            reads = [self._resolve(a['path']) for a in actions if a['effect'] == 'read']
            sensitive_reads = [r for r in reads if self._is_sensitive(r, sb)]
            if sensitive_reads: c['read_grant'] = sensitive_reads
            if hidden:
                c['consent'] = 'include_hidden'
                c['warn'] = '、'.join(Path(h).name for h in hidden[:3]) + ('等' if len(hidden) > 3 else '')
        if problems:
            raise ValueError('以下选项包含工具做不到的操作，用户还没有看到这个问题；请改写或删掉这些选项后重新提问：'
                             + '；'.join(problems))
        trees = list(self._subject_trees(subject, sb))
        hidden = [f'{p}/{rel}' for p, _, t in trees for rel in t['hidden']]
        if hidden and not any(c.get('consent') == 'include_hidden' for c in choices) and len(choices) < len(CHOICE_KEYS):
            names = '、'.join(Path(h).name for h in hidden[:3]) + ('等' if len(hidden) > 3 else '')
            choices.append(dict(key=CHOICE_KEYS[len(choices)], label=f'连同隐藏文件 {names} 一起回收（可撤销）',
                                detail='', consent='include_hidden', local=True, warn=names,
                                actions=None))  # consent only: it does not bound the other operations
        # A target folder that does not exist (user decision 2026-10-22): options into
        # existing folders, the candidates of similar meaning, come first, and cancelling
        # is always offered. Decided by declared actions, not wording.
        new = [c for c in choices if self._creates_folder(c, sb)]
        if new:
            choices.sort(key=lambda c: 2 if c.get('actions') == [] else 1 if c in new else 0)
            if not any(c.get('actions') == [] for c in choices) and len(choices) < len(CHOICE_KEYS):
                choices.append(dict(label='取消这部分，不移动', detail='', consent=None, local=True, actions=[]))
            for key, c in zip(CHOICE_KEYS, choices): c['key'] = key
        return choices

    def _question_scope(self, args):
        """The question's scope, checked locally (routing redesign S3, 4.2-4.3).

        parts: the ids are the current task's parts, or, before a task exists, the
        question's own drafts. A skip option needs parts; cancelling the request
        needs scope request. Without a scope the question is about the whole
        request as before, and an empty option is not read as cancelling it.
        """
        scope = args.scope
        drafts = {d.id for d in args.drafts}
        if len(drafts) != len(args.drafts):
            raise ValueError('drafts 的 ID 重复')
        for o in args.options:
            if o.disposition == 'cancel_request' and (not scope or scope.level != 'request'):
                raise ValueError(f'选项“{o.label[:20]}”标了 cancel_request：只有取消整个请求才用它，scope.level 须为 request；只是先不动就去掉 disposition、actions 写空列表')
            if o.disposition == 'skip' and (not scope or scope.level != 'parts'):
                raise ValueError(f'选项“{o.label[:20]}”标了 skip：须用 scope.level=parts 写明跳过哪些部分；只是先不动就去掉 disposition、actions 写空列表')
        if not scope:
            return None
        if scope.level == 'request':
            return dict(level='request', part_ids=[])
        if not scope.part_ids:
            raise ValueError('scope.level=parts 需要 part_ids')
        known = {g['id'] for g in self.current['goals']} if self.current else drafts
        unknown = [p for p in scope.part_ids if p not in known]
        if unknown:
            where = ('当前任务的部分：' + parts_text(self.current) if self.current
                     else '本问题 drafts 的 ID：' + '、'.join(sorted(drafts) or ['（无）']))
            raise ValueError('scope.part_ids 引用了不存在的部分：' + '、'.join(unknown) + '；' + where)
        return dict(level='parts', part_ids=list(scope.part_ids), drafts=not self.current)

    def scope_goals(self):
        """Goal ids the answered option is about: None (the whole request, as before scopes),
        or the parts the question named; a draft maps to a part by Part.draft_id only."""
        a = self.answered_choice or {}
        scope = a.get('scope')
        if not a.get('chosen') or not scope:
            return None
        if scope['level'] == 'request':
            return [g['id'] for g in (self.current or {}).get('goals', [])]
        if not scope.get('drafts'):
            return list(scope['part_ids'])
        mapped = {draft: goal for goal, draft in (self.current or {}).get('draft_map', {}).items()}
        missing = [d for d in scope['part_ids'] if d not in mapped]
        if missing:
            raise ValueError('UNRESOLVED_QUESTION_SCOPE：用户的选择只关于草案部分 ' + '、'.join(missing) +
                             '；登记任务时在对应 Part 写 draft_id，本地才能据此处理')
        return [mapped[d] for d in scope['part_ids']]

    def _creates_folder(self, choice, sb):
        """The option moves something into a folder that does not exist yet."""
        for a in choice.get('actions') or []:
            if a['effect'] not in {'move', 'rename'} or not a.get('destination'): continue
            try: dst = sb.path(self._resolve(a['destination']), exact=True)
            except Exception: continue
            if dst.exists(): continue
            # "归档" names a folder to put the file in; "归档/a.txt" a path in it.
            if not dst.parent.exists() or (not dst.suffix and dst.name != Path(a['path']).name):
                return True
        return False

    def _overwrite_check(self, actions, sb):
        """(why not, files this option overwrites). A move onto an existing file is an
        overwrite only when the same option also recycles that file."""
        from agent.file_plans import free_name
        trashed = set()
        for a in actions:
            if a['effect'] == 'trash':
                try: trashed.add(sb.path(self._resolve(a['path']), exact=True))
                except Exception: pass
        replaced = []
        for a in actions:
            if a['effect'] not in {'move', 'rename'} or not a.get('destination'): continue
            try:
                source = sb.path(self._resolve(a['path']), exact=True)
                target = sb.path(self._resolve(a['destination']), exact=True)
            except Exception: continue
            if target.is_dir() and not (source.exists() and os.path.samefile(target, source)):
                target = target / source.name  # mv semantics: into the folder, keeping the name
            if not target.is_file() or (source.exists() and os.path.samefile(source, target)): continue
            rel = sb.rel(target)
            if any(os.path.samefile(target, t) for t in trashed if t.exists()):
                replaced.append(rel)
            else:
                return (f'{rel} 已有同名文件。保留两份请改用新名字（如 {free_name(sb, rel)}）；'
                        f'覆盖请在同一选项里同时 trash {rel}'), []
        return None, replaced

    def _is_sensitive(self, rel, sb):
        from agent.sensitive import file_reasons
        try: return bool(sb.path(rel, exact=True).is_file() and file_reasons(sb, rel))
        except Exception: return False

    def _action_check(self, action, moved, sb):
        """(why the tools cannot do it or None, hidden files it would recycle)."""
        from agent.file_plans import scan_tree
        from agent.system_files import match
        path = self._resolve(action['path'])
        try: p = sb.path(path, exact=True)
        except Exception: return f'{path} 不在资料库中', []
        if not p.exists(): return f'{path} 不存在', []
        hidden = any(part.startswith('.') for part in Path(path).parts)
        entry = match(p.name, is_dir=False) if p.is_file() else None
        locked = bool(entry and entry['tier'] == 'busy')
        effect = action['effect']
        if effect == 'read':
            return ('工具不能读取隐藏文件：' + path, []) if hidden else (None, [])
        if effect in {'move', 'rename'}:
            if hidden: return '工具不能移动隐藏文件或隐藏文件夹：' + path, []
            if locked: return f'{path} 是锁文件，不能移动', []
            if p.is_dir():
                t = scan_tree(p)
                if t['busy']: return f"{path} 中有正被打开的文档（{'、'.join(t['busy'][:3])}），不能整体移动", []
                if t['special'] or t['hidden']:
                    return f"{path} 含 {'、'.join((t['special'] + list(t['hidden']))[:3])}（隐藏项、链接或应用包），不能整体移动", []
            return None, []
        if hidden: return '隐藏对象只能随所在文件夹一起回收：' + path, []
        if locked: return f'{path} 是锁文件，不能回收', []
        if not p.is_dir(): return None, []
        t = scan_tree(p)
        if t['busy']: return f"{path} 中有正被打开的文档（{'、'.join(t['busy'][:3])}），回收会失败", []
        if t['special']: return f"{path} 含 {'、'.join(t['special'][:3])}（隐藏文件夹、链接或应用包），不能回收", []
        left = [v for v in t['visible'] if f'{path}/{v}' not in moved]
        if left: return f"{path} 还有 {len(left)} 个文件没有被此选项移走（如 {'、'.join(left[:3])}），计划不会回收它", []
        return None, [f'{path}/{h}' for h in t['hidden']]

    def _sensitive_lines(self, subject, sb):
        """Local facts appended to a question about recycling folders: suspected
        secrets in their hidden files (kind and line; the chat history reaches the
        model, so no masked values here)."""
        from agent.file_plans import hidden_reports
        from agent.secrets_scan import describe
        found = [describe(f'{path}/{rel}', r, masked=False)
                 for path, folder, tree in self._subject_trees(subject, sb)
                 for rel, r in hidden_reports(folder, tree['hidden']).items()]
        return ['', '本地检查：' + '；'.join(found) + '。建议保留。'] if found else []

    def _choice_text(self):
        a = self.answered_choice
        if a['chosen']:
            c = a['chosen']
            text = f"用户回答了你上一轮的提问“{a['question']}”：选择 {c['key']}. {c['label']}" + (f"（{c['detail']}）" if c['detail'] else '') + '。按此执行。'
            if self.current:
                text += (f"已接回提问时的任务 {self.current['id']}（部分：{parts_text(self.current)}），不必重新登记；"
                         '按此选择不做的部分在 finish_turn.unfinished 写明。')
            return text
        # Typed text may answer the question or start something else; nothing local tells them apart.
        return (f"你上一轮提问了“{a['question']}”，用户没有选择选项。本轮消息可能是对它的回答，也可能是新的请求："
                '是回答就据此继续；是新请求就按新请求处理，不要沿用上一问的选项。')

    def _ask_user(self, raw, sb):
        return self._ask(AskUser.model_validate_json(raw), sb)

    def _ask(self, args, sb, preface=''):
        """preface: what the model said before the question (finish_turn.reply in mode=ask)."""
        scope = self._question_scope(args)
        choices = [dict(key=CHOICE_KEYS[i], label=o.label, detail=o.detail, consent=o.consent,
                        actions=None if o.actions is None else [a.model_dump() for a in o.actions],
                        **({'disposition': o.disposition} if o.disposition else {}))
                   for i, o in enumerate(args.options)]
        choices = self._ground_choices(choices, args.subject, sb)
        for c in choices:
            # What the button says under its label: the detail, then each warning.
            c['warnings'] = ([f"会一起删除你看不到的 {c['warn']}"] if c.get('warn') else []) \
                + ([f"覆盖 {'、'.join(c['overwrite'])}，旧文件移入回收区（可撤销）"] if c.get('overwrite') else []) \
                + ([f"授权读取敏感文件 {'、'.join(c['read_grant'])} 的内容（仅本任务）"] if c.get('read_grant') else [])
        checked = [line for line in self._sensitive_lines(args.subject, sb) if line]
        prefix = [f'本轮已执行 {len(self.operations)} 项文件操作，可撤销。', ''] if self.operations else []
        if preface.strip():
            prefix += [preface.strip(), '']
        # The page shows the question and buttons; the terminal (and evaluation) read
        # the full text with lettered options.
        question = '\n'.join(prefix + [args.question] + ([''] + checked if checked else []))
        lines = [args.question, '']
        for c in choices:
            lines.append(f"{c['key']}. {c['label']}" + (f"——{c['detail']}" if c['detail'] else '')
                         + ''.join(f'　⚠ {w}' for w in c['warnings']) + ('　（系统补充）' if c.get('local') else ''))
        lines += self._sensitive_lines(args.subject, sb)
        lines += ['', '请回复字母' + ('，或直接输入你的要求。' if args.allow_other else '。')]
        text = '\n'.join(prefix + lines)
        subject = [self._resolve(p) for p in args.subject]
        for path in subject: sb.path(path, exact=True)
        from agent.interactions import Interactions
        # Stable question and option ids: an answer names them exactly (routing redesign S2).
        self.pending_choice = Interactions(self.store).record_question(
            dict(question=args.question, reason=args.reason, choices=choices, allow_other=args.allow_other, subject=subject,
                 scope=scope, drafts=[d.model_dump() for d in args.drafts]),
            run_id=self.run_id, task_id=(self.current or {}).get('id'), task_revision=(self.current or {}).get('revision'),
            request_id=self.cfg.get('request_id'))
        choices = self.pending_choice['choices']
        if self.current:
            self.current.update(status='waiting_input', pending_choice=self.pending_choice)
            self.store.checkpoint(self.current)
        self.final_answer, self.stop_reason, self.finished = text, 'waiting_input', True
        self.report = dict(version=1, state='waiting_input', summary=text, question=question, sections=[],
                           choices=choices, allow_other=args.allow_other,
                           question_id=self.pending_choice['question_id'], question_version=self.pending_choice['version'])
        return registry.ToolResult(status='ok', text='OK: 已向用户提问，本轮结束，等待用户回答')
