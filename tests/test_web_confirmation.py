"""Structured operation reviews preserve the existing execution and confirmation boundary."""

import pytest

from safety.confirm import Confirmer
from safety.oplog import OpLog
from tools.dedup import ApplyDedupArgs, apply_dedup, scan_duplicates
from tools.registry import _trash_batch
from tests.test_web_ui import files, ui  # noqa: F401  reusable temporary sandbox and isolated API fixtures
from web_confirm import build_confirmation, describe_path


def test_batch_review_keeps_duplicates_together_with_per_file_disposition(files):
    (files.root / "copy.md").write_text("测试内容", encoding="utf-8")
    action = build_confirmation({"tool": "trash_many", "args": {"保留": ["notes.md"], "移入回收区": ["copy.md"]}, "file_count": 1,
                                 "groups": [{"id": "G001", "kind": "bytes", "keeper": "notes.md", "members": ["copy.md", "notes.md"]}]}, files)
    assert action["counts"] == {"affected": 1, "retained": 1}
    assert len(action["groups"]) == 1 and action["groups"][0]["key"] == "G001" and "G001" not in action["groups"][0]["title"]
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
    assert next(g for g in action["groups"] if g["key"] == original["id"])["items"][0]["path"] == chosen
    for group in action["groups"]:
        assert [item["disposition"] for item in group["items"]].count("retain") == 1
        assert all((files.root / item["path"]).exists() for item in group["items"])


def test_incomplete_family_metadata_never_hides_files(files):
    request = {"tool": "trash_many", "args": {"保留": ["notes.md"], "移入回收区": ["copy.md", "other.md"]},
               "groups": [{"id": "G001", "keeper": "notes.md", "members": ["notes.md", "copy.md"]}]}
    action = build_confirmation(request, files)
    assert len(action["groups"]) == 1
    assert {i["path"] for i in action["groups"][0]["items"]} == {"notes.md", "copy.md", "other.md"}
    assert "无法按重复组归类" in action["notes"][0]


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


# ---------------------------------------------------------------- the card as a permission prompt
# User request 2026-10-22: one line of what happens, warnings that need a decision,
# everything else folded; no plan IDs, digests or tool names.

def plan_request(**extra):
    steps = [dict(effect='create', source=None, destination='归档'),
             dict(effect='move', source='notes.md', destination='归档/notes.md')]
    return {"tool": "file_plan", "args": dict(plan_id='Xabc123def456', digest='d' * 64, steps=steps, **extra),
            "file_count": 2}


def test_plan_card_has_a_headline_and_no_internal_identifiers(files):
    card = build_confirmation(plan_request(new_folders=['归档']), files)
    assert card["headline"] == "把「notes.md」移到「归档」（可撤销）"
    assert card["warnings"] == ['⚠ 将新建文件夹：归档。不是你想要的位置就拒绝，拒绝后不执行任何操作。']
    shown = " ".join([card["headline"], card["summary"], *card["notes"], *card["details"]])
    for internal in ("Xabc123def456", "d" * 64, "计划摘要", "绑定此操作清单", "file_plan", "写入范围"):
        assert internal not in shown


def test_warnings_lead_with_names_then_secrets_and_include_the_users_limits(files):
    (files.root / "inv\u202etxt.exe").write_text("x")
    request = plan_request(hidden_contents=[dict(folder='文献', metadata=[], hidden=['.env'],
                                                 sensitive={'.env': dict(name_rule='环境变量文件', findings=[])})])
    request["args"]["steps"].append(dict(effect='move', source='inv\u202etxt.exe', destination='归档/inv\u202etxt.exe'))
    request["notes"] = ['待你确认的限制（本地无法自动检查）：只动今年的文件']
    card = build_confirmation(request, files)
    assert card["warnings"][0].startswith("⚠ 以下名称") and card["warnings"][1].startswith("⚠⚠")
    assert card["warnings"][-1].startswith("待你确认的限制") and not any(n.startswith("待你确认") for n in card["details"])


def test_task_edit_card_names_no_task_id(files):
    card = build_confirmation({"tool": "task_edit", "args": dict(task_id='Tabcdef123456', revision=3, reason='用户更正',
        changes=[dict(field='allowed_effects', before=['move'], after=['move', 'trash'])])}, files)
    assert card["headline"] == "修改任务要求（1 处）" and "Tabcdef123456" not in card["summary"]
