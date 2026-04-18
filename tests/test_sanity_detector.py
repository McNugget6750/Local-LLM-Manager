"""Tests for sanity_detector.py — verifies all failure modes and key false-positive guards."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sanity_detector import SanityDetector

EXAMPLE_DEATHS = os.path.join(os.path.dirname(__file__), "..", "..", "Example Deaths")

def _feed_file(path, mode="text"):
    det = SanityDetector()
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # Feed char by char to simulate streaming
    for ch in content:
        result = det.feed(ch, mode=mode)
        if result:
            return result
    return None

def _feed_string(s, mode="text"):
    det = SanityDetector()
    for ch in s:
        result = det.feed(ch, mode=mode)
        if result:
            return result
    return None


# ── Example Death files ───────────────────────────────────────────────────────

def test_word_repeat_d1w():
    """Single word repeated continuously → D1w."""
    text = "apple " * 12
    result = _feed_string(text)
    assert result == "D1w:word-repeat", f"Expected D1w, got: {result}"

def test_word_repeat_no_fp():
    """9 repetitions of a word should NOT trigger (threshold is 10)."""
    text = "apple " * 9
    result = _feed_string(text)
    assert result is None, f"Unexpected trigger at 9 reps: {result}"

def test_word_repeat_natural_prose():
    """Natural prose that repeats common words (the, and, is) must NOT trigger."""
    text = "the cat sat on the mat and the dog sat on the rug and the bird sat on the fence\n"
    result = _feed_string(text)
    assert result is None, f"False positive on common words: {result}"

def test_example1_d1():
    """Same line repeated consecutively → D1."""
    result = _feed_file(os.path.join(EXAMPLE_DEATHS, "Example1.txt"))
    assert result == "D1:same-line-repeat", f"Expected D1, got: {result}"

def test_example2_d2():
    """Cycling 3-line block → D2."""
    result = _feed_file(os.path.join(EXAMPLE_DEATHS, "Example2.txt"))
    assert result == "D2:cycling-block", f"Expected D2, got: {result}"

def test_example3_d3():
    """Hundreds of backticks → D3."""
    result = _feed_file(os.path.join(EXAMPLE_DEATHS, "Example3.txt"))
    assert result == "D3:char-flood", f"Expected D3, got: {result}"

def test_example4_d4():
    """Inline phrase loop → D4."""
    result = _feed_file(os.path.join(EXAMPLE_DEATHS, "Example4.txt"))
    assert result == "D4:inline-phrase-loop", f"Expected D4, got: {result}"


# ── Think-mode: same detectors fire with looser thresholds ───────────────────

def test_think_mode_d1():
    """D1 still fires in think mode, just needs more repetitions."""
    line = "For running agents locally on complex tasks\n"
    text = line * 9  # 9 > SD_THINK_SAME_LINE_REPEATS (8)
    result = _feed_string(text, mode="think")
    assert result == "D1:same-line-repeat", f"Expected D1 in think mode, got: {result}"

def test_think_mode_d1_no_fp():
    """7 repetitions in think mode should NOT trigger (threshold is 8)."""
    line = "For running agents locally on complex tasks\n"
    text = line * 7
    result = _feed_string(text, mode="think")
    assert result is None, f"Unexpected trigger in think mode at 7 reps: {result}"

def test_think_mode_d3():
    """D3 char flood fires in think mode too (same threshold)."""
    text = "`" * 200
    result = _feed_string(text, mode="think")
    assert result == "D3:char-flood", f"Expected D3 in think mode, got: {result}"


# ── False-positive guards ─────────────────────────────────────────────────────

def test_section_divider_no_trigger():
    """Section divider with ~85 ─ chars must NOT trigger D3 or D5.
    D3 needs 150 of the same char. D5 requires ≥2 distinct chars in the pattern,
    so a run of identical ─ chars is correctly ignored."""
    line = "# ── Imports ───────────────────────────────────────────────────────────────────\n"
    assert len([c for c in line if c == "─"]) < 150, "Test line too long"
    result = _feed_string(line)
    assert result is None, f"False positive on section divider: {result}"

def test_code_fence_no_trigger():
    """Triple backtick code fence must NOT trigger."""
    text = "```python\nprint('hello')\n```\n"
    result = _feed_string(text)
    assert result is None, f"False positive on code fence: {result}"

def test_markdown_table_no_trigger():
    """Repeated markdown table rows with varying content must NOT trigger."""
    rows = (
        "| Name | Value | Notes |\n"
        "|------|-------|-------|\n"
        "| foo  | 1     | abc   |\n"
        "| bar  | 2     | def   |\n"
        "| baz  | 3     | ghi   |\n"
        "| qux  | 4     | jkl   |\n"
    )
    result = _feed_string(rows * 3)
    assert result is None, f"False positive on markdown table: {result}"

def test_numbered_list_no_trigger():
    """Numbered list items with varying content must NOT trigger."""
    items = "".join(f"{i}. Item number {i} with some description here\n" for i in range(1, 20))
    result = _feed_string(items)
    assert result is None, f"False positive on numbered list: {result}"

def test_normal_prose_no_trigger():
    """Normal conversational prose must NOT trigger (3 reps, below cycle threshold of 4)."""
    prose = (
        "The server manager exposes a loopback control API on port 1235.\n"
        "Eli uses this to switch models automatically when dispatching agents.\n"
        "The GUI tracks state correctly throughout all model switches.\n"
        "Background agents run in parallel while Eli stays responsive.\n"
        "Results are injected into context and Eli is notified automatically.\n"
    ) * 3
    result = _feed_string(prose)
    assert result is None, f"False positive on normal prose: {result}"

# ── D5: character-level cycle ────────────────────────────────────────────────

def test_d5_quote_dash_cycle():
    """The reported failure: '-'-'-'... slips D3/D4/D1w but D5 catches it."""
    text = "'-'" * 30  # 2-char pattern "'-" repeating, no spaces
    result = _feed_string(text)
    assert result == "D5:char-cycle", f"Expected D5, got: {result}"

def test_d5_brace_cycle():
    """{}{}{}... — punctuation pair with no spaces."""
    text = "{}" * 35
    result = _feed_string(text)
    assert result == "D5:char-cycle", f"Expected D5, got: {result}"

def test_d5_unicode_emoji_alternation():
    """Alternating emoji — multi-codepoint, no spaces, slips D3/D4/D1w."""
    text = "😀😭" * 35
    result = _feed_string(text)
    assert result == "D5:char-cycle", f"Expected D5, got: {result}"

def test_d5_xml_angle_cycle():
    """XML-like degeneracy: ><><><... — 2-char cycle, no spaces."""
    text = "><" * 35
    result = _feed_string(text)
    assert result == "D5:char-cycle", f"Expected D5, got: {result}"

def test_d5_no_fp_short():
    """A short alternating sequence under the min-length gate must NOT trigger."""
    text = "'-'" * 10  # 30 chars, below SD_CHAR_CYCLE_MIN_LEN=60
    result = _feed_string(text)
    assert result is None, f"False positive on short char cycle: {result}"

def test_d5_no_fp_code_fence():
    """Triple backtick still must not trigger (already covered but re-verify with D5 live)."""
    text = "```python\nfor i in range(10):\n    print(i)\n```\n"
    result = _feed_string(text)
    assert result is None, f"False positive on code fence with D5 active: {result}"


# ── D4 extended: inline phrase > 8 words ─────────────────────────────────────

def test_d4_long_phrase_loop():
    """9-word repeating inline phrase — previously slipped D4 when phrase_max was 8."""
    phrase = "the quick brown fox jumps over the lazy dog "  # 9 words
    text = phrase * 20  # 180 words, well over inline_min_len
    result = _feed_string(text)
    assert result == "D4:inline-phrase-loop", f"Expected D4 for 9-word phrase, got: {result}"


def test_reset_clears_state():
    """reset() should clear all state so a new turn starts clean."""
    det = SanityDetector()
    line = "For running agents locally on complex tasks\n"
    # Feed 4 identical lines (below threshold of 5)
    for _ in range(4):
        for ch in line:
            det.feed(ch)
    det.reset()
    # Now feed 4 more — should not trigger since state was cleared
    for _ in range(4):
        for ch in line:
            r = det.feed(ch)
            assert r is None, f"Unexpected trigger after reset: {r}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
