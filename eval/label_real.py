"""给真实数据预填标准答案：python -m eval.label_real papers

只填能客观得出的字段，主观字段（主题）按当前所在文件夹预填、交给人审核。
标签绝不能让被测模型来标，否则就是自己考自己。
已有的标注不会被覆盖：重复运行只补新文件，所以可以放心手改。

字段：
  lang      zh / en，按第一页中文字符比例判断
  title     候选标题：PDF 元数据 → 像标题的文件名 → 第一页第一行像样的文字
  title_key 标题里有区分度的一段（改名任务判定用：新文件名包含它即可，不区分大小写和标点）
  topic_ok  可接受的分类（文件夹名）列表；先填当前所在文件夹，根目录的留空
  review    还需要人工确认的字段，确认后把它从列表里删掉
"""
import argparse
import logging
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "real"
BAD_META = re.compile(r"untitled|microsoft|^doi|\.dvi|\.docx?$|\.indd$|^paper$|^title$", re.I)


def first_page_text(path: Path) -> tuple[str, str]:
    from pypdf import PdfReader
    logging.disable(logging.WARNING)  # pypdf 对不规范的 PDF 会刷很多警告
    reader = PdfReader(path)
    meta = (reader.metadata.title or "").strip() if reader.metadata else ""
    text = reader.pages[0].extract_text() or "" if reader.pages else ""
    return meta, text


def guess_title(path: Path, meta: str, text: str) -> tuple[str, str]:
    if meta and len(meta) > 15 and not BAD_META.search(meta):
        return meta, "PDF 元数据"
    stem = path.stem.replace("_", " ").strip()
    if len(stem.split()) >= 4 or (re.search(r"[一-鿿]", stem) and len(stem) >= 8):
        return stem, "文件名"
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 20 and not re.search(r"arxiv|http|©|copyright|journal|vol\.|doi", line, re.I):
            return line, "正文第一行"
    return "", "未找到"


def title_key(title: str) -> str:
    """取标题前 5 个英文词或前 10 个汉字，作为改名判定的关键片段。"""
    if re.search(r"[一-鿿]", title):
        return re.sub(r"\s+", "", title)[:10]
    words = re.findall(r"[A-Za-z0-9]+", title)
    return " ".join(words[:5])


def label(name: str, force_fields: bool = False):
    data_dir = REAL / name
    out = REAL / f"{name}.labels.yaml"
    labels = yaml.safe_load(out.read_text(encoding="utf-8")) if out.exists() else {}
    labels = labels or {}
    added = 0
    for p in sorted(data_dir.rglob("*")):
        rel = p.relative_to(data_dir).as_posix()
        if not p.is_file() or any(x.startswith(".") for x in p.relative_to(data_dir).parts) or rel in labels:
            continue
        info, review = {}, []
        if p.suffix.lower() == ".pdf":
            meta, text = first_page_text(p)
            cjk = len(re.findall(r"[一-鿿]", text))
            info["lang"] = "zh" if cjk > 0.2 * max(1, len(re.sub(r"\s", "", text))) else "en"
            title, source = guess_title(p, meta, text)
            info["title"] = title
            info["title_key"] = title_key(title)
            info["title_source"] = source
            review += ["title"] if source != "PDF 元数据" else []
        parent = p.relative_to(data_dir).parent.as_posix()
        info["topic_ok"] = [] if parent == "." else [parent]
        review.append("topic_ok")
        info["review"] = review
        labels[rel] = info
        added += 1
    missing = [k for k in labels if not (data_dir / k).exists()]
    header = ("# 真实数据标准答案（由 eval/label_real.py 预填，需人工审核）。\n"
              "# 审核完一项就把它从该条的 review 列表里删掉；review 为空表示这条已确认。\n")
    out.write_text(header + yaml.safe_dump(labels, allow_unicode=True, sort_keys=True, width=200), encoding="utf-8")
    pending = sum(bool(v.get("review")) for v in labels.values())
    print(f"{out.relative_to(ROOT)}：新增 {added} 条，共 {len(labels)} 条，待审核 {pending} 条")
    if missing:
        print(f"注意：{len(missing)} 条标注对应的文件已不存在：{missing[:3]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="data/real/ 下的数据集名，如 papers")
    args = ap.parse_args()
    if not (REAL / args.name).is_dir():
        sys.exit(f"没有 {REAL / args.name}")
    label(args.name)
