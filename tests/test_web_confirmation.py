"""Structured operation reviews preserve the existing execution and confirmation boundary."""
import threading

import pytest

from agent.llm import LLM
from safety.confirm import Confirmer
from safety.oplog import OpLog
from tools.dedup import ApplyDedupArgs, apply_dedup, scan_duplicates
from tools.registry import _trash_batch
from tests.test_week1 import FakeClient
from tests.test_web_ui import files, ui  # reusable temporary sandbox and isolated API fixtures
from web_confirm import build_confirmation, describe_path
import webapp


def test_batch_review_keeps_duplicates_together_with_per_file_disposition(files):
    (files.root / "copy.md").write_text("测试内容", encoding="utf-8")
    action = build_confirmation({"tool": "trash_many", "args": {"保留": ["notes.md"], "移入回收区": ["copy.md"]}, "file_count": 1,
                                 "groups": [{"id": "G001", "kind": "bytes", "keeper": "notes.md", "members": ["copy.md", "notes.md"]}]}, files)
    assert action["counts"] == {"affected": 1, "retained": 1}
    assert len(action["groups"]) == 1 and "G001" in action["groups"][0]["title"]
    items = action["groups"][0]["items"]
    assert [(i["path"], i["disposition"]) for i in items] == [("notes.md", "retain"), ("copy.md", "trash")]
    assert all(i["previewable"] for i in items)
    assert (files.root / "copy.md").exists()  # building/reviewing a plan never executes it


def test_family_pairing_survives_plan_registry_and_confirmation(files):
    for name in ("a.md", "a-copy.md"):
        (files.root / name).write_text("alpha", encoding="utf-8")
    for name in ("b.md", "b-copy.md", "b-other.md"):
        (files.root / name).write_text("beta", encoding="utf-8")
    scan = scan_duplicates(files, mode="bytes")
    original = scan["groups"][0]
    chosen = next(p for p in original["members"] if p != original["keep"])
    plan = apply_dedup(ApplyDedupArgs(scan_id=scan["id"], keepers={original["id"]: chosen}), files)
    requests = []
    confirmer = Confirmer("risky", ask_action=lambda request: requests.append(request) or "n")
    assert _trash_batch(plan, files, OpLog(files, "family-test"), confirmer).startswith("ERROR: 用户拒绝")
    action = build_confirmation(requests[0], files)
    assert len(action["groups"]) == 2
    assert {frozenset(item["path"] for item in g["items"]) for g in action["groups"]} == {frozenset(g["members"]) for g in scan["groups"]}
    assert next(g for g in action["groups"] if original["id"] in g["title"])["items"][0]["path"] == chosen
    for group in action["groups"]:
        assert [item["disposition"] for item in group["items"]].count("retain") == 1
        assert all((files.root / item["path"]).exists() for item in group["items"])


def test_incomplete_family_metadata_never_hides_files(files):
    request = {"tool": "trash_many", "args": {"保留": ["notes.md"], "移入回收区": ["copy.md", "other.md"]},
               "groups": [{"id": "G001", "keeper": "notes.md", "members": ["notes.md", "copy.md"]}]}
    action = build_confirmation(request, files)
    assert len(action["groups"]) == 1
    assert {i["path"] for i in action["groups"][0]["items"]} == {"notes.md", "copy.md", "other.md"}
    assert "未提供完整重复组关系" in action["notes"][0]


def test_move_review_uses_real_source_name_and_computed_destination(files):
    (files.root / " actual.md ").touch()
    action = build_confirmation({"tool": "move", "args": {"src": "actual.md", "dst_dir": "文献"}}, files)
    source = action["groups"][0]["items"][0]
    assert source["path"] == " actual.md "
    assert source["destination"] == "文献/ actual.md "
    assert action["groups"][1]["items"][0]["kind"] == "directory"


def test_rename_shows_target_and_extension_change(files):
    action = build_confirmation({"tool": "rename", "args": {"path": "notes.md", "new_name": " 新笔记.txt "}}, files)
    assert action["groups"][0]["items"][0]["destination"] == "新笔记.txt"
    assert ".md" in action["notes"][0] and ".txt" in action["notes"][0]


def test_new_folder_is_planned_and_not_previewable(files):
    action = build_confirmation({"tool": "make_dir", "args": {"path": "new/folder"}}, files)
    item = action["groups"][0]["items"][0]
    assert item["planned"] and item["kind"] == "directory" and not item["previewable"]
    assert not (files.root / "new").exists()


@pytest.mark.parametrize("path", ["../outside.md", ".oplog/log.jsonl", "/etc/passwd"])
def test_unsafe_paths_have_no_preview_action(files, path):
    assert not describe_path(files, path)["previewable"]


def test_symlink_has_no_preview_action(files, tmp_path):
    outside = tmp_path / "private.md"; outside.touch()
    (files.root / "link.md").symlink_to(outside)
    assert not describe_path(files, "link.md")["previewable"]


def test_directory_action_mentions_descendants(files):
    action = build_confirmation({"tool": "trash", "args": {"path": "文献"}}, files)
    assert action["groups"][0]["items"][0]["previewable"]
    assert "全部文件" in action["notes"][0]


def test_structured_confirmation_receives_lists_and_keeps_rejection_count():
    seen = []
    c = Confirmer("risky", ask=lambda _: pytest.fail("web confirmation must not parse terminal output"),
                  ask_action=lambda data: seen.append(data) or "n")
    args = {"保留": ["keep.md"], "移入回收区": ["a.md", "b.md"]}
    assert not c.allow("trash_many", args, file_count=2)
    assert seen == [{"tool": "trash_many", "args": args, "file_count": 2}]
    assert c.rejections == 1 and c.file_writes == 0


def test_review_allows_preview_before_approval_and_can_undo_after(ui, monkeypatch, files):
    waiting, finished = threading.Event(), threading.Event()
    fake = FakeClient([[("trash", {"path": "notes.md"})], "已移入回收区。"])
    monkeypatch.setattr(webapp, "LLM", lambda cfg: LLM({"model": "fake"}, client=fake))
    original_event = webapp._event
    def notify(turn_id, event):
        original_event(turn_id, event)
        if event["type"] == "confirmation_required": waiting.set()
        if event["type"] in ("completed", "failed"): finished.set()
    monkeypatch.setattr(webapp, "_event", notify)
    cid = ui.post("/api/conversations").json()["id"]
    tid = ui.post(f"/api/conversations/{cid}/messages", json={"content": "清理笔记"}).json()["turn_id"]
    assert waiting.wait(4)
    pending = ui.get(f"/api/turns/{tid}").json()["pending_confirmation"]
    assert "请输入" not in pending["question"]
    item = pending["action"]["groups"][0]["items"][0]
    assert item["path"] == "notes.md" and item["previewable"]
    assert ui.get("/api/files/preview", params={"path": item["path"]}).json()["content"] == "测试内容"
    assert (files.root / "notes.md").exists()
    assert ui.post(f"/api/turns/{tid}/confirm", json={"answer": "y", "confirmation_id": pending["id"]}).status_code == 200
    assert finished.wait(5)
    assert not (files.root / "notes.md").exists()
    ui.post(f"/api/turns/{tid}/undo")
    assert (files.root / "notes.md").read_text(encoding="utf-8") == "测试内容"
