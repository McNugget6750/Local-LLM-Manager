"""
sanity_detector.py — Stream-time LLM degenerate output detector.

Detects six failure modes in real time as tokens arrive:
  D1   Same completed line repeated consecutively
  D1w  Same word repeated consecutively within a line (e.g. "apple apple apple...")
  D2   Cycling block of lines (N-line pattern repeating)
  D2n  Cycling block detected on *normalized* lines — catches numbered-list loops
       where incrementing counters (1702., 1706., 1710.) defeat exact D2 matching
  D3   Single character flood within one line
  D4   Inline phrase loop (short phrase repeating within one line, no newlines)

Think tokens use looser thresholds (thinking is naturally more repetitive).
Text tokens use strict thresholds.

Usage:
    detector = SanityDetector()
    for token in stream:
        trigger = detector.feed(token, mode="text")  # or mode="think"
        if trigger:
            raise SanityError(trigger)
    detector.reset()  # between turns
"""

import re
from collections import deque

# Pre-compiled patterns for _normalize_line
_RE_NUMBERED  = re.compile(r'^\d+[.)]\s*')   # "1702. " or "1702) "
_RE_BULLET    = re.compile(r'^[-*+]\s*')      # "- " or "* "
_RE_BOLD_ITAL = re.compile(r'\*{1,3}([^*]+)\*{1,3}')  # **bold**, *italic*, ***both***
_RE_SPACES    = re.compile(r'\s+')

# ── Thresholds ────────────────────────────────────────────────────────────────

# Text mode (strict)
SD_SAME_LINE_REPEATS   = 5      # D1: identical lines in a row
SD_CYCLE_BLOCK_MAX     = 30     # D2/D2n: max lines in a detectable cycling block
SD_CYCLE_REPETITIONS   = 4      # D2: full cycles before trigger
SD_CHAR_FLOOD_MIN      = 150    # D3: same non-whitespace char run length
SD_INLINE_PHRASE_MIN   = 2      # D4: min words in a repeating phrase
SD_INLINE_PHRASE_MAX   = 8      # D4: max words in a repeating phrase
SD_INLINE_REPETITIONS  = 8      # D4: repetitions before trigger
SD_INLINE_MIN_LEN      = 120    # D4: min line length before checking
SD_WORD_REPEATS        = 10    # D1w: same word repeated N times in a row

# Think mode (looser)
SD_THINK_SAME_LINE_REPEATS  = 8
SD_THINK_CYCLE_REPETITIONS  = 6
SD_THINK_CHAR_FLOOD_MIN     = 150   # same — no legitimate reason for char floods
SD_THINK_INLINE_REPETITIONS = 12
SD_THINK_WORD_REPEATS       = 15   # D1w: same word N times in think mode

# History window for D2
_HISTORY_SIZE = 200


class SanityError(Exception):
    """Raised when a degenerate output pattern is detected in the stream."""
    pass


class SanityDetector:
    """
    Feed tokens from the LLM stream one at a time.
    Call reset() between turns.
    """

    def __init__(self):
        self._line_buf: str = ""
        self._recent_lines: deque = deque(maxlen=_HISTORY_SIZE)
        self._recent_lines_norm: deque = deque(maxlen=_HISTORY_SIZE)  # D2n normalized history
        self._same_line_window: deque = deque()
        self._word_buf: str = ""          # current word being accumulated
        self._word_window: deque = deque()  # last N completed words

    def reset(self):
        self._line_buf = ""
        self._recent_lines.clear()
        self._recent_lines_norm.clear()
        self._same_line_window.clear()
        self._word_buf = ""
        self._word_window.clear()

    def feed(self, token: str, mode: str = "text") -> str | None:
        """
        Feed one token. mode is "text" or "think".
        Returns a trigger string on detection, None if healthy.
        """
        thresholds = _think_thresholds if mode == "think" else _text_thresholds

        # Accumulate into line buffer
        self._line_buf += token

        # D1w — word-level consecutive repeat (catches "apple apple apple...")
        for ch in token:
            if ch.isspace() or ch in ".,;:!?":
                word = self._word_buf.strip().lower()
                if word:
                    trigger = _check_d1w(word, self._word_window, thresholds)
                    if trigger:
                        return trigger
                self._word_buf = ""
            else:
                self._word_buf += ch

        # Check D3 and D4 on current line buffer
        trigger = (
            _check_d3(self._line_buf, thresholds) or
            _check_d4(self._line_buf, thresholds)
        )
        if trigger:
            return trigger

        # On newline: flush line, check D1 and D2
        if "\n" in self._line_buf:
            parts = self._line_buf.split("\n")
            # All but the last part are completed lines
            completed = parts[:-1]
            self._line_buf = parts[-1]

            for line in completed:
                line = line.strip()
                if not line:
                    continue  # skip blank lines

                # D1 — same line consecutive repeat
                trigger = _check_d1(line, self._same_line_window, thresholds)
                if trigger:
                    return trigger

                # D2 — cycling block (exact)
                self._recent_lines.append(line)
                trigger = _check_d2(self._recent_lines, thresholds)
                if trigger:
                    return trigger

                # D2n — cycling block on normalized lines (catches numbered-list loops
                # where incrementing counters defeat exact D2 matching)
                norm = _normalize_line(line)
                if norm:
                    self._recent_lines_norm.append(norm)
                    trigger = _check_d2(self._recent_lines_norm, thresholds)
                    if trigger:
                        return "D2n:normalized-cycling-block"

        return None


# ── Threshold dicts ───────────────────────────────────────────────────────────

_text_thresholds = {
    "same_line_repeats":  SD_SAME_LINE_REPEATS,
    "cycle_block_max":    SD_CYCLE_BLOCK_MAX,
    "cycle_repetitions":  SD_CYCLE_REPETITIONS,
    "char_flood_min":     SD_CHAR_FLOOD_MIN,
    "inline_phrase_min":  SD_INLINE_PHRASE_MIN,
    "inline_phrase_max":  SD_INLINE_PHRASE_MAX,
    "inline_repetitions": SD_INLINE_REPETITIONS,
    "inline_min_len":     SD_INLINE_MIN_LEN,
    "word_repeats":       SD_WORD_REPEATS,
}

_think_thresholds = {
    "same_line_repeats":  SD_THINK_SAME_LINE_REPEATS,
    "cycle_block_max":    SD_CYCLE_BLOCK_MAX,
    "cycle_repetitions":  SD_THINK_CYCLE_REPETITIONS,
    "char_flood_min":     SD_THINK_CHAR_FLOOD_MIN,
    "inline_phrase_min":  SD_INLINE_PHRASE_MIN,
    "inline_phrase_max":  SD_INLINE_PHRASE_MAX,
    "inline_repetitions": SD_THINK_INLINE_REPETITIONS,
    "inline_min_len":     SD_INLINE_MIN_LEN,
    "word_repeats":       SD_THINK_WORD_REPEATS,
}


# ── Detector functions ────────────────────────────────────────────────────────

def _normalize_line(line: str) -> str:
    """
    Strip numeric list prefixes, bullets, and markdown formatting so that
    lines differing only by an incrementing counter compare as equal.
    E.g. "1702. **Reading foo.md**" → "reading foo.md"
    """
    s = line.strip()
    s = _RE_NUMBERED.sub('', s)       # strip "1702. "
    s = _RE_BULLET.sub('', s)         # strip "- " / "* "
    s = _RE_BOLD_ITAL.sub(r'\1', s)   # strip **bold** / *italic*
    s = _RE_SPACES.sub(' ', s).strip().lower()
    return s


def _check_d1w(word: str, window: deque, t: dict) -> str | None:
    """D1w: same word repeated consecutively N times (e.g. apple apple apple...)."""
    n = t["word_repeats"]
    window.append(word)
    while len(window) > n:
        window.popleft()
    if len(window) == n and len(set(window)) == 1:
        return "D1w:word-repeat"
    return None


def _check_d1(line: str, window: deque, t: dict) -> str | None:
    """D1: same completed line repeated consecutively."""
    n = t["same_line_repeats"]
    window.append(line)
    # Keep window trimmed to n entries
    while len(window) > n:
        window.popleft()
    if len(window) == n and len(set(window)) == 1:
        return "D1:same-line-repeat"
    return None


def _check_d2(history: deque, t: dict) -> str | None:
    """D2: cycling block of lines."""
    lines = list(history)
    reps = t["cycle_repetitions"]
    for block_size in range(2, t["cycle_block_max"] + 1):
        needed = block_size * reps
        if len(lines) < needed:
            continue
        tail = lines[-needed:]
        chunks = [tuple(tail[i * block_size:(i + 1) * block_size]) for i in range(reps)]
        if len(set(chunks)) == 1:
            return "D2:cycling-block"
    return None


_re_char_flood_cache: dict[int, re.Pattern] = {}

def _check_d3(line_buf: str, t: dict) -> str | None:
    """D3: single non-whitespace character flood on current line."""
    n = t["char_flood_min"]
    if len(line_buf) < n:
        return None
    if n not in _re_char_flood_cache:
        _re_char_flood_cache[n] = re.compile(r'([^\s])\1{' + str(n - 1) + r',}')
    if _re_char_flood_cache[n].search(line_buf):
        return "D3:char-flood"
    return None


def _check_d4(line_buf: str, t: dict) -> str | None:
    """D4: inline phrase loop — short phrase repeating within one line."""
    if len(line_buf) < t["inline_min_len"]:
        return None
    words = line_buf.split()
    reps = t["inline_repetitions"]
    for phrase_len in range(t["inline_phrase_min"], t["inline_phrase_max"] + 1):
        needed = phrase_len * reps
        if len(words) < needed:
            continue
        tail = words[-needed:]
        chunks = [tuple(tail[i * phrase_len:(i + 1) * phrase_len]) for i in range(reps)]
        if len(set(chunks)) == 1:
            return "D4:inline-phrase-loop"
    return None
