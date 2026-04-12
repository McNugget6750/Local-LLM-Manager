# Plan: LLM Sanity Detector

## Problem

Local LLMs can enter degenerate states mid-generation that produce useless or infinite
output. Four observed failure modes, each requiring a distinct detector:

| ID | Mode | Example |
|----|------|---------|
| D1 | Same line repeated consecutively | "For running agents locally..." × ∞ |
| D2 | Cycling block of lines | 3-line rotation cycling forever |
| D3 | Single-character flood on one line | 500+ backticks (or any char) on one line |
| D4 | Inline phrase loop (no newlines) | "and for the other issue" × 40 on one line |

Observed in practice: almost exclusively during **agent (sub-model) inference**, not
during main Eli turns — but both paths must be protected since Eli can theoretically
fail too.

---

## Design Constraints

- Must not false-positive on legitimate output:
  - Section dividers: `# ── Imports ──────────────────` (~85 `─` chars)
  - Code fences: ` ``` ` (3 backticks)
  - Structured markdown: tables, numbered lists, repeated keys
  - Long prose lines
- Detection must happen **in the stream**, token by token — not post-hoc
- Think tokens are **included** — loops during thinking are common and equally fatal
  (think tokens use looser thresholds since thinking is naturally more repetitive)
- On trigger: cancel the stream cleanly, retry once, abort on second failure
- Thresholds are configurable constants, not magic numbers buried in logic

---

## Thresholds

```python
# Text token thresholds (strict)
SD_SAME_LINE_REPEATS    = 5     # D1
SD_CYCLE_BLOCK_MAX      = 5     # D2: max lines in a detectable block
SD_CYCLE_REPETITIONS    = 4     # D2: full cycles before trigger
SD_CHAR_FLOOD_MIN       = 150   # D3: same char run length
SD_INLINE_PHRASE_WORDS  = 2     # D4: min words in phrase
SD_INLINE_PHRASE_MAX    = 8     # D4: max words in phrase
SD_INLINE_REPETITIONS   = 8     # D4: repetitions before trigger

# Think token thresholds (looser — thinking repeats naturally)
SD_THINK_SAME_LINE_REPEATS   = 8
SD_THINK_CYCLE_REPETITIONS   = 6
SD_THINK_CHAR_FLOOD_MIN      = 150   # same — no legitimate reason for char floods
SD_THINK_INLINE_REPETITIONS  = 12
```

---

## File

New file: `sanity_detector.py` in the project root.
Used by: `chat.py` — wraps the stream loop for both Eli and agent turns.

---

## Class Interface

```python
class SanityDetector:
    def __init__(self): ...

    def feed(self, token: str, mode: str = "text") -> str | None:
        """
        Feed one token from the stream.
        mode: "text" (strict) or "think" (looser thresholds)
        Returns None if healthy.
        Returns trigger string on failure:
          "D1:same-line-repeat"
          "D2:cycling-block"
          "D3:char-flood"
          "D4:inline-phrase-loop"
        """

    def reset(self):
        """Call between turns to clear all buffers."""
```

---

## Detector Implementations

### D1 — Same line repeated consecutively

```
State: deque of last SD_SAME_LINE_REPEATS completed lines

On newline:
  - Flush current line buffer to completed_line
  - Skip if empty or whitespace-only
  - Append to deque
  - If all N entries in deque are identical → trigger D1
```

### D2 — Cycling block

```
State: list of last 30 completed non-empty lines

On newline:
  - Append to history
  - For block_size in range(2, SD_CYCLE_BLOCK_MAX + 1):
      - Need: block_size * SD_CYCLE_REPETITIONS lines in history
      - Extract that many lines, split into SD_CYCLE_REPETITIONS chunks
      - If all chunks are identical → trigger D2
```

### D3 — Single character flood

```
State: current line buffer

After each token append:
  - Run regex: re.search(r'([^\s])\1{N,}', line_buf)
    where N = SD_CHAR_FLOOD_MIN - 1 (non-whitespace chars only)
  - If match → trigger D3
```

### D4 — Inline phrase loop

```
State: current line buffer

After each token append:
  - Only check when line_buf > 120 chars
  - words = line_buf.split()
  - For phrase_len in range(SD_INLINE_PHRASE_WORDS, SD_INLINE_PHRASE_MAX + 1):
      - need = phrase_len * SD_INLINE_REPETITIONS words
      - If len(words) >= need:
          - tail = words[-need:]
          - Split into SD_INLINE_REPETITIONS chunks of phrase_len
          - If all chunks identical → trigger D4
```

---

## Retry / Watchdog Protocol

### Per-turn retry counter

```python
# In ChatSession state:
self._sanity_retry_count: int = 0   # resets to 0 on every user message
```

**Reset rule:** `_sanity_retry_count` is set to `0` every time a user message is
submitted — before `send_and_stream` is called. So each new user turn gets a clean
slate.

### On sanity trigger — first failure

1. Cancel / drain the stream immediately.
2. Discard the partial response (do not append to history).
3. Increment `_sanity_retry_count`.
4. Inject a **system message** into the conversation (not shown to user) before the
   original user message is re-sent:

```
[SANITY CHECK FAILED — RETRY ATTEMPT 1]
Your previous response was aborted by the sanity detector ({trigger_code}).
The output entered a degenerate loop and was discarded.
Please retry the task from scratch. Do not repeat or continue the previous output.
```

5. Re-submit the original user message automatically (no user interaction required).
6. Show a brief notice in the chat window:

```
⚠ Sanity check triggered ({trigger_code}) — retrying automatically…
```

### On sanity trigger — second failure (retry_count >= 2)

1. Cancel / drain the stream.
2. Discard partial response.
3. Do **not** retry again.
4. Display full abort message in chat window (TUI + Qt GUI panel):

```
╭─ ⚠ Sanity Check — Inference Aborted ──────────────────────────────────────╮
│                                                                             │
│  The model entered a degenerate output loop twice in a row.                │
│  Trigger: {trigger_code}                                                    │
│                                                                             │
│  Suggestions:                                                               │
│    • Rephrase your prompt — shorter or more specific often helps           │
│    • Try a different model profile (lower quantisation or smaller context) │
│    • Reduce context window size or clear history with /clear               │
│    • Restart the inference server if the problem persists                  │
│                                                                             │
╰─────────────────────────────────────────────────────────────────────────────╯
```

5. If a Telegram session was active at the time (i.e. the original message came via
   the Telegram bridge), also send the abort message to the Telegram user via the
   backend bridge.

### Agent vs Eli distinction

The same retry protocol applies to both Eli and agents. The key difference is where
the injected system message goes:

- **Eli loop:** injected into `ChatSession.messages` before re-submitting the user
  turn. Eli sees the instruction and retries.
- **Agent loop:** injected as a prefix into the agent's `task` string before re-spawning
  the agent. The agent restarts with the instruction in its context.

The watchdog counter is **per ChatSession turn**, not per agent.

- **Eli:** max **1 retry** (2 total attempts). Second failure → abort.
- **Agent:** max **4 retries** (5 total attempts). Fifth failure → abort.

If an agent exhausts its retries, Eli is informed via the tool result:

```
[SANITY_ABORT] Agent '{name}' was terminated by the sanity detector ({trigger_code})
after 5 attempts. The task has been abandoned. Inform the user and suggest rephrasing
or reconfiguring.
```

---

## Integration Points in chat.py

### 1. Stream wrapping (Eli turn)

```python
detector = SanityDetector()
self._sanity_retry_count = getattr(self, '_sanity_retry_count', 0)

async for event_type, data in stream_events(response, ...):
    mode = "think" if event_type == "think" else "text"
    trigger = detector.feed(data, mode=mode)
    if trigger:
        raise SanityError(trigger)
    # ... existing handling unchanged
```

### 2. Reset on user message

```python
# In send_and_stream, before appending user message:
self._sanity_retry_count = 0
detector.reset()
```

### 3. SanityError handler

```python
except SanityError as e:
    self._sanity_retry_count += 1
    if self._sanity_retry_count < 2:
        # inject system warning, re-submit automatically
        self._inject_sanity_warning(str(e))
        await self.send_and_stream(original_user_text, plan_mode=plan_mode)
    else:
        # abort — show full message, notify Telegram if active
        await self._sanity_abort(str(e))
```

### 4. Agent stream wrapping

In `_run_agent_turn` (or equivalent), same `SanityDetector` instance wraps the agent
stream. On `SanityError`, agent retry counter checked, task string prefixed with
warning, agent re-spawned once. Second failure returns `[SANITY_ABORT]` tool result.

---

## Telegram notification

`ChatSession` already knows if the originating message came via Telegram (the bridge
posts to `localhost:1237/chat`). Add a flag `self._telegram_origin: bool` set when
a message arrives via the bridge endpoint.

On `_sanity_abort`:

```python
if self._telegram_origin:
    await self._notify_telegram_sanity_abort(trigger_code)
```

This posts a short message back through the bridge to the Telegram user's chat.

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `sanity_detector.py` | **CREATE** — `SanityDetector`, `SanityError` |
| `chat.py` | **MODIFY** — wrap stream, add retry logic, Telegram notification flag |

---

## Verification

### Unit tests (`tests/test_sanity_detector.py`)

```python
def test_example1_d1():           # same line repeat → D1
def test_example2_d2():           # cycling block → D2
def test_example3_d3():           # char flood → D3
def test_example4_d4():           # inline phrase loop → D4
def test_think_tokens_d1():       # same loop in think mode → D1 (higher threshold)
def test_think_tokens_no_fp():    # normal verbose thinking → no trigger
def test_section_divider():       # ── chars → no trigger
def test_code_fence():            # ``` → no trigger
def test_table_rows():            # repeated markdown table → no trigger
def test_numbered_list():         # 1. 2. 3. → no trigger
def test_retry_resets_on_user():  # counter resets between user turns
def test_abort_on_second():       # second failure → abort, no third attempt
```

### Manual verification

1. Paste contents of each `Example Deaths/` file as model output — all four should trigger.
2. Paste a normal code file with section dividers — no trigger.
3. Simulate two consecutive failures — verify abort message appears and no third retry.
4. Verify Telegram message is sent if `_telegram_origin` is set.
