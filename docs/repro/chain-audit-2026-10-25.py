"""Read-only product audit: synthetic temporary files and scripted model only.

Run from repository root: .venv/bin/python docs/repro/chain-audit-2026-10-25.py
Assertions document the current defects; they should be replaced by regression
expectations when the corresponding implementation is authorized and fixed.
No API requests, user library, persistent user database, or production edits.
"""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from agent import coverage
from agent.file_plans import library_lock
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tools.retrieval import coverage as search_coverage


def scan(state="indexed", request_pending=0):
    return dict(scope=".", snapshot_id="same", index_revision="r", scan_id="S",
                candidates=["scan.pdf"], candidate_truncated=False, matched_files=1,
                pending_ocr_units=request_pending, ocr_failed_units=0,
                enumeration_complete=True,
                files={"scan.pdf": dict(version=[100, 1], state=state)})


def audit_ocr_transition():
    ledger = coverage.record_scan(None, scan("pending_ocr", 1), "req1")
    ledger = coverage.record_scan(ledger, scan(), "req1")["."]
    result = dict(file=ledger["files"]["scan.pdf"], summary=coverage.summary(ledger))
    assert result["file"]["state"] == "pending_ocr"
    assert result["summary"]["pending_ocr"] == 0
    index = SimpleNamespace(states={"a.txt": ((5, 1), "indexed", 0, 0)})
    ids = [search_coverage(index, ".", {"matched": 1}, [{"path": "a.txt"}], 5, query)["scan_id"]
           for query in ("alpha", "beta")]
    assert ids[0] == ids[1]
    return dict(**result, distinct_query_scan_ids=ids)


def audit_review_scope():
    ledger = coverage.record_scan(None, scan(), "req1")
    ledger["."]["files"]["scan.pdf"].update(status="reviewed", reason="topic A")
    ledger = coverage.record_scan(ledger, scan(), "req2")["."]
    assert ledger["key"]["request"] == "req2"
    assert ledger["files"]["scan.pdf"]["status"] == "reviewed"
    return dict(key=ledger["key"], inherited_file=ledger["files"]["scan.pdf"])


def audit_question_scope():
    from helpers import Session, step, call
    from test_protocol_v3 import start, finish_with, errors
    from test_answer_replays import clash_b, ASK_B, TWO_PARTS
    with tempfile.TemporaryDirectory(prefix="fb-question-audit-") as directory:
        root = Path(directory)
        clash_b(root)
        session = Session(root)
        session.run("处理两个独立问题", [step(
            start(TWO_PARTS, questions=["b 同名怎么处理？", "a 应当移动到哪个文件夹？"]),
            call("ask_user", dict(ASK_B, scope=dict(level="parts", part_ids=["b"]))))])
        before = list(session.sb.runtime.current["unresolved_questions"])
        result, client = session.run("A", [finish_with([dict(part_id="b", reason="用户不移动 b")])])
        task = session.sb.runtime.current
        assert not errors(client)
        assert len(before) == 2 and task["unresolved_questions"] == []
        assert task["answered_questions"][-1]["questions"] == before
        assert (root / "Inbox/a.txt").exists() and (root / "Inbox/b.txt").exists()
        return dict(before=before, after=task["unresolved_questions"],
                    recorded_answered=task["answered_questions"][-1]["questions"],
                    stop_reason=result.stop_reason, files_unchanged=True)


def audit_undo_boundary():
    with tempfile.TemporaryDirectory(prefix="fb-undo-audit-") as directory:
        root = Path(directory)
        sandbox = Sandbox(root)
        (root / "b.txt").write_text("moved original")
        OpLog(sandbox, "audit").record("move", "a.txt", "b.txt")
        rename = Path.rename

        def inject_between_check_and_rename(source, target):
            if source == root / "b.txt":
                (root / "a.txt").write_text("concurrent new file")
            return rename(source, target)

        # The lock is deliberately held: undo currently does not join this domain.
        # Injecting at rename deterministically represents another writer/Finder
        # creating the original path after the exists check. Not a live incident.
        with library_lock(sandbox), patch.object(Path, "rename", inject_between_check_and_rename):
            report = undo(sandbox, "audit")
        content = (root / "a.txt").read_text()
        assert content == "moved original" and not (root / "b.txt").exists()
        return dict(report=report, content_after=content, injected_file_overwritten=True,
                    undo_executed_while_library_locked=True)


if __name__ == "__main__":
    results = {name: fn() for name, fn in (
        ("ocr_state_transition", audit_ocr_transition),
        ("review_scope_inheritance", audit_review_scope),
        ("question_scope_clear", audit_question_scope),
        ("undo_atomic_boundary", audit_undo_boundary))}
    print(json.dumps(results, ensure_ascii=False, indent=2))
