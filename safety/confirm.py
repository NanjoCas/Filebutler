"""写操作确认。三档：always 每个写操作都问；risky 只在 trash 或累计写操作超过阈值时问；never 不问（评测用）。

结果带来源（ConfirmationOutcome）：批准、拒绝、超时、中断分开记录；本轮被拒绝、超时或中断的同一清单不再询问。"""
import time
from typing import Callable

from safety.names import escape

OPTIONS = {"y": "允许", "n": "拒绝", "a": "本次全部允许"}
OPTIONS_TEXT = "   ".join(f"{k} = {v}" for k, v in OPTIONS.items())
ASK_TEXT = "请输入 " + " / ".join(OPTIONS) + "："


class ConfirmationOutcome:
    """How one confirmation ended (routing redesign S1, 2026-10-24).

    decision: approved | declined | timed_out | interrupted. Only an explicit "n" is
    declined: a web wait that ran out is timed_out, a closed CLI input is
    interrupted, so no report says the user refused when they did not answer.
    source: web | cli | policy (no question was asked).
    """
    __slots__ = ('decision', 'source', 'confirmation_id', 'manifest_key', 'reason', 'repeated')

    def __init__(self, decision, source, confirmation_id='', manifest_key='', reason='', repeated=False):
        self.decision, self.source, self.confirmation_id = decision, source, confirmation_id
        self.manifest_key, self.reason, self.repeated = manifest_key, reason, repeated

    @property
    def approved(self):
        return self.decision == 'approved'

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


# What an ask callback may return besides y / n / a.
TIMED_OUT, INTERRUPTED = 'timed_out', 'interrupted'


class Confirmer:
    def __init__(self, mode: str = "risky", threshold: int = 10, ask: Callable[[str], str] = input,
                 *, ask_action: Callable[[dict], str] | None = None, source: str = 'cli'):
        if mode not in ("always", "risky", "never"):
            raise ValueError(f"confirm 只能是 always / risky / never，收到 {mode}")
        self.mode, self.threshold, self.ask = mode, threshold, ask
        self.ask_action, self.source = ask_action, source
        self.file_writes = 0     # 本次运行动过的文件数（make_dir 不算）
        self.allow_all = False   # 用户选 a 后，本次运行不再询问
        self.rejections = 0
        self.wait_seconds = 0.0
        self.outcomes = []       # every ConfirmationOutcome of this run
        self.stopped = {}        # manifest_key -> outcome that ended it without approval, this run

    def _choose(self, question: str, request: dict | None = None) -> tuple[str, str]:
        """(answer, reason): y / n / a, or timed_out / interrupted. Other input is asked again."""
        prompt = f"{question}\n  {OPTIONS_TEXT}\n{ASK_TEXT}"
        while True:
            try:
                ans = (self.ask_action(request) if self.ask_action and request is not None else self.ask(prompt))
            except EOFError:  # 输入流已结束（如 Ctrl-D），无法再问
                return INTERRUPTED, 'eof'
            except KeyboardInterrupt:
                return INTERRUPTED, 'user_interrupt'
            ans = (ans or '').strip().lower()
            if ans in OPTIONS or ans in (TIMED_OUT, INTERRUPTED):
                return ans, ''
            prompt = f"无效输入「{ans}」，{ASK_TEXT}"

    def allow(self, tool_name: str, args: dict, file_count: int | None = None, **kwargs) -> bool:
        return self.decide(tool_name, args, file_count, **kwargs).approved

    def decide(self, tool_name: str, args: dict, file_count: int | None = None,
               *, review_groups: list[dict] | None = None, force: bool = False,
               notes: list[str] | None = None, manifest_key: str = '') -> ConfirmationOutcome:
        if manifest_key and manifest_key in self.stopped:
            # Declined, timed out or interrupted earlier this run: not asked again,
            # and "allow all" does not override it.
            first = self.stopped[manifest_key]
            outcome = ConfirmationOutcome(first.decision, first.source, first.confirmation_id, manifest_key,
                                          first.reason, repeated=True)
            self.outcomes.append(outcome)
            return outcome
        count = int(tool_name != "make_dir") if file_count is None else file_count
        self.file_writes += count
        if self.mode == "never" or self.allow_all:
            return self._record(ConfirmationOutcome('approved', 'policy', manifest_key=manifest_key))
        need = (force or self.mode == "always" or tool_name in ("trash", "trash_many")
                or self.file_writes > self.threshold)
        if not need:
            return self._record(ConfirmationOutcome('approved', 'policy', manifest_key=manifest_key))
        # Names may carry newlines or bidi characters; the terminal shows them escaped.
        lines = "".join(f"\n    {k} = {escape(v) if isinstance(v, str) else v}" for k, v in args.items())
        lines += "".join(f"\n    ⚠ {escape(n)}" for n in notes or [])
        request = {"tool": tool_name, "args": args, "file_count": count}
        if notes:
            request["notes"] = list(notes)
        if review_groups:
            request["groups"] = review_groups
        started = time.perf_counter()
        try:
            choice, reason = self._choose(f"⚠ 需要确认：{tool_name}{lines}", request)
        finally:
            self.wait_seconds += time.perf_counter() - started
        cid = request.get('confirmation_id', '')
        if choice == "a":
            self.allow_all = True
        if choice in ("a", "y"):
            return self._record(ConfirmationOutcome('approved', self.source, cid, manifest_key))
        self.file_writes -= count  # 没有执行的不算动过
        decision = 'declined' if choice == 'n' else choice
        if decision == 'declined':
            self.rejections += 1
        outcome = self._record(ConfirmationOutcome(decision, self.source, cid, manifest_key,
                                                   reason or request.get('reason', '')))
        if manifest_key:
            self.stopped[manifest_key] = outcome
        return outcome

    def _record(self, outcome):
        self.outcomes.append(outcome)
        return outcome
