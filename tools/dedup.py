"""本地批量查重：字节相同 / PDF 全文规范化相同 / 高词面重合的近似版本。

近似是可解释的文本 shingles 比较，不声称理解任意语义改写。近似组只作复核候选，默认不清理。
扫描计划仅存在 Sandbox 实例内存中，模型只需 scan_id / group_id，不需传回整篇正文或所有路径。
"""
import hashlib
import json
import os
import re
import sys
import time
import threading
import unicodedata
import uuid
from collections import defaultdict, OrderedDict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tools.fs_read import _existing, _paginate, _pdf_pages
from tools.scopes import paths, scan_scope, scan_files
from tools.registry import MAX_RESULT_CHARS, ToolError, TrashBatch, tool


# Process-local, workspace-isolated cache. No extracted text is written to disk.
# Lock includes extraction so concurrent scans cannot parse the same PDF twice.
_PDF_CACHE = OrderedDict()
_PDF_CACHE_LOCK = threading.RLock()
_PDF_CACHE_BYTES = 0
_PDF_CACHE_LIMIT = 64 * 1024 * 1024
_PDF_CACHE_ENTRIES = 512


def cached_pdf_features(sb, rel, info):
    global _PDF_CACHE_BYTES
    p = sb.path(rel)
    key = (str(sb.root), info["stat"])
    with _PDF_CACHE_LOCK:
        if signature(p) != info["stat"]:
            raise ToolError(f"解析前文件变化：{rel}，请重扫")
        if key in _PDF_CACHE:
            value, weight = _PDF_CACHE.pop(key)
            _PDF_CACHE[key] = (value, weight)
            return value, True
        norm = normalize_pdf(_pdf_pages(p, pages=None))
        value = (len(norm), hashlib.sha256(norm.encode()).hexdigest(), frozenset(shingles(norm)))
        if signature(p) != info["stat"]:
            raise ToolError(f"解析期间文件变化：{rel}，请重扫")
        weight = 512 + sys.getsizeof(value[2]) + sum(map(sys.getsizeof, value[2]))
        if weight <= _PDF_CACHE_LIMIT:
            _PDF_CACHE[key] = (value, weight)
            _PDF_CACHE_BYTES += weight
            while _PDF_CACHE_BYTES > _PDF_CACHE_LIMIT or len(_PDF_CACHE) > _PDF_CACHE_ENTRIES:
                _, (_, old_weight) = _PDF_CACHE.popitem(last=False)
                _PDF_CACHE_BYTES -= old_weight
        return value, False


def signature(p):
    s = p.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def sha256(p):
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_pdf(pages):
    # 只去明确的分页边缘页码；保留数字、标点与运算符，避免把不同公式/结果当成相同文本。
    cleaned = []
    for number, page in enumerate(pages, 1):
        lines = page.strip().splitlines()
        for index in (len(lines) - 1, 0):
            if lines and 0 <= index < len(lines) and re.fullmatch(rf"\s*(?:page\s*)?{number}\s*", lines[index], re.I):
                lines.pop(index)
        cleaned.append("\n".join(lines))
    text = unicodedata.normalize("NFKC", "\n".join(cleaned)).replace("\u00ad", "")
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
    return " ".join(re.findall(r"\w+|[^\w\s]", text))


def shingles(text):
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.casefold())
    return {hashlib.blake2b(" ".join(words[i:i + 5]).encode(), digest_size=8).digest()
            for i in range(max(0, len(words) - 4))}


def similarity(a, b):
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def choose_keeper(members, files, keep):
    return min(members, key=lambda p: (
        -files[p]["chars"] if keep == "longest_text" else 0, len(Path(p).name), len(p), p))


def scan_duplicates(sb, path=".", mode="near", threshold=0.85, keep="longest_text"):
    started = time.perf_counter()
    base = _existing(sb, path)
    if not base.is_dir() or any(x.startswith(".") for x in base.relative_to(sb.root).parts):
        raise ToolError("查重范围必须是可见的沙盒内文件夹")
    files, sizes = {}, defaultdict(list)
    stats = dict(files=0, pdf=0, parsed=0, text_cache_hits=0, byte_hashed=0, parse_failed=0, short_or_empty=0, symlinks=0, cloud_only=0, hard_links=0)
    seen_inodes = set()
    for folder, dirs, names in os.walk(base, followlinks=False):
        d = Path(folder)
        stats["symlinks"] += sum((d / n).is_symlink() for n in dirs if not n.startswith("."))
        dirs[:] = sorted(n for n in dirs if not n.startswith(".") and not (d / n).is_symlink())
        for name in sorted(names):
            p = d / name
            if name.startswith("."):
                continue
            if p.is_symlink():
                stats["symlinks"] += 1
                continue
            if not p.is_file():
                continue
            from agent.artifacts import cloud_only
            if cloud_only(p):  # content only in iCloud: comparing it would download it
                stats["cloud_only"] += 1
                continue
            p = sb.path(sb.rel(p))
            sig = signature(p)
            # A hard link is another name for the same file, not a copy: recycling it frees
            # nothing. The first name found stands for the file.
            if (sig[0], sig[1]) in seen_inodes:
                stats["hard_links"] += 1
                continue
            seen_inodes.add((sig[0], sig[1]))
            rel = sb.rel(p)
            files[rel] = dict(stat=sig, byte_hash=None, text_hash=None, chars=0)
            sizes[sig[2]].append(rel)
    stats["files"] = len(files)
    pdfs = [p for p in files if Path(p).suffix.lower() == ".pdf"]
    stats["pdf"] = len(pdfs)
    if mode != "bytes" and len(pdfs) > 500:
        raise ToolError("全文近似查重当前最多处理 500 篇 PDF，请缩小 path；字节模式不受此限制")
    # 唯一大小的普通文件不必读取；PDF 后续按全文内容比对，不受大小筛选限制。
    for group in sizes.values():
        if len(group) > 1:
            for rel in group:
                files[rel]["byte_hash"] = sha256(sb.path(rel))
                stats["byte_hashed"] += 1
    features, same_bytes = {}, {}
    if mode != "bytes":
        for rel in pdfs:
            info = files[rel]
            try:
                digest = info["byte_hash"]
                if digest and digest in same_bytes:
                    value, hit = same_bytes[digest], True
                else:
                    value, hit = cached_pdf_features(sb, rel, info)
                    if digest:
                        same_bytes[digest] = value
                chars, text_hash, feature = value
                stats["text_cache_hits" if hit else "parsed"] += 1
                # Empty/short extraction never establishes text equivalence.
                if chars < 300 or len(feature) < 40:
                    stats["short_or_empty"] += 1
                    continue
                info["chars"] = chars
                info["text_hash"] = text_hash
                features[rel] = feature
            except Exception:
                stats["parse_failed"] += 1
    for rel, info in files.items():
        if signature(sb.path(rel)) != info["stat"]:
            raise ToolError(f"扫描期间文件发生变化：{rel}，请重扫")

    # 先合并确定等价关系，再对代表做完整连接聚类：不把 A≈B、B≈C 自动推导成 A≈C。
    parents = {p: p for p in files}
    def root(p):
        while parents[p] != p:
            p = parents[p]
        return p
    for field in ("byte_hash", "text_hash"):
        seen = {}
        for p, info in files.items():
            value = info[field]
            if value is not None:
                if value in seen:
                    parents[root(p)] = root(seen[value])
                else:
                    seen[value] = p
    clusters = defaultdict(list)
    for p in files:
        clusters[root(p)].append(p)
    parts = list(clusters.values())
    edges, scores, pair_metrics = [], {}, {}
    if mode == "near":
        for i, a in enumerate(parts):
            pa = next((p for p in a if p in features), None)
            if not pa:
                continue
            for j in range(i + 1, len(parts)):
                pb = next((p for p in parts[j] if p in features), None)
                if not pb:
                    continue
                a_features, b_features = features[pa], features[pb]
                shared = len(a_features & b_features)
                jaccard = similarity(a_features, b_features)
                containment = shared / min(len(a_features), len(b_features))
                ratio = min(len(a_features), len(b_features)) / max(len(a_features), len(b_features))
                # 有附录的长版本可能覆盖短版本的大部分内容，但 Jaccard 被新增内容拉低。
                # 仅把这种高覆盖情况列为复核候选，不当作确定重复。
                covered = containment >= max(0.93, threshold) and ratio >= 0.8
                score = max(jaccard, containment if covered else 0)
                scores[i, j] = score
                pair_metrics[i, j] = dict(jaccard=jaccard, containment=containment, length_ratio=ratio)
                if score >= threshold:
                    edges.append((score, i, j))
    sets = [{i} for i in range(len(parts))]
    for score, i, j in sorted(edges, reverse=True):
        a = next(c for c in sets if i in c)
        b = next(c for c in sets if j in c)
        if a is b:
            continue
        if all(scores.get(tuple(sorted((x, y))), 0) >= threshold for x in a for y in b):
            a.update(b)
            sets.remove(b)
    groups = []
    for c in sets:
        members = sorted(p for i in c for p in parts[i])
        if len(members) < 2:
            continue
        kind = "similar" if len(c) > 1 else (
            "bytes" if len({files[p]["byte_hash"] for p in members}) == 1 and files[members[0]]["byte_hash"] else "text")
        keeper = choose_keeper(members, files, keep)
        metrics = [pair_metrics[tuple(sorted((x, y)))] for x in c for y in c if x < y]
        minimum = min((m["jaccard"] for m in metrics), default=1.0)
        groups.append(dict(id=f"G{len(groups) + 1:03d}", kind=kind, members=members, keep=keeper,
                           text_chars={p: files[p]["chars"] for p in members},
                           similarity=round(minimum, 6),
                           containment=round(min((m["containment"] for m in metrics), default=1.0), 6),
                           length_ratio=round(min((m["length_ratio"] for m in metrics), default=1.0), 6),
                           confirmed_subgroups=[dict(members=parts[i], keep=choose_keeper(parts[i], files, keep))
                                                for i in sorted(c) if len(c) > 1 and len(parts[i]) > 1],
                           review_required=kind == "similar"))
    # 只为实际重复组保存加密指纹；执行时核对保留文件和所有待回收文件。
    fingerprints = {}
    for group in groups:
        for rel in group["members"]:
            p = sb.path(rel)
            digest = files[rel]["byte_hash"] or sha256(p)
            if signature(p) != files[rel]["stat"]:
                raise ToolError(f"生成计划期间文件变化：{rel}，请重扫")
            fingerprints[rel] = digest
    scan = dict(id=uuid.uuid4().hex[:12], path=sb.rel(base), mode=mode, threshold=threshold, keep=keep,
                groups=groups, fingerprints=fingerprints, stats=stats,
                seconds=time.perf_counter() - started)
    if not hasattr(sb, "_dedup_scans"):
        sb._dedup_scans = {}
    sb._dedup_scans[scan["id"]] = scan
    while len(sb._dedup_scans) > 3:
        del sb._dedup_scans[next(iter(sb._dedup_scans))]
    return scan


def get_scan(sb, scan_id):
    scan = getattr(sb, "_dedup_scans", {}).get(scan_id)
    if scan is None:
        raise ToolError("扫描计划不存在或已过期，请重新 find_duplicates；scan_id 仅在当前运行内有效")
    return scan


class FindDuplicatesArgs(BaseModel):
    path: str = Field(".", description="递归查重范围，相对沙盒")
    mode: Literal["bytes", "text", "near"] = Field("near", description="bytes 字节；text 加 PDF 全文规范化等价；near 再找近似版本候选")
    threshold: float = Field(0.85, ge=0.7, le=1, description="近似 Jaccard 阈值；或较短版本覆盖率>=max(0.93,阈值)且特征长度比>=0.8。仅词面复核候选")
    keep: Literal["longest_text", "shortest_name"] = Field("longest_text", description="默认优先保留提取文本较完整者，同分按短文件名确定；可选仅按短文件名")
    scan_id: str | None = Field(None, description="翻页/看明细时传回 scan_id，复用计划；此时不重新扫描，path/mode 等以原计划为准")
    page: int = Field(1, ge=1)
    detail: bool = Field(False, description="是否逐文件显示组内路径；默认仅返回组摘要，减少模型上下文")


@tool("find_duplicates",
      "一次本地批量查重，代替逐文件 file_info/read_file：按大小和 SHA256 找字节重复，PDF 用全文规范化哈希及 shingles 找相同文本/近似版本。"
      "仅把分组、保留建议和相似度返回模型，不回填全文。similar 组必须复核，相关论文不等于重复；不支持任意语义改写或 OCR。"
      "翻页传 scan_id。只查完全相同文件用 mode=bytes。清理可调用 apply_dedup，不要逐个 trash。例：{\"path\":\"文献\",\"mode\":\"near\"}",
      FindDuplicatesArgs, display_result=True, read_scope=scan_scope)
def find_duplicates(a, sb):
    scan = get_scan(sb, a.scan_id) if a.scan_id else scan_duplicates(sb, a.path, a.mode, a.threshold, a.keep)
    lines = []
    artifacts=getattr(sb,'artifacts',None)
    for g in scan['groups']:
        if artifacts:
            refs={p:artifacts.register(p) for p in g['members']}
            lines.append(f"{g['id']} {g['kind']} · {len(g['members'])} 文件 · J={g['similarity']:.3f} coverage={g['containment']:.3f} · 保留 file:{refs[g['keep']]}" + (' · 需复核' if g['review_required'] else ''))
            if a.detail:
                lines.extend(f"  {g['id']} file:{refs[p]} {json.dumps(Path(p).name,ensure_ascii=False)}" for p in g['members'])
        else:
            lines.append(f"{g['id']} {g['kind']} · {len(g['members'])} 文件 · Jaccard={g['similarity']:.3f} coverage={g['containment']:.3f}"
                         f" · 保留 {json.dumps(g['keep'], ensure_ascii=False)}" + (" · 需复核" if g["review_required"] else ""))
            if a.detail:
                lines.extend(f"  {g['id']} {json.dumps(p, ensure_ascii=False)}" for p in g["members"])
    s = scan["stats"]
    safe = sum(sum(len(u["members"]) - 1 for u in g["confirmed_subgroups"]) if g["review_required"]
               else len(g["members"]) - 1 for g in scan["groups"])
    header = (f"OK: scan_id={scan['id']} 范围={json.dumps(scan['path'], ensure_ascii=False)} mode={scan['mode']}\n"
            f"扫描 {s['files']} 文件、{s['pdf']} PDF；全文解析 {s['parsed']}，缓存复用 {s['text_cache_hits']}，失败 {s['parse_failed']}，过短/无文字 {s['short_or_empty']}，跳过链接 {s['symlinks']}"
            + (f"，跳过只在 iCloud 云端的文件 {s['cloud_only']}" if s.get('cloud_only') else "")
            + (f"，跳过同一文件的硬链接 {s['hard_links']}" if s.get('hard_links') else "") + "\n"
            f"重复/近似组 {len(scan['groups'])}，确定副本可回收 {safe}；similar 不是确定重复，默认仅清理其确定重复子组。\n")
    budget = min(1700, MAX_RESULT_CHARS - len(header) - 120)
    if budget < 100 or any(len(line) > budget for line in lines):
        raise ToolError("路径过长，无法完整显示，请缩小 path 或用本地 CLI JSON 报告")
    shown, footer = _paginate(lines, a.page, 50, max_chars=budget)
    return header + "\n".join(shown or ["未发现符合条件的重复组"]) + f"\n{footer}；翻页请带 scan_id"


class ApplyDedupArgs(BaseModel):
    scan_id: str = Field(description="find_duplicates 返回的当前运行内计划 ID")
    group_ids: list[str] | None = Field(None, description="要处理的组 ID；省略则处理符合条件的全部组")
    include_similar: bool = Field(False, description="只有用户已明确同意按近似版本合并时才设 true；默认仅处理 bytes/text")
    keepers: dict[str, str] = Field(default_factory=dict, description="可选：组 ID 到明确保留路径的映射；必须是该组成员")


@tool("apply_dedup",
      "按本地查重计划批量将副本移入回收区，不删除。默认只处理 bytes/text，similar 候选默认跳过；"
      "用户明确同意合并近似版本后才传 include_similar=true。终端一次展示保留/回收清单并确认，每个文件记日志可 undo。"
      "执行前重新核对所有保留/回收文件 SHA256，变化则拒绝并要求重扫。例：{\"scan_id\":\"扫描返回的ID\"}",
      ApplyDedupArgs, writes=True, batch=True, effects=('trash',),
      read_scope=scan_files, write_scope=scan_files, produces=('receipts',))
def apply_dedup(a, sb):
    scan = get_scan(sb, a.scan_id)
    by_id = {g["id"]: g for g in scan["groups"]}
    ids = list(dict.fromkeys(a.group_ids)) if a.group_ids is not None else [
        g["id"] for g in scan["groups"]]
    if set(ids) - by_id.keys() or set(a.keepers) - set(ids):
        raise ToolError("组 ID 无效，或 keepers 包含未选择的组")
    paths, keeps, checked, review_groups = [], [], set(), []
    for gid in ids:
        g = by_id[gid]
        if g["review_required"] and not a.include_similar:
            if a.group_ids is not None or gid in a.keepers:
                raise ToolError(f"{gid} 是近似版本，需复核后明确 include_similar=true")
            units = g["confirmed_subgroups"]  # 不漏掉近似大组中已证实相同的副本；保留其不同版本。
        else:
            units = [g]
        for index, unit in enumerate(units, 1):
            keeper = a.keepers.get(gid, unit["keep"])
            if keeper.startswith('file:') and getattr(sb,'artifacts',None):
                target,_=sb.artifacts.resolve(keeper[5:],allow_trash=False)
                keeper=sb.rel(target)
            if keeper not in unit["members"]:
                raise ToolError(f"{gid} 的保留路径不是组成员")
            keeps.append(keeper)
            paths.extend(p for p in unit["members"] if p != keeper)
            checked.update(unit["members"])
            kind = unit.get("kind") or ("bytes" if len({scan["fingerprints"][p] for p in unit["members"]}) == 1 else "text")
            review_groups.append({"id": gid if unit is g else f"{gid}.{index}", "kind": kind,
                                  "keeper": keeper, "members": list(unit["members"])})
    def validate():
        for rel in sorted(checked):
            lexical = sb.root / rel
            if any(p.is_symlink() for p in (lexical, *lexical.parents) if p != sb.root and p.is_relative_to(sb.root)):
                raise ToolError(f"计划路径已被换成符号链接：{rel}；请重扫")
            p = sb.path(rel, write=True, exact=True)
            if p.is_symlink() or not p.is_file() or sha256(p) != scan["fingerprints"][rel]:
                raise ToolError(f"计划已过期，文件缺失或内容变化：{rel}；请重扫，未继续回收")
    def verify_keeps():
        for rel in keeps:
            p = sb.path(rel, write=True, exact=True)
            if p.is_symlink() or not p.is_file() or sha256(p) != scan["fingerprints"][rel]:
                raise ToolError(f"保留文件变化或缺失：{rel}")
    return TrashBatch(paths, keeps, validate, verify_keeps=verify_keeps, groups=review_groups,
                      scan=dict(path=scan['path'], mode=scan['mode'], stats=scan['stats']))


class DeduplicateArgs(BaseModel):
    path: str = Field('.',description='用户指定的查重范围；本地检查其存在性，无需预先 list_dir')
    mode: Literal['bytes','text'] = Field('text',description='文件字节完全相同用 bytes，论文文字相同用 text；不处理近似版本')
    keep: Literal['longest_text','shortest_name'] = 'longest_text'
    keep_files: list[str] = Field(default_factory=list,description='用户明确要求保留的文件，传 file:F标识或工具确认的原路径；不确定时使用分步查重')


@tool('deduplicate',
      '按用户明确的字节/内容去重清理要求，一次在本地完成扫描、分组、网页核对确认、批量回收、核验与结果表格。'
      '内容相同用 text，字节相同用 bytes。无需 list_dir，也无需随后 apply_dedup 或再次扫描。每个移动写日志，可 undo。'
      '仅查重、要求比较近似版本或保留策略尚不明确时使用 find_duplicates 分步处理。',
      DeduplicateArgs,writes=True,batch=True,effects=('trash',),
      read_scope=paths('path'), write_scope=paths('path'), produces=('receipts',))
def deduplicate(a,sb):
    progress=getattr(sb,'progress',lambda _:None)
    progress('扫描文件并比较内容')
    scan=scan_duplicates(sb,a.path,a.mode,keep=a.keep)
    requested=[]
    for name in a.keep_files:
        if name.startswith('file:') and getattr(sb,'artifacts',None):
            p,_=sb.artifacts.resolve(name[5:],allow_trash=False)
        else:p=sb.path(name)
        requested.append(sb.rel(p))
    mapping={}
    for group in scan['groups']:
        chosen=[p for p in requested if p in group['members']]
        if len(chosen)>1: raise ToolError('同组指定了多个保留文件，请明确保留项')
        if chosen:mapping[group['id']]=chosen[0]
    if set(requested)-{p for g in scan['groups'] for p in g['members']}:
        raise ToolError('指定保留文件不属于本次重复组，未执行回收')
    progress('准备分组核对清单')
    return apply_dedup(ApplyDedupArgs(scan_id=scan['id'],keepers=mapping),sb)
