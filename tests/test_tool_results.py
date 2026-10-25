"""Typed tool results, error classes and recovery (routing redesign S6b, 8.2).
Temporary files and scripted models only."""
import json

import pytest

from helpers import Session, step, call, files_fixture, Sandbox
from test_protocol_v3 import start, execute, finish, holdout_fixture, read_calls, finding, PLAN, ARCHIVE_A
from tools import registry


def tool_messages(result):
    return [json.loads(m['content']) for m in result.messages if m['role'] == 'tool']


def test_the_model_still_sees_only_status_text_code_and_retryable(tmp_path):
    files_fixture(tmp_path)
    result, _ = Session(tmp_path).run('看看', [step(call('list_dir', dict(path='.')), call('read_file', dict(path='nope.txt')))],
                                      final='好。')
    assert all(set(m) == {'status', 'text', 'error_code', 'retryable'} for m in tool_messages(result))


def test_every_call_gets_one_result_id_and_errors_a_class_and_recovery(tmp_path):
    files_fixture(tmp_path)
    session = Session(tmp_path)
    parts = [dict(id='a', kind='organize', description='归档 a'), dict(id='b', kind='organize', description='归档 b')]
    session.run('归档', [step(start(parts), call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A)),
                              call('read_file', dict(path='Inbox/missing.txt')))], final='好。')
    runtime = session.sb.runtime
    rows = list(runtime.results.values())
    assert [r['tool'] for r in rows] == ['start_task', 'prepare_file_plan', 'read_file', 'finish_turn']
    rejected = next(r for r in rows if r['tool'] == 'prepare_file_plan')
    assert (rejected['error_code'], rejected['error_class'], rejected['recovery']) == ('AMBIGUOUS_GOAL', 'AMBIGUOUS_GOAL', 'replan')
    # A tool call's observation carries its result ID and class (control calls keep no observation).
    obs = [o for o in runtime.current['observations'] if o['tool'] == 'read_file'][0]
    assert obs['result_id'] in runtime.results and obs['error_class'] == rows[2]['error_class'] is not None


@pytest.mark.parametrize('code,text,expected', [
    ('USER_DECLINED', 'ERROR: USER_DECLINED：用户拒绝执行此计划', 'USER_DECLINED'),
    ('CONFIRMATION_TIMEOUT', 'ERROR: CONFIRMATION_TIMEOUT：等待用户确认超时', 'CONFIRMATION_TIMEOUT'),
    ('CONFIRMATION_INTERRUPTED', 'ERROR: CONFIRMATION_INTERRUPTED：确认被中断（eof）', 'CONFIRMATION_TIMEOUT'),
    ('TOOL_ERROR', 'ERROR: 资料库正有另一项整理在执行，请稍后重试', 'LIBRARY_BUSY'),
    ('CONTROL_REJECTED', 'ERROR: 工具路径超出任务读取范围', 'OUT_OF_SCOPE'),
    ('CONTROL_REJECTED', 'ERROR: 已达到任务检索预算，请报告证据缺口', 'BUDGET_EXCEEDED'),
    ('CONTROL_REJECTED', 'ERROR: 任务版本已改变，必须重新登记任务后继续', 'TASK_REVISION_CONFLICT'),
    ('TOOL_ERROR', 'ERROR: 检索期间文件变化，请重新查询后再引用', 'PLAN_STALE'),
    ('TOOL_ERROR', 'ERROR: KeyError: x', 'INTERNAL_ERROR'),
    ('INVALID_ARGUMENTS', 'ERROR: 参数不是合法 JSON', 'INVALID_ARGUMENTS'),
    ('INPUT_RELATION_REQUIRED', 'ERROR: INPUT_RELATION_REQUIRED：…', 'CONSENT_REQUIRED'),
])
def test_error_classes_follow_the_code_then_the_local_wording(code, text, expected):
    assert registry.classify(code, text)[0] == expected and expected in registry.ERROR_CLASSES


def test_a_timeout_or_decline_never_tells_the_model_to_retry():
    for code in ('USER_DECLINED', 'CONFIRMATION_TIMEOUT', 'CONFIRMATION_INTERRUPTED'):
        out = registry.typed(registry.ToolResult(status='error', text=f'ERROR: {code}：x', error_code=code))
        assert out.retryable is False and out.recovery['action'] == 'none'


def test_findings_return_their_table_and_reads_name_the_evidence_they_issued(tmp_path):
    holdout_fixture(tmp_path, 'notes_combo')
    session = Session(tmp_path)
    result, _ = session.run('哪些笔记讲光合作用？', [step(*read_calls(['note_01.txt', 'note_02.txt'])),
                                              step(start([dict(id='find', kind='find', description='光合作用笔记')])),
                                              step(call('report_findings', dict(findings=[finding(0), finding(1)]))), finish()])
    runtime = session.sb.runtime
    assert '模型判断：' in result.final  # the table still reaches the answer
    reads = [r for r in runtime.results.values() if r['tool'] == 'read_file']
    assert all(any(ref['kind'] == 'evidence' for ref in r['references']) for r in reads)
    assert runtime.reference_shadow == []  # the result's references agree with the old diff


def test_a_dedup_stopped_midway_reports_done_and_remaining(tmp_path, monkeypatch):
    from safety.oplog import OpLog
    import tools.fs_write as fs_write
    for name in ('a.txt', 'b.txt', 'c.txt'): (tmp_path / name).write_text('same')
    sb = Sandbox(tmp_path)
    real, calls = fs_write.trash, []
    def flaky(args, sandbox):
        calls.append(args.path)
        if len(calls) == 2: raise OSError('disk')
        return real(args, sandbox)
    monkeypatch.setattr(fs_write, 'trash', flaky)
    plan = registry.TrashBatch(paths=['b.txt', 'c.txt'], keeps=['a.txt'], validate=lambda: None)
    out = registry._trash_batch(plan, sb, OpLog(sb, 'r1'), None)
    assert out.status == 'error' and out.error_class == 'PARTIAL_EXECUTION'
    assert out.progress == dict(completed=['b.txt'], remaining=['c.txt'], unknown=[])
    assert (tmp_path / 'c.txt').is_file() and not (tmp_path / 'b.txt').exists()


def test_a_plan_stopped_in_flight_is_unknown_and_asks_for_reconciling(tmp_path, monkeypatch):
    import safety.atomic
    files_fixture(tmp_path)
    session = Session(tmp_path)
    def broken(src, dst): raise OSError(5, 'I/O error')
    monkeypatch.setattr(safety.atomic, 'rename_no_replace', broken)
    session.run('归档 a', [step(start([dict(kind='organize', description='归档 a')]),
                              call('prepare_file_plan', dict(description='归档', actions=ARCHIVE_A), alias=PLAN)), execute()],
                final='好。')
    row = next(r for r in session.sb.runtime.results.values() if r['tool'] == 'execute_file_plan')
    assert row['error_class'] == 'OUTCOME_UNKNOWN' and row['recovery'] == 'reconcile'
    assert row['progress']['unknown'] and not row['progress']['completed']
