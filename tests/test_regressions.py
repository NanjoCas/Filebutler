"""真实失败案例的确定性复现：把当时模型的输出按原样回放，环境、工具、checker 都是真的。
模型的随机错误重跑很难再现（C05 漏文件：端到端 0/10、决策点回放 0/40），录下来就能稳定复现，
以后改提示词或加核对机制时，可以在这里验证改动能不能拦住它。"""
import json
from pathlib import Path

from eval.runner import load_tasks, run_one
from agent.llm import LLM
from tests.test_week1 import FakeClient

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    script = [t if isinstance(t, str) else [tuple(c) for c in t] for t in data["assistant_turns"]]
    return data, lambda cfg, task, manifest: LLM({"model": "recorded"}, client=FakeClient(script))


def test_c05_missed_file_reproduces(tmp_path):
    data, make_llm = recorded("C05_3_missed_file.json")
    task = load_tasks("synthetic", ids=[data["task"]])[0]
    r = run_one(task, 1, {"max_steps": 15}, tmp_path, make_llm=make_llm)
    failed = [c["assert"] for c in r["checks"] if not c["ok"]]
    assert failed == ["moved scan_2616.pdf → 上半年/scan_2616.pdf"]   # 只漏了这一个
    assert r["stop_reason"] == "done" and r["restored"]               # 模型自认为完成了；undo 照常还原
    assert "scan_2616" not in r["final"]                              # 总结里根本没提到它
