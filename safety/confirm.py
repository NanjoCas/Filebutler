"""写操作确认。三档：always 每个写操作都问；risky 只在 trash 或累计写操作超过阈值时问；never 不问（评测用）。"""
import re
from typing import Callable

# 按顺序匹配：先“全部允许”，再“拒绝”，最后“允许”——避免“不允许”“不行”因为含“允许”“行”被当成同意
ALLOW_ALL = {"a", "all", "全部", "全部允许", "都允许", "全允许", "全部同意", "都可以", "以后都允许", "本次全部允许"}
REJECT = {"n", "no", "nope", "不", "否", "拒绝", "不要", "不行", "不可以", "不允许", "不同意", "取消", "别"}
ACCEPT = {"y", "yes", "ok", "okay", "sure", "是", "好", "好的", "行", "可以", "允许", "同意", "确认", "嗯", "对"}
MAX_TRIES = 3


def parse_answer(text: str) -> str | None:
    """把用户的回答归成 all / reject / accept；空输入按默认值 reject；认不出返回 None。"""
    ans = re.sub(r"[\s。，！？!?,.、~～]+", "", text).lower()
    if not ans:
        return "reject"
    for kind, words in (("all", ALLOW_ALL), ("reject", REJECT), ("accept", ACCEPT)):
        if ans in words:
            return kind
    return None


class Confirmer:
    def __init__(self, mode: str = "risky", threshold: int = 10, ask: Callable[[str], str] = input):
        if mode not in ("always", "risky", "never"):
            raise ValueError(f"confirm 只能是 always / risky / never，收到 {mode}")
        self.mode, self.threshold, self.ask = mode, threshold, ask
        self.file_writes = 0     # 本次运行动过的文件数（make_dir 不算）
        self.allow_all = False   # 用户回答“全部允许”后，本次运行不再询问
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
        prompt = f"允许 {tool_name} {desc} ？[y=允许 / a=本次全部允许 / N=拒绝，直接回车也是拒绝] "
        for _ in range(MAX_TRIES):  # 认不出就重问，而不是直接当成拒绝
            raw = self.ask(prompt)
            kind = parse_answer(raw)
            if kind is not None:
                break
            prompt = f"没看懂「{raw.strip()}」，请回答 y（允许）/ a（本次全部允许）/ n（拒绝）："
        else:
            kind = "reject"  # 连续认不出，保守处理
        if kind == "all":
            self.allow_all = True
            return True
        if kind == "accept":
            return True
        self.rejections += 1
        self.file_writes -= tool_name != "make_dir"  # 被拒绝的不算动过
        return False
