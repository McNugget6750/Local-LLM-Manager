"""Entry point: creates QApplication, applies stylesheet, launches MainWindow."""

import sys
import pathlib

# Allow `from chat import ChatSession` and `from qt.adapter import QtChatAdapter`
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication

# Local imports (relative to qt/)
_here = pathlib.Path(__file__).parent
sys.path.insert(0, str(_here))

from colors import QSS
from window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("qwen3-manager")
    app.setStyleSheet(QSS)

    win = MainWindow()
    win.setWindowTitle("qwen3-manager")
    win.setMinimumSize(1280, 1024)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
