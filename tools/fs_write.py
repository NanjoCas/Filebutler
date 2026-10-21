"""写工具：make_dir / move / rename / trash。没有删除，trash 只是移进回收区。"""
import os
import unicodedata
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from safety.sandbox import Sandbox
from tools.registry import ToolError, tool
from agent.constraints import enforce, operation, creations
from tools.scopes import paths, no_paths, mkdir_operations, move_operations, rename_operations, trash_operations


def _src(sb: Sandbox, path: str) -> Path:
    p = sb.path(path, write=True)
    if not p.exists():
        raise ToolError(f"{path} 不存在。可以先用 list_dir 或 search_files 确认路径")
    return p


# ---------- make_dir ----------
class MakeDirArgs(BaseModel):
    path: str = Field(description="要新建的文件夹路径，相对沙盒，可多级，如 \"文档/报告\"")


@tool("make_dir",
      "新建文件夹（自动创建中间层级；已存在也算成功）。移动文件前目标文件夹必须存在，先用它创建。"
      "例：{\"path\": \"报告\"}",
      MakeDirArgs, writes=True, effects=('create',), read_scope=no_paths, write_scope=paths('path'),
      path_fields=('path',), file_fields=(), operation_preview=mkdir_operations)
def make_dir(a: MakeDirArgs, sb: Sandbox) -> str:
    p = sb.path(a.path, write=True)
    if p.exists() and not p.is_dir():
        raise ToolError(f"{a.path} 已存在且是文件，不是文件夹")
    if p.exists():
        return f"OK: 文件夹已存在 {sb.rel(p)}"
    # 逐层记录新建的文件夹，撤销时从最深一层开始删
    created = [d for d in [p, *p.parents] if d.is_relative_to(sb.root) and not d.exists()][::-1]
    enforce(sb, creations(sb, a.path))
    p.mkdir(parents=True)
    return f"OK: 已创建 {sb.rel(p)}", [("make_dir", sb.rel(d), sb.rel(d)) for d in created]


# ---------- move ----------
class MoveArgs(BaseModel):
    src: str = Field(description="要移动的文件或文件夹，相对沙盒")
    dst_dir: str = Field(description="目标文件夹，相对沙盒，必须已存在；沙盒根目录用 \".\"")


@tool("move",
      "把一个文件或文件夹移动到另一个文件夹里，保留原名。一次只移动一个；目标文件夹不存在时先 make_dir；"
      "要改名请用 rename。例：{\"src\": \"a.pdf\", \"dst_dir\": \"报告\"}",
      MoveArgs, writes=True, effects=('move',), read_scope=paths('src'), write_scope=paths('src', 'dst_dir'),
      path_fields=('src', 'dst_dir'), file_fields=('src',), operation_preview=move_operations)
def move(a: MoveArgs, sb: Sandbox) -> str:
    src = _src(sb, a.src)
    dst_dir = sb.path(a.dst_dir) if a.dst_dir.strip() in ("", ".") else sb.path(a.dst_dir, write=True)
    if not dst_dir.is_dir():
        raise ToolError(f"目标文件夹 {a.dst_dir} 不存在，请先用 make_dir 创建")
    if src.parent == dst_dir:
        return f"OK: {sb.rel(src)} 已经在 {a.dst_dir} 里，无需移动"
    if src.is_dir() and dst_dir.is_relative_to(src):
        raise ToolError("不能把文件夹移动到它自己里面")
    dst = dst_dir / src.name
    if dst.exists():
        raise ToolError(f"目标位置已有同名文件 {sb.rel(dst)}，不会覆盖。可以先用 rename 换个名字")
    enforce(sb, [operation('move', sb.rel(src), sb.rel(dst))])
    src.rename(dst)
    return f"OK: {a.src} → {sb.rel(dst)}", [("move", sb.rel(src), sb.rel(dst))]


# ---------- rename ----------
class RenameArgs(BaseModel):
    path: str = Field(description="要改名的文件或文件夹，相对沙盒")
    new_name: str = Field(description="新名字（只是名字，不含路径），要带上扩展名，如 \"发票_京东_128.50.pdf\"")


@tool("rename",
      "在原文件夹内给文件或文件夹改名。new_name 只能是名字，不能含 /；要换文件夹请用 move。"
      "例：{\"path\": \"报告/a.pdf\", \"new_name\": \"年度报告.pdf\"}",
      RenameArgs, writes=True, effects=('rename',), read_scope=paths('path'), write_scope=paths('path'),
      path_fields=('path',), file_fields=('path',), operation_preview=rename_operations)
def rename(a: RenameArgs, sb: Sandbox) -> str:
    src = _src(sb, a.path)
    new_name = unicodedata.normalize("NFC", a.new_name.strip())
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise ToolError("new_name 只能是文件名，不能含路径分隔符；要换文件夹请用 move")
    if new_name.startswith("."):
        raise ToolError("新名字不能以 . 开头（会变成隐藏文件）")
    dst = src.parent / new_name
    # macOS 默认不区分大小写：a.pdf → A.pdf 时 dst.exists() 为真，但其实是同一个文件
    case_only = dst.exists() and os.path.samefile(src, dst) and src.name != new_name
    if dst.exists() and not case_only:
        if src.name == new_name:
            return f"OK: 名字没有变化，仍是 {sb.rel(src)}"
        raise ToolError(f"同一文件夹里已有 {new_name}，不会覆盖。可以换个名字")
    enforce(sb, [operation('rename', sb.rel(src), sb.rel(dst))])
    src.rename(dst)
    msg = f"OK: {a.path} → {sb.rel(dst)}"
    if dst.is_file() and src.suffix.lower() != dst.suffix.lower():
        msg += f"（注意：扩展名从 {src.suffix or '无'} 变成了 {dst.suffix or '无'}）"
    return msg, [("rename", sb.rel(src), sb.rel(dst))]


# ---------- trash ----------
class TrashArgs(BaseModel):
    path: str = Field(description="要移入回收区的文件或文件夹，相对沙盒")


@tool("trash",
      "把文件或文件夹移入回收区（代替删除，可以撤销）。只在用户明确要求删除、清理或去重时使用。"
      "例：{\"path\": \"IMG_2041 (1).jpg\"}",
      TrashArgs, writes=True, effects=('trash',), read_scope=paths('path'), write_scope=paths('path'),
      path_fields=('path',), file_fields=('path',), operation_preview=trash_operations)
def trash(a: TrashArgs, sb: Sandbox) -> str:
    src = _src(sb, a.path)
    enforce(sb, [operation('trash', sb.rel(src))])
    bin_dir = sb.root / ".trash"
    bin_dir.mkdir(exist_ok=True)
    dst = bin_dir / f"{datetime.now():%Y%m%d%H%M%S%f}_{src.name}"
    src.rename(dst)
    return f"OK: {a.path} 已移入回收区（{sb.rel(dst)}）", [("trash", sb.rel(src), sb.rel(dst))]
