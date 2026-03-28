# Mission Objective — qwen3-manager Qt GUI

## What We're Building

A full-featured Qt desktop GUI for Eli (the local Qwen3 AI agent), achieving complete feature parity with the terminal app (`chat.py`) while adding visual capabilities that are impossible in a terminal: file explorer, code editor, diff views, tool panels, session browser, and voice mode.

The Qt app will eventually replace the TUI as the primary interface.

---

## Architecture

- **`chat.py`** — unchanged core: ChatSession, tools, compaction, agents, skills
- **`qt/adapter.py`** — QtChatAdapter (QThread with asyncio loop), bridges session ↔ Qt signals
- **`qt/window.py`** — MainWindow, five-panel layout
- **`qt/`** — all GUI code lives here (approval_dialog.py, diff_renderer.py, etc.)

---

## Sub-Projects (Phases)

| Phase | Name | Status |
|-------|------|--------|
| SP1 | Migration + Backend Bridge | ✅ Done (b8b9942) |
| SP2 | Tools + Approval UI | 🔄 In Progress |
| SP3 | Session Management + Slash Commands | 📋 Planned |
| SP4 | Config Panel (right-panel settings) | 📋 Planned |
| SP5 | Skills + Agents UI | 📋 Planned |
| SP6 | Voice Mode | ⏳ Deferred (last) |

---

## Feature Gap — TUI → Qt

### SP2 (In Progress)
- [ ] Richer tool panels (cyan border, expand/collapse, diff on write/edit)
- [ ] ApprovalDialog (Allow / Allow, but… / Deny with notes)
- [ ] Tasks.md panel in left column splitter

### SP3 — Session Management + Slash Commands
- [ ] Auto-save session to sessions/*.json after each turn
- [ ] Session browser in ribbon (Sessions menu → list)
- [ ] `/resume [name]` — load saved session
- [ ] `/clear` — reset history
- [ ] `/save [path]` — export session JSON
- [ ] State persistence (think/approval/role/compact_mode across restarts)
- [ ] Slash command input: `/cmd` prefix → dropdown above chat input, OR dedicated command bar
- [ ] `/status` → inline status block in chat or status panel
- [ ] `/compact` manual button
- [ ] Auto-compact indicator (warning when >75% context)
- [ ] Interrupt button (cancel in-flight stream, like TUI Ctrl+C)
- [ ] Plan mode toggle button (currently no UI equivalent of Shift+Tab)
- [ ] Compact mode toggle button (currently no UI equivalent of Ctrl+O)

### SP4 — Config Panel
- [ ] Right-panel settings: compaction threshold slider (default 80%)
- [ ] Keep-recent spinner (default 6 messages)
- [ ] Input compress limit field (default 8000 chars)
- [ ] CWD picker with browse button
- [ ] eli.toml project config display (`/config` equivalent)
- [ ] Model profile list from server_manager `/api/profiles` (replaces hardcoded combo)
- [ ] Model switch via `/api/start` + progress indicator

### SP5 — Skills + Agents UI
- [ ] Skills browser panel (list from skills/*.md)
- [ ] Invoke skill from UI
- [ ] `/role [name]` — agent persona picker
- [ ] Queue results viewer (`/queue-results`)
- [ ] Sub-agent progress indicator in tool panel

### SP6 — Voice Mode (LAST, after all other phases)
- [ ] Voice ribbon section: green/red toggle button (off/active)
- [ ] PTT mode (button hold or keyboard shortcut)
- [ ] Auto mode (VAD-based)
- [ ] Activity status label (Idle / Listening / Transcribing / Speaking)
- [ ] TTS server status indicator
- [ ] Uses same `/transcribe` and TTS endpoints as TUI

---

## Key Constants (from chat.py)
- Context window: auto-detected from `/slots`, fallback 32,768 tokens
- Compact threshold: 80% (`CTX_COMPACT_THRESH = 0.80`)
- Keep recent: 6 messages (`CTX_KEEP_RECENT = 6`)
- Input compress: 8,000 chars (`INPUT_COMPRESS_CHARS = 8_000`)
- Sessions dir: `sessions/` relative to `chat.py`
- Max sessions kept: 10

---

## Voice Architecture (SP6 design, implement last)

The TUI voice loop uses:
- `pynput` for ScrollLock PTT detection
- `sounddevice`/`pyaudio` for mic capture
- `/transcribe` POST endpoint (TTS_URL) for STT
- TTS server for audio playback

Qt equivalent:
- `QAudioInput` / `QMediaDevices` for mic
- PTT: QPushButton (hold = record) + optional keyboard shortcut
- Auto: VAD threshold same as TUI constants
- Same `/transcribe` + TTS endpoints

Voice system prompt (from chat.py): concise 2-4 sentence conversational partner, no lists, spoken prose only.

---

*Last updated: 2026-03-25*
