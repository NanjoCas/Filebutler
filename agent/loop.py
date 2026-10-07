"""Agent 主循环：调模型 → 执行工具 → 结果塞回历史 → 再调模型，直到模型不再调用工具或触发终止条件。"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

import tools.fs_read  # noqa: F401  导入即注册工具
import tools.fs_write  # noqa: F401
from agent.context import ContextManager
from agent.llm import LLM
from agent.prompts import SYSTEM_PROMPTS
from agent.reflect import PASS_PREFIX, file_write_count, reflection_prompt, successful_writes
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from tools import registry


@dataclass
class RunResult:
    final: str
    stop_reason: str  # done / max_steps / cost_budget / token_budget
    steps: int
    tokens: int
    seconds: float
    tool_calls: int = 0
    violations: int = 0
    rejections: int = 0
    cost: float = 0.0               # 元，按缓存命中 / 未命中 / 输出分别计价
    cache_hit_tokens: int = 0
    reflection: str = ""            # ""=未触发 / pass=核对通过（可能做过只读检查）/ fixed=自检后执行了写操作
    reflection_cost: float = 0.0    # 自检本身花的钱（从发出自检提示到结束）
    compressions: int = 0           # 上下文压缩触发次数
    compressed_msgs: int = 0        # 被压缩的工具结果条数
    rereads: int = 0                # 压缩后又重读同一文件的次数
    messages: list = field(default_factory=list)


def _assistant_dict(msg) -> dict:
    """把 SDK 返回的消息转回可以再次发送的 dict。"""
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]
    # DeepSeek 思考模式下，同一轮工具调用过程中需要把 reasoning_content 带回去
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        d["reasoning_content"] = reasoning
    return d


def run_agent(instruction: str, llm: LLM, sandbox: Sandbox, cfg: dict,
              console: Console | None = None, trace_path: Path | None = None,
              oplog: OpLog | None = None, confirmer: Confirmer | None = None,
              history: list[dict] | None = None) -> RunResult:
    """history：从一段已有对话（含 system 和 user）继续跑，用于回放失败案例。"""
    console = console or Console()
    max_steps = cfg.get("max_steps", 15)
    cost_budget = cfg.get("cost_budget")      # 元；预算按费用算（缓存命中的 Token 很便宜，按 Token 计会高估成本）
    token_budget = cfg.get("token_budget")    # 模型没配价格时的后备

    def over_budget() -> str | None:
        if cost_budget is not None and llm.prices:
            return "cost_budget" if llm.cost > cost_budget else None
        if token_budget is not None:
            return "token_budget" if llm.total_tokens > token_budget else None
        return None
    messages = list(history) if history else [
        {"role": "system", "content": SYSTEM_PROMPTS[cfg.get("prompt", "v1")]},
        {"role": "user", "content": instruction},
    ]
    tool_schemas = registry.schemas()
    trace = open(trace_path, "a", encoding="utf-8") if trace_path else None
    if trace:
        trace.write(json.dumps({"event": "start", "instruction": instruction, "model": llm.model},
                               ensure_ascii=False) + "\n")
    t_start = time.time()
    final, stop_reason, n_calls, step = "", "max_steps", 0, 0
    reflect = cfg.get("reflect", False)
    reflect_min = cfg.get("reflect_min_writes", 5)
    reflection, draft_final, cost_at_reflect = "", "", None
    ctx = ContextManager(cfg.get("context_high_water", 12000), cfg.get("context_keep_recent", 1)) \
        if cfg.get("context_compress") else None

    try:
        for step in range(1, max_steps + 1):
            t0 = time.time()
            tokens_before = llm.total_tokens
            if ctx and (info := ctx.maybe_compress(messages)):
                console.print(f"[magenta]上下文压缩：{info['messages']} 条工具结果换成摘要，"
                              f"约 {info['tokens_before']} → {info['tokens_after']} tokens")
                if trace:
                    trace.write(json.dumps({"event": "compress", "step": step, **info}, ensure_ascii=False) + "\n")
            n_sent = len(messages)
            msg = llm.chat(messages, tool_schemas)
            if ctx:
                ctx.after_call(n_sent, llm.last_prompt_tokens)
            messages.append(_assistant_dict(msg))
            console.rule(f"[dim]第 {step} 轮 · {llm.total_tokens - tokens_before} tokens · {time.time() - t0:.1f}s")

            if not msg.tool_calls:
                writes = successful_writes(messages) if reflect and not reflection else []
                if writes and file_write_count(writes) >= reflect_min and not over_budget():
                    # 第一次准备结束且动过的文件够多：追加自检提示再跑一轮（追加在末尾，前缀命中缓存）
                    reflection, draft_final, cost_at_reflect = "pending", msg.content or "", llm.cost
                    messages.append({"role": "user", "content": reflection_prompt(
                        instruction, writes, sandbox, cfg.get("reflect_state_lines", 120))})
                    console.print("[magenta]自检：核对写操作和沙盒现状……")
                    continue
                final, stop_reason = msg.content or "", "done"
                if reflection == "pending":
                    reflection = "pass"
                    if final.strip().startswith(PASS_PREFIX):
                        final = draft_final  # 核对通过：沿用原来的总结，信息更完整
                break
            if reflection == "pending" and any(registry.TOOLS.get(tc.function.name) and
                                               registry.TOOLS[tc.function.name].writes for tc in msg.tool_calls):
                reflection = "fixed"  # 自检后执行了写操作，说明它发现了问题；只读检查不算
            if msg.content:
                console.print(f"[italic dim]{escape(msg.content)}")

            for tc in msg.tool_calls:
                name, raw = tc.function.name, tc.function.arguments
                t1 = time.time()
                console.print(f"[bold cyan]{escape(name)}[/] [dim]{escape(raw)}[/]")  # 先打印，确认提示才有上下文
                if ctx:
                    ctx.on_tool_call(name, raw)
                result = registry.dispatch(name, raw, sandbox, oplog, confirmer)
                n_calls += 1
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                ok = result.startswith("OK")
                first_line = result.split("\n", 2)
                preview = "\n".join(first_line[:2]) + (" …" if len(first_line) > 2 else "")
                console.print(f"  [{'green' if ok else 'red'}]{escape(preview)}[/]")
                if trace:
                    trace.write(json.dumps({
                        "step": step, "tool": name, "args": raw, "ok": ok,
                        "result": result[:300], "ms": int((time.time() - t1) * 1000),
                    }, ensure_ascii=False) + "\n")

            if reason := over_budget():
                stop_reason = reason
                break
        if stop_reason != "done":
            final = final or f"（未完成：触发终止条件 {stop_reason}）"
        result = RunResult(final=final, stop_reason=stop_reason, steps=step, tokens=llm.total_tokens,
                           seconds=time.time() - t_start, tool_calls=n_calls,
                           violations=sandbox.violations,
                           rejections=confirmer.rejections if confirmer else 0,
                           cost=llm.cost, cache_hit_tokens=llm.cache_hit_tokens,
                           reflection="" if reflection == "pending" else reflection,
                           reflection_cost=llm.cost - cost_at_reflect if cost_at_reflect is not None else 0.0,
                           compressions=ctx.stats.events if ctx else 0,
                           compressed_msgs=ctx.stats.messages if ctx else 0,
                           rereads=ctx.stats.rereads if ctx else 0,
                           messages=messages)
        if trace:
            trace.write(json.dumps({"event": "end", "final": final, "stop_reason": stop_reason,
                                    "steps": step, "tool_calls": n_calls, "tokens": result.tokens,
                                    "seconds": round(result.seconds, 2), "violations": result.violations,
                                    "rejections": result.rejections, "cost": round(result.cost, 5),
                                    "reflection": result.reflection, "compressions": result.compressions,
                                    "rereads": result.rereads},
                                   ensure_ascii=False) + "\n")
    finally:
        if trace:
            trace.close()

    console.print(Panel(escape(final), title="结果", border_style="green" if stop_reason == "done" else "yellow"))
    console.print(f"[dim]轮数 {result.steps} · 工具调用 {n_calls} · tokens {result.tokens} · "
                  f"缓存命中 {result.cache_hit_tokens} · ¥{result.cost:.4f} · 耗时 {result.seconds:.1f}s · "
                  f"越权尝试 {result.violations} · 用户拒绝 {result.rejections} · "
                  f"{'自检 ' + result.reflection + ' · ' if result.reflection else ''}结束原因 {stop_reason}")
    return result
