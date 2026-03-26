"""Color constants shared between QSS, HTML chat rendering, and highlighter."""

BG_PANEL    = "#0d0d1a"
BG_SIDEBAR  = "#0f0f1a"
BG_TOOLBAR  = "#16213e"
BG_CODE     = "#1a1a2e"
BG_STATUSBAR= "#1a1a2e"
BORDER      = "#333333"
BORDER_CODE = "#555555"
ACCENT      = "#7ec8e3"
TEXT_MUTED  = "#888888"
TEXT_DIM    = "#666666"
USER_COLOR  = "#36d7b7"
ASST_COLOR  = "#7dff7d"
RED         = "#ef4444"

QSS = f"""
QMainWindow, QDialog {{
    background: {BG_PANEL};
}}
QWidget {{
    background: {BG_PANEL};
    color: #cccccc;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}}
QMenuBar {{
    background: {BG_SIDEBAR};
    color: #aaaaaa;
    border-bottom: 1px solid {BORDER};
}}
QMenuBar::item:selected {{
    background: {BG_TOOLBAR};
}}
QMenu {{
    background: {BG_SIDEBAR};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background: {BG_TOOLBAR};
}}
QToolBar {{
    background: {BG_TOOLBAR};
    border-bottom: 1px solid {BORDER};
    spacing: 6px;
    padding: 3px 8px;
}}
QStatusBar {{
    background: {BG_STATUSBAR};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
}}
QSplitter::handle {{
    background: {BORDER};
    width: 1px;
    height: 1px;
}}
QTreeView {{
    background: {BG_SIDEBAR};
    border: none;
    color: #aaaaaa;
}}
QTreeView::item:selected {{
    background: {BG_TOOLBAR};
    color: {ACCENT};
}}
QTextBrowser, QPlainTextEdit {{
    background: {BG_PANEL};
    border: none;
    color: #cccccc;
    selection-background-color: {BG_TOOLBAR};
}}
QTabWidget::pane {{
    border: none;
    background: {BG_PANEL};
}}
QTabBar::tab {{
    background: {BG_SIDEBAR};
    color: {TEXT_DIM};
    padding: 5px 14px;
    border-right: 1px solid {BORDER};
}}
QTabBar::tab:selected {{
    background: {BG_PANEL};
    color: #ffffff;
    border-bottom: 2px solid {ACCENT};
}}
QPushButton {{
    background: {BG_TOOLBAR};
    color: {ACCENT};
    border: 1px solid {BORDER};
    padding: 3px 12px;
    border-radius: 2px;
}}
QPushButton:hover {{
    background: {BG_CODE};
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    border-color: {BORDER};
}}
QLineEdit, QComboBox {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    color: #cccccc;
    padding: 2px 6px;
    border-radius: 2px;
}}
QComboBox::drop-down {{
    border: none;
}}
QProgressBar {{
    background: #222222;
    border: none;
    border-radius: 2px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 2px;
}}
QScrollBar:vertical {{
    background: {BG_SIDEBAR};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
"""
