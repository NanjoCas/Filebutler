"""Full-set postconditions and concise reports, using real temporary file operations."""
import pytest

from agent.task import TaskStore, TaskSubmission, TaskPatch
from agent.verifier import verify_task
from safety.confirm import Confirmer
from safety.sandbox import Sandbox
from tests.test_runtime import Client
from tests.test_task_runtime_v2 import execute, spec


def flatten_task(**condition):
    return spec(('归集全部文件', [dict(kind='files_placed', path='papers', destination='papers', **condition)]),
                ('清理空子目录', [dict(kind='empty_directories_removed', path='papers')]),
                write_roots=['papers'], allowed_effects=['move', 'trash'])


def source_tree(root):
    (root/'papers/nested/empty').mkdir(parents=True)
    (root/'papers/a.pdf').write_bytes(b'already at destination')
    (root/'papers/nested/b.pdf').write_bytes(b'original B')
    (root/'papers/nested/c.pdf').write_bytes(b'original C')


def test_flatten_and_cleanup_verified_locally_with_one_compact_report(tmp_path):
    source_tree(tmp_path)
    stats = ('inventory_files', {'path':'papers'})
    result, sb, _ = execute(tmp_path, '归集并清理空目录', [
        [('set_task', flatten_task()), ('begin_step',dict(id='place',goal_ids=['g0'],description='归集文件')), stats,
         ('search_files', '{"ext": pdf}'), ('search_files', {'ext':'pdf','path':'papers'}),
         ('move', {'src':'papers/nested/b.pdf','dst_dir':'papers'}), stats],
        [('move', {'src':'papers/nested/c.pdf','dst_dir':'papers'}),
         ('begin_step',dict(id='cleanup',goal_ids=['g1'],description='清理空目录')), ('trash', {'path':'papers/nested/empty'}), ('trash', {'path':'papers/nested'}), stats,
         ('finish_task', {'summary':'全部完成。\n\n重复的长说明。'})]], confirmer=Confirmer('never'))
    assert result.stop_reason=='done' and result.runtime['task_status']=='completed'
    assert all(c['passed'] and not c['needs_review'] for c in result.runtime['checks'])
    assert '已移动 2 个文件' in result.final and '回收区 2 个文件夹' in result.final
    assert len(result.final)<120 and '本地核验通过' in result.final
    assert '|' not in result.final and 'ERROR' not in result.final and '重复' not in result.final
    statistics=[s for s in result.report['sections'] if s['title'].startswith('最终统计')]
    assert len(statistics)==1 and '3 次' in statistics[0]['title']
    assert '共 3 个文件' in statistics[0]['markdown']
    assert any('过程提示' in s['title'] for s in result.report['sections'])
    assert not (tmp_path/'papers/nested').exists()
    saved=sb.runtime.store.get(result.runtime['task_id'])
    assert saved['report']==result.report and len(saved['completion_snapshots']['g0:0']['files'])==3
    assert result.messages[-1]['content']==result.final


@pytest.mark.parametrize('fault', ['missed_file','changed_content','new_file','empty_left'])
def test_incomplete_or_changed_file_set_is_never_auto_completed(tmp_path, fault):
    source_tree(tmp_path)
    def model(kw, n):
        if n==1:
            calls=[('set_task', flatten_task()), ('begin_step',dict(id='place',goal_ids=['g0'],description='归集文件')), ('move', {'src':'papers/nested/b.pdf','dst_dir':'papers'})]
            if fault!='missed_file': calls.append(('move', {'src':'papers/nested/c.pdf','dst_dir':'papers'}))
            return calls
        if fault=='changed_content': (tmp_path/'papers/b.pdf').write_bytes(b'corrupted')
        if fault=='new_file': (tmp_path/'papers/nested/new.pdf').write_bytes(b'arrived later')
        return [('finish_task', {'summary':'都已完成'})]
    result, sb, _ = execute(tmp_path, '归集并清理', Client(model), confirmer=Confirmer('never'))
    assert result.stop_reason=='partial' and '尚未全部完成' in result.final
    checks=result.runtime['checks']
    assert not checks[1]['passed']
    assert checks[0]['passed']==(fault=='empty_left')
    with pytest.raises(ValueError,match='未满足'):
        sb.runtime.store.review(result.runtime['task_id'],1,['g1'],[],'accept','强行通过',sb)


def test_cleanup_condition_blocks_nonempty_folder_and_file_without_extra_rules(tmp_path):
    source_tree(tmp_path)
    task=spec(('只清空目录', [dict(kind='empty_directories_removed',path='papers')]),
              write_roots=['papers'],allowed_effects=['trash'])
    result, _, _ = execute(tmp_path, '只清空目录', [[('set_task',task),
        ('trash', {'path':'papers/nested'}), ('trash', {'path':'papers/a.pdf'}), ('finish_task',{})]],
        confirmer=Confirmer('never'))
    assert result.stop_reason=='partial'
    assert (tmp_path/'papers/nested/b.pdf').exists() and (tmp_path/'papers/a.pdf').exists()
    assert result.runtime['blocked_calls']==2


def test_already_satisfied_task_requires_no_token_spending_for_fake_operations(tmp_path):
    (tmp_path/'papers').mkdir(); (tmp_path/'papers/a.pdf').write_bytes(b'original')
    result, _, _ = execute(tmp_path, '归集和清理', [[('set_task',flatten_task()),('finish_task',{})]])
    assert result.stop_reason=='done' and '1 个文件' in result.final
    assert '已移动' not in result.final


def test_extension_filter_and_exclusions_define_the_manifest(tmp_path):
    source_tree(tmp_path)
    (tmp_path/'papers/nested/ignore.txt').write_text('preserved')
    task=spec(('仅归集指定 PDF', [dict(kind='files_placed',path='papers',destination='papers',
        extensions=['PDF'],exclude_paths=['papers/nested/c.pdf'])]),
        write_roots=['papers'],allowed_effects=['move'])
    result, _, _ = execute(tmp_path,'按筛选归集',[[('set_task',task),
        ('move',{'src':'papers/nested/b.pdf','dst_dir':'papers'}),('finish_task',{})]],confirmer=Confirmer('never'))
    assert result.stop_reason=='done'
    assert (tmp_path/'papers/nested/c.pdf').exists() and (tmp_path/'papers/nested/ignore.txt').exists()


def test_missing_source_directory_after_complete_move_is_valid(tmp_path):
    (tmp_path/'source').mkdir();(tmp_path/'target').mkdir()
    (tmp_path/'source/a.pdf').write_bytes(b'A')
    task=spec(('归集源目录所有文件',[dict(kind='files_placed',path='source',destination='target')]),
              write_roots=['.'],allowed_effects=['move','trash'])
    result, _, _ = execute(tmp_path,'归集并清理源目录',[[('set_task',task),
        ('move',{'src':'source/a.pdf','dst_dir':'target'}),('trash',{'path':'source'}),
        ('finish_task',{})]],confirmer=Confirmer('never'))
    assert result.stop_reason=='done' and (tmp_path/'target/a.pdf').read_bytes()==b'A'


def test_name_collision_and_missing_snapshot_cannot_be_claimed_complete(tmp_path):
    source_tree(tmp_path)
    (tmp_path/'papers/nested/a.pdf').write_bytes(b'different A')
    sb=Sandbox(tmp_path)
    store=TaskStore(tmp_path/'.oplog/tasks.sqlite3',tmp_path,'test')
    with pytest.raises(ValueError,match='同名目标'):
        store.submit(TaskSubmission.model_validate(flatten_task()),'归集','one')
    assert store.recent()==[]
    (tmp_path/'papers/nested/a.pdf').unlink()
    historical=store.submit(TaskSubmission.model_validate(flatten_task()),'归集','two')
    historical.pop('completion_snapshots')
    assert '缺少执行前清单' in verify_task(historical,sb)[0]['conditions'][0]['detail']


@pytest.mark.parametrize('tool',['move','trash','make_dir','apply_classification'])
def test_write_tool_success_is_rejected_as_a_completion_contract(tmp_path,tool):
    store=TaskStore(tmp_path/'tasks.sqlite3',tmp_path,'test')
    with pytest.raises(ValueError,match='写操作不能仅以 tool_success'):
        store.submit(TaskSubmission.model_validate(spec(('完成操作',[dict(kind='tool_success',tool=tool)]))),
                     '需求','r1')
    assert store.recent()==[]


def test_repeated_inventory_keeps_last_value_but_distinct_scope_is_preserved(tmp_path):
    source_tree(tmp_path)
    def model(kw,n):
        if n==1:return [('set_task',spec(('统计',[dict(kind='tool_success',tool='inventory_files')]))),
                       ('inventory_files',{'path':'papers'})]
        (tmp_path/'papers/new.pdf').write_bytes(b'new')
        return [('inventory_files',{'path':'papers'}),('inventory_files',{'path':'papers/nested'}),('finish_task',{})]
    result, sb, _ = execute(tmp_path,'统计',Client(model))
    assert result.stop_reason=='needs_review'
    assert len(sb.runtime.output_records)==2 and sb.runtime.output_records[0]['samples']==2
    assert '共 4 个文件' in result.final and '共 3 个文件' not in result.final
    assert '共 2 个文件' in result.final


@pytest.mark.parametrize('edit',['append_goal','update_scope'])
def test_unchanged_goal_keeps_original_manifest_across_revisions(tmp_path,edit):
    (tmp_path/'source').mkdir();(tmp_path/'target').mkdir()
    (tmp_path/'source/a.pdf').write_bytes(b'A')
    store=TaskStore(tmp_path/'tasks.sqlite3',tmp_path,'test')
    first=store.submit(TaskSubmission.model_validate(spec(('归集',[dict(kind='files_placed',path='source',destination='target')]),
        write_roots=['.'],allowed_effects=['move'])),'归集','r1')
    (tmp_path/'source/a.pdf').rename(tmp_path/'target/a.pdf')
    if edit=='append_goal':
        addition=TaskSubmission.model_validate(spec(('附加核对',[dict(kind='path_exists',path='target')]),
            task_id=first['id'],expected_revision=1))
        addition.goals[0].id='additional'
        changed=store.submit(addition,'也核对目标','r2')
    else:
        proposal=store.preview_edit(first['id'],TaskPatch(expected_revision=1,write_roots=['target'],reason='缩小写入范围'))
        changed=store.apply_edit(proposal['id'],'确认','r2',actor='user')
    assert changed['completion_snapshots']['g0:0']==first['completion_snapshots']['g0:0']
    (tmp_path/'target/a.pdf').write_bytes(b'corrupted after move')
    assert not verify_task(changed,Sandbox(tmp_path))[0]['passed']


def test_legacy_goal_can_resume_with_new_default_fields_without_false_edit_conflict(tmp_path):
    store=TaskStore(tmp_path/'tasks.sqlite3',tmp_path,'test')
    submission=TaskSubmission.model_validate(spec(('核对目录',[dict(kind='path_exists',path='.')])) )
    old=store.submit(submission,'核对','r1')
    for c in old['goals'][0]['conditions']:
        for field in ('destination','extensions','exclude_paths','recursive'):c.pop(field)
    store.checkpoint(old)
    submission.task_id=old['id'];submission.expected_revision=1
    resumed=store.submit(submission,'继续核对','r2')
    assert resumed['revision']==2 and resumed['goals'][0]['conditions'][0]['recursive'] is True
