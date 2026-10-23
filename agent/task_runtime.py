"""Task-driven controller: no natural-language keyword routing or authorization."""
from __future__ import annotations

import json
import os
from pathlib import Path

from typing import Literal

from pydantic import Field

from agent.task import LEGACY_NOTE, StrictModel, TaskStore, is_legacy, task_state, add_blocker, resolve_blockers
from agent.constraints import validate_operations
from agent.verifier import verify_task
from agent.reporting import build_report
from agent.policy import active_goals
from safety.sandbox import SandboxError
from tools import registry
from agent.file_plans import PrepareFilePlan, ExecuteFilePlan
from agent import choices
from agent.intent import StartTask, ReviseTask, compile_start, compile_revision, project, parts_text


class ChoiceAction(StrictModel):
    effect: Literal['move', 'rename', 'trash', 'read'] = Field(description='此选项会做的操作')
    path: str = Field(description='操作对象的相对路径')
    destination: str | None = Field(None, description='move/rename 的目标；可写目录')


class Choice(StrictModel):
    label: str = Field(min_length=1, max_length=80, description='一个可以直接执行的具体选项，如“只移动 Projects/alpha/report.txt”')
    detail: str = Field('', max_length=200, description='选这个会怎样；可留空')
    consent: Literal['include_hidden'] | None = Field(None, description='仅当此选项表示“同意连同隐藏文件一起回收”时填 include_hidden')
    actions: list[ChoiceAction] | None = Field(None, max_length=20,
        description='此选项会执行的操作，供本地在显示前检查能否做到（不展示给用户）；不做任何操作写空列表')


class AskUser(StrictModel):
    question: str = Field(min_length=1, max_length=300, description='问用户的一句话，说清缺什么信息')
    reason: Literal['multiple_candidates', 'missing_criterion', 'conflict', 'scope_beyond_request', 'cannot_do_as_asked'] = Field(
        description='为什么必须问：multiple_candidates 有多个候选对象；missing_criterion 标准没有可执行的界限；'
                    'conflict 遇到冲突而用户没交代；scope_beyond_request 要动的对象超出用户说的范围；cannot_do_as_asked 无法按原样做到')
    options: list[Choice] = Field(min_length=2, max_length=4, description='2–4 个互斥的具体选项，推荐的放第一个')
    allow_other: bool = Field(True, description='是否允许用户不选而自己补充说明')
    subject: list[str] = Field(default_factory=list, max_length=20, description='问题涉及的路径；询问能否回收含隐藏文件的文件夹时必填')


CHOICE_KEYS = 'ABCDE'  # the model gives up to four; local may add one


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


# Field descriptions added to the tool schemas. Keys: top-level field, or
# "<$defs name>.<field>".
FIELD_DOCS = {
    'prepare_file_plan': {
        'description': '一句话说明此计划的用途，显示在确认卡片上',
        'actions': '逐项列出的操作；可与 selection 同时使用',
        'selection': '按目录规则选一批文件，由本地展开成清单',
        'Action.effect': 'create 建目录；move 移动（可跨目录）；rename 在原目录改名；trash 移入回收区（可撤销）',
        'Action.source': '执行前已有的文件或文件夹的相对路径，也可写 file:F 标识；文件夹整体移动（含隐藏文件或锁文件时不行）；create 不填',
        'Action.destination': '完整目标路径；move 也可写已有或本计划创建的目录，表示按原名放入，新目录以 / 结尾；trash 不填',
        'Selection.path': '在哪个目录里选文件',
        'Selection.extensions': '只选这些后缀，如 ["txt"]；空列表表示全部可见文件',
        'Selection.exclude_paths': '不选的子目录或文件',
        'Selection.recursive': 'true 包含所有子目录里的文件；false 只选该目录本层的文件（用户说“根目录”“本层”时设 false）',
        'Selection.effect': '对选中的文件执行 move 或 trash',
        'Selection.destination': 'move 的目标目录，文件按原名放入；不存在时由本地创建',
        'Selection.preserve_tree': 'true 在目标目录下保留原来的子目录结构；false 全部放进目标目录这一层',
    },
    'start_task': {
        'parts': '用户要求的各个部分；复合请求每个子目标一项，不要遗漏',
        'limits': '用户明确说出的限制；用户没说就留空，不要替用户添加',
        'detail': '最终回复详略：concise 简要（默认）；用户要求详细说明时用 full',
        'Part.description': '用用户的话说明这部分要做什么，包括对象和目标位置',
    },
    'revise_task': {
        'task_id': '要修改的任务 ID（见近期任务）',
        'expected_revision': '该任务当前的版本号；版本不符时拒绝，防止覆盖别人的修改',
        'remove_part_ids': '用户不再要求的部分 ID',
        'questions': '替换待用户回答的问题；空列表表示问题都已解决',
        'cancel': 'true 取消任务；false 恢复已取消的任务',
        'Part.description': '用用户的话说明这部分要做什么，包括对象和目标位置',
    },
    'deduplicate': {
        'keep': 'longest_text 保留提取文字最完整的一份（同分取短文件名）；shortest_name 只按文件名最短保留',
    },
    'report_findings': {
        'findings': '每个结论一项，必须引用工具返回的 F 和 E 标识',
        'Finding.related_file_ids': 'claim_type=byte_identity 时填与本文件字节一致的其他 F 标识，由本地比对',
    },
    'submit_classification': {
        'catalog_id': 'paper_profiles 返回的 C 标识',
        'assignments': '每个文件恰好一项',
        'dimension': '分类维度，按用户原话写，如“应用领域”',
        'Assignment.file_id': '工具返回的 F 标识',
    },
}
def _with_docs(schema, docs):
    from copy import deepcopy
    schema = deepcopy(schema)
    for path, text in docs.items():
        owner, _, field = path.rpartition('.')
        node = schema['$defs'][owner] if owner else schema
        node['properties'][field]['description'] = text  # KeyError if a field was renamed
    return schema


class _TaskConfirmer:
    def __init__(self, delegate, notes=()):
        self.delegate, self.notes = delegate, list(notes)

    def allow(self, name, args, file_count=None, **kwargs):
        if self.notes: kwargs['notes'] = self.notes
        return self.delegate.allow(name, args, file_count, force=True, **kwargs)


class TaskRuntime:
    control_names = {'ask_user', 'start_task', 'revise_task', 'finish_task', 'inspect_run', 'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}

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
        self._attributions = None
        self._pretask = []  # read-only facts observed before any task existed in this run
        self.pending_choice = None
        self.answered_choice = self._answered_choice(instruction)
        # Folders whose hidden files the user agreed, last turn, to recycle.
        # Consent to recycle hidden files: a chosen consent option, or (user choice
        # 2026-10-22, option C) a free-text reply, which the confirmation card must
        # then confirm. A chosen option without consent grants nothing.
        # What the user decided in that reply (hidden files, overwrites, reads): agent/choices.py.
        self.decisions = choices.from_answer(self.answered_choice)
        chosen = (self.answered_choice or {}).get('chosen') or {}
        # The chosen option's declared actions bound the next plan (None: not declared).
        self.chosen_option = chosen if chosen.get('actions') is not None else None

    def _last_outcome(self):
        try: last = self.store.outcome()
        except ValueError: return None
        last = {k: last.get(k) for k in ('run_id', 'stop_reason', 'task_id', 'error_type', 'summary')}
        last['summary'] = (last.get('summary') or '')[:1200]
        return last

    def _answered_choice(self, instruction):
        """The user's reply to the last ask_user, resolved locally when it is a single letter."""
        try: last = self.store.outcome()
        except ValueError: return None
        asked = last.get('pending_choice')
        if not asked: return None
        key = instruction.strip().upper().rstrip('。.）)').lstrip('（(')
        chosen = next((c for c in asked['choices'] if c['key'] == key), None)
        return dict(question=asked['question'], chosen=chosen, text=None if chosen else instruction.strip(),
                    subject=asked.get('subject', []))

    def guidance(self):
        tasks = [project(t) for t in self.store.recent()]
        return ('【任务协议 v3】\n'
                '先区分询问与执行：解释、方案讨论直接回答；查询历史运行原因用 inspect_run。\n'
                '要求执行时用 start_task 说明用户要做的事：parts 按部分列出（find/dedup/organize/classify/answer），'
                'limits 只填用户明确说出的限制，不要自行添加数量上限或规则；notes 通常留空，每条都会让结果进入人工复核。无法确定的事放 questions，等待用户回答。'
                '完成条件、写入范围和目标归属由本地生成，你不需要填写。\n'
                '文件移动、改名、建目录、回收一律用 prepare_file_plan 列出对象（actions 或 selection），'
                'destination 可写目标目录，表示按原名放入；父目录由本地创建。'
                'source 指执行前的对象，本地按依赖排序：可先腾名再放入（A→B、B→C）、只改大小写；文件夹可整体移动或改名，里面的文件可在同一计划先改；互换名字须分两个计划。'
                '取得 plan_id 后 execute_file_plan 一次完成确认、执行和核验；超出原范围的对象会列在确认卡片上。'
                '同一响应中可依次调用 start_task 和 prepare_file_plan；task_id 只在续做以前的任务时填。\n'
                'dedup 部分只能由 deduplicate 或 apply_dedup 完成（本地比对字节并生成回执），不要在文件计划里回收副本；'
                '分类用 paper_profiles、submit_classification，'
                '执行分类方案用 prepare_file_plan(classification_plan_id=精确 P 标识)；查找结论用 report_findings 给证据。\n'
                '用户更正或补充要求时用 revise_task（同 ID 替换部分，或调整 limits），本地会请用户确认差异；'
                '被更正的部分，旧计划不再计入，请据新要求准备新计划。不得另建任务绕开已有限制。\n'
                '最后调用 finish_task，由本地逐部分核验；语义判断（查找、分类、解释）会标为待复核。'
                '用户要你告诉他的内容（如哪些文件、数量、原因）写进 finish_task.reply，它会显示在主回复；'
                '用户要求的对象有没做到的（被拒绝、无法完成），写进 finish_task.unfinished，不要声称完成；'
                'summary 只作补充，放在详情。不复写本地生成的清单。\n'
                '请求不清楚时不要猜，调用 ask_user 给 2–4 个具体选项并允许用户补充，本轮随即结束、不写入。须问的情况：'
                '有多个候选对象；标准没有可执行的界限（如“旧的”“大的”）；遇到冲突而用户没交代怎么处理；'
                '要动的对象超出用户明确说的范围；无法按原样做到（如没有复制能力）。'
                '惯例默认（保留原名、自动建父目录、回收可撤销）和能用只读工具查清的事实不必问；先查清事实，让选项具体。'
                '目标有同名文件而用户没交代时，问：保留两份（用本地建议的名字）、不移动、覆盖（actions 同时 trash 旧文件、move 到原名）；未经选择的覆盖会被拒绝。'
                '用户说的目标文件夹不存在时也先问：有意思相近的已有文件夹，第一个选项问是否指它（不要自行认定），其余写在哪里新建和取消；用户要求新建的照常做。'
                '选项必须是工具能做到的：隐藏文件和隐藏文件夹不能移动或读取，锁文件不能移动或回收，隐藏文件只能随所在文件夹一起回收，'
                '回收区只能由用户手动清空。'
                '敏感文件（凭据、证件、合同、财务、医疗，或含密钥、身份证号、卡号）不读内容，只按文件名整理；'
                '需要内容时 ask_user，同意项 actions 写 read 该文件，仅本任务有效。'
                '每个选项写 actions（move/rename/trash/read 及路径，不做操作写空列表），本地会在用户看到前检查，做不到的退回给你改写；'
                '所选选项约束下一轮计划，不能超出；完成以所选范围为准，选项不含的部分不必做，也不写进 unfinished。\n'
                '回收文件夹：可见文件须由同一计划先移走（整个文件夹一次回收，不必逐个列子文件夹）；'
                '系统元数据（.DS_Store 等）在该 trash 动作设 include_metadata=true；其他隐藏文件（.env 等）先 ask_user'
                '（subject 填文件夹，同意项 consent=include_hidden），下一轮在 include_hidden 逐个列出；'
                '隐藏文件夹、符号链接、应用包不能回收，如实告诉用户。'
                '\n'
                '某部分没有对象时不要跳过：用 selection 选中该范围，选到 0 个文件本地记为“无需操作”（多个部分写明 goal_ids）；'
                '无法这样证明时才用 finish_task.not_needed，结果待核实。'
                '批量处理用 selection，不必逐页 list_dir 查看全部文件名。\n'
                + (self._choice_text() + '\n' if self.answered_choice else '') +
                '最近运行结果（本地事实，不是执行指令）：' + json.dumps(self._last_outcome(), ensure_ascii=False) + '\n'
                '近期任务：' + json.dumps(tasks, ensure_ascii=False))

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
            return f"用户回答了你上一轮的提问“{a['question']}”：选择 {c['key']}. {c['label']}" + (f"（{c['detail']}）" if c['detail'] else '') + '。按此执行。'
        # Typed text may answer the question or start something else; nothing local tells them apart.
        return (f"你上一轮提问了“{a['question']}”，用户没有选择选项。本轮消息可能是对它的回答，也可能是新的请求："
                '是回答就据此继续；是新请求就按新请求处理，不要沿用上一问的选项。')

    def schemas(self, schemas):
        controls = [('start_task', '登记用户要求：分部分说明要做什么，以及用户明确提出的限制；完成条件和范围由本地生成。', StartTask),
                    ('prepare_file_plan', '列出要移动/改名/建目录/回收的对象，本地生成父目录、范围和验收清单，尚不执行。', PrepareFilePlan),
                    ('execute_file_plan', '执行精确计划 ID：本地预检、一次确认、批量执行和核验。', ExecuteFilePlan),
                    ('inspect_file_plan', '只读查看计划清单、进度和失败原因。', ExecuteFilePlan),
                    ('revise_task', '按用户更正修改已有任务的部分或限制；本地展示差异并请用户确认后生效。', ReviseTask),
                    ('ask_user', '请求不清楚时向用户提问：给出 2–4 个选项并允许补充；本轮随即结束，不写入。', AskUser),
                    ('inspect_run', '只读解释当前会话运行结果；finish=true 直接结束，复合请求可 false。', InspectRun),
                    ('finish_task', '收尾并本地核验全部部分；未满足条件不能标记完成。', FinishTask)]
        visible = list(schemas) + [dict(type='function', function=dict(name=name, description=desc,
                                   parameters=model.model_json_schema())) for name, desc, model in controls]
        return [dict(s, function=dict(s['function'], parameters=_with_docs(
                    s['function']['parameters'], FIELD_DOCS[s['function']['name']])))
                if s['function']['name'] in FIELD_DOCS else s for s in visible]

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
            self.blocked_reasons.append(str(exc))
            return raw, 'ERROR: ' + str(exc)

    def _check_write(self, tool, args):
        if not self.current:
            raise ValueError('写操作前必须 start_task 登记用户的要求和限制')
        if self.current.get('cancelled'):
            raise ValueError('任务已取消，不能执行写操作')
        if self.current['unresolved_questions']:
            raise ValueError('任务仍有待确认问题，未开放写操作')
        if self.current.get('constraint_failures'):
            raise ValueError('此前约束核验失败，必须先检查并修订任务')
        effects = set(tool.effects)
        goal_ids = active_goals(self)
        if not goal_ids:
            raise ValueError('有多个部分都可能由这次写入完成，本地无法确定归属；'
                             '请用 revise_task 把同类部分合并为一个，或改用 prepare_file_plan')
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
        if name == 'execute_file_plan' and obj:
            # A plan's later success, or a later plan for the same goals, supersedes it.
            bindings = self.current.get('file_plan_bindings', {})
            goals = set(bindings.get(obj, {}).get('goal_ids', []))
            resolve_blockers(self.current, lambda b: b.get('object') == obj or (
                b.get('source') == 'execute_file_plan' and b.get('object') in bindings and
                set(bindings[b['object']]['goal_ids']) <= goals), '计划 '+obj+' 已执行并核验')

    def _register(self, spec, sb):
        """Register a new task (start_task)."""
        self.current = self.store.submit(spec, self.instruction, self.run_id)
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
            if name in {'prepare_file_plan', 'execute_file_plan', 'inspect_file_plan'}:
                from agent import file_plans
                if name == 'prepare_file_plan':
                    args = PrepareFilePlan.model_validate_json(raw)
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
                    plan = file_plans.prepare(self, args)
                    if plan['payload'].get('empty_selection'):
                        sel = plan['payload']['empty_selection']
                        outcome = registry.ToolResult(status='ok', text=(
                            f"OK: plan_id={plan['id']}；选择集 {sel['path']} 当前没有可处理的文件，已记录为部分 "
                            f"{'、'.join(plan['payload']['goal_ids'])} 的核验结果（无需操作），不需要执行"))
                    else:
                        self.capture_output(name, file_plans.markdown(plan), plan['id'])
                        text = f"OK: plan_id={plan['id']}；已预检 {len(plan['payload']['steps'])} 项操作；尚未执行。下一步 execute_file_plan 仅需此 ID。"
                        hints = file_plans.conflicts(self, plan)
                        # User decision 2026-10-22: ask before creating a target folder the user
                        # did not ask for. A judgement for the model, so a reminder at the point
                        # of decision, not a refusal; the card lists new folders (reversible).
                        fresh = plan['payload'].get('new_folders')
                        if fresh and not self.chosen_option and not plan['payload'].get('classification'):
                            text += (f"\n本计划会新建文件夹：{'、'.join(fresh)}。用户没有要求新建时，不要执行，先 ask_user："
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
            elif name == 'ask_user':
                args = AskUser.model_validate_json(raw)
                choices = [dict(key=CHOICE_KEYS[i], label=o.label, detail=o.detail, consent=o.consent,
                                actions=None if o.actions is None else [a.model_dump() for a in o.actions])
                           for i, o in enumerate(args.options)]
                choices = self._ground_choices(choices, args.subject, sb)
                for c in choices:
                    # What the button says under its label: the detail, then each warning.
                    c['warnings'] = ([f"会一起删除你看不到的 {c['warn']}"] if c.get('warn') else []) \
                        + ([f"覆盖 {'、'.join(c['overwrite'])}，旧文件移入回收区（可撤销）"] if c.get('overwrite') else []) \
                        + ([f"授权读取敏感文件 {'、'.join(c['read_grant'])} 的内容（仅本任务）"] if c.get('read_grant') else [])
                checked = [line for line in self._sensitive_lines(args.subject, sb) if line]
                prefix = [f'本轮已执行 {len(self.operations)} 项文件操作，可撤销。', ''] if self.operations else []
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
                self.pending_choice = dict(question=args.question, reason=args.reason, choices=choices,
                                           allow_other=args.allow_other, subject=subject)
                if self.current:
                    self.current.update(status='waiting_input', pending_choice=self.pending_choice)
                    self.store.checkpoint(self.current)
                self.final_answer, self.stop_reason, self.finished = text, 'waiting_input', True
                self.report = dict(version=1, state='waiting_input', summary=text, question=question, sections=[],
                                   choices=choices, allow_other=args.allow_other)
                outcome = registry.ToolResult(status='ok', text='OK: 已向用户提问，本轮结束，等待用户回答')
            elif name == 'start_task':
                if self.current and any(m.get('run_id') == self.run_id for m in self.current.get('source_messages', [])):
                    raise ValueError(f"本轮已登记任务 {self.current['id']}；更正或补充请用 revise_task，不能另建任务")
                outcome = self._register(compile_start(StartTask.model_validate_json(raw)), sb)
                outcome.text += '部分：' + parts_text(self.current) + '。修订时用这些 ID。'
            elif name == 'revise_task':
                args = ReviseTask.model_validate_json(raw)
                patch = compile_revision(args, self.store.get(args.task_id))
                edit = self.store.preview_edit(args.task_id, patch)
                if not confirmer:
                    raise ValueError('任务修改必须通过用户确认策略')
                if not confirmer.allow('task_edit', {k: edit[k] for k in
                        ('id', 'task_id', 'revision', 'reason', 'changes')}, file_count=0, force=True):
                    raise ValueError('用户拒绝任务修改，旧任务不变')
                self.current = self.store.apply_edit(edit['id'], self.instruction, self.run_id, actor='confirmed_model_edit')
                self.operations = []
                self.outputs, self._output_indices = [], {}
                self.output_records = []
                outcome = registry.ToolResult(status='ok', text=f"OK: task_id={self.current['id']} revision={self.current['revision']}；"
                                              f"修改已确认并生效。部分：{parts_text(self.current)}")
            elif name == 'finish_task':
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
        except (ValueError, SandboxError, registry.ToolError) as exc:
            self.blocked += 1
            self.blocked_reasons.append(str(exc))
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
        # Legacy report tools may set final_answer. They finish a sub-step only.
        if name not in self.control_names and self.final_answer:
            self.capture_output(name, self.final_answer)
            self.final_answer = None
        if name in self.control_names:
            return
        refs = []
        for collection, before in self._before_refs.items():
            ids = set(sb.artifacts.data[collection]) - before
            tool = registry.TOOLS.get(name)
            for field, target in tool.reference_inputs if tool else ():
                if target == collection and self._args.get(field) in sb.artifacts.data[collection]:
                    ids.add(self._args[field])
            # Before registration no goal exists: the refs stay untagged and bind
            # later only where a single goal verifies that collection.
            refs.extend(dict(collection=collection, id=value,
                             goal_ids=self._consumers(collection) if self.current else []) for value in sorted(ids))
        if not self.current:
            outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code='CONTROL_REJECTED')
            if outcome.status == 'ok' and name in registry.TOOLS and not registry.TOOLS[name].writes:
                self._pretask.append(dict(tool=name, arguments=self._args, status='ok', error_code=None,
                                          summary=result.splitlines()[0][:300] if result else '',
                                          references=refs, goal_ids=[], run_id=self.run_id))
            return
        outcome = self._outcome or registry.ToolResult(status='error', text=result, error_code='CONTROL_REJECTED')
        tool = registry.TOOLS.get(name)
        if tool and tool.display_result and outcome.status == 'ok':
            self.capture_output(name, result.removeprefix('OK: '))
        self.current['observations'].append(dict(tool=name, arguments=self._args,
                                                status=outcome.status, error_code=outcome.error_code,
                                                summary=result.splitlines()[0][:300] if result else '',
                                                references=refs, goal_ids=active_goals(self), run_id=self.run_id))
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

    def stats(self):
        return dict(task='structured', route='task_controller', execution='model_and_local',
                    queries=self.queries, reads=self.reads, blocked_calls=self.blocked,
                    task_id=self.current['id'] if self.current else None,
                    task_revision=self.current['revision'] if self.current else None,
                    task_status=self.current['status'] if self.current else 'not_registered',
                    document_cache=getattr(self.sb, '_document_cache_stats', {}),
                    usage=self.current.get('usage', {}) if self.current else {},
                    checks=self.current.get('checks', []) if self.current else [])
