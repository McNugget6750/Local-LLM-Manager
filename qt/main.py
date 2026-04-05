"""Entry point: creates QApplication, applies stylesheet, launches MainWindow."""

import sys
import pathlib
import logging

# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(name)s:%(lineno)d %(levelname)s %(message)s",
# )

logging.basicConfig(level=logging.WARNING)  # global default
logging.getLogger("slot_manager").setLevel(logging.DEBUG)
logging.getLogger("agents").setLevel(logging.DEBUG)
logging.getLogger("qt.adapter").setLevel(logging.DEBUG)

# Allow `from chat import ChatSession` and `from qt.adapter import QtChatAdapter`
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication

# Local imports (relative to qt/)
_here = pathlib.Path(__file__).parent
sys.path.insert(0, str(_here))

from colors import QSS
from window import MainWindow


def main() -> None:
    # Parse --resume [session_name] before handing argv to Qt
    resume_session: str | None = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg in ("--resume", "--continue"):
            resume_session = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else ""
            break

    app = QApplication(sys.argv)
    app.setApplicationName("qwen3-manager")
    app.setStyleSheet(QSS)

    win = MainWindow()
    win.setWindowTitle("qwen3-manager")
    win.setMinimumSize(1280, 1024)
    win.show()

    if resume_session is not None:
        # Empty string = latest session; named string = specific session
        cmd = f"/resume {resume_session}".rstrip()
        # Delay until the event loop and adapter are running
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500, lambda: win._adapter.submit_slash(cmd))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
