"""论文前两页的 BM25 主题检索。BM25 索引属于 Sandbox 实例；PDF 抽取按内容和页范围缓存到本资料库，可跨运行复用。

采用正 IDF：log(1 + (N-df+0.5)/(df+0.5))，k1=1.2、b=0.75。
参考 https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables
按论文计文档频率，文件名和目录不参与评分；候选片段不是语义相关性的保证。
"""
import html
import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from safety.sandbox import Sandbox
from tools.fs_read import _existing, _paginate, _pdf_pages
from tools.registry import ToolError, tool
from tools.scopes import paths
from agent.policy import evidence_policy

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
class Paper:
    pages: list[str]
    terms: Counter
    length: int
    error: str = ""


class PaperIndex:
    def __init__(self):
        self.cache = {}  # 实际路径 -> (stat 指纹, Paper)；每次查询重新枚举可见文件

    def collect(self, sb: Sandbox, path: str = ".", pdf_pages=2):
        base = _existing(sb, path)
        if not base.is_dir():
            raise ToolError(f"{path} 不是文件夹")
        if any(p.startswith(".") for p in base.relative_to(sb.root).parts):
            raise ToolError("不检索隐藏目录或回收区")
        papers, live = {}, set()
        stats = dict(pdf=0, indexed=0, failed=0, empty=0, non_pdf=0, symlinks=0, extracted=0)
        for directory, dirs, files in os.walk(base, followlinks=False):
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
                if p.suffix.lower() != ".pdf":
                    stats["non_pdf"] += 1
                    continue
                p = sb.path(sb.rel(p))  # 抽取前再次走沙盒检查
                stats["pdf"] += 1
                live.add(p)
                st = p.stat()
                signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                cached = self.cache.get(p)
                if not cached or cached[0] != signature or cached[2] != pdf_pages:
                    stats["extracted"] += 1
                    try:
                        from agent.document_cache import pdf_pages as cached_pages
                        pages = cached_pages(sb, p, pages=pdf_pages, extractor=_pdf_pages)
                        terms = Counter(tokenize("\n".join(pages)))
                        paper = Paper(pages, terms, sum(terms.values()))
                    except Exception as e:
                        paper = Paper([], Counter(), 0, type(e).__name__)
                    self.cache[p] = (signature, paper, pdf_pages)
                paper = self.cache[p][1]
                if paper.error:
                    stats["failed"] += 1
                elif not paper.length:
                    stats["empty"] += 1
                else:
                    papers[sb.rel(p)] = paper
                    stats["indexed"] += 1
        # 子目录查询后可丢弃其他目录缓存，避免把过期路径/已删除内容保留在索引里。
        self.cache = {p: v for p, v in self.cache.items() if p in live}
        return papers, stats

    def search(self, sb: Sandbox, query: str, path: str = ".", top_k: int = 5, match: str = "any", pdf_pages=2):
        query_terms = set(tokenize(query))
        if not query_terms:
            raise ToolError("query 没有可检索的关键词，请提供主题词")
        papers, stats = self.collect(sb, path, pdf_pages)
        n = len(papers)
        if not n:
            return [], stats
        df = Counter(t for p in papers.values() for t in p.terms)
        avg = sum(p.length for p in papers.values()) / n
        idf = {t: math.log1p((n - df[t] + 0.5) / (df[t] + 0.5)) for t in query_terms}
        hits = []
        for rel, p in papers.items():
            if match == "all" and not query_terms.issubset(p.terms):
                continue
            score = sum(idf[t] * p.terms[t] * 2.2 /
                        (p.terms[t] + 1.2 * (0.25 + 0.75 * p.length / avg))
                        for t in sorted(query_terms) if p.terms[t])
            if score > 0:
                page, offset, snippet = best_snippet(p.pages, query_terms, idf)
                hits.append(dict(path=rel, score=score, page=page, offset=offset, snippet=snippet))
        hits.sort(key=lambda h: (-h["score"], h["path"]))
        stats["matched"] = len(hits)
        return hits[:top_k], stats


def best_snippet(pages, query_terms, idf):
    # 在原文窗口上评分，偏移可直接交给 read_file；不在规范化后的字符串上计算偏移。
    candidates = []
    for page, text in enumerate(pages, 1):
        for offset in range(0, len(text), 120):
            fragment = text[offset:offset + 240]
            matched = query_terms.intersection(tokenize(fragment))
            score = sum(idf[t] for t in sorted(matched))
            candidates.append((score, page, offset, fragment))
    _, page, offset, snippet = max(candidates, key=lambda x: (x[0], -x[1], -x[2]))
    return page, offset, snippet


class ContentSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="主题关键词；英文论文优先用英文词，如 semi supervised fake news detection")
    path: str = Field(".", description="递归检索的沙盒内文件夹")
    top_k: int = Field(5, ge=1, le=5000, description="最多保留多少篇候选；分页输出，不代表所有相关论文")
    page: int = Field(1, ge=1, description="输出页码，结果较长时按提示翻页")
    match: Literal["any", "all"] = Field("any", description="any 为相关性召回；all 要求所有查询词均出现，适合 Sumiko Anno 等完整姓名；不是作者身份验证")
    retry_reason: Literal["insufficient_evidence"] | None = Field(None, description="普通查找已有命中但证据不足，需要唯一一次补充查询时填写")
    view: Literal["compact", "detail", "evidence"] = Field("compact", description="compact 返回路径/分数/位置；detail 加片段；evidence 返回本地文件与证据标识的卡片，适合普通主题查找")
    pdf_pages: int | None = Field(None, ge=1, le=10000, description='不填沿用任务策略；显式 null 抽取全部文字层，正整数抽取前 N 页')


@tool("search_content",
      "递归用 BM25 找 PDF 候选；抽取范围由任务 evidence.pdf_pages 或本次 pdf_pages 指定，默认前两页。"
      "按需求设置 top_k 并翻页，不必先列目录；view=evidence 返回引用片段。查全名可用 match=all，不能保证作者身份。"
      "英文论文用英文关键词；分数不是置信度。compact 候选需正文核实，evidence 已提供原文，确实不足时才 read_file。无命中不代表没有相关论文，跨语言或同义词会漏检。"
      "低分不能证明主题不相关；跳过非PDF文件不等于PDF无文字层，前两页无词项也不能诊断整份PDF没有文字层。"
      "找文件名/后缀用 search_files；全部分类或要求找全时不能只依赖 top_k，需要遍历核查。"
      "例：{\"query\":\"semi supervised fake news detection\",\"top_k\":5}",
      ContentSearchArgs, read_scope=paths('path'), usage='queries')
def search_content(a: ContentSearchArgs, sb: Sandbox) -> str:
    if not hasattr(sb, "_paper_index"):
        sb._paper_index = PaperIndex()
    pages = a.pdf_pages if 'pdf_pages' in a.model_fields_set else evidence_policy(sb).pdf_pages
    hits, stats = sb._paper_index.search(sb, a.query, a.path, a.top_k, a.match, pages)
    coverage = '前两页' if pages == 2 else f'前 {pages} 页' if pages is not None else '全部文字层页'
    header = (f"OK: BM25，检索范围仅 PDF {coverage}\n"
              f"PDF覆盖：扫描PDF={stats['pdf']}，已索引PDF={stats['indexed']}，解析失败PDF={stats['failed']}，"
              f"{coverage}无可检索词项PDF={stats['empty']}\n"
              f"范围外：跳过非PDF文件={stats['non_pdf']}（不是无文字层PDF），跳过符号链接={stats['symlinks']}\n"
              f"有词项重合 {stats.get('matched', 0)} 篇，候选 {len(hits)} 篇；低分不证明主题不相关，不能保证找全。")
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
        blocks = []
        # Page bounded batches; top_k is the retained candidate count, not a
        # silent five-result cap. Only issue evidence for the displayed page.
        page_size = 4
        total_pages = max(1, (len(hits)+page_size-1)//page_size)
        if a.page > total_pages: raise ToolError(f'共 {total_pages} 页')
        shown_hits = hits[(a.page-1)*page_size:a.page*page_size]
        allowance = max(40, (1500 - len(header)) // page_size - 150)
        for index, hit in enumerate(shown_hits, (a.page-1)*page_size+1):
            p = sb.path(hit['path'])
            fid = artifacts.register(sb.rel(p))
            why = withheld(hit)
            if why:
                blocks.append(f"{index}. file:{fid} 名称={p.stem[:65]}\n[敏感文件：{why}；未显示内容，读取需用户授权]")
                continue
            if tuple(artifacts.data['files'][fid]['stat']) != sb._paper_index.cache[p][0]:
                raise ToolError('检索期间文件变化，请重新查询后再引用')
            snippet = hit['snippet'][:min(240, allowance)]
            eid = artifacts.evidence(fid, 'fragment', page=hit['page'], offset=hit['offset'],
                                     excerpt=snippet, scope=f'PDF {coverage}的检索命中片段',
                                     extracted_pages=len(sb._paper_index.cache[p][1].pages))
            name = p.stem[:65]
            blocks.append(f"{index}. file:{fid} evidence:{eid} p={hit['page']} offset={hit['offset']} 名称={name}\n"
                          f'<file_content>{html.escape(snippet, quote=False)}</file_content>')
        footer = f'第 {a.page}/{total_pages} 页'
        if a.page < total_pages: footer += f'，下一页用 page={a.page+1}'
        return header + '\n' + '\n'.join(blocks or ['无命中；可更换关键词。']) + '\n'+footer+'；候选片段不是相关性结论，需要上下文时 read_file。'
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"{i}. {json.dumps(h['path'], ensure_ascii=False)} · score={h['score']:.4f} · "
                      f"page={h['page']} offset={h['offset']}")
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
