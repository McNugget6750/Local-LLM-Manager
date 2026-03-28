"""Validates and auto-repairs malformed tool call JSON from the LLM."""

import json
import re


def check_and_fix(raw: str) -> tuple[dict | None, str | None]:
    """
    Try to parse raw as a tool call JSON dict.
    Apply progressive fixes if initial parse fails.
    Returns (dict, None) on success, (None, error_message) on failure.
    """
    # Apply key renames before parsing (string-level)
    raw = _rename_keys(raw)

    for fix in (_no_fix, _fix_trailing_commas, _fix_single_quotes,
                _fix_truncated_string, _fix_unclosed_braces,
                _fix_truncated_string_then_braces):
        candidate = fix(raw)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, None
        except (json.JSONDecodeError, ValueError):
            continue

    return None, f"Could not parse tool call JSON after all fix attempts. Raw: {raw[:200]}"


# ── Fix helpers ───────────────────────────────────────────────────────────────

def _no_fix(raw: str) -> str:
    return raw


def _rename_keys(raw: str) -> str:
    """Rename common field name mistakes before parsing."""
    raw = re.sub(r'"tool"\s*:', '"name":', raw)
    raw = re.sub(r'"input"\s*:', '"parameters":', raw)
    return raw


def _fix_trailing_commas(raw: str) -> str:
    """Remove trailing commas before } or ]."""
    return re.sub(r',\s*([}\]])', r'\1', raw)


def _fix_single_quotes(raw: str) -> str:
    """Replace single-quoted strings with double-quoted."""
    return raw.replace("'", '"')


def _fix_truncated_string(raw: str) -> str:
    """Close an unclosed string at the end of the input."""
    stripped = raw.rstrip()
    unescaped = re.sub(r'\\.', '', stripped)
    if unescaped.count('"') % 2 != 0:
        stripped += '"'
    return stripped


def _fix_truncated_string_then_braces(raw: str) -> str:
    """Close an unclosed string then close unclosed braces/brackets."""
    return _fix_unclosed_braces(_fix_truncated_string(raw))


def _fix_unclosed_braces(raw: str) -> str:
    """Append missing closing braces and brackets."""
    stack = []
    closer = {'{': '}', '[': ']'}
    in_string = False
    prev = ''
    for ch in raw:
        if ch == '"' and prev != '\\':
            in_string = not in_string
        if not in_string:
            if ch in ('{', '['):
                stack.append(closer[ch])
            elif ch in ('}', ']') and stack and stack[-1] == ch:
                stack.pop()
        prev = ch
    return raw + ''.join(reversed(stack))
