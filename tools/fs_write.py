"""Trash helper used by batch writers (deduplicate / apply_dedup). Nothing is deleted:
trash only moves into the recycle area, which the run's undo can restore."""
from datetime import datetime

from pydantic import BaseModel, Field

from safety.sandbox import Sandbox
from tools.registry import ToolError
from agent.constraints import enforce, operation


class TrashArgs(BaseModel):
    path: str = Field(description="要移入回收区的文件或文件夹，相对沙盒")


def trash(a: TrashArgs, sb: Sandbox):
    src = sb.path(a.path, write=True)
    if not src.exists():
        raise ToolError(f"{a.path} 不存在。可以先用 list_dir 或 search_files 确认路径")
    enforce(sb, [operation('trash', sb.rel(src))])
    bin_dir = sb.root / ".trash"
    bin_dir.mkdir(exist_ok=True)
    dst = bin_dir / f"{datetime.now():%Y%m%d%H%M%S%f}_{src.name}"
    src.rename(dst)
    return f"OK: {a.path} 已移入回收区（{sb.rel(dst)}）", [("trash", sb.rel(src), sb.rel(dst))]
