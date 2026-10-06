"""工具注册、Schema 生成、参数校验与分发。"""
import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from safety.confirm import Confirmer
from safety.oplog import OpLog
from safety.sandbox import Sandbox, SandboxError

MAX_RESULT_CHARS = 2000
PATH_FIELDS = ("path", "src", "dst_dir")  # 写操作执行前要预检查的路径参数


class ToolError(Exception):
    """工具内部的可预期错误（文件不存在、重名等），信息会原样回传给模型。"""


@dataclass
class Tool:
    name: str
    desc: str
    args_model: type[BaseModel]
    func: Callable  # (args, sandbox) -> str；写工具返回 (str, [(op, src, dst), ...]) 供记日志
    writes: bool


TOOLS: dict[str, Tool] = {}


def tool(name: str, desc: str, args_model: type[BaseModel], writes: bool = False):
    def deco(func):
        TOOLS[name] = Tool(name, desc, args_model, func, writes)
        return func
    return deco


def schemas() -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.desc,
                      "parameters": t.args_model.model_json_schema()}}
        for t in TOOLS.values()
    ]


def dispatch(name: str, raw_args: str, sandbox: Sandbox,
             oplog: OpLog | None = None, confirmer: Confirmer | None = None) -> str:
    """JSON 解析 → pydantic 校验 → 沙盒路径检查 →（写操作）确认 → 执行 →（写操作）记日志 → 截断。
    任何错误都转成一句 ERROR 文本返回给模型，不让程序崩溃。"""
    t = TOOLS.get(name)
    if t is None:
        return f"ERROR: 没有工具 {name}。可用工具：{', '.join(TOOLS)}"
    try:
        data = json.loads(raw_args or "{}")
    except json.JSONDecodeError as e:
        return f"ERROR: 参数不是合法 JSON：{e}。请重新生成"
    try:
        args = t.args_model.model_validate(data)
    except ValidationError as e:
        errs = "；".join(f"{'.'.join(map(str, x['loc'])) or '参数'}: {x['msg']}" for x in e.errors())
        return f"ERROR: 参数校验失败：{errs}"
    try:
        if t.writes:
            # 先查路径再问用户，不让用户去确认一个本来就会被拦下的操作
            for f in PATH_FIELDS:
                if hasattr(args, f):
                    v = getattr(args, f)
                    sandbox.path(v, write=not (f == "dst_dir" and v.strip() in ("", ".")))
            if confirmer and not confirmer.allow(name, args.model_dump()):
                return "ERROR: 用户拒绝了这个操作。请调整计划，或在总结里说明没有执行的部分"
        result = t.func(args, sandbox)
    except SandboxError as e:
        return f"ERROR: {e}"
    except ToolError as e:
        return f"ERROR: {e}"
    except Exception as e:  # 兜底：意外异常也回传给模型
        return f"ERROR: {type(e).__name__}: {e}"
    if isinstance(result, tuple):
        result, ops = result
        if oplog:
            for op in ops:
                oplog.record(*op)
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + f"\n……[已截断，共 {len(result)} 字]"
    return result
