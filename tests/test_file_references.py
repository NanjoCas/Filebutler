"""Regression cases for local chat references: canonical paths, not guessed files."""
import unicodedata
from urllib.parse import quote

import pytest
from safety.sandbox import Sandbox
from web_render import FileCatalog, render_markdown
from tests.test_web_ui import files, ui


def test_unique_directory_suffix_from_real_chat(tmp_path):
    sb = Sandbox(tmp_path)
    path = sb.root / "文献/强化学习/Weak Supervision Reinforcement Learning.pdf"
    path.parent.mkdir(parents=True); path.touch()
    catalog = FileCatalog(sb)
    assert catalog.resolve("强化学习/Weak Supervision Reinforcement Learning.pdf") == (sb.rel(path), None)
    result = render_markdown("`强化学习/Weak Supervision Reinforcement Learning.pdf`", catalog)
    assert 'data-file-path="文献/强化学习/Weak Supervision Reinforcement Learning.pdf"' in result


def test_ambiguous_suffix_is_not_guessed(tmp_path):
    sb = Sandbox(tmp_path)
    for folder in ("a", "b"):
        path = sb.root / folder / "强化学习/paper.pdf"; path.parent.mkdir(parents=True); path.touch()
    catalog = FileCatalog(sb)
    assert catalog.resolve("强化学习/paper.pdf") is None
    assert catalog.resolve("a/强化学习/paper.pdf") == ("a/强化学习/paper.pdf", None)
    assert "多个" in catalog.explain("强化学习/paper.pdf")
    assert catalog.resolve("invented/paper.pdf") is None


def test_whitespace_case_and_unicode_ligatures_resolve_to_disk_names(tmp_path):
    sb = Sandbox(tmp_path)
    path = sb.root / "文献/Transformer/ Exploiting Diﬀerent Information.PDF "
    path.parent.mkdir(parents=True); path.touch()
    catalog = FileCatalog(sb)
    assert catalog.resolve("文献/transformer/Exploiting Different Information.pdf") == (sb.rel(path), None)
    assert catalog.resolve("transformer/Exploiting Different Information.pdf") == (sb.rel(path), None)


def test_unicode_composition_and_normalisation_collisions(tmp_path):
    sb = Sandbox(tmp_path)
    decomposed = unicodedata.normalize("NFD", "café.pdf")
    (sb.root / decomposed).touch()
    assert FileCatalog(sb).resolve("café.pdf") == (decomposed, None)
    (sb.root / "x").mkdir(); (sb.root / "y").mkdir()
    (sb.root / "x/ paper.pdf").touch(); (sb.root / "y/paper.pdf ").touch()
    assert FileCatalog(sb).resolve("paper.pdf") is None


def test_directories_are_marked_for_browsing_instead_of_file_preview(files):
    catalog = FileCatalog(files)
    assert catalog.resolve("文献/") == ("文献", None)
    assert catalog.resolve("sandbox:/") == (".", None)
    result = render_markdown("`文献/` 和 [目录](sandbox:/文献/)", catalog)
    assert result.count('data-file-kind="directory"') == 2


def test_unescaped_markdown_destinations_with_spaces_parentheses_and_hash(files):
    path = "文献/paper #1 (初稿).pdf"
    result = render_markdown(f"[论文](sandbox:/{path}) 和 [第二页]({path}#page=2)", FileCatalog(files))
    assert result.count('data-file-path="文献/paper #1 (初稿).pdf"') == 2
    assert 'data-file-page="2"' in result
    assert "[论文](" not in result


def test_literal_percent_and_fragment_characters_in_filenames(files):
    for name in ("100% report.pdf", "what?#page=4.pdf", "notes.md#page=3"):
        (files.root / name).touch()
    catalog = FileCatalog(files)
    assert catalog.resolve("sandbox:/100% report.pdf") == ("100% report.pdf", None)
    assert catalog.resolve("sandbox:/what?#page=4.pdf") == ("what?#page=4.pdf", None)
    assert catalog.resolve("sandbox:/notes.md#page=3") == ("notes.md#page=3", None)
    assert catalog.resolve("sandbox:/" + quote("100% report.pdf") + "#page=2") == ("100% report.pdf", 2)


def test_plain_text_does_not_link_substrings_of_another_filename(files):
    result = render_markdown("invented-notes.md notes.md.bak absent/notes.md `absent/notes.md`", FileCatalog(files))
    assert 'class="file-reference"' not in result


def test_code_examples_are_not_rewritten(files):
    example = "[论文](sandbox:/文献/paper #1 (初稿).pdf)"
    result = render_markdown(f"`{example}`\n\n```md\n{example}\n```", FileCatalog(files))
    assert 'class="file-reference"' not in result and result.count(example) == 2


@pytest.mark.parametrize("reference", ["sandbox:/../notes.md", "sandbox:/%2e%2e/notes.md", "sandbox://host/notes.md", "/etc/passwd", "file:///etc/passwd", "sandbox:/.trash/notes.md", "sandbox:/文献/../notes.md"])
def test_normalisation_does_not_bypass_path_boundaries(files, reference):
    assert FileCatalog(files).resolve(reference) is None


def test_expanded_and_normalised_links_open_through_real_preview_api(ui, files):
    path = files.root / "文献/ More Diﬀerent Notes.MD"
    path.write_text("正确的预览内容", encoding="utf-8")
    resolved = FileCatalog(files).resolve("more different notes.md")
    assert resolved == (files.rel(path), None)
    response = ui.get("/api/files/preview", params={"path": resolved[0]})
    assert response.status_code == 200 and response.json()["content"] == "正确的预览内容"
    directory = FileCatalog(files).resolve("文献/")
    assert ui.get("/api/files", params={"path": directory[0]}).status_code == 200


def test_ellipsis_is_explained_without_guessing_a_file(files):
    catalog = FileCatalog(files)
    assert catalog.resolve("文献/…paper…pdf") is None
    assert "省略" in catalog.explain("文献/…paper…pdf")
    assert 'title="路径含有省略内容' in render_markdown("`文献/…paper…pdf`", catalog)
