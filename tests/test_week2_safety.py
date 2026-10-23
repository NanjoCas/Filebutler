"""第 2 周：确认机制（y/n/a 字母选项）。undo 由文件计划测试覆盖（tests/test_file_plans.py、test_plan_ordering.py）。"""
import pytest

from safety.confirm import Confirmer


def test_confirm_threshold_and_allow_all():
    asked = []
    c = Confirmer("risky", threshold=2, ask=lambda q: asked.append(q) or "a")
    assert all(c.allow("move", {"src": f"{i}"}) for i in range(5))
    assert len(asked) == 1                                   # 第 3 个开始问，答 a 后不再问
    c2 = Confirmer("always", ask=lambda q: "y")
    assert c2.allow("make_dir", {"path": "x"})
    c3 = Confirmer("never", ask=lambda q: pytest.fail("never 模式不应询问"))
    assert c3.allow("trash", {"path": "x"})



@pytest.mark.parametrize("answer,allowed,allow_all", [
    ("y", True, False), ("Y", True, False), (" a ", True, True), ("A", True, True), ("n", False, False),
])
def test_letter_options(answer, allowed, allow_all):
    c = Confirmer("always", ask=lambda q: answer)
    assert c.allow("trash", {"path": "x"}) is allowed and c.allow_all is allow_all


def test_invalid_input_asks_again_until_valid():
    answers = iter(["全部允许", "ok", "", "yes", "a"])   # 只有单个字母有效，其余都要重输
    asked = []
    c = Confirmer("always", ask=lambda q: asked.append(q) or next(answers))
    assert c.allow("trash", {"path": "x"}) and c.allow_all and c.rejections == 0
    assert len(asked) == 5
    assert asked[0].startswith("⚠ 需要确认：trash") and "path = x" in asked[0]
    assert "y = 允许   n = 拒绝   a = 本次全部允许" in asked[0] and asked[0].endswith("请输入 y / n / a：")
    assert asked[1] == "无效输入「全部允许」，请输入 y / n / a："


def test_eof_rejects():
    def eof(q):
        raise EOFError
    c = Confirmer("always", ask=eof)
    assert not c.allow("trash", {"path": "x"}) and c.rejections == 1
