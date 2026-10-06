"""写操作确认。三档：always 每个写操作都问；risky 只在 trash 或累计写操作超过阈值时问；never 不问（评测用）。"""
from typing import Callable

OPTIONS = {"y": "允许", "n": "拒绝", "a": "本次全部允许"}
OPTIONS_TEXT = "   ".join(f"{k} = {v}" for k, v in OPTIONS.items())
ASK_TEXT = "请输入 " + " / ".join(OPTIONS) + "："


class Confirmer:
    def __init__(self, mode: str = "risky", threshold: int = 10, ask: Callable[[str], str] = input):
        if mode not in ("always", "risky", "never"):
            raise ValueError(f"confirm 只能是 always / risky / never，收到 {mode}")
        self.mode, self.threshold, self.ask = mode, threshold, ask
        self.file_writes = 0     # 本次运行动过的文件数（make_dir 不算）
        self.allow_all = False   # 用户选 a 后，本次运行不再询问
        self.rejections = 0

    def _choose(self, question: str) -> str:
        """只接受 y / n / a（不区分大小写），其他输入一律要求重新输入。"""
        prompt = f"{question}\n  {OPTIONS_TEXT}\n{ASK_TEXT}"
        while True:
            try:
                ans = self.ask(prompt).strip().lower()
            except EOFError:  # 输入流已结束（如 Ctrl-D），无法再问，按拒绝处理
                return "n"
            if ans in OPTIONS:
                return ans
            prompt = f"无效输入「{ans}」，{ASK_TEXT}"

    def allow(self, tool_name: str, args: dict) -> bool:
        if tool_name != "make_dir":
            self.file_writes += 1
        if self.mode == "never" or self.allow_all:
            return True
        need = (self.mode == "always" or tool_name == "trash"
                or self.file_writes > self.threshold)
        if not need:
            return True
        lines = "".join(f"\n    {k} = {v}" for k, v in args.items())
        choice = self._choose(f"⚠ 需要确认：{tool_name}{lines}")
        if choice == "a":
            self.allow_all = True
            return True
        if choice == "y":
            return True
        self.rejections += 1
        self.file_writes -= tool_name != "make_dir"  # 被拒绝的不算动过
        return False
