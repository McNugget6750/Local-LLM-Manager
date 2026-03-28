"""Wraps QFileSystemWatcher to emit clean signals for directory and file changes."""

from PySide6.QtCore import QObject, QFileSystemWatcher, Signal


class DirWatcher(QObject):
    file_changed = Signal(str)   # path of changed file
    dir_changed  = Signal(str)   # path of changed directory

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self.file_changed)
        self._watcher.directoryChanged.connect(self.dir_changed)

    def set_cwd(self, path: str) -> None:
        """Watch a directory (e.g. after CWD changes)."""
        current = self._watcher.directories()
        if current:
            self._watcher.removePaths(current)
        self._watcher.addPath(path)

    def watch_file(self, path: str) -> None:
        """Add a file to the watch list (e.g. when opened in editor)."""
        self._watcher.addPath(path)

    def unwatch_file(self, path: str) -> None:
        """Stop watching a file."""
        self._watcher.removePath(path)
