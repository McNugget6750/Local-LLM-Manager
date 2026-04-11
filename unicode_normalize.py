"""
unicode_normalize.py — Unicode homoglyph normalization for tool arguments.

Local models occasionally emit Cyrillic or Greek characters that are visually
indistinguishable from ASCII (e.g. Cyrillic с U+0441 vs ASCII c U+0063).
This module provides normalize_tool_args() which should be called on every
parsed tool-argument dict before dispatch.
"""
import re
import unicodedata

# Zero-width and soft-hyphen characters that add no visible content.
_ZWS = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

# Mapping of common single-script homoglyphs to their ASCII equivalents.
# Covers the Cyrillic and Greek characters most frequently confused with ASCII
# by local LLMs. NFKC normalization handles compatibility equivalents but does
# not remap these cross-script lookalikes, so we handle them explicitly.
_CONFUSABLES = str.maketrans({
    # Cyrillic lowercase
    "\u0430": "a",   # а → a
    "\u0435": "e",   # е → e
    "\u043e": "o",   # о → o
    "\u0440": "p",   # р → p
    "\u0441": "c",   # с → c
    "\u0445": "x",   # х → x
    "\u0456": "i",   # і → i
    # Cyrillic uppercase
    "\u0410": "A",   # А → A
    "\u0412": "B",   # В → B
    "\u0415": "E",   # Е → E
    "\u041a": "K",   # К → K
    "\u041c": "M",   # М → M
    "\u041d": "H",   # Н → H
    "\u041e": "O",   # О → O
    "\u0420": "P",   # Р → P
    "\u0421": "C",   # С → C
    "\u0422": "T",   # Т → T
    "\u0425": "X",   # Х → X
    # Greek lowercase
    "\u03b1": "a",   # α → a
    "\u03b5": "e",   # ε → e
    "\u03bf": "o",   # ο → o
    "\u03c1": "p",   # ρ → p
    # Greek uppercase
    "\u0391": "A",   # Α → A
    "\u0395": "E",   # Ε → E
    "\u039f": "O",   # Ο → O
    "\u03a1": "P",   # Ρ → P
})


def _fix_str(v: str) -> str:
    v = unicodedata.normalize("NFKC", v)
    v = v.translate(_CONFUSABLES)
    v = _ZWS.sub("", v)
    return v


def _fix(v):
    if isinstance(v, str):
        return _fix_str(v)
    if isinstance(v, dict):
        return {k: _fix(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_fix(item) for item in v]
    return v


def normalize_tool_args(args: dict) -> dict:
    """Return a copy of args with all string values Unicode-normalized.

    Applies NFKC, a Cyrillic/Greek confusables map, and zero-width char
    stripping to every string value, recursively through nested dicts and lists.
    """
    return {k: _fix(v) for k, v in args.items()}
