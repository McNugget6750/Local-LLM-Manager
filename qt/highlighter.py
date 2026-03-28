"""
Syntax highlighter for QPlainTextEdit.
Provides RULES dict (for unit testing) and SyntaxHighlighter(QSyntaxHighlighter).

RULES structure: {lang: [(pattern_str, (color_hex, bold, italic)), ...]}
Tests access only the pattern string: `pattern, _ = RULES[lang][i]`.

Block comment support (/* … */) is stored separately in BLOCK_COMMENTS.
"""

import os
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
from PySide6.QtCore import QRegularExpression

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette (dark theme)
# ─────────────────────────────────────────────────────────────────────────────
_KW    = "#6699cc"   # keywords          – soft blue
_STR   = "#ffcc66"   # strings           – amber yellow
_CMT   = "#5a8a5a"   # comments          – muted green (italic)
_NUM   = "#99cc99"   # numbers/literals  – light green
_DEC   = "#dcdcaa"   # decorators/macros – pale yellow
_TYPE  = "#4db8b8"   # types/builtins    – teal
_OP    = "#cccccc"   # operators         – white-grey
_VAR   = "#88ccff"   # variables/params  – light blue
_TAG   = "#6699cc"   # HTML tags         – soft blue
_ATTR  = "#88ccff"   # HTML attributes   – light blue
_NS    = "#cc6666"   # namespaces/dirs   – muted red
_PUNCT = "#6688aa"   # punctuation       – steel blue-grey

# ─────────────────────────────────────────────────────────────────────────────
# Rules — (pattern_str, (color_hex, bold, italic))
# All patterns are matched per-block; the last match wins (applied in order).
# ─────────────────────────────────────────────────────────────────────────────

RULES: dict[str, list[tuple[str, tuple]]] = {

    # ── Python ────────────────────────────────────────────────────────────────
    # Indices 0-4 are stable (tests reference them by index).
    "python": [
        (r"\b(def|class|return|if|elif|else|for|while|try|except|finally|with|as|"
         r"import|from|and|or|not|in|is|None|True|False|pass|break|continue|raise|"
         r"yield|lambda|global|nonlocal|del|assert|async|await)\b",
         (_KW, False, False)),                                           # 0
        (r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
         (_STR, False, False)),                                          # 1
        (r"#[^\n]*",           (_CMT, False, True)),                     # 2
        (r"\b\d+\.?\d*\b",    (_NUM, False, False)),                    # 3
        (r"@\w+",              (_DEC, False, False)),                    # 4
        # Extra rules appended — don't affect existing test indices
        (r'"""[^"]*(?:""(?!")|"(?!""))*[^"]*"""|'
         r"'''[^']*(?:''(?!')|'(?!''))*[^']*'''",
         (_STR, False, False)),                                          # 5: triple-quoted (paints over 1)
        (r"f\"[^\"\\]*(?:\\.[^\"\\]*)*\"|f\'[^\'\\]*(?:\\.[^\'\\]*)*\'",
         (_STR, False, False)),                                          # 6: f-strings
        (r"\b(int|float|str|bool|list|dict|set|tuple|bytes|type|object|"
         r"len|range|print|input|open|isinstance|hasattr|getattr|setattr|"
         r"super|property|staticmethod|classmethod|zip|map|filter|sorted|"
         r"enumerate|reversed|any|all|min|max|sum|abs|round|repr|id|hash)\b",
         (_TYPE, False, False)),                                         # 7: builtins
    ],

    # ── Bash / Shell ──────────────────────────────────────────────────────────
    # Indices 0-3 stable (tests reference them by index).
    "bash": [
        (r"\b(if|then|else|elif|fi|for|in|do|done|while|until|case|esac|"
         r"function|return|exit|local|export|source|declare|readonly|"
         r"echo|printf|read|shift|set|unset|trap|wait|exec|eval|"
         r"true|false|break|continue)\b",
         (_KW, False, False)),                                           # 0
        (r'"[^"]*"|\'[^\']*\'', (_STR, False, False)),                  # 1
        (r"\$\{?[A-Za-z_]\w*\}?|\$\d|\$[@#?*!$]",
         (_VAR, False, False)),                                          # 2
        (r"#[^\n]*",           (_CMT, False, True)),                    # 3
        # Extra rules
        (r"\b(ls|cd|mkdir|rm|cp|mv|grep|sed|awk|cat|head|tail|sort|uniq|"
         r"wc|find|xargs|cut|tr|tee|curl|wget|chmod|chown|ln|touch|"
         r"pwd|env|which|type|test|kill|ps|df|du|tar|zip|gzip|ssh|scp)\b",
         (_TYPE, False, False)),                                         # 4: common commands
        (r"\b\d+\.?\d*\b",    (_NUM, False, False)),                    # 5
    ],

    # ── JSON ──────────────────────────────────────────────────────────────────
    "json": [
        (r'"[^"\\]*(?:\\.[^"\\]*)*"\s*(?=:)', (_KW,  False, False)),
        (r'(?<=:\s)"[^"\\]*(?:\\.[^"\\]*)*"', (_STR, False, False)),
        (r"(?<=:\s)-?\d+\.?\d*(?:[eE][+-]?\d+)?", (_NUM, False, False)),
        (r"\b(true|false|null)\b",             (_TYPE, False, False)),
    ],

    # ── C / C++ ───────────────────────────────────────────────────────────────
    "c": [
        (r"\b(auto|break|case|const|continue|default|do|else|enum|extern|for|"
         r"goto|if|inline|register|restrict|return|sizeof|static|struct|switch|"
         r"typedef|union|unsigned|volatile|while|"
         r"class|delete|explicit|friend|namespace|new|operator|private|protected|"
         r"public|template|this|throw|try|catch|virtual|override|final|"
         r"nullptr|true|false|and|or|not)\b",
         (_KW, False, False)),
        (r"\b(void|int|float|double|char|short|long|signed|wchar_t|size_t|"
         r"int8_t|int16_t|int32_t|int64_t|uint8_t|uint16_t|uint32_t|uint64_t|"
         r"bool|string|vector|map|set|list|array|unique_ptr|shared_ptr|"
         r"auto|decltype|std)\b",
         (_TYPE, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"', (_STR, False, False)),
        (r"'[^'\\](?:\\.[^'\\]*)*'|'\\?.'",  (_STR, False, False)),
        (r"//[^\n]*",           (_CMT, False, True)),
        (r"#\s*(include|define|ifdef|ifndef|endif|if|elif|else|pragma|undef|error)\b",
         (_NS, False, False)),
        (r"\b0[xX][0-9a-fA-F]+[uUlL]*\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[fFlLuU]*\b",
         (_NUM, False, False)),
    ],

    # ── C# ────────────────────────────────────────────────────────────────────
    "csharp": [
        (r"\b(abstract|as|base|break|case|catch|checked|class|const|continue|"
         r"default|delegate|do|else|enum|event|explicit|extern|false|finally|"
         r"fixed|for|foreach|goto|if|implicit|in|interface|internal|is|lock|"
         r"namespace|new|null|object|operator|out|override|params|private|"
         r"protected|public|readonly|ref|return|sealed|sizeof|stackalloc|"
         r"static|struct|switch|this|throw|true|try|typeof|unchecked|unsafe|"
         r"using|virtual|void|volatile|while|async|await|var|dynamic|"
         r"get|set|add|remove|value|yield|partial|where|from|select|"
         r"let|join|orderby|group|into|ascending|descending)\b",
         (_KW, False, False)),
        (r"\b(bool|byte|char|decimal|double|float|int|long|sbyte|short|string|"
         r"uint|ulong|ushort|nint|nuint|Task|List|Dictionary|IEnumerable|"
         r"IList|ICollection|Action|Func|EventHandler)\b",
         (_TYPE, False, False)),
        (r'@"[^"]*"|"[^"\\]*(?:\\.[^"\\]*)*"', (_STR, False, False)),
        (r"'[^'\\]'|'\\.'",    (_STR, False, False)),
        (r"//[^\n]*",           (_CMT, False, True)),
        (r"\[\w+(?:\([^)]*\))?\]",             (_DEC, False, False)),
        (r"\b0[xX][0-9a-fA-F]+[uUlL]*\b|\b\d+\.?\d*[fFdDmMlLuU]*\b",
         (_NUM, False, False)),
    ],

    # ── JavaScript / TypeScript ───────────────────────────────────────────────
    "javascript": [
        (r"\b(break|case|catch|class|const|continue|debugger|default|delete|do|"
         r"else|export|extends|finally|for|function|if|import|in|instanceof|"
         r"let|new|of|return|static|super|switch|this|throw|try|typeof|var|"
         r"void|while|with|yield|async|await|from|as|null|undefined|"
         r"true|false)\b",
         (_KW, False, False)),
        (r"\b(Array|Object|String|Number|Boolean|Symbol|BigInt|Map|Set|WeakMap|"
         r"WeakSet|Promise|Proxy|Reflect|Error|Date|RegExp|Math|JSON|"
         r"console|window|document|process|require|module|exports|"
         r"setTimeout|setInterval|clearTimeout|clearInterval|"
         r"parseInt|parseFloat|isNaN|isFinite|encodeURI|decodeURI|"
         r"type|interface|enum|namespace|implements|declare|abstract|"
         r"readonly|override|keyof|typeof|infer|never|unknown|any|void)\b",
         (_TYPE, False, False)),
        (r'`[^`\\]*(?:\\.[^`\\]*)*`', (_STR, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
         (_STR, False, False)),
        (r"//[^\n]*",           (_CMT, False, True)),
        (r"\b0[xX][0-9a-fA-F]+[uUlLn]*\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[fn]?\b",
         (_NUM, False, False)),
        (r"(=>)",               (_KW, False, False)),
    ],

    # ── HTML / XML ────────────────────────────────────────────────────────────
    "html": [
        (r"<!--.*?-->",         (_CMT, False, True)),
        (r"</?[a-zA-Z][a-zA-Z0-9_:-]*",         (_TAG, False, False)),
        (r">",                  (_TAG, False, False)),
        (r'\b[a-zA-Z_:][a-zA-Z0-9_:.-]*(?=\s*=)', (_ATTR, False, False)),
        (r'"[^"]*"|\'[^\']*\'', (_STR, False, False)),
        (r"&[a-zA-Z]+;|&#\d+;|&#x[0-9a-fA-F]+;", (_NUM, False, False)),
        (r"<!DOCTYPE[^>]*>",    (_CMT, False, False)),
    ],

    # ── CSS ───────────────────────────────────────────────────────────────────
    "css": [
        (r"[.#]?[a-zA-Z][\w-]*\s*(?=\{)",      (_TAG, False, False)),
        (r"@[a-zA-Z][\w-]*",                   (_NS, False, False)),
        (r"\b([a-zA-Z][\w-]*)\s*(?=:)",        (_ATTR, False, False)),
        (r'"[^"]*"|\'[^\']*\'',                (_STR, False, False)),
        (r"#[0-9a-fA-F]{3,8}\b",              (_NUM, False, False)),
        (r"\b\d+(?:\.\d+)?(?:px|em|rem|%|vw|vh|pt|pc|ex|ch|fr|deg|rad|s|ms)?\b",
         (_NUM, False, False)),
        (r"\b(important|auto|none|inherit|initial|unset|revert|"
         r"flex|grid|block|inline|absolute|relative|fixed|sticky|"
         r"hidden|visible|solid|dashed|dotted|bold|normal|italic)\b",
         (_KW, False, False)),
        (r"/\*.*?\*/",          (_CMT, False, True)),
    ],

    # ── YAML ──────────────────────────────────────────────────────────────────
    "yaml": [
        (r"^[ \t]*[a-zA-Z_][\w./-]*\s*(?=:)",    (_KW, False, False)),
        (r":\s*(true|false|null|~)\b",            (_TYPE, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\']*\'',  (_STR, False, False)),
        (r":\s*\|[-+]?|:\s*>[-+]?",              (_NS, False, False)),
        (r"#[^\n]*",            (_CMT, False, True)),
        (r":\s*-?\d+\.?\d*(?:[eE][+-]?\d+)?",    (_NUM, False, False)),
        (r"^[ \t]*-\s",                          (_OP, True, False)),
        (r"^---$|^\.\.\.$",                      (_NS, True, False)),
        (r"&\w+|\*\w+",                          (_VAR, False, False)),
    ],

    # ── TOML ──────────────────────────────────────────────────────────────────
    "toml": [
        (r"^\[[\w.]+\]$|^\[\[[\w.]+\]\]$",       (_NS, True, False)),
        (r"^[a-zA-Z_][\w.-]*\s*(?==)",            (_KW, False, False)),
        (r'"""[^"]*"""|\'\'\'[^\']*\'\'\'',       (_STR, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\']*\'',  (_STR, False, False)),
        (r"#[^\n]*",            (_CMT, False, True)),
        (r"\b(true|false)\b",  (_TYPE, False, False)),
        (r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b",  (_NUM, False, False)),
    ],

    # ── Markdown ──────────────────────────────────────────────────────────────
    "markdown": [
        (r"^#{1,6}\s.*$",       (_KW, True, False)),
        (r"`[^`]+`|```[\s\S]*?```", (_STR, False, False)),
        (r"\*\*[^*]+\*\*|__[^_]+__", (_OP, True, False)),
        (r"\*[^*]+\*|_[^_]+_",      (_OP, False, True)),
        (r"^\s*[-*+]\s|^\s*\d+\.\s", (_TYPE, False, False)),
        (r"\[([^\]]+)\]\([^)]+\)",   (_NS, False, False)),
        (r"!\[([^\]]*)\]\([^)]+\)",  (_VAR, False, False)),
        (r"^>+\s.*$",               (_CMT, False, True)),
        (r"^---+$|^\*\*\*+$",       (_CMT, False, False)),
    ],

    # ── Rust ──────────────────────────────────────────────────────────────────
    "rust": [
        (r"\b(as|async|await|break|const|continue|crate|dyn|else|enum|extern|"
         r"false|fn|for|if|impl|in|let|loop|match|mod|move|mut|pub|ref|return|"
         r"self|Self|static|struct|super|trait|true|type|unsafe|use|where|while|"
         r"abstract|become|box|do|final|macro|override|priv|typeof|unsized|"
         r"virtual|yield)\b",
         (_KW, False, False)),
        (r"\b(bool|char|f32|f64|i8|i16|i32|i64|i128|isize|"
         r"u8|u16|u32|u64|u128|usize|str|String|Vec|Option|Result|"
         r"Box|Rc|Arc|Cell|RefCell|Mutex|RwLock|"
         r"Some|None|Ok|Err|std)\b",
         (_TYPE, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"',  (_STR, False, False)),
        (r"b'[^'\\](?:\\.[^'\\]*)*'|'[^'\\](?:\\.[^'\\]*)*'",
         (_STR, False, False)),
        (r"//[^\n]*",           (_CMT, False, True)),
        (r"#!?\[[\w:, ()\"]+\]",        (_DEC, False, False)),
        (r"\b0[xXoObB][0-9a-fA-F_]+[uUiI]?\w*\b|\b\d[\d_]*\.?[\d_]*(?:[eE][+-]?[\d_]+)?[uUiIfF]?\w*\b",
         (_NUM, False, False)),
        (r"'[a-z_]\w*\b",      (_VAR, False, False)),
    ],

    # ── Go ────────────────────────────────────────────────────────────────────
    "go": [
        (r"\b(break|case|chan|const|continue|default|defer|else|fallthrough|for|"
         r"func|go|goto|if|import|interface|map|package|range|return|select|"
         r"struct|switch|type|var|nil|true|false|iota)\b",
         (_KW, False, False)),
        (r"\b(bool|byte|complex64|complex128|error|float32|float64|"
         r"int|int8|int16|int32|int64|rune|string|uint|uint8|uint16|"
         r"uint32|uint64|uintptr|any|comparable|"
         r"make|new|len|cap|close|delete|copy|append|panic|recover|"
         r"print|println|real|imag|complex)\b",
         (_TYPE, False, False)),
        (r'`[^`]*`',            (_STR, False, False)),
        (r'"[^"\\]*(?:\\.[^"\\]*)*"', (_STR, False, False)),
        (r"//[^\n]*",           (_CMT, False, True)),
        (r"\b0[xXoObB][0-9a-fA-F_]+\b|\b\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?[iFi]?\b",
         (_NUM, False, False)),
    ],

    "plain": [],
}

# ── Block comment delimiters per language ─────────────────────────────────────
# Languages listed here get extra state-machine handling for /* … */ comments.
BLOCK_COMMENTS: dict[str, tuple[str, str]] = {
    "c":          ("/*", "*/"),
    "csharp":     ("/*", "*/"),
    "javascript": ("/*", "*/"),
    "css":        ("/*", "*/"),
    "rust":       ("/*", "*/"),
    "go":         ("/*", "*/"),
}

# ── Extension map ─────────────────────────────────────────────────────────────
_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".bat": "bash", ".cmd": "bash", ".ps1": "bash",
    ".json": "json", ".jsonc": "json",
    ".c": "c", ".h": "c",
    ".cpp": "c", ".cxx": "c", ".cc": "c",
    ".hpp": "c", ".hxx": "c", ".hh": "c",
    ".cs": "csharp",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".xml": "html", ".svg": "html",
    ".css": "css", ".scss": "css", ".less": "css",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown", ".markdown": "markdown",
    ".rs": "rust",
    ".go": "go",
}


# Global punctuation — applied before language rules so specific rules override
_PUNCT_RE  = QRegularExpression(r"""[(){}\[\]+\-=;.,/*!#$&^"'\\|<>:@%~`?]""")

def detect_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _EXT_MAP.get(ext, "plain")


_HTML_ESC = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})
_PUNCT_PY = None   # compiled lazily


def highlight_code_html(code: str, lang: str) -> str:
    """Return syntax-highlighted HTML for a code block (inline spans, no <pre>).

    Uses Python re instead of QRegularExpression so it works outside of Qt
    contexts (e.g. pure rendering).  Patterns that are not valid Python re
    are silently skipped.
    """
    import re as _re
    global _PUNCT_PY
    if _PUNCT_PY is None:
        _PUNCT_PY = _re.compile(r"""[(){}\[\]+\-=;.,/*!#$&^"'\\|<>:@%~`?]""")

    lines_out = []
    for line in code.split("\n"):
        n = len(line)
        if n == 0:
            lines_out.append("")
            continue

        # Character-level color map (None → default text colour)
        colors: list[str | None] = [None] * n

        # Punctuation first — overridden by specific rules below
        for m in _PUNCT_PY.finditer(line):
            for i in range(m.start(), m.end()):
                colors[i] = _PUNCT

        # Language rules in order — later entries paint over earlier ones
        for pattern, (color, _bold, _italic) in RULES.get(lang, []):
            try:
                for m in _re.finditer(pattern, line):
                    for i in range(m.start(), m.end()):
                        colors[i] = color
            except _re.error:
                pass  # skip patterns that use QRE extensions unsupported by re

        # Emit colour runs as <span> elements
        parts: list[str] = []
        i = 0
        while i < n:
            c = colors[i]
            j = i + 1
            while j < n and colors[j] == c:
                j += 1
            seg = line[i:j].translate(_HTML_ESC)
            parts.append(f'<span style="color:{c};">{seg}</span>' if c else seg)
            i = j

        lines_out.append("".join(parts))

    return "<br>".join(lines_out)


def _make_fmt(color_hex: str, bold: bool, italic: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color_hex))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


# ─────────────────────────────────────────────────────────────────────────────
# SyntaxHighlighter
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_COMMENT_STATE = 1   # QTextBlock user-state for "inside /* */ comment"


class SyntaxHighlighter(QSyntaxHighlighter):
    def __init__(self, document, language: str = "plain"):
        super().__init__(document)
        self._punct_fmt = _make_fmt(_PUNCT, False, False)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = [
            (QRegularExpression(pattern), _make_fmt(*spec))
            for pattern, spec in RULES.get(language, [])
        ]
        # Block comment support
        bc = BLOCK_COMMENTS.get(language)
        if bc:
            self._bc_start = QRegularExpression(QRegularExpression.escape(bc[0]))
            self._bc_end   = QRegularExpression(QRegularExpression.escape(bc[1]))
            self._bc_fmt   = _make_fmt(_CMT, False, True)
        else:
            self._bc_start = None

    def highlightBlock(self, text: str) -> None:
        # Punctuation pass first — language rules paint over it where needed
        it = _PUNCT_RE.globalMatch(text)
        while it.hasNext():
            m = it.next()
            self.setFormat(m.capturedStart(), m.capturedLength(), self._punct_fmt)

        # Single-line language rules
        for regex, fmt in self._rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)

        # Block comment state machine (paints on top, so it wins)
        if self._bc_start is None:
            return

        self.setCurrentBlockState(0)

        start_idx = 0
        if self.previousBlockState() != _BLOCK_COMMENT_STATE:
            # Not in a comment at block start — find the first /*
            m = self._bc_start.match(text, start_idx)
            start_idx = m.capturedStart() if m.hasMatch() else -1
        # else: we're continuing a comment from the previous block

        while start_idx >= 0 or self.previousBlockState() == _BLOCK_COMMENT_STATE:
            if start_idx < 0:
                start_idx = 0  # continued from previous block

            m_end = self._bc_end.match(text, start_idx)
            if m_end.hasMatch():
                # Comment ends on this line
                length = m_end.capturedEnd() - start_idx
                self.setFormat(start_idx, length, self._bc_fmt)
                self.setCurrentBlockState(0)
                # Look for another /* after the end of this comment
                next_start = m_end.capturedEnd()
                m2 = self._bc_start.match(text, next_start)
                start_idx = m2.capturedStart() if m2.hasMatch() else -1
            else:
                # Comment continues to next block
                self.setCurrentBlockState(_BLOCK_COMMENT_STATE)
                self.setFormat(start_idx, len(text) - start_idx, self._bc_fmt)
                break

            # After processing a closed comment, only loop if we found another /*
            if start_idx < 0:
                break
