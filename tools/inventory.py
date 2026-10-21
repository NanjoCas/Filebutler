"""One-pass metadata inventory; no PDF parsing, hashing or model requests."""
import os
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field
from agent.artifacts import label
from safety.sandbox import is_hidden
from tools.fs_read import _existing, _fmt_size
from tools.registry import ToolError, tool
from tools.scopes import paths


class InventoryArgs(BaseModel):
    path: str = Field('.', description='递归统计的沙盒内目录')
    ext: str | None = Field(None, max_length=20, description='可选后缀，如 pdf；只按后缀统计，不判断语义类型')


@tool('inventory_files', '本地一次遍历统计文件数量、总大小和后缀分布，不读取正文或逐个 file_info。跳过隐藏项和符号链接。', InventoryArgs,
      display_result=True, read_scope=paths('path'))
def inventory_files(args, sb):
    base = _existing(sb, args.path)
    if not base.is_dir() or any(is_hidden(p) for p in [base, *base.parents] if p != sb.root and p.is_relative_to(sb.root)):
        raise ToolError('统计范围必须是可见的沙盒内文件夹')
    ext = args.ext.lower().lstrip('.') if args.ext else None
    counts, sizes = Counter(), Counter()
    skipped = 0
    for folder, dirs, names in os.walk(base, followlinks=False):
        parent = Path(folder)
        dirs[:] = [d for d in dirs if not d.startswith('.') and not (parent / d).is_symlink()]
        for name in names:
            p = parent / name
            if name.startswith('.') or p.is_symlink():
                continue
            if ext and p.suffix.lower().lstrip('.') != ext:
                continue
            try:
                if not p.is_file():
                    continue
                # Recheck sandbox containment if the tree changes during enumeration.
                size = sb.path(sb.rel(p), exact=True).stat().st_size
            except (FileNotFoundError, PermissionError):
                skipped += 1
                continue
            key = p.suffix.lower() or '无后缀'
            counts[key] += 1
            sizes[key] += size
    lines = [f"OK: 递归统计目录 {label(sb.rel(base))}：共 {sum(counts.values())} 个{'PDF ' if ext == 'pdf' else ''}文件，总大小 {_fmt_size(sum(sizes.values()))}。",
             '', '| 类型 | 文件数 | 大小 |', '| --- | ---: | ---: |']
    # Bound model-visible output even with adversarial or unusual extensions.
    ordered = sorted(counts, key=lambda k: (-counts[k], k))
    for key in ordered[:20]:
        safe = label(key)
        lines.append(f'| {safe} | {counts[key]} | {_fmt_size(sizes[key])} |')
    rest = ordered[20:]
    if rest:
        lines.append(f'| 其他 {len(rest)} 种后缀 | {sum(counts[k] for k in rest)} | {_fmt_size(sum(sizes[k] for k in rest))} |')
    lines += ['', f'按文件后缀统计，不代表独立论文篇数；隐藏项和符号链接不计入。扫描期间无法访问的文件 {skipped} 个。']
    return '\n'.join(lines)
