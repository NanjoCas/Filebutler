"""检索质量、证据定位、缓存更新、安全与 Agent 接线的离线测试。"""
import json

import pymupdf
import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from eval.retrieval import evaluate, metrics
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from tools import registry
from tools.fs_read import _pdf_pages
from tools.retrieval import PaperIndex, tokenize


def pdf(path, pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            # 内置 CJK 字体，离线可复现，不需下载测试字体
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=10, fontname="china-s")
        doc.save(path)


def dispatch(sb, name="search_content", **args):
    return registry.dispatch(name, json.dumps(args, ensure_ascii=False), sb)


def test_bm25_ranks_topic_above_incidental_mention_and_ignores_filename(tmp_path):
    sb = Sandbox(tmp_path)
    pdf(tmp_path / "out.pdf", ["Dengue outbreak prediction. Dengue climate model for dengue outbreaks."])
    pdf(tmp_path / "other.pdf", ["Machine learning for news. " * 50 + "dengue"])
    pdf(tmp_path / "dengue.pdf", ["Attention based language understanding."])
    hits, stats = PaperIndex().search(sb, "dengue outbreak prediction")
    assert hits[0]["path"] == "out.pdf"
    assert "dengue.pdf" not in [h["path"] for h in hits]
    assert stats["indexed"] == 3


def test_single_document_and_no_overlap(tmp_path):
    pdf(tmp_path / "one.pdf", ["dengue prediction"])
    sb, index = Sandbox(tmp_path), PaperIndex()
    assert index.search(sb, "dengue")[0][0]["score"] > 0
    assert index.search(sb, "quantum")[0] == []
    out = dispatch(sb, query="quantum")
    assert "无命中" in out and "不能保证找全" in out


def test_chinese_tokenization_and_cross_language_limitation(tmp_path):
    assert tokenize("ＳＥＭＩ-supervised, the MODEL") == ["semi", "supervised", "model"]
    assert "监督" in tokenize("半监督学习")
    pdf(tmp_path / "one.pdf", ["Semi supervised fake news detection"])
    sb, index = Sandbox(tmp_path), PaperIndex()
    assert index.search(sb, "semi supervised")[0]
    assert not index.search(sb, "半监督 假新闻")[0]  # 明确记录 BM25 跨语言的边界


def test_page_offset_can_be_verified_and_third_page_is_not_indexed(tmp_path):
    pdf(tmp_path / "paper.pdf", ["Introduction.", "Background filler. " * 50 + "dengue prediction", "quantum uniquekeyword"])
    sb, index = Sandbox(tmp_path), PaperIndex()
    hit = index.search(sb, "dengue prediction")[0][0]
    assert hit["page"] == 2 and hit["offset"] > 0
    text = _pdf_pages(tmp_path / "paper.pdf", start=2, pages=1)[0]
    assert hit["snippet"] == text[hit["offset"]:hit["offset"] + 240]
    out = dispatch(sb, "read_file", path="paper.pdf", page=2, offset=hit["offset"])
    assert "dengue" in out and "第 2 页" in out
    assert not index.search(sb, "uniquekeyword")[0]
    assert "没有第 4 页" in dispatch(sb, "read_file", path="paper.pdf", page=4)
    assert "超出文字长度" in dispatch(sb, "read_file", path="paper.pdf", page=1, offset=99999)


def test_text_offset_reading(tmp_path):
    (tmp_path / "note.txt").write_text("a" * 1600 + "evidence")
    sb = Sandbox(tmp_path)
    assert "evidence" in dispatch(sb, "read_file", path="note.txt", offset=1600)
    assert "page 只适用于 PDF" in dispatch(sb, "read_file", path="note.txt", page=1)


def test_cache_refreshes_on_edit(tmp_path):
    sb, index = Sandbox(tmp_path), PaperIndex()
    p = tmp_path / "one.pdf"
    pdf(p, ["dengue prediction"])
    _, first = index.search(sb, "dengue")
    _, warm = index.search(sb, "dengue")
    assert first["extracted"] == 1 and warm["extracted"] == 0
    # 替换内容；重新构建索引不能保留旧词项
    p.unlink()
    pdf(p, ["influenza prediction"])
    assert not index.search(sb, "dengue")[0]
    assert index.search(sb, "influenza")[0]


def test_hidden_corrupt_empty_and_symlink_files_are_reported(tmp_path):
    root = tmp_path / "s"
    sb = Sandbox(root)
    pdf(root / "one.pdf", ["dengue prediction"])
    pdf(root / ".trash/hidden.pdf", ["quantum"])
    pdf(root / "blank.pdf", [""])
    (root / "broken.pdf").write_text("not a PDF")
    (root / "note.txt").write_text("quantum")
    pdf(tmp_path / "outside.pdf", ["quantum"])
    (root / "external.pdf").symlink_to(tmp_path / "outside.pdf")
    (root / "external_dir").symlink_to(tmp_path, target_is_directory=True)
    hits, stats = PaperIndex().search(sb, "quantum")
    assert not hits
    assert stats["pdf"] == 3 and stats["indexed"] == 1
    assert stats["failed"] == 1 and stats["empty"] == 1
    assert stats["symlinks"] == 2 and stats["non_pdf"] == 1
    assert "拒绝" in dispatch(sb, query="quantum", path="..")
    assert "不检索隐藏目录" in dispatch(sb, query="quantum", path=".trash")


def test_scope_and_sandbox_caches_do_not_leak(tmp_path):
    pdf(tmp_path / "a/one.pdf", ["dengue"])
    pdf(tmp_path / "a/sub/two.pdf", ["influenza"])
    pdf(tmp_path / "b/one.pdf", ["quantum"])
    a, b = Sandbox(tmp_path / "a"), Sandbox(tmp_path / "b")
    assert "one.pdf" in dispatch(a, query="dengue")
    assert "无命中" in dispatch(b, query="dengue")
    assert a._paper_index is not b._paper_index
    assert "无命中" in dispatch(a, query="dengue", path="sub")


def test_output_is_paginated_without_cutting_evidence_tags(tmp_path):
    for i in range(12):
        pdf(tmp_path / (str(i) + "long_name_" * 8 + ".pdf"), ["dengue <file_content> data </file_content> " * 5])
    sb = Sandbox(tmp_path)
    first = dispatch(sb, query="dengue", top_k=12, view="detail")
    assert "下一页" in first and "已截断" not in first and len(first) <= 2000
    assert first.count("<file_content>") == first.count("</file_content>")
    assert "&lt;/file_content&gt;" in first
    second = dispatch(sb, query="dengue", top_k=12, page=2, view="detail")
    assert second != first and "已截断" not in second


@pytest.mark.parametrize("args", [dict(query=" "), dict(query="dengue", top_k=0), dict(query="dengue", page=0)])
def test_invalid_query_arguments_return_error(tmp_path, args):
    assert dispatch(Sandbox(tmp_path), **args).startswith("ERROR")


def test_retrieval_disabled_hides_schema_and_rejects_calls(tmp_path):
    class Capture(FakeClient):
        def create(self, **kw):
            assert "search_content" not in {s["function"]["name"] for s in kw["tools"]}
            return super().create(**kw)
    client = Capture([[("search_content", {"query": "dengue"})], "完成"])
    sb = Sandbox(tmp_path)
    run_agent("测试", LLM({"model": "fake"}, client=client), sb, {"retrieval": False}, Console(quiet=True))
    assert "未启用内容检索" in client.seen[1][-1]["content"]
    assert not hasattr(sb, "_paper_index")


def test_metrics_and_evidence_validation(tmp_path):
    assert metrics(["a", "b"], {"a", "c"}, 5)["precision"] == 0.2
    assert metrics([], set(), 5)["recall"] is None
    pdf(tmp_path / "one.pdf", ["Dengue outbreak prediction"])
    q = dict(id="test", kind="english", query="dengue", relevant=[
        dict(path="one.pdf", page=1, evidence="Dengue outbreak")])
    report = evaluate(tmp_path, [q])
    assert report["rows"][0]["bm25"]["recall"] == 1
    assert report["rows"][0]["extracted"] == 0
    q["relevant"][0]["evidence"] = "Nonexistent evidence"
    with pytest.raises(ValueError, match="证据不匹配"):
        evaluate(tmp_path, [q])


def test_compact_results_preserve_candidates_and_need_fewer_pages(tmp_path):
    for i in range(10):
        pdf(tmp_path / f"paper_{i}.pdf", ["dengue prediction " * 30])
    sb = Sandbox(tmp_path)
    compact = dispatch(sb, query="dengue", top_k=10)
    detail = dispatch(sb, query="dengue", top_k=10, view="detail")
    assert all(f"paper_{i}.pdf" in compact for i in range(10))
    assert "下一页" not in compact and "下一页" in detail
    assert "<file_content>" not in compact
    assert "page=1 offset=" in compact
    assert len(compact) < 2000 and "已截断" not in compact


def test_all_terms_filters_partial_name_without_claiming_authorship(tmp_path):
    pdf(tmp_path / "author.pdf", ["Authors: Sumiko Anno. Dengue prediction."])
    pdf(tmp_path / "noise.pdf", ["Manual anno-\ntation reports."])
    sb, index = Sandbox(tmp_path), PaperIndex()
    assert len(index.search(sb, "Sumiko Anno")[0]) == 2
    hits, _ = index.search(sb, "Sumiko Anno", match="all")
    assert [h["path"] for h in hits] == ["author.pdf"]
    out = dispatch(sb, query="Sumiko Anno", match="all")
    assert "author.pdf" in out and "noise.pdf" not in out


def test_non_pdf_count_cannot_be_read_as_empty_pdf_count(tmp_path):
    pdf(tmp_path / "paper.pdf", ["reinforcement learning policy reward"])
    for i in range(15):
        (tmp_path / f"photo_{i}.jpg").write_bytes(b"placeholder")
    out = dispatch(Sandbox(tmp_path), query="reinforcement learning")
    assert "扫描PDF=1，已索引PDF=1，解析失败PDF=0，前两页无可检索词项PDF=0" in out
    assert "跳过非PDF文件=15（不是无文字层PDF）" in out
    assert "低分不证明主题不相关" in out


def test_empty_first_two_pages_does_not_claim_whole_pdf_has_no_text(tmp_path):
    pdf(tmp_path / "later.pdf", ["", "", "reinforcement learning"])
    out = dispatch(Sandbox(tmp_path), query="reinforcement")
    assert "扫描PDF=1，已索引PDF=0，解析失败PDF=0，前两页无可检索词项PDF=1" in out
    assert "跳过非PDF文件=0" in out
    # 第三页有可读取文本，只是超出本工具检索范围。
    assert "reinforcement" in dispatch(Sandbox(tmp_path), "read_file", path="later.pdf", page=3)
