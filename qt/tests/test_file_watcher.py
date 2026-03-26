import time
import pytest
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from qt.file_watcher import DirWatcher


@pytest.fixture
def app():
    a = QCoreApplication.instance() or QCoreApplication([])
    return a


def test_file_changed_signal_emitted(app, tmp_path, qtbot):
    watcher = DirWatcher()
    test_file = tmp_path / "test.txt"
    test_file.write_text("initial")
    time.sleep(0.1)  # let OS settle before registering watcher
    watcher.watch_file(str(test_file))
    time.sleep(0.1)  # let watcher register

    with qtbot.waitSignal(watcher.file_changed, timeout=5000) as blocker:
        test_file.write_text("modified")

    assert blocker.args[0] == str(test_file)


def test_set_cwd_watches_directory(app, tmp_path):
    watcher = DirWatcher()
    watcher.set_cwd(str(tmp_path))
    assert str(tmp_path) in watcher._watcher.directories()


def test_watch_file_adds_to_watcher(app, tmp_path):
    watcher = DirWatcher()
    f = tmp_path / "foo.py"
    f.write_text("x = 1")
    watcher.watch_file(str(f))
    assert str(f) in watcher._watcher.files()
