"""Persistent identities, journalled moves, versioned evidence and safe previews."""
import json
import re

import pytest
from agent.artifacts import ArtifactStore
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox
from tools import registry
from tools.dedup import scan_duplicates
from web_render import FileCatalog, render_markdown
from tests.test_web_ui import files, ui
import webapp


def test_identical_files_keep_distinct_ids_and_changes_stale_old_evidence(tmp_path):
    sb = Sandbox(tmp_path)
    for n in ('a.txt','b.txt'): (tmp_path/n).write_text('same')
    store=ArtifactStore(sb)
    a,b=store.register('a.txt'),store.register('b.txt')
    assert a != b and store.register('a.txt') == a
    eid=store.evidence(a,'fragment',excerpt='same')
    (tmp_path/'a.txt').write_text('changed')
    with pytest.raises(ValueError,match='变化'): store.resolve_evidence(eid)
    newer=store.register('a.txt')
    assert newer != a
    restored=ArtifactStore(sb,json.loads(json.dumps(store.export())))
    assert restored.resolve(newer)[0].read_text()=='changed'


def test_registered_id_never_follows_external_rename_or_symlink(tmp_path):
    sb=Sandbox(tmp_path);(tmp_path/'a.txt').write_text('same')
    store=ArtifactStore(sb);fid=store.register('a.txt')
    (tmp_path/'a.txt').rename(tmp_path/'renamed.txt')
    with pytest.raises(ValueError): store.resolve(fid)
    (tmp_path/'a.txt').symlink_to(tmp_path/'renamed.txt')
    with pytest.raises(ValueError): store.resolve(fid)
    with pytest.raises(ValueError): store.register('a.txt')
    with pytest.raises(ValueError): store.register('../outside.txt')


def test_receipt_previews_trash_and_same_id_restores_after_undo(ui,files):
    cid=ui.post('/api/conversations').json()['id']
    other=ui.post('/api/conversations').json()['id']
    for n in ('a.txt','b.txt'): (files.root/n).write_text('copy')
    store=webapp._artifacts(cid,files,persist=True);files.artifacts=store;store.run_id='identity'
    refs={n:store.register(n) for n in ('a.txt','b.txt')}
    scan=scan_duplicates(files,mode='bytes')
    result=registry.dispatch('apply_dedup',json.dumps({'scan_id':scan['id']}),files,OpLog(files,'identity'),Confirmer('never'))
    assert result.startswith('OK')
    receipt=next(iter(store.data['receipts']))
    markdown=store.receipt_markdown(receipt)
    html=render_markdown(markdown,FileCatalog(files,store,cid))
    assert html.count('data-artifact-id=')==2
    victim=refs['b.txt'];route=f'/api/conversations/{cid}/files/{victim}'
    assert ui.get(route+'/preview').json()['status']=='trash'
    assert ui.get(route+'/raw').text=='copy'
    assert ui.get('/api/files/raw',params={'path':store.data['files'][victim]['path']}).status_code==404
    assert ui.get(f'/api/conversations/{other}/files/{victim}/raw').status_code==404
    with webapp._db() as db:
        db.execute('INSERT INTO turns(id,conversation_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',('identity',cid,'identity','completed',webapp._now(),webapp._now()))
    assert ui.post('/api/turns/identity/undo').status_code==200
    assert ui.get(route+'/preview').json()['status']=='visible'
    assert ui.get(route+'/raw').text=='copy'
    (files.root/'b.txt').write_text('external change')
    assert ui.get(route+'/raw').status_code==404


def test_ids_and_evidence_render_without_rewriting_long_filename(tmp_path):
    sb=Sandbox(tmp_path);name='algorithms and datasets #初稿.txt';(tmp_path/name).write_text('evidence')
    store=ArtifactStore(sb);fid=store.register(name);eid=store.evidence(fid,'fragment',page=1,offset=2,excerpt='evidence')
    catalog=FileCatalog(sb,store,'conversation')
    html=render_markdown(f'[文献](file:{fid}) [依据](evidence:{eid}) [坏引用](file:F0000000000)',catalog)
    assert html.count('data-artifact-id=')==2 and 'data-file-page="1"' in html
    assert 'data-file-name="algorithms and datasets #初稿.txt"' in html
    assert html.count('unavailable-reference')==1
    assert catalog.resolve('sandbox:/algorithms datasets #初稿.txt') is None
