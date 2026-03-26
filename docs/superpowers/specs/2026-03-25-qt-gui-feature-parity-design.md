# Qt GUI Feature Parity Design

**Date:** 2026-03-25
**Status:** Draft

---

## Goal

Build a Qt GUI frontend for qwen3-manager that exposes every feature of `chat.py` in a visual interface, while keeping `chat.py` fully operational as a terminal app. Both UIs share the same backend engine — additions to the backend benefit both.

---

## Approved Layout

Five-panel layout (left to right):

| Panel | Contents |
|---|---|
| Left column (160px) | Explorer 60% / Tasks.md live view 40% (stacked vertically) |
| Chat (flex 2.2) | Compact tab / Full Output tab, tool panels, thinking blocks, slash bar, input |
| Editor (flex 1.8) | File viewer with syntax highlighting, Save button |
| Right panel (175px) | Server stats, Config controls |

**Toolbar (ribbon):** server indicator, model dropdown, Think dropdown (off/on/deep), Approval dropdown (auto/ask-writes/ask-all/yolo), Role dropdown, Plan mode toggle, CWD label.

**Menu bar:** File | Sessions | Model | Tools | Skills | Voice | Help

**Status bar:** CWD, think level, approval mode, role | token count, session timestamp

---

## Architecture

### Backend: ChatSession via tui_queue

`chat.py` implements a `tui_queue` protocol at line 1787:

```python
self.tui_queue: asyncio.Queue | None = None  # set by TUI to receive typed events
```

When set, the backend emits typed events instead of printing to the terminal. Full confirmed event protocol:

| Event type | Fields | When emitted |
|---|---|---|
| `think_token` | `text` | Incremental thinking text (streaming) |
| `text_token` | `text` | Incremental response text (streaming) |
| `tool_start` | `id`, `name`, `args` | Immediately before tool execution |
| `tool_done` | `id`, `name`, `result`, `is_error` | After tool execution completes |
| `text_done` | `text` | Full response text at end of streaming turn |
| `usage` | `tokens`, `ctx` | After each turn |
| `system` | `text` | Status/info messages |
| `done` | — | Turn fully complete |
| `error` | `text` | Error text |
| `approval_request` | `title`, `message`, `style`, `future` | When approval is required before a tool fires |

### Persistent event loop (critical)

`asyncio.run()` creates and destroys a new event loop per call. `ChatSession` holds async resources (httpx `AsyncClient`, the `tui_queue` itself) that must persist across turns. Using `asyncio.run()` per turn would orphan these resources and invalidate the queue.

**Correct pattern:** one persistent event loop for the adapter's lifetime, started in `QThread.run()`:

```python
import threading

class QtChatAdapter(QThread):
    def __init__(self):
        super().__init__()
        self._session = ChatSession()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_queue: asyncio.Queue | None = None
        self._ready = threading.Event()   # set when loop is running and safe to call submit()

    def run(self):
        """Worker thread entry point. Keeps loop alive for session lifetime."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._work_queue = asyncio.Queue()
        self._session.tui_queue = asyncio.Queue()
        self._ready.set()                  # signal: loop is running, submit() is safe
        self._loop.run_until_complete(self._main())

    async def _main(self):
        while True:
            item = await self._work_queue.get()
            if item is None:               # shutdown sentinel
                break
            kind = item[0]
            if kind == "__slash__":
                await handle_slash_command(item[1], self._session)
                # output arrives via system events on tui_queue; drain runs after
            else:
                text, plan_mode = item     # kind is the text itself
                await self._run(text, plan_mode)

    def submit(self, text: str, plan_mode: bool) -> None:
        """Called from main thread. Thread-safe. Blocks briefly until loop is ready."""
        self._ready.wait()
        self._loop.call_soon_threadsafe(self._work_queue.put_nowait, (text, plan_mode))

    def submit_slash(self, cmd: str) -> None:
        """Run a slash command through the persistent loop. Thread-safe."""
        self._ready.wait()
        self._loop.call_soon_threadsafe(self._work_queue.put_nowait, ("__slash__", cmd))

    def shutdown(self) -> None:
        """Stop the worker loop. Thread-safe."""
        self._ready.wait()
        self._loop.call_soon_threadsafe(self._work_queue.put_nowait, None)
```

`submit()`, `submit_slash()`, and `shutdown()` are plain Python methods (not Qt Signals/Slots) called from the main thread. The `_ready` event prevents the startup race — main thread cannot call `submit()` before the loop is running.

### Approval intercept (blocking pattern)

`_approval_prompt()` (line 2337) posts an `approval_request` event with an `asyncio.Future` onto `tui_queue`, then `await future` — blocking the asyncio task. The adapter handles this inside `_run()`:

```
[Worker thread — asyncio loop]              [Main thread — Qt]
  tui_queue.put({approval_request, future})
  [adapter sees event in drain loop]
  store future; emit approval_needed signal →
  await future  ← adapter coroutine suspends
                                            MainWindow shows modal dialog
                                            user clicks Allow / Allow with notes / Deny
                                            → adapter.resolve_approval(bool, notes)
                                              → loop.call_soon_threadsafe(
                                                   future.set_result, (approved, notes))
  (future resolved, drain loop resumes)
```

`resolve_approval(approved: bool, notes: str)` is a plain method called from the main thread. It calls `self._loop.call_soon_threadsafe(self._pending_future.set_result, (approved, notes))`. The adapter stores `self._pending_future` when it sees an `approval_request` event.

The adapter emits `approval_needed` for **every** `approval_request` event — the backend already decided to ask.

### Plan mode

`send_and_stream(user_text, plan_mode: bool)` (line 2057). The toolbar Plan toggle sets `MainWindow._plan_mode: bool`. On submit: `adapter.submit(text, self._plan_mode)`. Plan mode auto-resets after `done` (mirrors chat.py line 4460).

### Slash commands integration

`handle_slash_command(cmd, session)` (line 3840) is a module-level async function, currently outputting via `console.print`. For Qt, it must emit `system` events to `session.tui_queue` when set.

**Required change to chat.py:** add tui_queue emit path to `handle_slash_command`. Every `console.print` call in that function gets a parallel `await session.tui_queue.put({"type": "system", "text": ...})` when `session.tui_queue` is set. This is the only structural change to chat.py beyond SP1's asyncio patch.

Slash commands run inside the persistent loop:
```python
async def _run_slash(self, cmd: str) -> None:
    await handle_slash_command(cmd, self._session)
    # system events arrive on tui_queue; adapter drains them normally
```

Called via `adapter.submit_slash(cmd)` → `loop.call_soon_threadsafe(_work_queue.put_nowait, ("__slash__", cmd))` (or a dedicated slash queue).

The 19 slash commands (confirmed from source):

| Command | Notes |
|---|---|
| `/help` | List commands |
| `/clear` | Reset history |
| `/tools` | List tools |
| `/think [off\|on\|deep]` | Set/cycle thinking level; calls `_save_state(think_level=...)` |
| `/save [path]` | Save conversation to JSON |
| `/compact` | Manual compaction |
| `/status` | Token usage and context info |
| `/sessions` | List saved sessions |
| `/resume [name]` | Load saved session; sets messages, _n_fixed, _session_path |
| `/approval [mode]` | Set tier; calls `_save_state(approval_level=...)` |
| `/cd [path]` | Set working directory |
| `/pwd` | Show working directory |
| `/model [id]` | Switch model; calls `_save_state(model=...)` |
| `/role [name]` | Adopt persona; calls `_save_state(role=...)` |
| `/config` | Show eli.toml config |
| `/skills` | List skills |
| `/skill <name> [args]` | Invoke a skill |
| `/queue-results [label]` | List agent queue runs |
| `/voice [ptt\|auto] [tools]` | Start voice mode |

### Persistent state (state.json)

`_save_state()` (line 375) writes `sessions/state.json`. Confirmed fields written:

| Field | Written by |
|---|---|
| `last_session` | `_load_session()` at line 342 |
| `think_level` | `/think` command, line 3912 |
| `approval_level` | `/approval` command, line 3994 |
| `model` | `/model` command, line 4078 |
| `role` | `/role` command, line 4142 |
| `compact_mode` | Ctrl+O toggle, line 4353 |

**Compaction threshold, keep-recent, input-compress are module-level constants** (`CTX_COMPACT_THRESH`, `CTX_KEEP_RECENT`, `INPUT_COMPRESS_CHARS`) — they are not in `state.json` and are not persisted by chat.py today. The Qt config panel shows them as session-only overrides (affects current session, not saved to disk). If persistence of these values is needed, it is a future enhancement out of scope.

### Session resume consistency

Both the Sessions menu and the `/resume` command must use the same code path. The Sessions menu calls `adapter.submit_slash(f"/resume {name}")` — delegating to `handle_slash_command` rather than doing direct message assignment. This ensures state resets (`_n_fixed`, `_session_path`, token counters) are always applied consistently.

### File layout after migration

```
qwen3-manager/
  chat.py                    ← terminal app; two targeted additions (see below)
  sessions/
    state.json               ← persisted state (think_level, approval_level, model, role, compact_mode)
  qt/
    __init__.py              ← empty
    main.py                  ← QApplication entry point; adds .. to sys.path
    window.py                ← MainWindow (5-panel layout)
    adapter.py               ← QtChatAdapter(QThread) — persistent event loop
    chat_view.py             ← Chat panel (thinking blocks, tool panels, markdown render)
    editor_view.py           ← Editor panel + syntax highlighter
    explorer_view.py         ← Explorer + Tasks.md panels
    config_panel.py          ← Right panel (server stats + config controls)
    toolbar.py               ← Ribbon + menu bar
    colors.py                ← Color constants + QSS
    highlighter.py           ← SyntaxHighlighter (existing, moved)
    tool_call_checker.py     ← (existing, moved)
    file_watcher.py          ← (existing, moved)
    run.bat                  ← .venv\Scripts\python.exe qt\main.py
  requirements-qt.txt        ← PySide6, httpx; sounddevice added in SP6
```

`main.py` imports:
```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from chat import ChatSession, handle_slash_command
```

Existing `qt-chat-proto/` files move to `qwen3-manager/qt/`. `llm_client.py` is retired.

### Known chat.py patches (targeted, minimal)

| Change | Location | Reason |
|---|---|---|
| `get_event_loop()` → `get_running_loop()` | line 2346 | Python 3.13 deprecation |
| Emit `system` events to tui_queue in `handle_slash_command` | lines ~3845–4100 | Slash command output reaches Qt chat view |
| Emit `system` event to tui_queue in `_compact_history()` | line ~2008 | Compaction notification appears in Qt chat (currently only `console.print`) |
| Add instance attrs to `ChatSession.__init__`: `self.compact_threshold = CTX_COMPACT_THRESH`, `self.keep_recent = CTX_KEEP_RECENT`, `self.input_compress_limit = INPUT_COMPRESS_CHARS` | line ~1783 | Config panel sliders need settable instance attributes; `_compact_history` and `_maybe_compact_input` updated to use `self.` versions |

**Compaction behavior confirmed:** `_compact_history()` uses only `console.print` — no tui_queue events. The drain loop in the adapter continues running during compaction; it just won't see any events until the next `text_token`/`done` arrives. Adding a `system` emit gives the Qt user visibility of the "Context compacted" notification.

**`set_role()` does not exist.** SP4 toolbar Role dropdown changes role by calling `adapter.submit_slash(f"/role {name}")` — same path as the slash command.

All other `ChatSession` behavior, tool implementations, session file format, and the terminal loop remain untouched.

---

## Sub-project Breakdown

### Sub-project 1: Migration + Backend Bridge

**Scope:** Move qt-chat-proto into qwen3-manager/qt/, wire ChatSession as the backend.

- Move all files from `qt-chat-proto/` → `qwen3-manager/qt/`; add `__init__.py`; fix `sys.path` in `main.py`
- Patch `chat.py` line 2346: `get_event_loop()` → `get_running_loop()`
- Create `adapter.py` with `QtChatAdapter` implementing the persistent-loop pattern above
- `_run(text, plan_mode)`: captures nothing (loop is already running); calls `session.send_and_stream(text, plan_mode)`; drains `session.tui_queue` until `done`/`error`; emits Qt signals per event; for `approval_request` stores future, emits signal, suspends on future
- Update `window.py` to use `QtChatAdapter` signals
- Delete `llm_client.py`; update `run.bat`
- Verify: `python chat.py` still works; Qt app sends "say hello", receives response

**Signals emitted by adapter:**
```python
think_token      = Signal(str)
text_token       = Signal(str)
tool_start       = Signal(str, str, str)        # id, name, args_json
tool_done        = Signal(str, str, str, bool)  # id, name, result, is_error
approval_needed  = Signal(str, str)             # title, message
text_done        = Signal(str)
usage            = Signal(int, int)             # tokens_used, ctx_window
system_msg       = Signal(str)
error_msg        = Signal(str)
done             = Signal()
```

---

### Sub-project 2: Tools + Approval UI

**Scope:** Render tool calls visually; implement approval dialogs; render diffs.

**Tool panels** (cyan left-border in chat output):
- `tool_start` → insert panel: `⚙ tool_name  args_preview  [spinner]`
- `tool_done` → update panel: replace spinner with `✓` (green) or `✗` (red); show result summary
- Click to expand full args + result

**Approval dialog** triggered by `approval_needed` signal:
- Shows title, message, three buttons: [Allow] [Allow, but…] [Deny]
- "Allow, but…" reveals a text field for notes
- On confirm: calls `adapter.resolve_approval(approved, notes)` → `loop.call_soon_threadsafe(future.set_result, (approved, notes))`

**Diff rendering** for `write_file` and `edit` tools:
- On `tool_start` for these tools: read current file contents synchronously (before-state snapshot)
- On `tool_done` (queue events are sequential — this is safe, no race): re-read file; compute unified diff
- Render below tool panel: `+` lines green, `-` lines red; if file is new, all lines are additions

**task_list tool ↔ Tasks.md panel:**
- On `tool_done` for `task_list`: reload the Tasks.md panel in the left column from disk

---

### Sub-project 3: Session Management + Slash Commands + Plan Mode

**Scope:** Expose session persistence, all 19 slash commands, and plan mode.

**Sessions menu (Sessions menu bar item):**
- Lists sessions from `sessions/` dir (name, date, token estimate from JSON)
- "Resume" calls `adapter.submit_slash(f"/resume {name}")` — delegates to `handle_slash_command`
- "New" calls `adapter.submit_slash("/clear")`
- Current session name shown in status bar

**Slash command bar** (strip above input, always visible):
- Shows quick-access for frequently used commands
- Typing `/` in input triggers popup filtered to all 19 commands with argument hints
- On select: inserts command text into input (user confirms with Enter) for arg-taking commands; executes directly for no-arg commands
- Execution: `adapter.submit_slash(cmd)` → `loop.call_soon_threadsafe(_work_queue.put_nowait, ("__slash__", cmd))` → `await handle_slash_command(cmd, session)` in the loop; output via `system` events

**chat.py addition for SP3:** `handle_slash_command` emits `system` events to `tui_queue` in parallel with `console.print`. All 19 commands must be covered — verified by test.

**Plan mode:**
- Plan toggle in toolbar sets `MainWindow._plan_mode: bool`
- Submit: `adapter.submit(text, plan_mode=self._plan_mode)`
- `done` signal: `self._plan_mode = False`; untoggle button

**Compaction config** (right panel):
- Compact threshold slider → `session.compact_threshold` (instance attr added in SP1 patch; default 0.80, session-only)
- Keep-recent spinbox → `session.keep_recent` (instance attr added in SP1 patch; default 6, session-only)
- Input compress limit → `session.input_compress_limit` (instance attr added in SP1 patch; default 8000, session-only)
- Compact mode ON/OFF toggle → `session.compact_mode`; calls `_save_state(compact_mode=...)`
- Note: threshold/keep-recent/compress values are session-only — reset to defaults on next launch (persistence deferred)

---

### Sub-project 4: Context Injection + Config Panel

**Scope:** Surface chat.py's context injection and live configuration state.

**Context files** read by ChatSession:
- `ELI.md` — behavioral rules and persona
- `MEMORY.md` — persistent memory
- `MISSION_OBJECTIVE.md` — active mission
- `eli.toml` — config (model, thresholds, hooks)

**Toolbar live state sync:**
- Think dropdown, Approval dropdown, Role dropdown initialize from `state.json` on startup
- On change: update `session.think_level` / `session.approval_level` / `session.role` directly + call `_save_state(...)` — same effect as the equivalent slash command
- Changing Role calls `adapter.submit_slash(f"/role {name}")` — no `set_role()` method exists; always use slash path

**Config panel additions:**
- Read-only display of current eli.toml values (model, base_url, think, approval)
- "Edit in Editor" link opens the file in the Editor panel

**Model dropdown** (toolbar):
- Reads profiles from eli.toml `[models]` section
- Switching sets `session.model`; calls `_save_state(model=...)`

---

### Sub-project 5: Agent System

**Scope:** Visualize `spawn_agent` and `queue_agents` tool results.

**spawn_agent** panel (purple left-border):
- `tool_start` for `spawn_agent`: show panel — agent name, model, status: running
- `tool_done`: update status to done/error; show result summary; click to expand output

**queue_agents** panel:
- `tool_start` for `queue_agents`: parse args JSON; show one row per agent (name, status: pending)
- `tool_done` events for sub-agents: update rows
- Overall progress bar; `tool_done` for `queue_agents` itself: collapse to summary

---

### Sub-project 6: Voice

**Scope:** PTT + auto-silence input; TTS output.

**PTT:**
- Hotkey (default F9, configurable in eli.toml) holds mic open while pressed
- Audio captured via `sounddevice`; sent to whisper endpoint from eli.toml
- Transcription text inserted into input field

**Auto-silence VAD:**
- Mic stays open; silence > threshold triggers end-of-utterance
- Configurable silence duration (default 1.5s) in eli.toml

**TTS output:**
- On `text_done`: if TTS enabled, call speak tool endpoint from eli.toml config
- Toggle in toolbar; non-blocking

**Voice menu:** PTT vs auto-silence, hotkey, silence threshold, TTS on/off.

**New dependency:** `sounddevice` added to `requirements-qt.txt`.

---

## Data Flow (per turn)

```
User types → input field
  → MainWindow.send_message(text)
    → adapter.submit(text, plan_mode)
      → loop.call_soon_threadsafe(_work_queue.put_nowait, (text, plan_mode))
        [worker asyncio loop picks up work]
        → session.send_and_stream(text, plan_mode)
        → adapter drains session.tui_queue (sequential):
            think_token      → Full Output: append to thinking block
            text_token       → Full Output: append; accumulate full_text
            tool_start       → Full Output: insert tool panel + spinner
                               if write_file/edit: snapshot file for diff (sequential, no race)
            approval_request → store future; emit approval_needed signal; await future
            tool_done        → Full Output: update tool panel ✓/✗
                               if write_file/edit: compute + render diff
                               if task_list: reload Tasks.md panel
            text_done        → Compact tab: render _markdown_to_html(full_text)
            usage            → toolbar context meter + right panel stats
            system_msg       → status bar or chat info line
            done             → emit done signal → main thread re-enables input; resets plan_mode
            error_msg        → error in chat
```

---

## Testing Strategy

**SP1:**
- Mock: feed each event type onto a real `asyncio.Queue`; assert adapter emits correct Qt signal
- Startup race: call `adapter.submit("hi", False)` within 10ms of `adapter.start()`; assert no AttributeError or crash (proves `_ready` event guard works)
- Approval flow: emit `approval_request` with a Future; call `resolve_approval(True, "")`; assert future result is `(True, "")`
- Multi-turn: send two messages in sequence; assert second turn receives a response (proves persistent loop survives across turns)
- Smoke test: launch Qt app; type "say hello"; assert `text_done` fires with non-empty string
- Regression: `python chat.py` starts and shows prompt after migration

**SP2:**
- Tool panel: emit `tool_start` → assert panel widget exists with spinner; emit `tool_done` → assert ✓ shown
- Approval dialog: trigger `approval_needed`; click Allow → assert `resolve_approval(True, "")` called
- Approval with notes: click "Allow, but…"; type notes "be careful" → assert `resolve_approval(True, "be careful")`
- Diff: create temp file "before"; emit `tool_start` for `write_file`; write "after"; emit `tool_done`; assert diff HTML has `+after` and `-before`

**SP3:**
- Sessions list: fixture 3 session JSON files → assert Sessions menu shows 3 items
- Resume via menu: click Resume → assert `submit_slash("/resume name")` called → assert chat populated
- Slash bar filter: type `/th` → assert only `/think` and `/tools` appear in popup
- Plan toggle: click toggle → `_plan_mode=True`; submit → assert `send_and_stream` called with `plan_mode=True`; `done` → assert `_plan_mode=False`
- Slash output via tui_queue: call `/status`; assert `system_msg` signal fires with string containing token count
- Slash with args: call `/think on`; assert `system_msg` fires + `session.think_level == "on"`
- Slash multi-line: call `/help`; assert multiple `system_msg` signals or one with newlines
- Slash session change: call `/clear`; assert chat view clears

**SP4:**
- eli.toml parse: fixture toml with custom model "test-model" → assert model label in toolbar shows "test-model"
- state.json sync: change Think dropdown to "deep" → assert `state.json` contains `"think_level": "deep"`; reload app → assert dropdown shows "deep"

**SP5:**
- spawn_agent: emit `tool_start` for spawn_agent with `{"name": "test", "model": "x"}` → assert purple panel with "test" appears
- queue_agents: emit `tool_start` with 3-agent fixture → assert 3 rows; emit 2 `tool_done` → assert progress shows 2/3

**SP6:**
- VAD silence logic: unit test with mock sounddevice; assert silence > 1.5s triggers end-of-utterance callback
- PTT hotkey registration, transcription insertion, and TTS `text_done` → speak call: manual test only (hardware dependency)

**Threading (all SPs):**
- Assert adapter Qt signals fire on main thread, not worker thread
- Assert `call_soon_threadsafe` used for all main-thread-to-worker communication

---

## Open Questions / Risks

| Item | Notes |
|---|---|
| Session file locking | Terminal + Qt running simultaneously share `sessions/`. Last-write wins — document in README. |
| Whisper endpoint | SP6 requires a running whisper server — document setup requirements. |
| `handle_slash_command` console coverage | All `console.print` calls in the function must have a tui_queue parallel emit. A missed print silently outputs to terminal. SP3 tests must cover all 19 commands. |
| auto mode approval behavior | Verify from chat.py source whether `approval_request` is emitted in `auto` mode for safe tools. If never emitted for safe tools, the approval dialog test only fires for dangerous commands. |
| compaction threshold persistence | CTX_COMPACT_THRESH etc. are not in state.json. Session-only override is acceptable for now; persistence would require eli.toml write-back (deferred). |
