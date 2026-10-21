"""Verify routing boundaries and offline execution, not just regex matches."""
import json

import pytest
from rich.console import Console

from agent.llm import LLM
from agent.loop import run_agent
from agent.local_tasks import local_task
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tests.test_week1 import FakeClient

CFG = {'model': 'fake', 'api_key_env': 'FILEBUTLER_TEST_ABSENT_KEY',
       'base_url': 'https://example.invalid', 'local_runtime': True}


def offline(instruction, root, **kwargs):
    return run_agent(instruction, LLM(CFG), Sandbox(root), CFG, Console(quiet=True), **kwargs)


def test_inventory_works_without_client_or_credentials_and_emits_truthful_trace(tmp_path, monkeypatch):
    monkeypatch.delenv(CFG['api_key_env'], raising=False)
    monkeypatch.setattr('agent.llm.OpenAI', lambda **kw: pytest.fail('Local task constructed a remote client'))
    (tmp_path/'x').mkdir()
    (tmp_path/'x'/'a.PDF').write_bytes(b'not a real PDF; count must not parse it')
    (tmp_path/'b.txt').write_text('text')
    (tmp_path/'.secret.pdf').write_text('hidden')
    (tmp_path/'alias.pdf').symlink_to(tmp_path/'x'/'a.PDF')
    (tmp_path/'folder_alias').symlink_to(tmp_path/'x', target_is_directory=True)
    trace = tmp_path.parent / (tmp_path.name + '.local.jsonl')
    events = []
    result = offline('一共有多少篇论文？', tmp_path, trace_path=trace, on_event=events.append)
    assert '共 1 个PDF 文件' in result.final and '不代表独立论文篇数' in result.final
    assert result.steps == result.tool_calls == 1
    assert result.tokens == result.cost == result.llm_seconds == 0
    assert result.runtime['route'] == 'inventory'
    assert not any(e['type'].startswith('llm_') for e in events)
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert not any(e.get('event') in {'llm', 'llm_request'} for e in records)
    assert records[-1]['runtime']['execution'] == 'local'


def test_inventory_scope_and_escaping_are_preserved(tmp_path):
    (tmp_path/'资料 库').mkdir()
    (tmp_path/'资料 库'/'a.pdf').write_text('a')
    (tmp_path/'b.pdf').write_text('b')
    result = offline('请在「资料 库」中统计PDF数量', tmp_path)
    assert '资料 库' in result.final and '共 1 个PDF 文件' in result.final
    denied = offline('在「../」中统计文件数量', tmp_path)
    assert denied.stop_reason == 'operation_error' and denied.violations == 1
    assert denied.tokens == 0


@pytest.mark.parametrize('instruction', [
    '不要清理字节完全相同的文件，每组只留一份',
    '先查找字节完全相同的文件，然后移动到备份',
    '清理字节完全相同的文件，每组只留一份，但保留最新版本',
    '清理重复的照片，每组只留一份',
    '统计PDF数量并找出半监督论文',
    '统计最近三个月的PDF数量',
    '用户说“清理字节完全相同的文件，每组只留一份”是什么意思',
    '查找主题相同的PDF',
    '在「文献」中查找正文相同的PDF但不要忽略图片',
])
def test_constraints_negation_and_semantic_tasks_do_not_bypass_model(instruction):
    assert local_task(instruction) is None


def test_readonly_dedup_is_local_and_never_changes_files(tmp_path):
    for name in ('a.txt', 'b.txt'):
        (tmp_path/name).write_text('identical')
    result = offline('查找字节完全相同的文件', tmp_path)
    assert result.tokens == 0 and result.tool_calls == 1 and result.stop_reason == 'done'
    assert '发现 1 组' in result.final and '(file:F' in result.final
    assert (tmp_path/'a.txt').exists() and (tmp_path/'b.txt').exists()
    assert not (tmp_path/'.trash').exists()


@pytest.mark.parametrize('answer', ['y', 'n'])
def test_local_cleanup_keeps_confirmation_and_undo(tmp_path, answer):
    for name in ('a.txt', 'b.txt'):
        (tmp_path/name).write_text('identical')
    sb = Sandbox(tmp_path)
    asked = []
    result = offline('清理字节完全相同的文件，每组只留一份', tmp_path,
                     confirmer=Confirmer('always', ask=lambda prompt: asked.append(prompt) or answer),
                     oplog=OpLog(sb, 'local-cleanup'))
    assert len(asked) == 1 and result.tokens == 0 and result.steps == 1
    assert result.stop_reason == 'done'
    assert (tmp_path/'a.txt').exists()
    if answer == 'y':
        assert not (tmp_path/'b.txt').exists() and '判定依据' in result.final
        undo(sb, 'local-cleanup')
        assert (tmp_path/'b.txt').read_text() == 'identical'
    else:
        assert (tmp_path/'b.txt').exists() and result.rejections == 1


def test_local_failure_finishes_without_remote_repair_or_false_success(tmp_path):
    result = offline('在「不存在」中查找正文相同的PDF', tmp_path)
    assert result.stop_reason == 'operation_error' and result.tokens == 0
    assert '本地任务未完成' in result.final


def test_opt_out_preserves_model_routing_and_lazy_client_fails_only_when_needed(tmp_path, monkeypatch):
    monkeypatch.delenv(CFG['api_key_env'], raising=False)
    cfg = {**CFG, 'local_dispatch': False}
    fake = FakeClient([[('inventory_files', {})], '模型已汇总'])
    result = run_agent('统计文件数量', LLM(cfg, client=fake), Sandbox(tmp_path), cfg, Console(quiet=True))
    assert len(fake.seen) == 2 and result.final == '模型已汇总'
    with pytest.raises(RuntimeError, match='未设置'):
        offline('解释一下什么是半监督学习', tmp_path)
