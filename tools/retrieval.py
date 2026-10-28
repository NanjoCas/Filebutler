"""全文 BM25 检索：PDF、文本、Office、网页，图片和扫描页经本地 OCR（功能方案阶段 3，2026-10-24）。

索引属于 Sandbox 实例；文字由 agent/extract 提供，按内容缓存到本资料库，可跨运行复用。
每个文件按约 CHUNK 字切段，按段计 BM25，文件得分取最好的一段；片段和偏移取自该段，可直接交给 read_file。
采用正 IDF：log(1 + (N-df+0.5)/(df+0.5))，k1=1.2、b=0.75。
参考 https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables
文件名和目录不参与评分；候选片段不是语义相关性的保证。
"""
import html
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from safety.sandbox import Sandbox
from tools.fs_read import _existing, _paginate
from tools.registry import ToolError, ToolResult, tool
from tools.scopes import paths

OCR_SECONDS = 20  # OCR time per search; scanned pages left over are recognised by the next one
CHUNK = 500       # characters per scored segment (eval/retrieval_set.py compares sizes)

STOP = set("a an and are as at be been by can for from has have in is it its of on or that the their this to was were we with".split())
TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")


def tokenize(text: str) -> list[str]:
    words = []
    for m in TOKEN.finditer(unicodedata.normalize("NFKC", text).casefold()):
        s = m.group()
        if s[0] >= "\u3400":
            words.extend(s[i:i + 2] for i in range(max(1, len(s) - 1)))
        elif s not in STOP:
            words.append(s)
    return words


@dataclass
class Segment:
    unit: int      # index into Paper.pages
    start: int     # character offset in that unit
    terms: Counter
    length: int


@dataclass
class Paper:
    pages: list[str]   # text units: PDF pages, slides, sheets; one for other files
    terms: Counter
    length: int
    error: str = ""
    pdf: bool = True
    segments: list[Segment] = field(default_factory=list)

    def offset(self, unit, start):
        """Offset for read_file: within the page for a PDF, else within the joined text."""
        return start if self.pdf else sum(len(u) + 1 for u in self.pages[:unit]) + start


def segments(pages, size):
    """Segments of about size characters; size None keeps the whole file as one segment."""
    if size is None:
        terms = Counter(tokenize("\n".join(pages)))
        return [Segment(0, 0, terms, sum(terms.values()))]
    found = []
    for unit, text in enumerate(pages):
        for start in range(0, max(1, len(text)), size):
            terms = Counter(tokenize(text[start:start + size]))
            if terms:
                found.append(Segment(unit, start, terms, sum(terms.values())))
    return found


class PaperIndex:
    """formats 'all' reads every supported format, 'pdf' only PDFs; pdf_pages None is the
    whole PDF; chunk None scores whole files (the pre-stage-3 search was pdf, 2, None)."""

    def __init__(self, formats="all", pdf_pages=None, chunk=CHUNK):
        self.formats, self.pdf_pages, self.chunk = formats, pdf_pages, chunk
        self.cache = {}  # 实际路径 -> (stat 指纹, Paper)；每次查询重新枚举可见文件

    def collect(self, sb: Sandbox, path: str = "."):
        from agent import ocr
        from agent.extract import READABLE_EXTS, units
        base = _existing(sb, path)
        if not base.is_dir():
            raise ToolError(f"{path} 不是文件夹")
        if any(p.startswith(".") for p in base.relative_to(sb.root).parts):
            raise ToolError("不检索隐藏目录或回收区")
        readable = READABLE_EXTS if self.formats == "all" else {".pdf"}
        papers, live = {}, set()
        stats = dict(files=0, indexed=0, failed=0, empty=0, unsupported=0, symlinks=0, extracted=0)
        # Per-file state for the coverage summary (S7, 9): rel -> (version, state, pending, ocr_failed).
        self.states, walk_errors = {}, []
        for directory, dirs, files in os.walk(base, followlinks=False, onerror=walk_errors.append):
            parent = Path(directory)
            stats["symlinks"] += sum((parent / d).is_symlink() for d in dirs if not d.startswith("."))
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (parent / d).is_symlink())
            for name in sorted(files):
                if name.startswith("."):
                    continue
                p = parent / name
                if p.is_symlink():
                    stats["symlinks"] += 1
                    continue
                if p.suffix.lower() not in readable:
                    stats["unsupported"] += 1
                    self.states[sb.rel(p)] = (None, "unsupported", 0, 0)
                    continue
                p = sb.path(sb.rel(p))  # 抽取前再次走沙盒检查
                stats["files"] += 1
                live.add(p)
                st = p.stat()
                signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                cached = self.cache.get(p)
                if not cached or cached[0] != signature:
                    stats["extracted"] += 1
                    before = dict(ocr.stats(sb))
                    try:
                        pages = units(sb, p, pages=self.pdf_pages)
                        terms = Counter(tokenize("\n".join(pages)))
                        paper = Paper(pages, terms, sum(terms.values()), pdf=p.suffix.lower() == ".pdf",
                                      segments=segments(pages, self.chunk))
                    except Exception as e:
                        paper = Paper([], Counter(), 0, type(e).__name__)
                    # Scanned pages and images left for lack of time are recognised by a later search.
                    after = ocr.stats(sb)
                    pending, ocr_failed = after["skipped"] - before["skipped"], after["failed"] - before["failed"]
                    self.cache[p] = (signature if not pending else None, paper, pending, ocr_failed)
                paper = self.cache[p][1]
                pending, ocr_failed = (self.cache[p][2], self.cache[p][3]) if len(self.cache[p]) > 2 else (0, 0)
                state = "failed" if paper.error else "pending_ocr" if pending else "empty" if not paper.length else "indexed"
                self.states[sb.rel(p)] = ((st.st_size, st.st_mtime_ns), state, pending, ocr_failed)
                if paper.error:
                    stats["failed"] += 1
                elif not paper.length:
                    stats["empty"] += 1
                else:
                    papers[sb.rel(p)] = paper
                    stats["indexed"] += 1
        # 子目录查询后可丢弃其他目录缓存，避免把过期路径/已删除内容保留在索引里。
        self.cache = {p: v for p, v in self.cache.items() if p in live}
        stats["enumeration_complete"] = not walk_errors
        return papers, stats

    def search(self, sb: Sandbox, query: str, path: str = ".", top_k: int = 5, match: str = "any"):
        query_terms = set(tokenize(query))
        if not query_terms:
            raise ToolError("query 没有可检索的关键词，请提供主题词")
        from agent import ocr
        before = dict(ocr.stats(sb))
        sb._ocr_deadline = time.monotonic() + OCR_SECONDS
        try:
            papers, stats = self.collect(sb, path)
        finally:
            sb._ocr_deadline = None
        after = ocr.stats(sb)
        stats.update({f"ocr_{k}": after[k] - before[k] for k in ("pages", "images", "skipped", "failed")})
        segs = [s for p in papers.values() for s in p.segments]
        if not segs:
            return [], stats
        n = len(segs)
        df = Counter(t for s in segs for t in s.terms if t in query_terms)
        avg = sum(s.length for s in segs) / n
        idf = {t: math.log1p((n - df[t] + 0.5) / (df[t] + 0.5)) for t in query_terms}
        bm25 = lambda s: sum(idf[t] * s.terms[t] * 2.2 / (s.terms[t] + 1.2 * (0.25 + 0.75 * s.length / avg))
                             for t in sorted(query_terms) if s.terms[t])
        hits = []
        for rel, p in papers.items():
            if match == "all" and not query_terms.issubset(p.terms):
                continue
            score, best = max(((bm25(s), i) for i, s in enumerate(p.segments)), key=lambda x: (x[0], -x[1]))
            if score > 0:
                seg = p.segments[best]
                end = seg.start + self.chunk if self.chunk else None
                unit, start, snippet = best_snippet(p.pages, query_terms, idf, seg.unit if self.chunk else None, seg.start, end)
                hits.append(dict(path=rel, score=score, page=unit + 1 if p.pdf else None,
                                 offset=p.offset(unit, start), snippet=snippet))
        hits.sort(key=lambda h: (-h["score"], h["path"]))
        stats["matched"] = len(hits)
        return hits[:top_k], stats


INDEX_REVISION = f"bm25-chunk{CHUNK}-extract1"
STRATEGY = "bm25"


def coverage(index, scope, stats, hits, top_k, query):
    """What one search covered (S7, 9): counted from each file's index state, not from
    this call's OCR delta, so pages left by an earlier search still count as pending.
    ``extract_complete`` is about extraction, never about having found everything."""
    import hashlib
    states = index.states
    snapshot = hashlib.sha256(json.dumps(sorted((rel, s[0]) for rel, s in states.items()), default=list)
                              .encode()).hexdigest()[:12]
    count = Counter(s[1] for s in states.values())
    pending = sum(s[2] for s in states.values())
    ocr_failed = sum(s[3] for s in states.values())
    scan = hashlib.sha256(f"{scope}|{snapshot}|{INDEX_REVISION}".encode()).hexdigest()[:10]
    return dict(scan_id="S" + scan, scope=scope, snapshot_id=snapshot, index_revision=INDEX_REVISION,
                enumeration_complete=stats.get("enumeration_complete", True),
                indexed_files=count["indexed"], failed_files=count["failed"], empty_files=count["empty"],
                unsupported_files=count["unsupported"], pending_ocr_units=pending, ocr_failed_units=ocr_failed,
                extract_complete=not (count["failed"] or count["pending_ocr"] or ocr_failed),
                matched_files=stats.get("matched", 0), returned_files=len(hits),
                candidate_truncated=stats.get("matched", 0) > len(hits),
                query_terms=sorted(set(tokenize(query)))[:20], strategy=STRATEGY,
                files={rel: dict(version=list(s[0]) if s[0] else None, state=s[1]) for rel, s in states.items()},
                candidates=[h["path"] for h in hits])


def coverage_line(c):
    """One line for the model and the report: what is not covered, in facts."""
    gaps = []
    if not c["enumeration_complete"]: gaps.append("有文件夹无法列出")
    if c["pending_ocr_units"]: gaps.append(f"{c['pending_ocr_units']} 个扫描页或图片尚未识别（再次检索会继续）")
    if c["ocr_failed_units"]: gaps.append(f"{c['ocr_failed_units']} 个扫描页或图片识别失败")
    if c["failed_files"]: gaps.append(f"{c['failed_files']} 个文件读取失败")
    if c["candidate_truncated"]: gaps.append(f"有词项重合 {c['matched_files']} 个、只返回 {c['returned_files']} 个")
    return ("覆盖缺口：" + "；".join(gaps) + "。不能据此说范围内没有其他相关文件") if gaps else ""


def best_snippet(pages, query_terms, idf, unit=None, start=0, end=None):
    """Best 240-character window (step 120) in one unit's [start, end), or in every unit."""
    # 在原文窗口上评分，偏移可直接交给 read_file；不在规范化后的字符串上计算偏移。
    candidates = []
    for index, text in enumerate(pages):
        if unit is not None and index != unit:
            continue
        stop = len(text) if end is None else min(len(text), end)
        for offset in range(start if unit is not None else 0, max(stop, 1), 120):
            fragment = text[offset:offset + 240]
            matched = query_terms.intersection(tokenize(fragment))
            score = sum(idf[t] for t in sorted(matched))
            candidates.append((score, index, offset, fragment))
    _, index, offset, snippet = max(candidates, key=lambda x: (x[0], -x[1], -x[2]))
    return index, offset, snippet


class ContentSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="主题关键词；英文论文优先用英文词，如 semi supervised fake news detection")
    path: str = Field(".", description="递归检索的沙盒内文件夹")
    top_k: int = Field(5, ge=1, le=5000, description="最多保留多少篇候选；分页输出，不代表所有相关论文")
    page: int = Field(1, ge=1, description="输出页码，结果较长时按提示翻页")
    match: Literal["any", "all"] = Field("any", description="any 为相关性召回；all 要求所有查询词均出现，适合 Sumiko Anno 等完整姓名；不是作者身份验证")
    retry_reason: Literal["insufficient_evidence"] | None = Field(None, description="普通查找已有命中但证据不足，需要唯一一次补充查询时填写")
    view: Literal["compact", "detail", "evidence"] = Field("compact", description="compact 返回路径/分数/位置；detail 加片段；evidence 返回本地文件与证据标识的卡片，适合普通主题查找")


@tool("search_content",
      "递归用 BM25 检索文件全文：PDF、文本、Office、网页，图片和扫描页本地 OCR。"
      "按需求设置 top_k 并翻页，不必先列目录；view=evidence 返回引用片段。查全名可用 match=all，不能保证作者身份。"
      "英文论文用英文关键词；分数不是置信度。compact 候选需正文核实，evidence 已提供原文，确实不足时才 read_file。无命中不代表没有相关文件，跨语言或同义词会漏检；低分不能证明主题不相关。"
      "找文件名/后缀用 search_files；全部分类或要求找全时不能只依赖 top_k，需要遍历核查。"
      "例：{\"query\":\"semi supervised fake news detection\",\"top_k\":5}",
      ContentSearchArgs, read_scope=paths('path'), usage='queries')
def search_content(a: ContentSearchArgs, sb: Sandbox) -> str:
    if not hasattr(sb, "_paper_index"):
        sb._paper_index = PaperIndex()
    hits, stats = sb._paper_index.search(sb, a.query, a.path, a.top_k, a.match)
    covered = coverage(sb._paper_index, sb.rel(_existing(sb, a.path)) or '.', stats, hits, a.top_k, a.query)
    header = (f"OK: BM25 全文检索\n"
              f"覆盖：文件={stats['files']}，已索引={stats['indexed']}，读取失败={stats['failed']}，没有文字={stats['empty']}；"
              f"不支持的格式={stats['unsupported']}（压缩包等），跳过符号链接={stats['symlinks']}\n"
              f"有词项重合 {stats.get('matched', 0)} 个文件，候选 {len(hits)} 个；低分不证明主题不相关，不能保证找全。")
    pending, failed = covered['pending_ocr_units'], covered['ocr_failed_units']
    if stats.get('ocr_pages') or stats.get('ocr_images') or pending or failed:
        # Pending and failed pages are counted from the files' index state, so pages an
        # earlier search left still show here (9: not from this call's delta alone).
        header += (f"\n本地 OCR：{stats['ocr_pages']} 个扫描页、{stats['ocr_images']} 张图片已识别"
                   + (f"，{pending} 个因时间上限未识别（再次检索会继续）" if pending else '')
                   + (f"，{failed} 个识别失败" if failed else ''))
    gap = coverage_line(dict(covered, pending_ocr_units=0, ocr_failed_units=0))
    if gap: header += "\n" + gap
    return ToolResult(status='ok', text=_content_results(a, sb, hits, header), data=dict(coverage=covered))


def _content_results(a, sb, hits, header):
    where = lambda h: (f"page={h['page']} " if h['page'] else '') + f"offset={h['offset']}"
    artifacts = getattr(sb, 'artifacts', None)
    from agent.sensitive import enabled, reasons, granted
    def withheld(hit):
        """Why this hit's text is not shown: a sensitive file the user has not granted."""
        if not enabled(sb):
            return ''
        text = '\n'.join(sb._paper_index.cache[sb.path(hit['path'])][1].pages)
        why = reasons(hit['path'], text)
        return '；'.join(why) if why and not granted(sb, hit['path']) else ''
    if artifacts and a.view == 'evidence':
        blocks, batches, batch, used = [], [], [], 0
        budget = 1900 - len(header) - 140
        # Partition with complete paths before issuing evidence. Handles and
        # snippets fit in the tool's output budget; only displayed hits get E IDs.
        for index, hit in enumerate(hits, 1):
            p = sb.path(hit['path'])
            fid = artifacts.register(sb.rel(p))
            why = withheld(hit)
            prefix = f"{index}. file:{fid} {where(hit)} 路径={json.dumps(hit['path'], ensure_ascii=False)}"
            snippet = hit['snippet'][:240]
            estimate = len(prefix) + len(html.escape(snippet, quote=False)) + len(why) + 120
            if estimate > budget:
                raise ToolError('候选路径或覆盖说明过长，无法安全输出；请缩小 path 范围')
            if batch and (len(batch) == 4 or used + estimate > budget):
                batches.append(batch)
                batch, used = [], 0
            batch.append((hit, p, fid, why, prefix, snippet))
            used += estimate
        if batch: batches.append(batch)
        total_pages = max(1, len(batches))
        if a.page > total_pages: raise ToolError(f'共 {total_pages} 页')
        for hit, p, fid, why, prefix, snippet in (batches[a.page-1] if batches else []):
            if why:
                blocks.append(f"{prefix}\n[敏感文件：{why}；未显示内容，读取需用户授权]")
                continue
            if tuple(artifacts.data['files'][fid]['stat']) != sb._paper_index.cache[p][0]:
                raise ToolError('检索期间文件变化，请重新查询后再引用')
            eid = artifacts.evidence(fid, 'fragment', page=hit['page'], offset=hit['offset'],
                                     excerpt=snippet, scope='全文检索命中片段',
                                     extracted_pages=len(sb._paper_index.cache[p][1].pages))
            blocks.append(f"{prefix} evidence:{eid}\n"
                          f'<file_content>{html.escape(snippet, quote=False)}</file_content>')
        footer = f'第 {a.page}/{total_pages} 页'
        if a.page < total_pages: footer += f'，下一页用 page={a.page+1}'
        return header + '\n' + '\n'.join(blocks or ['无命中；可更换关键词。']) + '\n'+footer+'；候选片段不是相关性结论，需要上下文时 read_file。'
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"{i}. {json.dumps(h['path'], ensure_ascii=False)} · score={h['score']:.4f} · "
                      f"{where(h)}")
        if a.view == "detail":
            why = withheld(h)
            if why:
                blocks[-1] += f"\n[敏感文件：{why}；未显示内容，读取需用户授权]"
                continue
            escaped = html.escape(h['snippet'], quote=False)
            blocks[-1] += f"\n<file_content>{escaped}</file_content>"
    budget = min(1700, 2000 - len(header) - 100)
    if any(len(b) > budget for b in blocks):
        raise ToolError("候选路径过长，无法安全输出；请缩小 path 范围")
    shown, footer = _paginate(blocks, a.page, a.top_k, max_chars=budget)
    return header + "\n" + "\n".join(shown or ["无命中；可更换关键词、翻译查询或逐篇读取核查。 "]) + "\n" + footer
