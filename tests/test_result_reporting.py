"""What the user reads (2026-10-22).

The main answer is in the user's words; tool names, plan/task IDs and instructions
meant for the model stay in details shown only in developer mode.
"""
from agent.reporting import build_report, plain


def test_plain_keeps_the_reason_and_drops_internals():
    assert plain('本目标尚无已执行的文件计划') == '还没有执行'
    assert plain('计划 Xabc123def456 部分执行后未完成：a.txt；请用 revise_task 修正') == '只执行了一部分：a.txt'
    assert plain('本轮已登记任务 Tabcdef123456；更正或补充请用revise_task，不能另建任务') == '本轮已登记任务'
    assert plain('prepare_file_plan 失败：目标已存在') == '准备文件操作 失败：目标已存在'


def test_main_answer_has_no_tool_names_or_ids_and_the_raw_text_is_developer_detail():
    message = '计划 Xabc123def456 部分执行后未完成：b.txt；请用 inspect_file_plan 查看已完成项，不要重复'
    task = dict(status='partial', operations=[dict(op='move', kind='文件')], unresolved_questions=[],
                observations=[dict(tool='execute_file_plan', status='error', summary=message)],
                blockers=[dict(source='execute_file_plan', object='', message=message, status='open')],
                checks=[dict(goal_id='g1', description='移动 a 和 b', passed=False, needs_review=False,
                             conditions=[dict(kind='file_plan_verified', passed=False, needs_review=False, detail=message)])])
    report = build_report(task, '', [])
    main = report['summary']
    assert '尚未全部完成' in main and '只执行了一部分：b.txt' in main
    for internal in ('Xabc123def456', 'execute_file_plan', 'inspect_file_plan', 'file_plan_verified', '阻塞原因', '最近一次失败的调用'):
        assert internal not in main
    dev = [s for s in report['sections'] if s.get('audience') == 'dev']
    assert any(message.split('；')[0] in s['markdown'] for s in dev)


# ---------------------------------------------------------------- five outcomes per part (2026-10-23)

from agent.verifier import _settle


def settle(rows, *, review=False, declared=None, skipped=(), touched=False):
    goal = dict(id='g', description='部分', semantic_review=review, conditions=[])
    task = dict(revision=1, declared_unfinished=dict(revision=1, **(declared or {})),
                file_plan_bindings={'X': dict(goal_ids=['g'])} if touched else {})
    return _settle(task, goal, rows, 'A（只做一半）' if skipped else None, set(skipped))


def test_each_part_ends_in_one_of_five_outcomes():
    ok, bad = dict(passed=True, needs_review=False), dict(passed=False, needs_review=False)
    assert settle([ok])['outcome'] == 'done'
    assert settle([ok], review=True)['outcome'] == 'review'
    assert settle([bad])['outcome'] == 'unfinished'
    assert settle([ok], declared=dict(items=[dict(part_id='g', reason='被拒绝')]))['outcome'] == 'unfinished'
    nothing = settle([bad], declared=dict(not_needed=[dict(part_id='g', reason='没有对象')]))
    assert nothing['outcome'] == 'not_needed' and nothing['passed'] and nothing['needs_review']
    assert settle([bad], skipped=['g'])['outcome'] == 'by_choice'


def test_a_declaration_never_passes_work_the_part_did():
    # "Nothing to do" for a part that prepared a plan is contradicted locally.
    check = settle([dict(passed=True, needs_review=False)], touched=True,
                   declared=dict(not_needed=[dict(part_id='g', reason='没有对象')]))
    assert check['outcome'] == 'unfinished' and check['notes'][0]['by'] == 'local'
