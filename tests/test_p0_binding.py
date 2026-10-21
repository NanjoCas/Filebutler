"""Local completion binding, per-action attribution and blocker lifecycle; temporary files only."""
import json


from agent.reporting import build_report
from agent.verifier import verify_task
from tests.test_policy_redesign import runtime, call
from tests.test_task_runtime_v2 import spec


def plan_id(text):
    return text.split('plan_id=')[1].split('；')[0]


def test_tool_success_binds_call_made_before_registration(tmp_path):
    (tmp_path/'Inbox').mkdir(); (tmp_path/'Inbox/a.txt').write_text('A')
    r = runtime(tmp_path)
    assert call(r, 'list_dir', {'path': 'Inbox'}).startswith('OK')
    task = spec(('统计', [dict(kind='tool_success', tool='list_dir', arguments={'path': 'Inbox'})]),
                ('另一目标', [dict(kind='manual_review')]))
    assert call(r, 'set_task', task).startswith('OK')
    checks = verify_task(r.current, r.sb)
    assert checks[0]['passed'] and checks[0]['needs_review']  # call success is still not semantic proof
    # Without explicit arguments a multi-goal task cannot guess the untagged call.
    loose = spec(('统计', [dict(kind='tool_success', tool='list_dir')]), ('另一目标', [dict(kind='manual_review')]))
    r2 = runtime(tmp_path, run='second')
    call(r2, 'list_dir', {'path': 'Inbox'}); call(r2, 'set_task', loose)
    assert not verify_task(r2.current, r2.sb)[0]['passed']


def test_empty_file_plan_reference_binds_to_goal_plan_and_detects_half_execution(tmp_path):
    for name in ('a', 'b'): (tmp_path/f'{name}.txt').write_text(name)
    r = runtime(tmp_path)
    task = spec(('移动两个文件', [dict(kind='file_plan_verified')]), write_roots=['.'], allowed_effects=['create', 'move'])
    assert call(r, 'set_task', task).startswith('OK')
    assert '尚无已执行' in verify_task(r.current, r.sb)[0]['conditions'][0]['detail']
    text = call(r, 'prepare_file_plan', dict(description='移动', actions=[
        dict(effect='move', source='a.txt', destination='out/a.txt'),
        dict(effect='move', source='b.txt', destination='out/b.txt')]))
    pid = plan_id(text)
    assert '尚未执行' in verify_task(r.current, r.sb)[0]['conditions'][0]['detail']
    assert call(r, 'execute_file_plan', {'plan_id': pid}).startswith('OK')
    assert verify_task(r.current, r.sb)[0]['passed']
    (tmp_path/'out/a.txt').write_text('changed')
    assert not verify_task(r.current, r.sb)[0]['passed']


def test_partial_plan_is_not_hidden_by_another_completed_plan(tmp_path, monkeypatch):
    for name in ('a', 'b', 'c'): (tmp_path/f'{name}.txt').write_text(name)
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('移动', [dict(kind='file_plan_verified')]), write_roots=['.'], allowed_effects=['create', 'move']))
    pid = plan_id(call(r, 'prepare_file_plan', dict(description='移动', actions=[
        dict(effect='move', source='a.txt', destination='out/a.txt'),
        dict(effect='move', source='b.txt', destination='out/b.txt')])))
    import safety.atomic
    original, moved = safety.atomic.rename_no_replace, []
    def flaky(src, dst):
        moved.append(src)
        if len(moved) == 2: raise OSError('磁盘错误')
        return original(src, dst)
    monkeypatch.setattr(safety.atomic, 'rename_no_replace', flaky)
    assert call(r, 'execute_file_plan', {'plan_id': pid}).startswith('ERROR')
    monkeypatch.setattr(safety.atomic, 'rename_no_replace', original)
    assert r.current['file_plan_bindings'][pid]['pending']
    other = plan_id(call(r, 'prepare_file_plan', dict(description='另一项', actions=[
        dict(effect='move', source='c.txt', destination='out/c.txt')])))
    assert call(r, 'execute_file_plan', {'plan_id': other}).startswith('OK')
    detail = verify_task(r.current, r.sb)[0]['conditions'][0]['detail']
    assert not verify_task(r.current, r.sb)[0]['passed'] and '部分执行后未完成' in detail


def test_explicit_reference_must_bind_before_registration(tmp_path):
    r = runtime(tmp_path)
    text = call(r, 'set_task', spec(('核验', [dict(kind='file_plan_verified', reference='X000000000000')])))
    assert text.startswith('ERROR') and 'file_plan_verified' in text and r.current is None
    text = call(r, 'set_task', spec(('核验', [dict(kind='plan_applied', reference='Pmissing0000')])))
    assert text.startswith('ERROR') and '不存在' in text


def test_classification_condition_on_plain_move_rejected_before_confirmation(tmp_path):
    (tmp_path/'a.txt').write_text('A')
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('移动', [dict(kind='path_exists', path='out/a.txt'), dict(kind='plan_applied')]),
                             write_roots=['.'], allowed_effects=['create', 'move']))
    text = call(r, 'prepare_file_plan', dict(description='移动', actions=[
        dict(effect='move', source='a.txt', destination='out/a.txt')]))
    assert text.startswith('ERROR') and 'plan_applied' in text and 'edit_task' in text
    assert (tmp_path/'a.txt').exists() and not (tmp_path/'out').exists()


def test_composite_plan_attributes_actions_locally_and_keeps_scoped_rules(tmp_path):
    (tmp_path/'Inbox/empty').mkdir(parents=True)
    (tmp_path/'Inbox/old.txt').write_text('old'); (tmp_path/'Inbox/keep.txt').write_text('keep')
    r = runtime(tmp_path)
    task = spec(('回收旧文件', [dict(kind='path_absent', path='Inbox/old.txt')]),
                ('清理空目录', [dict(kind='empty_directories_removed', path='Inbox')]),
                write_roots=['Inbox'], allowed_effects=['trash'], constraints=[
                    dict(id='dirs_only', description='清理步骤只回收空目录', goal_ids=['g1'],
                         rules=[dict(effects=['trash'], predicate='empty_directory')])])
    call(r, 'set_task', task)
    text = call(r, 'prepare_file_plan', dict(description='两项回收', actions=[
        dict(effect='trash', source='Inbox/old.txt'), dict(effect='trash', source='Inbox/empty')]))
    assert text.startswith('OK'), text
    steps = r.store.file_plan(plan_id(text))['payload']['steps']
    assert {s['source']: s['goal_ids'] for s in steps} == {'Inbox/old.txt': ['g0'], 'Inbox/empty': ['g1']}
    # An action no condition explains needs explicit goals, then carries all their rules.
    text = call(r, 'prepare_file_plan', dict(description='无关', actions=[dict(effect='trash', source='Inbox/keep.txt')]))
    assert text.startswith('ERROR') and 'goal_ids' in text
    text = call(r, 'prepare_file_plan', dict(description='无关', goal_ids=['g0', 'g1'],
                                             actions=[dict(effect='trash', source='Inbox/keep.txt')]))
    assert text.startswith('ERROR') and (tmp_path/'Inbox/keep.txt').exists()


def test_task_wide_prohibition_survives_attribution(tmp_path):
    (tmp_path/'Inbox').mkdir(); (tmp_path/'Inbox/old.txt').write_text('old')
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('回收旧文件', [dict(kind='path_absent', path='Inbox/old.txt')]),
                             ('其他', [dict(kind='manual_review')]), write_roots=['Inbox'], allowed_effects=['trash'],
                             constraints=[dict(id='keep', description='全局禁止回收', forbidden_effects=['trash'])]))
    text = call(r, 'prepare_file_plan', dict(description='回收', actions=[dict(effect='trash', source='Inbox/old.txt')]))
    assert text.startswith('ERROR') and (tmp_path/'Inbox/old.txt').exists()


def test_blocker_lifecycle_resolves_supersedes_and_keeps_open_reasons(tmp_path):
    (tmp_path/'a.txt').write_text('A')
    r = runtime(tmp_path)
    call(r, 'set_task', spec(('移动', [dict(kind='path_exists', path='out/a.txt')]),
                             write_roots=['out'], allowed_effects=['create', 'move']))
    first = call(r, 'prepare_file_plan', dict(description='移动', actions=[dict(effect='move', source='a.txt', destination='out/a.txt')]))
    assert '写入范围外' in first
    assert [b['status'] for b in r.current['blockers']] == ['open']
    r.current['checks'] = verify_task(r.current, r.sb); r.current['status'] = 'partial'
    report = build_report(dict(r.current, operations=[]), '', [])
    assert '写入范围外' in report['summary']  # unresolved: main answer
    edit = json.loads(call(r, 'edit_task', dict(task_id=r.current['id'], expected_revision=1,
                                                write_roots=['.'], reason='源文件也需要写入')).split('：', 1)[1])
    assert call(r, 'apply_task_edit', {'edit_id': edit['id']}).startswith('OK')
    assert r.current['blockers'][0]['status'] == 'superseded'
    pid = plan_id(call(r, 'prepare_file_plan', dict(description='移动', actions=[dict(effect='move', source='a.txt', destination='out/a.txt')])))
    assert call(r, 'execute_file_plan', {'plan_id': pid}).startswith('OK')
    r.current['checks'] = verify_task(r.current, r.sb); r.current['status'] = 'needs_review'
    report = build_report(dict(r.current, operations=[]), '', [])
    assert '写入范围外' not in report['summary'] and '阻塞原因' not in report['summary']
    assert any('已恢复的阻塞' in s['title'] and '写入范围外' in s['markdown'] for s in report['sections'])
