"""The reviewed system-file list (agent/system_files.json) as used when recycling folders.

Each entry has a genuine sample; impostors (right name, wrong content), oversized
files and case variants are checked, as are generated folders mixed with other
content and the busy tier (lock files of open documents).
"""
import json

import pytest

from agent.file_plans import scan_tree
from agent.system_files import entries

import test_live_regressions_20261021 as H
from test_protocol_v3 import Session, step, call, start, errors, PLAN, execute, finish, DS_STORE

OLE = bytes.fromhex('d0cf11e0a1b11ae1') + b'\x00' * 64
APPLEDOUBLE = bytes.fromhex('00051607') + b'\x00' * 32
DESKTOP_INI = '\ufeff[.ShellClassInfo]\r\nIconResource=C:\\icon.ico,0\r\n'.encode('utf-16-le')


def tiers(folder):
    t = scan_tree(folder)
    return dict(metadata=set(t['metadata']), hidden=set(t['hidden']), visible=set(t['visible']),
                special=set(t['special']), busy=set(t['busy']), notes=t['notes'])


def write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data if isinstance(data, bytes) else data.encode())


def test_list_is_well_formed():
    ids = [e['id'] for e in entries()]
    assert len(ids) == len(set(ids))
    for e in entries():
        assert e['tier'] in {'metadata', 'metadata_dir', 'busy', 'special', 'hidden'} and e['patterns'] and e['sources']
        assert all(p == p.lower() for p in e['patterns'])  # matched case-insensitively
        if e['tier'] in {'metadata', 'metadata_dir'}: assert e['max_bytes'] > 0


def test_genuine_system_files_are_metadata(tmp_path):
    write(tmp_path, {'.DS_Store': DS_STORE, '._a.pdf': APPLEDOUBLE, '.localized': b'', 'Icon\r': b'',
                     'Thumbs.db': OLE, 'ehthumbs.db': OLE, 'Desktop.ini': DESKTOP_INI,
                     '.directory': '[Dolphin]\nViewMode=1\n'})
    t = tiers(tmp_path)
    assert t['metadata'] == {'.DS_Store', '._a.pdf', '.localized', 'Icon\r', 'Thumbs.db', 'ehthumbs.db',
                             'Desktop.ini', '.directory'}
    assert not t['visible'] and not t['hidden']
    assert '自定义文件夹图标' in t['notes']['Icon\r'] and '自定义设置' in t['notes']['Desktop.ini']


def test_case_variants_match(tmp_path):
    write(tmp_path, {'thumbs.db': OLE, 'DESKTOP.INI': DESKTOP_INI, '.ds_store': DS_STORE})
    assert tiers(tmp_path)['metadata'] == {'thumbs.db', 'DESKTOP.INI', '.ds_store'}


def test_impostors_and_oversized_files_are_not_metadata(tmp_path):
    write(tmp_path, {'.DS_Store': 'TOKEN=not finder data', 'Thumbs.db': 'my notes', 'desktop.ini': 'plain text',
                     '.localized': b'x' * 5000})
    t = tiers(tmp_path)
    assert not t['metadata']
    assert t['hidden'] == {'.DS_Store', '.localized'} and '内容或大小' in t['notes']['.DS_Store']
    assert t['visible'] == {'Thumbs.db', 'desktop.ini'}  # visible names fall back to user files
    write(tmp_path / 'big', {'.DS_Store': DS_STORE + b'\x00' * (1 << 20)})
    assert tiers(tmp_path / 'big')['hidden'] == {'.DS_Store'}


def test_generated_folders(tmp_path):
    write(tmp_path / 'ok', {'__MACOSX/._a.pdf': APPLEDOUBLE, '__MACOSX/sub/._b.pdf': APPLEDOUBLE, '@eaDir/a.jpg/SYNOPHOTO_THUMB_S.jpg': b'jpg'})
    t = tiers(tmp_path / 'ok')
    assert t['metadata'] == {'__MACOSX/', '@eaDir/'} and '压缩包' in t['notes']['__MACOSX/']
    write(tmp_path / 'mixed', {'__MACOSX/._a.pdf': APPLEDOUBLE, '__MACOSX/report.pdf': 'a real document'})
    assert tiers(tmp_path / 'mixed')['special'] == {'__MACOSX'}


def test_volume_folders_are_special_and_locks_are_busy(tmp_path):
    write(tmp_path, {'.Trashes/501/deleted.txt': 'a deleted file', '$RECYCLE.BIN/x': 'x', '~$report.docx': 'lock',
                     '.~lock.sheet.ods#': 'lock', 'plan.dwl': 'lock', '.dropbox': 'cfg', '.env': 'A=1'})
    t = tiers(tmp_path)
    assert t['special'] == {'.Trashes', '$RECYCLE.BIN'}
    assert t['busy'] == {'~$report.docx', '.~lock.sheet.ods#', 'plan.dwl'}
    assert t['hidden'] == {'.dropbox', '.env'} and '同步软件' in t['notes']['.dropbox']


# ---------------------------------------------------------------- in the recycle flow


PARTS = [dict(kind='organize', description='移出文件并回收文件夹')]


def test_windows_folder_with_visible_system_files_is_recycled_in_one_plan(tmp_path):
    # The ordering bug: Thumbs.db / desktop.ini were taken for user files.
    write(tmp_path, {'Photos/a.jpg': 'a', 'Photos/Thumbs.db': OLE, 'Photos/desktop.ini': DESKTOP_INI})
    session = Session(tmp_path)
    _, client = session.run('把照片移出来并删掉 Photos', [
        step(start(PARTS), call('prepare_file_plan', dict(description='移出并回收', actions=[
            dict(effect='move', source='Photos/a.jpg', destination='.'),
            dict(effect='trash', source='Photos', include_metadata=True)]), alias=PLAN)), execute(), finish()])
    assert errors(client) == [], errors(client)
    assert (tmp_path / 'a.jpg').is_file() and not (tmp_path / 'Photos').exists()
    [request] = [r for r in session.approvals if r['tool'] == 'file_plan']
    [row] = request['args']['hidden_contents']
    assert set(row['metadata']) == {'Thumbs.db', 'desktop.ini'}


def test_card_names_customisations(tmp_path):
    write(tmp_path, {'Work/Icon\r': b'', 'Work/desktop.ini': DESKTOP_INI})
    from web_confirm import build_confirmation
    review = dict(plan_id='X1', digest='d', steps=[dict(effect='trash', source='Work', destination='.trash/x')],
                  hidden_contents=[dict(folder='Work', metadata=['Icon\r', 'desktop.ini'], hidden=[], sensitive={},
                                        notes={'Icon\r': '自定义文件夹图标，回收后文件夹恢复默认图标',
                                               'desktop.ini': 'Windows 文件夹的自定义设置（图标、显示名等），回收后恢复默认'})])
    [note] = [n for n in build_confirmation(dict(tool='file_plan', args=review), H.Sandbox(tmp_path))['notes'] if n.startswith('回收 Work')]
    assert '自定义文件夹图标' in note and '回收后恢复默认' in note


def test_open_document_blocks_recycling_its_folder(tmp_path):
    write(tmp_path, {'Reports/q4.docx': 'doc', 'Reports/~$q4.docx': 'lock'})
    session = Session(tmp_path)
    _, client = session.run('删除 Reports', [step(start(PARTS), call('prepare_file_plan', dict(description='回收', actions=[
        dict(effect='move', source='Reports/q4.docx', destination='.'), dict(effect='trash', source='Reports')])))])
    text = client.calls('prepare_file_plan')[0]['text']
    assert '正被程序打开' in text and '关闭' in text and 'Office' in text
    assert (tmp_path / 'Reports/q4.docx').is_file() and not session.approvals


def test_lock_file_itself_is_never_planned(tmp_path):
    write(tmp_path, {'Reports/~$q4.docx': 'lock'})
    session = Session(tmp_path)
    _, client = session.run('移走', [step(start(PARTS), call('prepare_file_plan', dict(description='移走', actions=[
        dict(effect='move', source='Reports/~$q4.docx', destination='.')])))])
    assert '是锁文件' in client.calls('prepare_file_plan')[0]['text']
    assert (tmp_path / 'Reports/~$q4.docx').is_file()
