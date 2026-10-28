"""Agent 主循环：调模型 → 执行工具 → 结果塞回历史 → 再调模型，直到模型不再调用工具或触发终止条件。"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

import tools.fs_read  # noqa: F401  导入即注册工具
import tools.retrieval  # noqa: F401
import tools.dedup  # noqa: F401
import tools.findings  # noqa: F401
import tools.classification  # noqa: F401
import tools.inventory  # noqa: F401
from agent.artifacts import ArtifactStore
from agent.llm import LLM
from agent.prompts import SYSTEM_PROMPT
from agent.finishing import TERMINAL_REQUIRED
from agent.outcomes import stop_report, close_messages, save_outcome
from agent.states import display_of, project_run
from agent.repetition import RepeatDetector, NoProgressGuard
from safety import names
from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox
from agent.task_runtime import TaskRuntime
from tools import registry


@dataclass
class RunResult:
    final: str
    stop_reason: str  # done / max_steps / cost_budget / token_budget / repeated_call
    steps: int
    tokens: int
    seconds: float
    tool_calls: int = 0
    violations: int = 0
    rejections: int = 0
    cost: float = 0.0               # 元，按缓存命中 / 未命中 / 输出分别计价
    cache_hit_tokens: int = 0
    llm_seconds: float = 0.0        # 模型请求墙钟时间（含 SDK/封装内重试等待）
    wait_seconds: float = 0.0
    active_seconds: float = 0.0
    tool_seconds: float = 0.0
    runtime: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)   # what the last request saw, snapshot included
    added_messages: list = field(default_factory=list)  # this run's messages to keep; the snapshot is rebuilt next run
    report: dict = field(default_factory=dict)


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
              history: list[dict] | None = None,
              on_event: Callable[[dict], None] | None = None,
              artifacts=None) -> RunResult:
    """history：从一段已有对话（含 system 和 user）继续跑，用于回放失败案例。"""
    console = console or Console()

    def emit(event_type: str, **data):
        if on_event:
            try:
                on_event({"type": event_type, **data})
            except Exception as exc:
                # A UI/reporting failure must not interrupt an already authorised file operation.
                console.print(f"[dim]运行状态通知失败：{escape(str(exc))}")
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
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": instruction},
    ]
    artifacts = artifacts or ArtifactStore(sandbox)
    sandbox.artifacts = artifacts
    artifacts.run_id = oplog.path.stem if oplog else str(time.time_ns())
    runtime = TaskRuntime(instruction, cfg, sandbox, artifacts.run_id)
    kept_from = len(history) if history else 0
    snapshot = {'role': 'system', 'content': names.shield(runtime.guidance(), sandbox)}
    messages.append(snapshot)
    added = lambda: [m for m in messages[kept_from:] if m is not snapshot]
    sandbox.runtime = runtime
    sandbox.progress = lambda message: emit('local_step', message=message)
    retrieval = cfg.get("retrieval", True)
    tool_schemas = [s for s in registry.schemas() if retrieval or s["function"]["name"] != "search_content"]
    trace = open(trace_path, "a", encoding="utf-8") if trace_path else None
    if trace:
        trace.write(json.dumps({"event": "start", "instruction": instruction, "model": llm.model},
                               ensure_ascii=False) + "\n")
    t_start = time.perf_counter()
    wait_before = confirmer.wait_seconds if confirmer else 0.0
    final, stop_reason, n_calls, step = "", "max_steps", 0, 0
    repeats = RepeatDetector() if cfg.get("repeat_detection", True) else None
    repair_guard = NoProgressGuard(cfg.get('task_repair_limit', 6))
    llm_seconds = tool_seconds = 0.0
    failure_stage='准备运行'
    terminal_report={}
    corrected = False  # strict terminal: one correction for a turn ended with bare text
    if repeats and history:
        for m in history:
            if m.get("role") == "user" or m.get("content", "").startswith("[本地撤销状态 run="):
                repeats = RepeatDetector()
            for tc in m.get("tool_calls", []):
                repeats.observe(tc["function"]["name"], tc["function"]["arguments"])

    try:
        for step in range(1, max_steps + 1):
            if runtime.final_answer is None and runtime.stop_requested():
                runtime.stopped_by_user()  # the stop button while this run was working (S3, 5.2)
            if runtime.final_answer is not None:  # decided before any model call (a cancel button)
                final, stop_reason = runtime.final_answer, runtime.stop_reason
                messages.append({'role': 'assistant', 'content': final})
                break
            if reason := over_budget():
                stop_reason = reason
                break
            t0 = time.time()
            tokens_before = llm.total_tokens
            emit("llm_start", step=step)
            call_start = time.perf_counter()
            def request_event(info):
                if trace:
                    trace.write(json.dumps({"event": "llm_request", "step": step, **info}) + "\n")
                    trace.flush()
                if info.get("retry"):
                    emit("llm_retry", step=step, **info)
            try:
                failure_stage = '模型请求'
                msg = llm.chat(messages, runtime.schemas(tool_schemas), on_request=request_event)
            finally:
                # Include failed attempts and backoff in model time as well.
                llm_seconds += time.perf_counter() - call_start
            call_seconds = time.perf_counter() - call_start
            emit("llm_end", step=step, ms=round(call_seconds * 1000))
            if trace:
                trace.write(json.dumps({"event": "llm", "step": step,
                                        "ms": round(call_seconds * 1000),
                                        "tokens": llm.total_tokens - tokens_before}, ensure_ascii=False) + "\n")
            messages.append(_assistant_dict(msg))
            console.rule(f"[dim]第 {step} 轮 · {llm.total_tokens - tokens_before} tokens · {time.time() - t0:.1f}s")

            if not msg.tool_calls:
                if not corrected:
                    # Routing redesign S5: a turn ends by finish_turn. Bare text gets one
                    # correction (it counts as a step); the draft is not shown as an answer.
                    corrected = True
                    messages.append({'role': 'system', 'content': TERMINAL_REQUIRED})
                    emit('protocol_retry', step=step, reason='TERMINAL_REQUIRED')
                    continue
                runtime.unstructured(msg.content)
                final, stop_reason = runtime.final_answer, runtime.stop_reason
                messages[-1]['content'] = final
                break
            if msg.content:
                console.print(f"[italic dim]{escape(msg.content)}")

            for tc in msg.tool_calls:
                name, raw = tc.function.name, tc.function.arguments
                failure_stage='工具 '+name
                requested_raw=raw
                raw, blocked = runtime.prepare(name, raw, sandbox)
                t1 = time.perf_counter()
                tool_wait_before = confirmer.wait_seconds if confirmer else 0.0
                try:
                    event_args = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    event_args = {}
                emit("tool_start", step=step, call_id=tc.id, tool=name,
                     args=event_args if isinstance(event_args, dict) else {})
                console.print(f"[bold cyan]{escape(name)}[/] [dim]{escape(raw)}[/]")  # 先打印，确认提示才有上下文
                repeat_count = repeats.observe(name, raw) if repeats else 0
                executed = False
                # A call the runtime blocked (invalid JSON, out of scope, over budget) counts
                # as a repeat too: retrying it unchanged is the same idling (user decision 2026-10-23).
                if stop_reason in {"repeated_call", "repair_limit"}:
                    result = "ERROR: 本次运行已触发重复或无进展保护，此调用未执行"
                elif repeat_count >= 3:
                    stop_reason = "repeated_call"
                    result = "ERROR: 相同工具和参数连续调用 3 次，已中止运行；此调用未执行"
                else:
                    if blocked:
                        result = blocked
                    elif name == "search_content" and not retrieval:
                        result = "ERROR: 本次运行未启用内容检索，请使用 search_files 和 read_file"
                    else:
                        executed = True
                        result = runtime.dispatch(name, raw, sandbox, oplog, confirmer)
                    if repeat_count == 2:
                        result += "\n[重复调用提醒] 你已连续两次调用相同工具和参数，结果如上。请调整做法；再次原样调用将中止运行。"
                runtime.after(name, result, sandbox)
                if name in runtime.control_names:
                    emit('task_updated', **runtime.stats())
                tool_wait = (confirmer.wait_seconds if confirmer else 0.0) - tool_wait_before
                tool_elapsed = time.perf_counter() - t1
                tool_seconds += max(0, tool_elapsed - tool_wait)
                n_calls += 1
                result = names.shield(result, sandbox)  # file names reach the model as data only
                outcome = runtime._outcome or registry.ToolResult(status='error', text=result,
                                                                   error_code=registry.error_code(result))
                outcome.text = result  # Keep repeat warnings added after dispatch.
                registry.typed(outcome, f"{runtime.run_id}:{tc.id}")
                runtime.record_result(name, outcome)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": outcome.for_model()})
                ok = outcome.status == 'ok'
                if repair_guard and stop_reason not in {'repair_limit', 'repeated_call'}:
                    task = runtime.current or {}
                    artifacts = getattr(sandbox, 'artifacts', None)
                    evidence = (artifacts.data.get('evidence', {}) if artifacts else {}).values()
                    proof = {(e.get('file_id'), e.get('version'), e.get('kind'), e.get('page'), e.get('offset'), e.get('excerpt')) for e in evidence}
                    progress = (len(task.get('operation_history', []))+len(runtime.operations), len(proof))
                    tool = registry.TOOLS.get(name)
                    repairable_call = (bool(tool and tool.writes) or name in
                        {'start_task', 'revise_task', 'prepare_file_plan', 'execute_file_plan',
                         'finish_turn', 'ask_user', 'finish_task'})
                    closed = outcome.error_code == 'TURN_CLOSED'
                    failure = (outcome.error_code or 'TOOL_ERROR') if not ok and repairable_call and not closed else None
                    if failure: runtime.last_failure = result[:1200]
                    if repair_guard.observe(failure, progress): stop_reason = 'repair_limit'

                emit("tool_end", step=step, call_id=tc.id, tool=name, ok=ok,
                     executed=executed, ms=round(tool_elapsed * 1000),
                     active_ms=round(max(0, tool_elapsed - tool_wait) * 1000), wait_ms=round(tool_wait * 1000),
                     summary=result.splitlines()[0][:200] if result else "")
                first_line = result.split("\n", 2)
                preview = "\n".join(first_line[:2]) + (" …" if len(first_line) > 2 else "")
                console.print(f"  [{'green' if ok else 'red'}]{escape(preview)}[/]")
                if trace:
                    trace.write(json.dumps({
                        "step": step, "tool": name, "args": raw, "requested_args": requested_raw, "ok": ok,
                        "result": result[:300], "ms": round(tool_elapsed * 1000),
                        "active_ms": round(max(0, tool_elapsed - tool_wait) * 1000), "wait_ms": round(tool_wait * 1000),
                        "repeat_count": repeat_count,
                        "executed": executed,
                        "result_id": outcome.result_id, "error_code": outcome.error_code,
                        "error_class": outcome.error_class, "references": outcome.references[:20],
                        "operation_refs": len(outcome.operation_refs), "progress": outcome.progress,
                    }, ensure_ascii=False) + "\n")

            if runtime.final_answer is not None:
                final,stop_reason=runtime.final_answer,runtime.stop_reason
                messages.append({'role':'assistant','content':final})
                break
            if stop_reason in {"repeated_call", "repair_limit"}:
                break
            if reason := over_budget():
                stop_reason = reason
                break
        runtime.interrupt(stop_reason)
        if not final and stop_reason!='done':
            report=stop_report(stop_reason,cfg,llm,runtime,oplog)
            terminal_report=report
            final=report['summary']
            runtime.report=report
        if runtime.report or terminal_report:
            # S6a: flow/result/verification beside the old state; 2.0 P1-D: display comes from them.
            shown=runtime.report or terminal_report
            shown['states']=project_run(runtime,stop_reason,shown)
            shown['display']=display_of(shown['states'])
        close_messages(messages,final)
        save_outcome(runtime,stop_reason,final,cfg,llm)
        elapsed = time.perf_counter() - t_start
        wait_seconds = (confirmer.wait_seconds if confirmer else 0.0) - wait_before
        result = RunResult(final=final, stop_reason=stop_reason, steps=step, tokens=llm.total_tokens,
                           seconds=elapsed, wait_seconds=wait_seconds, active_seconds=max(0, elapsed - wait_seconds),
                           tool_seconds=tool_seconds, tool_calls=n_calls,
                           violations=sandbox.violations,
                           rejections=confirmer.rejections if confirmer else 0,
                           cost=llm.cost, cache_hit_tokens=llm.cache_hit_tokens,
                           llm_seconds=llm_seconds, runtime=runtime.stats(), messages=messages, added_messages=added(),
                           report=runtime.report or terminal_report)
        if trace:
            trace.write(json.dumps({"event": "end", "final": final, "stop_reason": stop_reason,
                                    "steps": step, "tool_calls": n_calls, "tokens": result.tokens,
                                    "seconds": round(result.seconds, 2), "violations": result.violations,
                                    "rejections": result.rejections, "cost": round(result.cost, 5),
                                    "llm_seconds": round(result.llm_seconds, 3),
                                    "active_seconds": result.active_seconds, "wait_seconds": result.wait_seconds,
                                    "tool_seconds": result.tool_seconds, "runtime": result.runtime},
                                   ensure_ascii=False) + "\n")
    except BaseException as exc:
        # A failed checkpoint must not hide the original failure.
        try:runtime.interrupt('exception')
        except Exception:pass
        elapsed = time.perf_counter() - t_start
        wait = (confirmer.wait_seconds if confirmer else 0.0) - wait_before
        metrics = dict(seconds=elapsed, wait_seconds=wait, active_seconds=max(0, elapsed - wait),
                       llm_seconds=llm_seconds, tool_seconds=tool_seconds)
        reason='cancelled' if isinstance(exc,KeyboardInterrupt) else 'exception'
        report=stop_report(reason,cfg,llm,runtime,oplog,error=exc,stage=failure_stage)
        try:
            report['states']=project_run(runtime,reason,report,error_type=type(exc).__name__)
            report['display']=display_of(report['states'])
        except Exception:pass
        final=report['summary']
        close_messages(messages,final)
        try:save_outcome(runtime,reason,final,cfg,llm,error=exc)
        except Exception:pass
        failure=dict(final=final,report=report,stop_reason=reason,error_type=type(exc).__name__,
                     error_stage=failure_stage,
                     runtime=runtime.stats(),cost=llm.cost,tokens=llm.total_tokens,
                     tool_calls=n_calls,steps=step,**metrics)
        exc.filebutler_outcome={**failure,'messages':messages,'added_messages':added()}
        emit("run_failed", **failure)
        if trace:
            trace.write(json.dumps({"event": "failed", **failure},ensure_ascii=False) + "\n")
        console.print(Panel(escape(final),title='运行中止' if reason=='cancelled' else '运行失败',border_style='yellow'))
        raise
    finally:
        if trace:
            trace.close()

    console.print(Panel(escape(final), title="结果", border_style="green" if stop_reason == "done" else "yellow"))
    console.print(f"[dim]轮数 {result.steps} · 工具调用 {n_calls} · tokens {result.tokens} · "
                  f"缓存命中 {result.cache_hit_tokens} · ¥{result.cost:.4f} · 运行 {result.active_seconds:.1f}s / 确认等待 {result.wait_seconds:.1f}s · "
                  f"越权尝试 {result.violations} · 用户拒绝 {result.rejections} · "
                  f"结束原因 {stop_reason}")
    return result
