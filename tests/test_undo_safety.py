"""2.0 A0: undo shares the executor lock and never replaces a concurrent file."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent.file_plans import library_lock
from safety import atomic
from safety.oplog import OpLog, undo
from safety.sandbox import Sandbox


def moved(root):
    sb = Sandbox(root)
    (root / 'b.txt').write_text('original')
    OpLog(sb, 'run').record('move', 'a.txt', 'b.txt')
    return sb


@pytest.mark.parametrize('cli', [False, True])
def test_another_process_cannot_undo_while_the_executor_holds_the_lock(tmp_path, cli):
    sb = moved(tmp_path / 'library')
    repo = Path(__file__).resolve().parents[1]
    if cli:
        config = tmp_path / 'config.yaml'
        config.write_text(yaml.safe_dump({'sandbox': str(sb.root)}))
        command = [sys.executable, 'main.py', '--config', str(config), 'undo', 'run']
    else:
        command = [sys.executable, '-c',
                   'from safety.oplog import undo; from safety.sandbox import Sandbox; '
                   'import sys; print(undo(Sandbox(sys.argv[1]), "run"))', str(sb.root)]
    with library_lock(sb):
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=20)
    assert (sb.root / 'b.txt').read_text() == 'original'
    assert not (sb.root / 'a.txt').exists()
    assert (sb.root / '.oplog/run.jsonl').exists()
    assert '资料库已有' in result.stdout + result.stderr


def test_a_file_created_after_the_check_is_never_overwritten(tmp_path, monkeypatch):
    sb = moved(tmp_path)
    old_rename, old_atomic = Path.rename, atomic.rename_no_replace

    def race(rename):
        def apply(source, target):
            if Path(source) == tmp_path / 'b.txt':
                (tmp_path / 'a.txt').write_text('concurrent')
            return rename(source, target)
        return apply

    monkeypatch.setattr(Path, 'rename', race(old_rename))
    monkeypatch.setattr(atomic, 'rename_no_replace', race(old_atomic))
    restored = []
    report = undo(sb, 'run', on_restore=lambda *args: restored.append(args))
    assert (tmp_path / 'a.txt').read_text() == 'concurrent'
    assert (tmp_path / 'b.txt').read_text() == 'original'
    assert not restored and any('跳过' in line for line in report)


def test_an_occupied_original_location_is_skipped(tmp_path):
    sb = moved(tmp_path)
    (tmp_path / 'a.txt').write_text('occupied')
    assert '已被占用' in undo(sb, 'run')[0]
    assert (tmp_path / 'a.txt').read_text() == 'occupied'
    assert (tmp_path / 'b.txt').read_text() == 'original'


def test_case_only_undo_restores_the_exact_file_and_folder_names(tmp_path):
    sb = Sandbox(tmp_path)
    (tmp_path / 'Docs').mkdir()
    (tmp_path / 'photo.jpg').write_text('photo')
    log = OpLog(sb, 'run')
    log.record('rename', 'docs', 'Docs')
    log.record('rename', 'photo.JPG', 'photo.jpg')
    assert all(line.startswith('还原') for line in undo(sb, 'run'))
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith('.')) == ['docs', 'photo.JPG']
