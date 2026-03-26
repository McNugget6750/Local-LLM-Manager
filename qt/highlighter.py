"""
Syntax highlighter for QPlainTextEdit.
Provides RULES dict (for unit testing) and SyntaxHighlighter(QSyntaxHighlighter).
"""

import os
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
from PySide6.QtCore import QRegularExpression

# ── Rules (pattern_str, (color_hex, bold, italic)) ───────────────────────────
# Second element is a plain tuple so no Qt objects are created at import time.
# Tests access only the pattern string: `pattern, _ = RULES[lang][i]`.

RULES: dict[str, list[tuple[str, tuple]]] = {
    "python": [
        (r"\b(def|class|return|if|elif|else|for|while|try|except|finally|with|as|"
         r"import|from|and|or|not|in|is|None|True|False|pass|break|continue|raise|"
         r"yield|lambda|global|nonlocal|del|assert|async|await)\b",
         ("#7ec8e3", False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
         ("#f9a8d4", False, False)),
        (r"#[^\n]*",           ("#666666", False, True)),
        (r"\b\d+\.?\d*\b",    ("#ffd700", False, False)),
        (r"@\w+",              ("#ffd700", False, False)),
    ],
    "bash": [
        (r"\b(if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|"
         r"exit|local|export|source|echo|cd|ls|mkdir|rm|cp|mv|grep|sed|awk|cat|"
         r"true|false)\b",     ("#7ec8e3", False, False)),
        (r'"[^"]*"|\'[^\']*\'', ("#f9a8d4", False, False)),
        (r"\$\{?\w+\}?",       ("#7dff7d", False, False)),
        (r"#[^\n]*",           ("#666666", False, True)),
    ],
    "json": [
        (r'"[^"\\]*(?:\\.[^"\\]*)*"\s*(?=:)', ("#7ec8e3", False, False)),
        (r'(?<=:\s)"[^"\\]*(?:\\.[^"\\]*)*"', ("#f9a8d4", False, False)),
        (r"(?<=:\s)-?\d+\.?\d*",              ("#ffd700", False, False)),
        (r"\b(true|false|null)\b",             ("#7dff7d", False, False)),
    ],
    "plain": [],
}

_EXT_MAP = {
    ".py": "python",
    ".sh": "bash", ".bat": "bash", ".cmd": "bash",
    ".json": "json",
}


def detect_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _EXT_MAP.get(ext, "plain")


def _make_fmt(color_hex: str, bold: bool, italic: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color_hex))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


class SyntaxHighlighter(QSyntaxHighlighter):
    def __init__(self, document, language: str = "plain"):
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = [
            (QRegularExpression(pattern), _make_fmt(*spec))
            for pattern, spec in RULES.get(language, [])
        ]

    def highlightBlock(self, text: str) -> None:
        for regex, fmt in self._rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)
