"""第 2 周：测试数据生成器——可复现、内容和 manifest 一致、工具读得出来。"""
import json

import pytest

from eval.checker import snapshot
from eval.generator import generate
from safety.sandbox import Sandbox
from tools import registry
import tools.fs_read  # noqa: F401


@pytest.mark.parametrize("scenario", ["downloads", "invoices"])
def test_same_seed_same_bytes(tmp_path, scenario):
    m1 = generate(scenario, tmp_path / "a", seed=7)
    m2 = generate(scenario, tmp_path / "b", seed=7)
    assert m1 == m2 and snapshot(tmp_path / "a") == snapshot(tmp_path / "b")
    generate(scenario, tmp_path / "c", seed=8)
    assert snapshot(tmp_path / "c") != snapshot(tmp_path / "a")


def test_manifest_matches_disk(tmp_path):
    for scenario in ("downloads", "invoices"):
        root = tmp_path / scenario
        m = generate(scenario, root, seed=42)
        assert set(m["files"]) == set(snapshot(root))      # manifest 覆盖所有可见文件
        assert (root / ".DS_Store").exists()


def test_invoices_readable_by_tools(tmp_path):
    m = generate("invoices", tmp_path / "s", seed=42)
    sb = Sandbox(tmp_path / "s")
    invoices = {k: v for k, v in m["files"].items() if v["category"] == "发票"}
    assert len(invoices) == 10 and len(m["files"]) == 12
    assert len({v["merchant"] for v in invoices.values()}) == 10   # 商家不重复，重命名任务才无歧义
    for name, info in invoices.items():
        text = registry.dispatch("read_file", json.dumps({"path": name}), sb)
        assert info["merchant"] in text and info["amount"] in text, text


def test_downloads_exif_and_categories(tmp_path):
    m = generate("downloads", tmp_path / "s", seed=42)
    sb = Sandbox(tmp_path / "s")
    cats = [v["category"] for v in m["files"].values()]
    assert len(cats) == 25 and {"文档", "图片", "压缩包", "其他"} == set(cats)
    for name, info in m["files"].items():
        if "taken" in info:
            out = registry.dispatch("file_info", json.dumps({"path": name}), sb)
            assert info["taken"].replace("-", ":") in out
