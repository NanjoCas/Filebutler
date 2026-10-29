"""A configured library that is gone is reported, never silently recreated empty (D 1, 2026-10-29).

Before: Sandbox() created any missing path, parents included, so a moved or renamed
library opened as an empty one ("files all gone"); an unplugged drive gave the web
page an Internal Server Error."""
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import webapp
from main import ROOT
from safety.sandbox import LibraryMissing, open_library


def test_first_use_creates_only_the_last_folder_and_remembers_it(tmp_path):
    known = tmp_path / 'ui/libraries.json'
    assert open_library(tmp_path / '资料库', known) == (tmp_path / '资料库').resolve()
    assert (tmp_path / '资料库').is_dir() and str((tmp_path / '资料库').resolve()) in known.read_text()
    with pytest.raises(LibraryMissing, match='上级文件夹 .* 也不存在'):
        open_library(tmp_path / 'typo/资料库', known)
    assert not (tmp_path / 'typo').exists()


def test_a_known_library_that_is_gone_is_not_recreated(tmp_path):
    known = tmp_path / 'libraries.json'
    open_library(tmp_path / '资料库', known)
    (tmp_path / '资料库').rename(tmp_path / '资料库_moved')
    with pytest.raises(LibraryMissing, match='它之前在这里，可能被移动、改名，或所在的硬盘没有连接'):
        open_library(tmp_path / '资料库', known)
    assert not (tmp_path / '资料库').exists()


def test_a_file_in_place_of_the_library_and_an_unreadable_record(tmp_path):
    (tmp_path / 'lib').write_text('not a folder')
    with pytest.raises(LibraryMissing, match='不是文件夹'):
        open_library(tmp_path / 'lib', tmp_path / 'libraries.json')
    (tmp_path / 'broken.json').write_text('{not json')
    assert open_library(tmp_path / 'new', tmp_path / 'broken.json').is_dir()  # treated as first use


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / 'config.yaml').write_text((ROOT / 'config.yaml').read_text().replace('sandbox: ./sandbox', 'sandbox: ./资料库'))
    assert 'sandbox: ./资料库' in (tmp_path / 'config.yaml').read_text()
    state = tmp_path / '.filebutler-ui'
    for name, value in dict(HOME=tmp_path, STATE_DIR=state, DB_PATH=state / 'state.sqlite3', LOG_DIR=tmp_path / 'logs',
                            TURN_STATES={}, ACTIVE_CONVERSATIONS=set()).items():
        monkeypatch.setattr(webapp, name, value)
    return tmp_path


def test_the_web_page_says_the_library_is_gone_instead_of_showing_it_empty(home):
    with TestClient(webapp.app) as client:
        assert client.get('/api/files?path=.').status_code == 200  # first use: created
        (home / '资料库/a.txt').write_text('a')
        (home / '资料库').rename(home / '资料库_moved')
        for url in ('/api/health', '/api/files?path=.'):
            r = client.get(url)
            assert r.status_code == 503 and r.json()['library_missing'] and '可能被移动、改名' in r.json()['detail']
    assert not (home / '资料库').exists()
    # The server still starts with the library missing (startup recovery reads the config).
    with TestClient(webapp.app) as client:
        assert client.get('/').status_code == 200


def test_the_command_line_stops_with_the_reason(tmp_path):
    (tmp_path / 'config.yaml').write_text((ROOT / 'config.yaml').read_text().replace('sandbox: ./sandbox', 'sandbox: ./资料库'))
    def search():
        return subprocess.run([sys.executable, str(ROOT / 'main.py'), '--config', str(tmp_path / 'config.yaml'),
                               'search', 'federated'], capture_output=True, text=True, cwd=ROOT)
    assert search().returncode == 0 and (tmp_path / '资料库').is_dir()
    (tmp_path / '资料库').rmdir()
    gone = search()
    assert gone.returncode == 1 and '找不到资料库' in gone.stdout and not (tmp_path / '资料库').exists()
