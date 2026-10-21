"""Error attribution over traces (eval/protocol_errors.py); synthetic traces, no results needed."""
import json

from eval.protocol_errors import analyse, analyse_trace, classify


def trace(instruction, steps):
    events = [dict(event='start', instruction=instruction)]
    for number, calls in enumerate(steps, 1):
        events.append(dict(event='llm_request', phase='end', step=number))
        events += [dict(step=number, tool=tool, args=args, result=result) for tool, args, result in calls]
    return events


def test_categories_follow_the_local_rejection_text():
    assert classify('ERROR: 约束 c 未满足：within，对象 a') == 'rule'
    assert classify('ERROR: 约束 c：超过task操作数量上限 2') == 'rule'
    assert classify('ERROR: 操作包含任务写入范围外的对象：Inbox/a.txt') == 'scope_roots'
    assert classify('ERROR: 目标已存在，不会覆盖：Archive') == 'destination_shape'
    assert classify('ERROR: 任务版本已变化，必须重新预览计划') == 'stale_revision'
    assert classify('ERROR: Extra data: line 1 column 686') == 'format'
    assert classify('ERROR: 增加允许副作用必须通过任务编辑确认') == 'needs_edit'
    assert classify('ERROR: 完全没见过') == 'other'


def test_protocol_only_requests_duplicates_and_self_set_limits():
    events = trace('把 a.txt 移到 Archive', [
        [('set_task', '{"constraints":[{"rules":[{"predicate":"max_count","limit":1}]}]}', 'OK: task_id=T1')],
        [('prepare_file_plan', '{"a":1}', 'ERROR: 操作包含任务写入范围外的对象：a.txt')],
        [('edit_task', '{}', 'OK: 编辑'), ('apply_task_edit', '{}', 'OK')],
        [('prepare_file_plan', '{"a":1}', 'OK: plan_id=X1')],
        [('execute_file_plan', '{"plan_id":"X1"}', 'OK'), ('list_dir', '{"path":"Archive"}', 'OK')],
    ])
    row = analyse_trace(events)
    assert row['requests'] == 5 and row['protocol_only_requests'] == 2  # the failed prepare and the self-edit
    assert row['duplicates'] == dict(total=1, after_state_change=1)
    assert row['post_write_checks'] == 1 and row['unrequested_count_limits'] == 1
    # In a later turn an edit carries the user's correction, not protocol repair.
    assert analyse_trace(events, first_turn=False)['protocol_only_requests'] == 1


def test_a_stated_limit_is_not_counted_as_self_set():
    events = trace('最多移动 2 个文件', [[('set_task', '{"rules":[{"predicate":"max_count"}]}', 'OK')]])
    assert analyse_trace(events)['unrequested_count_limits'] == 0
    keep_one = trace('副本只保留一份', [[('set_task', '{"rules":[{"predicate":"max_count"}]}', 'OK')]])
    assert analyse_trace(keep_one)['unrequested_count_limits'] == 1


def test_analyse_reads_run_folders(tmp_path):
    case = tmp_path / 'run' / 'case'; case.mkdir(parents=True)
    (case / 'turn_1.trace.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in trace('x', [
        [('prepare_file_plan', '{}', 'ERROR: 目标重复或名称折叠后冲突：T')]])))
    summary = analyse([tmp_path / 'run'])
    assert summary['totals']['errors'] == 1 and summary['categories'] == {'destination_shape': 1}
    assert summary['protocol_only_share'] == 1.0
