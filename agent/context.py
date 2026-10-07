"""上下文压缩：对话变长时，把较早的工具结果换成一行摘要。

设计约束（见 README“上下文压缩”）：
- 只压缩 tool 消息的内容，不删消息——工具调用和结果必须成对保留，否则接口报错。
- 系统提示词、工具定义、用户指令、模型自己的回复永不压缩：模型在回复里写的要点就是它的笔记。
- 只压缩模型已经看过的结果，并保留最近 keep_recent 轮原文。
- 高水位触发、一次压一批：改写旧消息会让那之后的前缀缓存失效（命中价约为未命中的 1/50），
  一次压一批，之后对话只在末尾追加，大多数调用仍能命中缓存；每轮都压则每轮都破坏缓存。
- 摘要按规则生成，不花 Token；read_file 的摘要仍包在 <file_content> 里，注入防护不失效。
"""
import json
import re
from dataclasses import dataclass, field

MARK = "[已压缩]"
CHARS_PER_TOKEN = 2      # 中英混合的粗略估计；只用于判断是否超过水位
READ_SNIPPET = 150       # read_file 摘要保留的开头字数（论文通常含标题）


@dataclass
class CompressStats:
    events: int = 0                  # 触发了几次压缩
    messages: int = 0                # 一共压缩了多少条工具结果
    chars_saved: int = 0
    rereads: int = 0                 # 压缩后又重新读同一个文件的次数：越高说明摘要丢了需要的信息
    compressed_reads: set = field(default_factory=set)


def _chars(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        n += len(m.get("content") or "")
        n += sum(len(tc["function"]["arguments"]) + len(tc["function"]["name"]) for tc in m.get("tool_calls", []))
    return n


def summarize(name: str, args: dict, content: str) -> str | None:
    """按工具类型生成摘要；不值得压缩时返回 None。"""
    if content.startswith(MARK) or not content.startswith("OK"):
        return None  # 已压缩过、或是 ERROR（本来就短且有用）
    if name == "read_file":
        m = re.search(r"<file_content>\n?(.*?)\n?</file_content>", content, re.S)
        text = re.sub(r"\s+", " ", m.group(1) if m else content).strip()
        snippet = text[:READ_SNIPPET] + ("…" if len(text) > READ_SNIPPET else "")
        return (f"{MARK} read_file({args.get('path', '?')})：<file_content>{snippet}</file_content>"
                f"（原 {len(text)} 字；需要细节请重新读取）")
    if name == "list_dir":
        lines = content.splitlines()
        names = [l.split(" · ")[0] + ("/" if " · 文件夹 · " in l else "") for l in lines[1:-1] if " · " in l]
        return f"{MARK} list_dir({args.get('path', '.')})：" + "、".join(names) + "\n" + lines[-1]
    if name == "file_info":
        keep = [l for l in content.splitlines() if l.startswith(("路径", "MD5", "拍摄日期", "页数"))]
        return f"{MARK} file_info：" + " | ".join(keep)
    return None  # search_files 本身只有路径，写工具结果本来就短


class ContextManager:
    def __init__(self, high_water: int = 12000, keep_recent: int = 1):
        self.high_water = high_water
        self.keep_recent = keep_recent
        self.stats = CompressStats()
        self.n_sent = 0                  # 上次调用时发了多少条消息
        self.sent_tokens = None          # 上次调用的准确输入 Token（API 返回）

    def after_call(self, n_sent: int, prompt_tokens: int | None):
        self.n_sent, self.sent_tokens = n_sent, prompt_tokens

    def estimate(self, messages: list[dict]) -> int:
        """上次的准确值 + 之后新增内容的估算。工具定义的长度已包含在 API 返回的值里。"""
        if self.sent_tokens is None:
            return _chars(messages) // CHARS_PER_TOKEN
        return self.sent_tokens + _chars(messages[self.n_sent:]) // CHARS_PER_TOKEN

    def on_tool_call(self, name: str, raw_args: str):
        if name == "read_file":
            try:
                if json.loads(raw_args).get("path") in self.stats.compressed_reads:
                    self.stats.rereads += 1
            except (json.JSONDecodeError, AttributeError):
                pass

    def maybe_compress(self, messages: list[dict]) -> dict | None:
        """超过高水位时，把最近 1 + keep_recent 轮之前的工具结果一次性换成摘要。返回本次压缩的统计。"""
        before = self.estimate(messages)
        if before <= self.high_water:
            return None
        rounds = [i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("tool_calls")]
        protect_from = rounds[-(1 + self.keep_recent)] if len(rounds) > self.keep_recent else 0
        calls = {tc["id"]: tc["function"] for m in messages[:protect_from] for tc in m.get("tool_calls", [])}
        n, saved = 0, 0
        for m in messages[:protect_from]:
            if m["role"] != "tool" or m["tool_call_id"] not in calls:
                continue
            fn = calls[m["tool_call_id"]]
            try:
                args = json.loads(fn["arguments"])
            except json.JSONDecodeError:
                continue
            short = summarize(fn["name"], args if isinstance(args, dict) else {}, m["content"])
            if short is None or len(short) >= len(m["content"]):
                continue
            if fn["name"] == "read_file":
                self.stats.compressed_reads.add(args.get("path"))
            saved += len(m["content"]) - len(short)
            m["content"] = short
            n += 1
        if not n:
            return None
        self.stats.events += 1
        self.stats.messages += n
        self.stats.chars_saved += saved
        self.sent_tokens = None  # 旧消息变了，上次的准确值不再适用，改用纯估算
        return {"messages": n, "chars_saved": saved, "tokens_before": before, "tokens_after": self.estimate(messages)}
