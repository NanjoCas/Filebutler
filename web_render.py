"""Render assistant Markdown and verified sandbox file references without allowing raw HTML."""
from __future__ import annotations

import html
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from safety.sandbox import Sandbox


WEB_PRESENTATION_GUIDANCE = """
## 网页回复格式
- 用清晰的 Markdown 回复，不输出 ANSI 转义、终端边框或 Rich 标记。
- 多个论文或文件适合比较时使用 Markdown 表格，列出名称、主题/依据等有用信息；其他回复使用段落或列表。
- 引用已通过工具确认的文件时，使用 [显示名称](sandbox:/沙盒内相对路径)，例如 [报告](sandbox:/文献/report.pdf)。路径必须来自工具结果，保留真实文件名；空格、括号、# 等 URL 特殊字符可以百分号编码。
- 引用某页时可使用 sandbox:/文献/report.pdf#page=3。网页会将有效引用显示为蓝色文件按钮，点击打开预览。
- 不生成 HTML、颜色代码或自定义按钮；不编造文件路径。格式化不改变检索范围、证据要求或操作确认规则。
"""


class FileCatalog:
    """Resolve visible paths using exact names, then unique suffixes/normalised aliases."""

    def __init__(self, sandbox: Sandbox, artifacts=None, conversation_id=None):
        self.root = sandbox.root
        self.artifacts = artifacts
        self.conversation_id = conversation_id
        self.paths: set[str] = set()
        self.directories: set[str] = set()
        for base, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(base) / d).is_symlink()]
            self.directories.update((Path(base) / d).relative_to(self.root).as_posix() for d in dirs)
            for name in files:
                path = Path(base) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(self.root).as_posix()
                self.paths.add(rel)
        self.all_paths = self.paths | self.directories
        self.suffixes, self.normalised = defaultdict(set), defaultdict(set)
        spellings = set()
        for rel in self.all_paths:
            parts = rel.split("/")
            for index in range(len(parts)):
                suffix = "/".join(parts[index:])
                self.suffixes[suffix].add(rel)
                normal = self._normal(suffix)
                if normal:
                    self.normalised[normal].add(rel)
                # Keep common original-case spellings for automatic text recognition.
                spelling = "/".join(unicodedata.normalize("NFKC", p).strip() for p in parts[index:])
                for name in (suffix, spelling):
                    if not name.strip():
                        continue
                    if rel in self.paths and ("." in Path(name).name or "/" in name):
                        spellings.add(name)
                    elif rel in self.directories:
                        spellings.add(name + "/")
        self.aliases = {name: next(iter(paths)) for name, paths in self.suffixes.items() if len(paths) == 1}
        self.aliases.update({path: path for path in self.all_paths})
        names = sorted(spellings, key=len, reverse=True)
        # Do not turn a suffix inside 'invented-notes.md' or 'notes.md.bak' into a link.
        self.pattern = re.compile(r"(?<![\w/\\.%+\-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w/\\.%+\-])", re.IGNORECASE) if names else None

    @staticmethod
    def _normal(value: str) -> str:
        return "/".join(unicodedata.normalize("NFKC", part).strip().casefold() for part in value.split("/"))

    @staticmethod
    def _clean(value: str) -> str | None:
        value = value.replace("\\", "/")
        if value.startswith("/") or re.match(r"^[A-Za-z][\w+.-]*:", value):
            return None
        parts = [p for p in value.split("/") if p not in ("", ".")]
        if any(p.startswith(".") for p in parts):
            return None
        return "/".join(parts)

    def _matches(self, candidate: str) -> set[str]:
        cleaned = self._clean(candidate)
        if not cleaned:
            return set()
        if cleaned in self.all_paths:
            return {cleaned}
        exact = self.suffixes.get(cleaned)
        if exact:
            return exact
        normal = self._normal(cleaned)
        if self._clean(normal) is None:
            return set()
        return self.normalised.get(normal, set())

    def _variants(self, value: str):
        if value.startswith("sandbox:/"):
            candidate = value[len("sandbox:/"):]
            if candidate.startswith("/"):
                return  # reject a sandbox://host URL
            if candidate in ("", "."):
                yield ".", None
                return
        else:
            candidate = value
        # Preserve literal %, # and ? in names; decode once, only as a fallback.
        candidates = list(dict.fromkeys([candidate, unquote(candidate)]))
        for candidate in candidates:
            yield candidate, None
        for candidate in candidates:
            match = re.fullmatch(r"(.+)#page=([1-9]\d*)", candidate)
            if match:
                yield match[1], int(match[2])

    def kind(self, path: str) -> str:
        return "directory" if path == "." or path in self.directories else "file"

    def artifact_reference(self, value):
        if not self.artifacts:
            return None
        m = re.fullmatch(r'(file|evidence):([FE][a-f0-9]+)(?:#page=([1-9]\d*))?', value)
        if not m:
            return None
        page = int(m[3]) if m[3] else None
        fid = m[2]
        if m[1] == 'evidence':
            ev = self.artifacts.resolve_evidence(fid)
            fid, page = ev['file_id'], ev.get('page') or page
        p, row = self.artifacts.resolve(fid)
        return fid, self.artifacts.sb.rel(p), page, row

    def resolve(self, value: str) -> tuple[str, int | None] | None:
        if value.startswith(('file:', 'evidence:')):
            try:
                ref = self.artifact_reference(value)
                return (ref[1], ref[2]) if ref else None
            except ValueError:
                return None
        for candidate, page in self._variants(value):
            if candidate == "." and value.startswith("sandbox:/"):
                return ".", page
            matches = self._matches(candidate)
            if len(matches) > 1:
                return None
            if not matches:
                continue
            rel = next(iter(matches))
            path = self.root / rel
            if not path.exists() or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(self.root)):
                return None
            return rel, page
        return None

    def explain(self, value: str) -> str:
        if value.startswith(('file:', 'evidence:')):
            try:
                self.artifact_reference(value)
            except ValueError as exc:
                return str(exc)
            return '文件引用不存在或不属于此对话'
        for candidate, _ in self._variants(value):
            if self._clean(candidate) is None:
                return "路径不在可预览的沙盒范围内"
            if len(self._matches(candidate)) > 1:
                return "此路径匹配多个文件或文件夹，请使用完整相对路径"
        if "…" in value or "..." in value:
            return "路径含有省略内容，请使用完整文件名"
        return "文件或文件夹不存在，可能已移动、改名或移入回收区"


def _button(label: str, reference: tuple[str, int | None], kind: str = "file") -> str:
    path, page = reference
    page_attr = f' data-file-page="{page}"' if page else ""
    return (f'<button type="button" class="file-reference" data-file-path="{html.escape(path, quote=True)}" data-file-kind="{kind}"'
            f'{page_attr} title="查看 {html.escape(path, quote=True)}">'
            f'<span class="file-ref-icon" aria-hidden="true">{"▰" if kind == "directory" else "▤"}</span>{html.escape(label)}</button>')


def render_markdown(content: str, files: FileCatalog) -> str:
    md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
    validate_link = md.validateLink
    md.validateLink = lambda value: bool(re.fullmatch(r'(file:F|evidence:E)[a-f0-9]+(?:#page=[1-9]\d*)?', value)) or validate_link(value)

    def repair_local_links(state):
        # Models sometimes omit URL escaping around spaces or parentheses. Repair only
        # destinations that resolve to an actual visible path; code examples stay literal.
        for block in state.tokens:
            if block.type != "inline":
                continue
            source, chunks, cursor = block.content, [], 0
            code_spans = [m.span() for m in re.finditer(r"(`+)(?!`)(.*?)\1(?!`)", source, re.S)]
            for match in re.finditer(r"(?<!!)\[[^\]\n]*\]\(", source):
                if match.start() < cursor or any(a <= match.start() < b for a, b in code_spans):
                    continue
                if source[match.end():].startswith(("file:", "evidence:")):
                    continue
                end_of_line = source.find("\n", match.end())
                limit = end_of_line if end_of_line >= 0 else len(source)
                for end in range(match.end(), min(limit, match.end() + 8192)):
                    if source[end] != ")":
                        continue
                    ref = files.resolve(source[match.end():end])
                    if not ref:
                        continue
                    path, page = ref
                    destination = "sandbox:/" + quote(path, safe="/") + (f"#page={page}" if page else "")
                    chunks.extend([source[cursor:match.end()], destination, ")"])
                    cursor = end + 1
                    break
            if chunks:
                block.content = "".join(chunks) + source[cursor:]

    md.core.ruler.before("inline", "repair_local_links", repair_local_links)

    def reference_text(state):
        for block in state.tokens:
            if not block.children:
                continue
            children, depth = [], 0
            for token in block.children:
                if token.type == "link_open":
                    depth += 1
                if token.type == "text" and not depth and files.pattern:
                    cursor = 0
                    for match in files.pattern.finditer(token.content):
                        ref = files.resolve(match.group())
                        if not ref:
                            continue
                        if match.start() > cursor:
                            before = Token("text", "", 0)
                            before.content = token.content[cursor:match.start()]
                            children.append(before)
                        button = Token("html_inline", "", 0)
                        button.content = _button(match.group(), ref, files.kind(ref[0]))
                        children.append(button)
                        cursor = match.end()
                    if cursor:
                        if cursor < len(token.content):
                            after = Token("text", "", 0)
                            after.content = token.content[cursor:]
                            children.append(after)
                        continue
                children.append(token)
                if token.type == "link_close":
                    depth -= 1
            block.children = children

    md.core.ruler.after("inline", "file_references", reference_text)

    def inside_link(tokens, idx):
        return sum(t.nesting for t in tokens[:idx] if t.type in ("link_open", "link_close")) > 0

    def code_inline(tokens, idx, options, env):
        value = tokens[idx].content
        ref = None if inside_link(tokens, idx) else files.resolve(value)
        if ref:
            return _button(value, ref, files.kind(ref[0]))
        if not inside_link(tokens, idx) and re.search(r"[/\\]|\.(?:pdf|md|txt|csv|json|ya?ml|jpg|jpeg|png|webp)(?:$|#page=)", value, re.IGNORECASE):
            return f'<code class="unavailable-reference" title="{html.escape(files.explain(value), quote=True)}">{html.escape(value)}</code>'
        return f"<code>{html.escape(value)}</code>"

    def link_open(tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrGet("href") or ""
        ref = files.resolve(href)
        closing = next((t for t in tokens[idx + 1:] if t.type == "link_close"), None)
        if ref:
            path, page = ref
            token.tag = "button"
            token.attrs = {"type": "button", "class": "file-reference", "data-file-path": path, "title": f"查看 {path}"}
            token.attrSet("data-file-kind", files.kind(path))
            if href.startswith(('file:', 'evidence:')):
                artifact = files.artifact_reference(href)
                token.attrSet('data-artifact-id', artifact[0])
                token.attrSet('data-conversation-id', files.conversation_id or '')
                token.attrSet('data-file-name', artifact[3]['name'])
                token.attrSet('data-file-status', artifact[3]['status'])
                if href.startswith('evidence:'):
                    token.attrSet('data-evidence', 'true')
            if page:
                token.attrSet("data-file-page", str(page))
        elif re.match(r"^(https?://|mailto:)", href, re.IGNORECASE):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
        elif href.startswith("#"):
            pass
        else:
            token.tag = "span"
            token.attrs = {"class": "unavailable-reference", "title": files.explain(href)}
        if closing:
            closing.tag = token.tag
        return md.renderer.renderToken(tokens, idx, options, env)

    def image(tokens, idx, options, env):
        token = tokens[idx]
        label = token.content or "图片"
        ref = None if inside_link(tokens, idx) else files.resolve(token.attrGet("src") or "")
        return _button(label, ref, files.kind(ref[0])) if ref else f'<span class="unavailable-reference">{html.escape(label)}</span>'

    md.renderer.rules.update(code_inline=code_inline, link_open=link_open, image=image)
    return md.render(content)
