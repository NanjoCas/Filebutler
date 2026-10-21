"""Acceptance-oracle fault injection; these tests never call a model."""
from types import SimpleNamespace

from agent.artifacts import ArtifactStore
from eval.checker import snapshot
from eval.live_policy import checks, fixture
from safety.sandbox import Sandbox


def test_root_only_oracle_rejects_nested_changes_and_content_swaps(tmp_path):
    fixture(tmp_path,'files')
    before=snapshot(tmp_path,True)
    store=ArtifactStore(Sandbox(tmp_path)); runtime=SimpleNamespace(current=None)
    for name in ('a.txt','b.txt'): (tmp_path/'Inbox'/name).rename(tmp_path/'Archive'/name)
    assert all(checks('root_only',0,tmp_path,before,store,runtime,None).values())
    (tmp_path/'Archive/a.txt').write_text('beta'); (tmp_path/'Archive/b.txt').write_text('alpha')
    outcome=checks('root_only',0,tmp_path,before,store,runtime,None)
    assert outcome['contents_preserved'] and not outcome['a_contents_correct'] and not outcome['b_contents_correct']
    (tmp_path/'Inbox/nested/keep.txt').rename(tmp_path/'Archive/keep.txt')
    assert not checks('root_only',0,tmp_path,before,store,runtime,None)['others_unchanged']


def test_application_oracle_compares_original_content_at_each_target(tmp_path):
    fixture(tmp_path,'applications'); before=snapshot(tmp_path,True)
    for i,category in enumerate(('农业','医疗','能源'),1):
        dest=tmp_path/'资料/按领域'/category;dest.mkdir(parents=True)
        (tmp_path/'资料'/f'CNN_{i:02d}.pdf').rename(dest/f'CNN_{i:02d}.pdf')
    runtime=SimpleNamespace(current=None)
    assert all(checks('application_dimension',0,tmp_path,before,None,runtime,None).values())
    (tmp_path/'资料/按领域/农业/CNN_01.pdf').write_bytes(b'wrong')
    assert not checks('application_dimension',0,tmp_path,before,None,runtime,None)['correct_final_locations']


def test_cross_turn_oracle_requires_original_task_revision(tmp_path):
    fixture(tmp_path,'files');before=snapshot(tmp_path,True)
    (tmp_path/'Inbox/a.txt').rename(tmp_path/'Archive/a.txt')
    runtime=SimpleNamespace(current=dict(id='new-task',revision=1))
    assert not checks('cross_turn_correction',1,tmp_path,before,None,runtime,'original-task')['same_task_updated']
    runtime.current=dict(id='original-task',revision=2)
    assert all(checks('cross_turn_correction',1,tmp_path,before,None,runtime,'original-task').values())
