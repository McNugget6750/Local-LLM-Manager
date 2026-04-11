"""
markdown_html.py — Markdown → HTML renderer for the Open Harness chat window.

Converts full markdown responses (fenced code, headings, tables, lists,
blockquotes, inline formatting, LaTeX math) to HTML suitable for QTextBrowser.
"""
import re

from highlighter import detect_language, highlight_code_html

# ── Constants ─────────────────────────────────────────────────────────────────

_LANG_ALIAS = {
    "js": "javascript", "ts": "javascript", "jsx": "javascript", "tsx": "javascript",
    "sh": "bash", "zsh": "bash",
    "cpp": "c", "c++": "c", "cxx": "c",
    "cs": "csharp",
    "py": "python",
    "yml": "yaml",
}

_MD_ESC = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})

# LaTeX command → Unicode symbol, used for $...$ inline math spans.
_LATEX_SYMS: dict[str, str] = {
    # Arrows
    r"\rightarrow": "→", r"\to": "→",
    r"\leftarrow": "←", r"\gets": "←",
    r"\leftrightarrow": "↔",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔",
    r"\uparrow": "↑", r"\Uparrow": "⇑",
    r"\downarrow": "↓", r"\Downarrow": "⇓",
    r"\nearrow": "↗", r"\searrow": "↘",
    r"\nwarrow": "↖", r"\swarrow": "↙",
    r"\mapsto": "↦",
    # Relations
    r"\neq": "≠", r"\ne": "≠",
    r"\leq": "≤", r"\le": "≤",
    r"\geq": "≥", r"\ge": "≥",
    r"\approx": "≈", r"\equiv": "≡",
    r"\sim": "∼", r"\simeq": "≃",
    r"\propto": "∝", r"\cong": "≅",
    r"\ll": "≪", r"\gg": "≫",
    # Arithmetic / operators
    r"\pm": "±", r"\mp": "∓",
    r"\times": "×", r"\div": "÷",
    r"\cdot": "·", r"\circ": "∘",
    r"\oplus": "⊕", r"\otimes": "⊗",
    # Set / logic
    r"\in": "∈", r"\notin": "∉",
    r"\subset": "⊂", r"\supset": "⊃",
    r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\cup": "∪", r"\cap": "∩",
    r"\emptyset": "∅", r"\varnothing": "∅",
    r"\forall": "∀", r"\exists": "∃",
    r"\neg": "¬", r"\lnot": "¬",
    r"\land": "∧", r"\lor": "∨",
    # Calculus / analysis
    r"\infty": "∞",
    r"\nabla": "∇", r"\partial": "∂",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫",
    r"\sqrt": "√",
    r"\ldots": "…", r"\cdots": "⋯", r"\vdots": "⋮",
    # Greek lowercase
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\epsilon": "ε", r"\varepsilon": "ε",
    r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ", r"\vartheta": "ϑ",
    r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ",
    r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\pi": "π", r"\varpi": "ϖ",
    r"\rho": "ρ", r"\sigma": "σ", r"\varsigma": "ς",
    r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ", r"\varphi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    # Greek uppercase
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ",
    r"\Lambda": "Λ", r"\Xi": "Ξ", r"\Pi": "Π",
    r"\Sigma": "Σ", r"\Upsilon": "Υ", r"\Phi": "Φ",
    r"\Psi": "Ψ", r"\Omega": "Ω",
    # Misc
    r"\hbar": "ℏ", r"\ell": "ℓ",
    r"\dag": "†", r"\ddag": "‡",
    r"\bullet": "•", r"\star": "★",
}


# ── Inline formatting ─────────────────────────────────────────────────────────

def _inline_html(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links) to HTML.
    Input must NOT be HTML-escaped yet — this function escapes it first.
    """
    t = text.translate(_MD_ESC)
    # Inline LaTeX math: $...$  →  unicode symbols in a subtle italic span.
    # Processed before bold/italic so that _ inside math doesn't trigger italic.
    def _latex_math(m: re.Match) -> str:
        inner = m.group(1)
        def _sub_cmd(cm: re.Match) -> str:
            return _LATEX_SYMS.get(cm.group(0), cm.group(0))
        inner = re.sub(r'\\[A-Za-z]+', _sub_cmd, inner)
        return f'<span style="color:#c8a8e8;font-style:italic;">{inner}</span>'
    t = re.sub(r'\$([^$\n]+)\$', _latex_math, t)
    # Bold+italic combined
    t = re.sub(r'\*\*\*(.*?)\*\*\*', r'<b><i>\1</i></b>', t)
    # Bold
    t = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'__(.*?)__',     r'<b>\1</b>', t)
    # Italic
    t = re.sub(r'\*(.*?)\*',     r'<i>\1</i>', t)
    t = re.sub(r'\b_(.*?)_\b',   r'<i>\1</i>', t)
    # Inline code — file paths become Ctrl+clickable links; other code stays as styled spans
    _FILE_PATH_RE = re.compile(
        r'^[a-zA-Z0-9_./ \\:@-]*[a-zA-Z0-9_-]+'
        r'\.(py|cpp|c|h|hpp|cs|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|txt|md|sh|bat|ps1|go|rs|java|rb|lua|html|css|sql)'
        r'(?::(\d+))?$'
    )
    def _code_span(m):
        inner = m.group(1)
        if _FILE_PATH_RE.match(inner.strip()):
            safe = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return (
                f'<a href="eli://open/{inner}" style="background:#0d0d1f;color:#7dd3fc;'
                f'font-family:Consolas,monospace;padding:0 3px;text-decoration:none;">'
                f'{safe}</a>'
            )
        return (
            f'<span style="background:#0d0d1f;color:#ffcc66;'
            f'font-family:Consolas,monospace;padding:0 3px;">'
            f'{inner}</span>'
        )
    t = re.sub(r'`([^`]+)`', _code_span, t)
    # Links → show text only (QTextBrowser handles href separately)
    t = re.sub(r'\[([^\]]+)\]\([^)]*\)',
               r'<span style="color:#6699cc;text-decoration:underline;">\1</span>', t)
    return t


# ── Diff block ────────────────────────────────────────────────────────────────

def _looks_like_diff(code: str) -> bool:
    lines = code.splitlines()
    return (any(l.startswith("---") for l in lines)
            and any(l.startswith("+++") for l in lines)
            and any(l.startswith("@@")  for l in lines))


def _diff_block_html(code: str) -> str:
    """Render a unified diff with coloured rows, line numbers, and syntax-highlighted content."""
    # Detect underlying language from the +++ filename line
    src_lang = "plain"
    for line in code.splitlines():
        if line.startswith("+++ "):
            filename = re.split(r"[\s/]", line[4:])[-1].split("\t")[0]
            src_lang = detect_language(filename)
            break

    _HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    # Row styles: (bg, glyph-color, glyph)
    _STYLES = {
        "+":   ("background:#0a2210;", "#33bb33", "+"),
        "-":   ("background:#220a0a;", "#cc3333", "-"),
        "@@":  ("background:#0a1828;", "#6699cc", "~"),
        "hdr": ("background:#0d0d1a;", "#555577", " "),
        " ":   ("background:#090914;", "#444466", " "),
    }

    _NUM_STYLE = (
        "color:#445566;padding:0 6px;text-align:right;white-space:pre;"
        "vertical-align:top;user-select:none;min-width:32px;"
    )
    _GLYPH_STYLE = (
        "padding:0 5px;white-space:pre;vertical-align:top;user-select:none;"
    )

    old_num = 0
    new_num = 0

    rows = []
    for raw in code.splitlines():
        if not raw:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            bg, gc, glyph = _STYLES["hdr"]
            content_html = f'<span style="color:#666688;">{raw.translate(_MD_ESC)}</span>'
            num_cell = ""
        elif raw.startswith("@@"):
            bg, gc, glyph = _STYLES["@@"]
            content_html = f'<span style="color:#6699cc;">{raw.translate(_MD_ESC)}</span>'
            m = _HUNK_RE.match(raw)
            if m:
                old_num = int(m.group(1))
                new_num = int(m.group(2))
            num_cell = "···"
        elif raw.startswith("+"):
            bg, gc, glyph = _STYLES["+"]
            content_html = highlight_code_html(raw[1:], src_lang)
            num_cell = str(new_num)
            new_num += 1
        elif raw.startswith("-"):
            bg, gc, glyph = _STYLES["-"]
            content_html = highlight_code_html(raw[1:], src_lang)
            num_cell = str(old_num)
            old_num += 1
        elif raw.startswith("\\"):
            bg, gc, glyph = _STYLES[" "]
            content_html = f'<span style="color:#555566;">{raw.translate(_MD_ESC)}</span>'
            num_cell = ""
        else:
            bg, gc, glyph = _STYLES[" "]
            content = raw[1:] if raw.startswith(" ") else raw
            content_html = highlight_code_html(content, src_lang)
            num_cell = str(new_num)
            old_num += 1
            new_num += 1

        rows.append(
            f'<tr style="{bg}">'
            f'<td style="{_NUM_STYLE}">{num_cell}</td>'
            f'<td style="color:{gc};{_GLYPH_STYLE}">{glyph}</td>'
            f'<td style="white-space:pre-wrap;padding:0;word-wrap:break-word;">{content_html}</td>'
            f'</tr>'
        )

    inner = (
        f'<table style="border-spacing:0;border-collapse:collapse;width:100%;">'
        + "".join(rows)
        + "</table>"
    )
    return (
        f'<div style="background:#090914;border-left:3px solid #334455;'
        f'padding:4px 0;margin:4px 0;font-family:Consolas,monospace;font-size:12px;">'
        f'{inner}</div>'
    )


# ── Code block ────────────────────────────────────────────────────────────────

def _code_block_html(lang: str, code: str) -> str:
    """Render a fenced code block as a numbered-line HTML table."""
    lang = _LANG_ALIAS.get(lang, lang) or "plain"

    if lang in ("markdown", "md"):
        return _markdown_to_html(code)

    if lang in ("diff", "patch") or _looks_like_diff(code):
        return _diff_block_html(code)

    code_lines = highlight_code_html(code, lang).split("<br>")
    num_w = len(str(len(code_lines)))
    rows = "".join(
        f'<tr>'
        f'<td style="color:#444466;text-align:right;padding:0 8px 0 6px;'
        f'white-space:pre;vertical-align:top;">{str(i).rjust(num_w)}</td>'
        f'<td style="white-space:pre-wrap;padding:0;word-wrap:break-word;">{line_html}</td>'
        f'</tr>'
        for i, line_html in enumerate(code_lines, 1)
    )
    inner = (
        f'<table style="border-spacing:0;border-collapse:collapse;width:100%;">'
        f'{rows}</table>'
    )
    return (
        f'<div style="background:#0d0d1f;border-left:3px solid #334466;'
        f'padding:4px 0;margin:4px 0;font-family:Consolas,monospace;'
        f'font-size:12px;">{inner}</div>'
    )


# ── Tables ────────────────────────────────────────────────────────────────────

def _cell_align(raw: str) -> str:
    """Guess alignment from a raw (unstripped) cell string."""
    stripped = raw.strip()
    if not stripped:
        return "left"
    lead  = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    if lead > 1 and trail > 1 and abs(lead - trail) <= 2:
        return "center"
    if lead > trail:
        return "right"
    return "left"


def _gfm_sep_aligns(sep_line: str) -> list[str]:
    """Extract per-column alignment from a GFM separator row."""
    cells = [c.strip() for c in sep_line.split("|")[1:-1]]
    result = []
    for c in cells:
        if c.startswith(":") and c.endswith(":"):
            result.append("center")
        elif c.endswith(":"):
            result.append("right")
        else:
            result.append("left")
    return result


def _table_html(lines: list[str], footer_rows: set[int] | None = None) -> str:
    """Render pipe-table rows as HTML. Handles both GFM and box-table row lists."""
    footer_rows = footer_rows or set()
    sep_re = re.compile(r'^[\s|:\-]+$')

    def parse_cells(line: str) -> tuple[list[str], list[str]]:
        parts = line.split("|")[1:-1]
        return [p.strip() for p in parts], parts

    aligns: list[str] = []
    for line in lines:
        if sep_re.match(line) and re.search(r'-', line):
            aligns = _gfm_sep_aligns(line)
            break

    data = [(i, l) for i, l in enumerate(lines) if not sep_re.match(l)]
    if not data:
        return ""

    _, header_line = data[0]
    header_cells, header_raw = parse_cells(header_line)

    if not aligns:
        aligns = [_cell_align(r) for r in header_raw]

    th = "".join(
        f'<th style="padding:5px 12px;border-bottom:2px solid #334466;'
        f'border-right:1px solid #1e2a40;color:#7aafdd;background:#080818;'
        f'text-align:{aligns[ci] if ci < len(aligns) else "left"};font-weight:bold;'
        f'word-wrap:break-word;">'
        f'{_inline_html(h)}</th>'
        for ci, h in enumerate(header_cells)
    )

    trs = []
    for li, (orig_i, row_line) in enumerate(data[1:]):
        cells, raw = parse_cells(row_line)
        is_footer = orig_i in footer_rows
        bg = "#0e0e22" if is_footer else ("#07071a" if li % 2 == 0 else "#0a0a1e")
        color = "color:#aaccee;font-weight:bold;" if is_footer else ""
        tds = "".join(
            f'<td style="padding:4px 12px;border-bottom:1px solid #141428;'
            f'border-right:1px solid #141428;background:{bg};{color}'
            f'text-align:{aligns[ci] if ci < len(aligns) else "left"};word-wrap:break-word;">'
            f'{_inline_html(c)}</td>'
            for ci, c in enumerate(cells)
        )
        trs.append(f"<tr>{tds}</tr>")

    return (
        f'<table style="border-collapse:collapse;margin:6px 0;font-size:12px;'
        f'border:1px solid #1e2a40;width:100%;table-layout:fixed;">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table>'
    )


def _box_table_html(lines: list[str]) -> str:
    """Render an ASCII box table (+---+---+ style) as a styled HTML table."""
    sep_re  = re.compile(r'^\+[-+]+\+\s*$')
    data_re = re.compile(r'^\|.*\|\s*$')

    segments: list[list[str]] = []
    current:  list[str]       = []
    for line in lines:
        if sep_re.match(line):
            if current:
                segments.append(current)
                current = []
        elif data_re.match(line):
            current.append(line)
    if current:
        segments.append(current)

    if not segments:
        return ""

    all_data_lines: list[str] = []
    footer_set: set[int] = set()
    offset = 0
    for si, seg in enumerate(segments):
        for row in seg:
            if si > 0 and si == len(segments) - 1 and len(segments) > 2:
                footer_set.add(offset)
            all_data_lines.append(row)
            offset += 1

    return _table_html(all_data_lines, footer_set)


# ── Panel (box-drawing) ───────────────────────────────────────────────────────

def _panel_html(box_lines: list[str]) -> str:
    """Render a ╭─ Title ─╮ / │ content │ / ╰──╯ box as a styled HTML panel."""
    m = re.search(r"─ (.+?) ─", box_lines[0])
    title = m.group(1).strip() if m else ""

    raw: list[str] = []
    for line in box_lines[1:-1]:
        s = line.strip()
        if s.startswith("│"):
            s = s[1:]
        if s.endswith("│"):
            s = s[:-1]
        raw.append(s.strip())

    _ENTRY_RE = re.compile(r"^(\w+\(|/\w+)")
    entries: list[str] = []
    cur = ""
    for line in raw:
        if _ENTRY_RE.match(line) and cur:
            entries.append(cur)
            cur = line
        else:
            cur = (cur + " " + line).strip() if cur else line
    if cur:
        entries.append(cur)

    rows: list[str] = []
    for entry in entries:
        if " — " in entry:
            sig, desc = entry.split(" — ", 1)
        else:
            sig, desc = entry, ""

        if sig.startswith("/"):
            sig_html = (
                f'<span style="color:#ffcc66;font-weight:bold;">'
                f'{sig.translate(_MD_ESC)}</span>'
            )
        else:
            pm = re.match(r"^(\w+)(\(.*\))$", sig)
            if pm:
                sig_html = (
                    f'<span style="color:#6699cc;font-weight:bold;">'
                    f'{pm.group(1).translate(_MD_ESC)}</span>'
                    f'<span style="color:#5577aa;">'
                    f'{pm.group(2).translate(_MD_ESC)}</span>'
                )
            else:
                sig_html = (
                    f'<span style="color:#6699cc;font-weight:bold;">'
                    f'{sig.translate(_MD_ESC)}</span>'
                )

        desc_safe = desc.translate(_MD_ESC)
        desc_safe = re.sub(
            r"· triggers: (.+)",
            lambda tm: (
                '<span style="color:#445566;"> · triggers: </span>'
                + " ".join(
                    f'<span style="background:#12122a;color:#7799aa;'
                    f'padding:0 4px;border-radius:2px;">{t.strip()}</span>'
                    for t in tm.group(1).split(",")
                )
            ),
            desc_safe,
        )

        sep_html = ' <span style="color:#2a3a55;font-weight:bold;">—</span> ' if desc else ""
        rows.append(
            f'<div style="padding:4px 12px;border-bottom:1px solid #0e0e20;">'
            f'{sig_html}{sep_html}'
            f'<span style="color:#888899;">{desc_safe}</span>'
            f'</div>'
        )

    return (
        f'<div style="border:1px solid #1e2e4a;border-radius:3px;margin:6px 0;'
        f'font-family:Consolas,monospace;font-size:12px;">'
        f'<div style="background:#0a1828;color:#7ec8e3;font-weight:bold;'
        f'padding:5px 12px;border-bottom:1px solid #1e2e4a;">'
        f'{title.translate(_MD_ESC)}</div>'
        f'<div style="background:#070710;">{"".join(rows)}</div>'
        f'</div>'
    )


# ── Prose ─────────────────────────────────────────────────────────────────────

def _prose_to_html(text: str) -> str:
    """Convert a prose markdown segment to HTML (no fenced code, headings, lists)."""
    lines = text.split("\n")
    out: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            if out and not out[-1].endswith("<br>"):
                out.append("<br>")
            i += 1
            continue
        out.append(_inline_html(line.rstrip()) + "<br>")
        i += 1

    return "".join(out)


# ── Top-level entry point ─────────────────────────────────────────────────────

def _markdown_to_html(text: str) -> str:
    """Convert full markdown response to HTML for the chat output window."""
    _FENCE_OPEN  = re.compile(r'^(`{3,})(\w*)\s*$')
    _HEADING     = re.compile(r'^(#{1,4})\s+(.*)')
    _BQ_START    = re.compile(r'^>')
    _UL_START    = re.compile(r'^\s*[-*+]\s')
    _OL_START    = re.compile(r'^\s*\d+(?:\.\d+)*\.\s')
    _OL_HIER     = re.compile(r'^\s*(\d+(?:\.\d+)+)\.\s')
    _BOX_TABLE   = re.compile(r'^\+[-+]+\+')
    _PIPE_TABLE  = re.compile(r'^\s*\|')
    _BOX_START   = re.compile(r'^╭')
    _HR          = re.compile(r'^[-*=_]{3,}\s*$')

    parts: list[str] = []
    lines = text.split("\n")
    i = 0
    prose_buf: list[str] = []

    def _flush_prose() -> None:
        if not prose_buf:
            return
        html = _prose_to_html("\n".join(prose_buf))
        if html and not html.endswith("<br>"):
            html += "<br>"
        parts.append(html)
        prose_buf.clear()

    def _is_prose_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        if stripped.startswith("╭"):
            return False
        if _BOX_TABLE.match(stripped):
            return False
        if _PIPE_TABLE.match(stripped):
            return False
        if _HEADING.match(stripped):
            return False
        if _BQ_START.match(stripped):
            return False
        if _UL_START.match(stripped):
            return False
        if _OL_START.match(stripped):
            return False
        if _HR.match(stripped):
            return False
        return True

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Fenced code block ───────────────────────────────────────────────
        m = _FENCE_OPEN.match(stripped)
        if m:
            _flush_prose()
            lang = m.group(2).lower()
            close_re = re.compile(r'^`{' + str(len(m.group(1))) + r',}\s*$')
            i += 1
            code_buf: list[str] = []
            while i < len(lines):
                if close_re.match(lines[i].strip()):
                    i += 1
                    break
                code_buf.append(lines[i])
                i += 1
            code = "\n".join(code_buf).rstrip("\n")
            parts.append(_code_block_html(lang, code))
            parts.append("<br>")
            continue

        # ── Box-drawing panel ───────────────────────────────────────────────
        if stripped.startswith("╭"):
            _flush_prose()
            box: list[str] = [line]
            i += 1
            while i < len(lines):
                box.append(lines[i])
                if lines[i].startswith("╰"):
                    break
                i += 1
            parts.append(_panel_html(box))
            parts.append("<br>")
            continue

        # ── ASCII box table ─────────────────────────────────────────────────
        if _BOX_TABLE.match(stripped):
            _flush_prose()
            tbl: list[str] = []
            while i < len(lines) and re.match(r'^[+|]', lines[i]):
                tbl.append(lines[i])
                i += 1
            parts.append(_box_table_html(tbl))
            parts.append("<br>")
            continue

        # ── GFM pipe table ──────────────────────────────────────────────────
        if _PIPE_TABLE.match(stripped):
            _flush_prose()
            tbl = []
            while i < len(lines) and "|" in lines[i]:
                tbl.append(lines[i])
                i += 1
            parts.append(_table_html(tbl))
            parts.append("<br>")
            continue

        # ── Heading ─────────────────────────────────────────────────────────
        hm = _HEADING.match(stripped)
        if hm:
            _flush_prose()
            level = len(hm.group(1))
            content = _inline_html(hm.group(2))
            sizes  = {1: "15px", 2: "14px", 3: "13px", 4: "12px"}
            colors = {1: "#88bbee", 2: "#7799cc", 3: "#6688bb", 4: "#557799"}
            border = ("border-bottom:1px solid #223344;padding-bottom:3px;margin:8px 0 4px 0;"
                      if level <= 2 else "margin:5px 0 2px 0;")
            parts.append(
                f'<div style="font-size:{sizes[level]};font-weight:bold;'
                f'color:{colors[level]};{border}">{content}</div><br>'
            )
            i += 1
            continue

        # ── Blockquote ──────────────────────────────────────────────────────
        if _BQ_START.match(stripped):
            _flush_prose()
            bq: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                bq.append(lines[i].lstrip(">").lstrip())
                i += 1
            inner = "<br>".join(_inline_html(l) for l in bq)
            parts.append(
                f'<div style="border-left:3px solid #7755aa;padding:2px 8px;'
                f'color:#9977cc;margin:3px 0;">{inner}</div><br>'
            )
            continue

        # ── List (ordered / unordered, nested, mixed) ───────────────────────
        if _UL_START.match(stripped) or _OL_START.match(stripped):
            _flush_prose()
            stack: list[tuple[int, str, int]] = []

            while i < len(lines):
                raw = lines[i]
                lstripped = raw.lstrip()
                abs_indent = len(raw) - len(lstripped)
                is_ul = bool(_UL_START.match(raw))
                is_ol = bool(_OL_START.match(raw))

                if not lstripped:
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and (_UL_START.match(lines[j]) or _OL_START.match(lines[j])):
                        i += 1
                        continue
                    else:
                        break

                if not (is_ul or is_ol):
                    break

                hier_m = _OL_HIER.match(raw) if is_ol else None
                if hier_m:
                    components = hier_m.group(1).split('.')
                    depth = len(components) - 1
                    marker = f"{components[-1]}."
                    content = re.sub(r'^\s*[\d.]+\.\s+', '', raw)
                else:
                    cur_type = "ul" if is_ul else "ol"

                    if not stack:
                        stack.append((abs_indent, cur_type, 0))
                    else:
                        top_indent = stack[-1][0]
                        if abs_indent > top_indent:
                            stack.append((abs_indent, cur_type, 0))
                        elif abs_indent < top_indent:
                            while len(stack) > 1 and stack[-1][0] > abs_indent:
                                stack.pop()
                            if stack[-1][0] != abs_indent:
                                stack[-1] = (abs_indent, cur_type, stack[-1][2])
                        if stack[-1][1] != cur_type:
                            stack[-1] = (stack[-1][0], cur_type, 0)

                    depth = len(stack) - 1
                    s_indent, s_type, s_counter = stack[-1]
                    if s_type == "ol":
                        s_counter += 1
                        stack[-1] = (s_indent, s_type, s_counter)
                        marker = f"{s_counter}."
                    else:
                        marker = "•"

                    if is_ul:
                        content = re.sub(r'^\s*[-*+]\s+', "", raw)
                    else:
                        content = re.sub(r'^\s*\d+\.\s+', "", raw)

                pad = '&nbsp;' * (2 + depth * 4)
                parts.append(
                    f'<span style="color:#888888;">{pad}{marker}&nbsp;&nbsp;</span>'
                    f'{_inline_html(content)}<br>'
                )
                i += 1

            continue

        # ── Horizontal rule ─────────────────────────────────────────────────
        if _HR.match(stripped):
            _flush_prose()
            parts.append('<hr style="border:none;border-top:1px solid #334466;margin:6px 0;"><br>')
            i += 1
            continue

        # ── Empty line ──────────────────────────────────────────────────────
        if not stripped:
            prose_buf.append("")
            i += 1
            continue

        # ── Prose line ──────────────────────────────────────────────────────
        prose_buf.append(line)
        i += 1

    _flush_prose()
    return "".join(parts)
