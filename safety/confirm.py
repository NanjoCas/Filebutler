"""写操作确认。三档：always 每个写操作都问；risky 只在 trash 或累计写操作超过阈值时问；never 不问（评测用）。"""
from typing import Callable

ACCEPT = {"y", "yes", "是", "好"}


class Confirmer:
    def __init__(self, mode: str = "risky", threshold: int = 10, ask: Callable[[str], str] = input):
        if mode not in ("always", "risky", "never"):
            raise ValueError(f"confirm 只能是 always / risky / never，收到 {mode}")
        self.mode, self.threshold, self.ask = mode, threshold, ask
        self.file_writes = 0     # 本次运行动过的文件数（make_dir 不算）
        self.allow_all = False   # 用户回答 a 后，本次运行不再询问
        self.rejections = 0

    def allow(self, tool_name: str, args: dict) -> bool:
        if tool_name != "make_dir":
            self.file_writes += 1
        if self.mode == "never" or self.allow_all:
            return True
        need = (self.mode == "always" or tool_name == "trash"
                or self.file_writes > self.threshold)
        if not need:
            return True
        desc = " ".join(f"{k}={v}" for k, v in args.items())
        ans = self.ask(f"允许 {tool_name} {desc} ？[y=允许 / n=拒绝 / a=本次全部允许] ").strip().lower()
        if ans == "a":
            self.allow_all = True
            return True
        if ans in ACCEPT:
            return True
        self.rejections += 1
        self.file_writes -= tool_name != "make_dir"  # 被拒绝的不算动过
        return False
