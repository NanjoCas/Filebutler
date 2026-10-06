"""结束前自检（反思）。事实由程序收集（不花 Token），判断交给模型（多一次调用）。

成本控制：
- 只在本次成功动过的文件不少于 reflect_min_writes 个时触发（默认 5，make_dir 不算）；
  纯问答、只读和单文件的小任务零开销。实测遗漏出现在一次处理多个文件时（C05：一次移 7 个时漏了 1 个）。
- 每次运行最多一次（由主循环控制）。
- 自检提示追加在对话末尾，前面的历史命中前缀缓存（DeepSeek 缓存命中价约为未命中的 1/50）。
- 直接附上写操作清单和沙盒文件清单，模型不用再调 list_dir 查一轮。
"""
import json

from safety.sandbox import Sandbox
from tools import registry

PASS_PREFIX = "核对通过"


def successful_writes(messages: list[dict]) -> list[str]:
    """从对话里找出执行成功的写操作，格式化成一行一个。"""
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    out = []
    for m in messages:
        for tc in m.get("tool_calls", []):
            name = tc["function"]["name"]
            tool = registry.TOOLS.get(name)
            if tool and tool.writes and results.get(tc["id"], "").startswith("OK"):
                try:
                    args = json.loads(tc["function"]["arguments"])
                    args = ", ".join(f"{k}={v}" for k, v in args.items())
                except json.JSONDecodeError:
                    args = tc["function"]["arguments"]
                out.append(f"{name}({args})")
    return out


def file_write_count(writes: list[str]) -> int:
    """动过的文件数：move / rename / trash 各算一个，make_dir 不算。"""
    return sum(not w.startswith("make_dir(") for w in writes)


def sandbox_listing(sandbox: Sandbox, max_lines: int) -> str:
    files = sorted(p.relative_to(sandbox.root).as_posix() for p in sandbox.root.rglob("*")
                   if p.is_file() and not any(x.startswith(".") for x in p.relative_to(sandbox.root).parts))
    shown = files[:max_lines]
    more = f"\n……还有 {len(files) - max_lines} 个文件未列出（可用 list_dir 查看）" if len(files) > max_lines else ""
    return "\n".join(shown) + more


def reflection_prompt(instruction: str, writes: list[str], sandbox: Sandbox, max_lines: int) -> str:
    return f"""【结束前自检】这条消息由系统自动发出，不是用户的新要求。

用户的原始指令：{instruction}

你实际成功执行的写操作（共 {len(writes)} 个）：
{chr(10).join(writes)}

沙盒现在的全部文件：
{sandbox_listing(sandbox, max_lines)}

请对照原始指令逐项核对：
1. 指令要求处理的文件，是否每一个都已处理？特别注意有没有遗漏。
2. 有没有动了不该动的文件？
如果发现问题，直接调用工具修正，只做必要的操作。
如果全部正确，只回复一句以「{PASS_PREFIX}」开头的话，不要调用工具。"""
