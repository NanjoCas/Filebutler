"""本地查重的内容区分、批量安全、部分失败日志与低模型调用链。"""
import json
import textwrap

import pymupdf
import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from agent.reflect import file_write_count, successful_writes
from eval.checker import files_preserved, snapshot
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient
from tools import registry
from tools.dedup import normalize_pdf, scan_duplicates

BASE = " ".join(f"term{i}" for i in range(180))


def pdf(path, text=BASE, subject="v1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        p = doc.new_page()
        assert p.insert_textbox(pymupdf.Rect(40, 40, 550, 790), textwrap.fill(text, 85), fontsize=9) >= 0
        doc.set_metadata({"subject": subject})
        doc.save(path)


def apply(sb, scan, confirmer=None, **args):
    return registry.dispatch("apply_dedup", json.dumps(dict(scan_id=scan["id"], **args)), sb,
                             OpLog(sb, "test-dedup"), confirmer)


def test_bytes_size_filter_and_full_undo(tmp_path):
    sb = Sandbox(tmp_path)
    for name in ("a.bin", "b.bin", "c.bin"):
        (tmp_path / name).write_bytes(b"duplicate")
    (tmp_path / "unique.bin").write_bytes(b"unique-longer-content")
    before = snapshot(tmp_path, True)
    scan = scan_duplicates(sb, mode="bytes")
    assert scan["stats"]["byte_hashed"] == 3
    assert len(scan["groups"]) == 1 and scan["groups"][0]["kind"] == "bytes"
    asked = []
    c = Confirmer("risky", ask=lambda q: asked.append(q) or "y")
    assert apply(sb, scan, c).startswith("OK: 已将 2")
    assert len(asked) == 1 and c.file_writes == 2
    assert all(name in asked[0] for name in ("a.bin", "b.bin", "c.bin"))
    assert files_preserved(before, snapshot(tmp_path, True))
    assert len((tmp_path / ".oplog/test-dedup.jsonl").read_text().splitlines()) == 2
    undo(sb, "test-dedup")
    assert snapshot(tmp_path, True) == before


def test_pdf_same_text_different_bytes_and_metadata(tmp_path):
    pdf(tmp_path / "a.pdf", subject="preprint")
    pdf(tmp_path / "b.pdf", subject="journal")
    sb = Sandbox(tmp_path)
    assert not scan_duplicates(sb, mode="bytes")["groups"]
    scan = scan_duplicates(sb, mode="text")
    assert len(scan["groups"]) == 1 and scan["groups"][0]["kind"] == "text"
    before = snapshot(tmp_path, True)
    assert apply(sb, scan).startswith("OK: 已将 1")
    undo(sb, "test-dedup")
    assert snapshot(tmp_path, True) == before


def test_normalization_keeps_results_and_operators():
    assert normalize_pdf(["1\nA ﬁle with anno-\ntation\n1"]) == normalize_pdf(["A file with annotation"])
    assert normalize_pdf(["score = 0.91; x+y"]) != normalize_pdf(["score = 0.92; x-y"])
    assert normalize_pdf(["A = 1"]) != normalize_pdf(["a = 1"])


def test_near_version_is_review_only_and_prefers_longer_text(tmp_path):
    pdf(tmp_path / "a.pdf")
    pdf(tmp_path / "long.pdf", BASE + " Additional experiment results are discussed in this revised version.")
    sb = Sandbox(tmp_path)
    scan = scan_duplicates(sb)
    g, = scan["groups"]
    assert g["kind"] == "similar" and g["review_required"] and g["keep"] == "long.pdf"
    before = snapshot(tmp_path, True)
    assert "没有需要" in apply(sb, scan)
    assert snapshot(tmp_path, True) == before
    assert "需复核" in apply(sb, scan, group_ids=[g["id"]])
    c = Confirmer("risky", ask=lambda q: "y")
    assert apply(sb, scan, c, include_similar=True).startswith("OK: 已将 1")
    assert (tmp_path / "long.pdf").exists()
    undo(sb, "test-dedup")
    assert snapshot(tmp_path, True) == before


def test_shared_topic_is_not_enough_to_merge(tmp_path):
    pdf(tmp_path / "a.pdf", "Fake news detection " + BASE)
    pdf(tmp_path / "b.pdf", "Fake news detection " + " ".join(f"different{i}" for i in range(180)))
    assert not scan_duplicates(Sandbox(tmp_path))["groups"]


def test_longer_version_with_appendix_is_a_review_candidate(tmp_path):
    pdf(tmp_path / "short.pdf")
    pdf(tmp_path / "appendix.pdf", BASE + " " + " ".join(f"appendix{i}" for i in range(30)))
    g, = scan_duplicates(Sandbox(tmp_path))["groups"]
    assert g["kind"] == "similar" and g["review_required"]
    assert g["containment"] >= 0.93 and g["length_ratio"] >= 0.8


def test_safe_subgroup_can_be_cleaned_without_merging_near_version(tmp_path):
    pdf(tmp_path / "a.pdf")
    (tmp_path / "copy.pdf").write_bytes((tmp_path / "a.pdf").read_bytes())
    pdf(tmp_path / "appendix.pdf", BASE + " Additional results for a revised appendix are presented here.")
    sb = Sandbox(tmp_path)
    scan = scan_duplicates(sb)
    g, = scan["groups"]
    assert g["kind"] == "similar" and len(g["confirmed_subgroups"]) == 1
    assert apply(sb, scan).startswith("OK: 已将 1")
    assert (tmp_path / "a.pdf").exists() and (tmp_path / "appendix.pdf").exists()


def test_complete_link_does_not_merge_transitive_similarities(tmp_path, monkeypatch):
    # A≈B、B≈C，但 A 与 C 未通过阈值：最多有一组两篇，不能三篇一起归并。
    for n in ("a", "b", "c"):
        pdf(tmp_path / f"{n}.pdf", BASE + n)
    features = [{i.to_bytes(2, "big") for i in range(lo, lo + 100)} for lo in (0, 8, 16)]
    it = iter(features)
    monkeypatch.setattr("tools.dedup.shingles", lambda text: next(it))
    scan = scan_duplicates(Sandbox(tmp_path))
    assert all(len(g["members"]) <= 2 for g in scan["groups"])


@pytest.mark.parametrize("change", ["keeper", "victim", "remove", "symlink"])
def test_stale_or_replaced_files_block_before_confirmation(tmp_path, change):
    sb = Sandbox(tmp_path)
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    scan = scan_duplicates(sb, mode="bytes")
    if change == "keeper":
        (tmp_path / "a.txt").write_text("changed")
    elif change == "victim":
        (tmp_path / "b.txt").write_text("changed")
    elif change == "remove":
        (tmp_path / "a.txt").unlink()
    else:
        (tmp_path / "a.txt").unlink()
        (tmp_path / "a.txt").symlink_to(tmp_path / "b.txt")
    c = Confirmer("always", ask=lambda q: pytest.fail("过期计划不应询问"))
    assert apply(sb, scan, c).startswith("ERROR")
    assert not (tmp_path / ".trash").exists()


def test_change_during_confirmation_is_rechecked(tmp_path):
    sb = Sandbox(tmp_path)
    for n in ("a", "b"):
        (tmp_path / n).write_text("same")
    scan = scan_duplicates(sb, mode="bytes")
    def ask(q):
        (tmp_path / "a").write_text("new contents")
        return "y"
    assert apply(sb, scan, Confirmer("always", ask=ask)).startswith("ERROR")
    assert (tmp_path / "b").exists() and not (tmp_path / ".trash").exists()


def test_rejection_changes_nothing_and_counts_files(tmp_path):
    sb = Sandbox(tmp_path)
    for n in ("a", "b", "c"):
        (tmp_path / n).write_text("same")
    scan = scan_duplicates(sb, mode="bytes")
    before = snapshot(tmp_path, True)
    c = Confirmer("risky", ask=lambda q: "n")
    assert "用户拒绝" in apply(sb, scan, c)
    assert c.rejections == 1 and c.file_writes == 0
    assert snapshot(tmp_path, True) == before


def test_partial_journal_failure_rolls_back_current_file_and_undo_restores_previous(tmp_path, monkeypatch):
    sb = Sandbox(tmp_path)
    for n in ("a", "b", "c"):
        (tmp_path / n).write_text("same")
    before = snapshot(tmp_path, True)
    scan = scan_duplicates(sb, mode="bytes")
    original = OpLog.record
    def failing(self, *args):
        if self.seq == 1:
            raise OSError("journal failure")
        return original(self, *args)
    monkeypatch.setattr(OpLog, "record", failing)
    result = apply(sb, scan)
    assert result.startswith("ERROR") and "已回收 1" in result
    assert (tmp_path / "c").exists()
    undo(sb, "test-dedup")
    assert snapshot(tmp_path, True) == before


def test_failed_journal_rollback_never_overwrites_a_new_file(tmp_path, monkeypatch):
    sb = Sandbox(tmp_path)
    for n in ("a", "b"):
        (tmp_path / n).write_text("same")
    scan = scan_duplicates(sb, mode="bytes")
    def failing(self, op, src, dst):
        (sb.root / src).write_text("new user file")
        raise OSError("journal failure")
    monkeypatch.setattr(OpLog, "record", failing)
    result = apply(sb, scan)
    assert result.startswith("ERROR") and "未覆盖" in result and "需手动恢复" in result
    assert (tmp_path / "b").read_text() == "new user file"
    assert next((tmp_path / ".trash").iterdir()).read_text() == "same"


def test_scan_skips_hidden_symlinks_and_broken_pdf(tmp_path):
    sb = Sandbox(tmp_path / "sandbox")
    pdf(sb.root / "bad.pdf", text="short")
    (sb.root / "broken.pdf").write_text("not pdf")
    (sb.root / ".trash").mkdir()
    (sb.root / ".trash/secret.txt").write_text("same")
    (sb.root / "link.pdf").symlink_to(tmp_path / "outside.pdf")
    scan = scan_duplicates(sb)
    assert scan["stats"]["files"] == 2 and scan["stats"]["symlinks"] == 1
    assert scan["stats"]["parse_failed"] == 1 and scan["stats"]["short_or_empty"] == 1
    assert not scan["groups"]
    result = registry.dispatch("find_duplicates", '{"path":"../"}', sb)
    assert result.startswith("ERROR: 拒绝")


def test_keep_override_and_invalid_selection(tmp_path):
    sb = Sandbox(tmp_path)
    for n in ("a", "b"):
        (tmp_path / n).write_text("same")
    scan = scan_duplicates(sb, mode="bytes")
    assert apply(sb, scan, group_ids=["missing"]).startswith("ERROR")
    assert apply(sb, scan, keepers={"G001": "outside"}).startswith("ERROR")
    assert apply(sb, scan, keepers={"G001": "b"}).startswith("OK")
    assert (tmp_path / "b").exists() and not (tmp_path / "a").exists()


def test_cached_plan_pagination_does_not_scan_again(tmp_path, monkeypatch):
    sb = Sandbox(tmp_path)
    for group in range(14):
        for copy in range(2):
            (tmp_path / (f"{group}_{copy}_" + "name" * 20)).write_text(str(group))
    scan = scan_duplicates(sb, mode="bytes")
    monkeypatch.setattr("tools.dedup.scan_duplicates", lambda *a, **kw: pytest.fail("不得重扫"))
    out = registry.dispatch("find_duplicates", json.dumps(dict(scan_id=scan["id"], page=2)), sb)
    assert out.startswith("OK") and "已截断" not in out and len(out) <= 2000


def test_dynamic_fake_model_only_needs_two_tool_calls(tmp_path):
    sb = Sandbox(tmp_path)
    for n in ("a", "b", "c", "d", "e", "f"):
        (tmp_path / n).write_text("same")
    before = snapshot(tmp_path, True)
    class Client(FakeClient):
        def create(self, **kw):
            if len(self.seen) == 1:
                scan_id = next(iter(sb._dedup_scans))
                self.script[0] = [("apply_dedup", {"scan_id": scan_id})]
            return super().create(**kw)
    client = Client([[("find_duplicates", {"mode": "bytes"})], [], "已完成去重"])
    result = run_agent("清理完全相同的文件", LLM({"model": "fake"}, client=client), sb,
                       {"prompt": "v3_local"}, Console(quiet=True), oplog=OpLog(sb, "test-dedup"))
    assert result.tool_calls == 2 and result.steps == 3
    assert file_write_count(successful_writes(result.messages)) == 5
    undo(sb, "test-dedup")
    assert snapshot(tmp_path, True) == before
