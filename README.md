# Local LLM Manager

A local LLM chat CLI + server manager GUI for [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) (a high-performance fork of llama.cpp).

Includes **Eli** — a coding assistant persona with tool use, sub-agents, agent queues, vision analysis, voice I/O, plan mode, slash commands, and persistent session state.

---

## What this is

- **`server_manager.py`** — Tkinter GUI for launching and monitoring llama-server instances. Tracks t/s, VRAM, GPU load, RAM, and CPU in real time. Supports multiple named model profiles loaded from `commands.json`. Includes a loopback control API so Eli can switch models automatically.
- **`chat.py`** — Terminal chat client (Eli). Connects to a running llama-server, supports tool use, slash commands, plan mode, sub-agents, agent queues, and image analysis via a local vision model.

---

## Requirements

- Python 3.11+
- [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) built — `llama-server.exe` on PATH or configured in `commands.json`
- GGUF model files
- Windows (server_manager.py uses Windows APIs for GPU/RAM stats)

---

## Quick start

```bat
git clone https://github.com/McNugget6750/Local-LLM-Manager.git
cd Local-LLM-Manager

:: Create venv
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

:: Configure model profiles
copy commands.example.json commands.json
:: Edit commands.json — fill in your llama-server path and model paths

:: Optional: personalize Eli
copy USER_PROFILE.example.md USER_PROFILE.md
:: Edit USER_PROFILE.md with your name, background, projects

:: Start server manager GUI
run.bat

:: Start Eli chat — or click "Open Chat" in the GUI once a model is loaded
chat.bat
```

---

## Usage

### Server manager

```bat
run.bat
```

Opens the GUI. Select a model profile, click **Start**. The GUI monitors t/s, VRAM, GPU, RAM, and CPU in real time. Add new model profiles with **+ Add Model** — they are saved to `commands.json`.

Once a server is running, click **Open Chat** to launch Eli in a new terminal.

The server manager also exposes a loopback control API on port 1235. Eli uses this to switch models automatically when running agents on different model profiles — the GUI stays in sync with start/stop state throughout.

### Eli chat CLI

```bat
chat.bat           # new session
chat.bat --continue  # resume last session with all settings restored
```

Connects to `http://localhost:1234` by default. **Open Chat** in the server manager passes `--continue` automatically. Type naturally or use slash commands.

**Session persistence** — think level, compact mode, approval level, model, and active role are written to `sessions/state.json` whenever they change and restored on `--continue`.

**Slash commands:**

| Command | Description |
|---------|-------------|
| `/skills` | List available skills |
| `/commit` | Generate a conventional commit message |
| `/review <file>` | Spawn code-review sub-agent on a file |
| `/research <topic>` | Spawn researcher sub-agent |
| `/queue-results [label]` | List recent agent queue runs or show one by label |
| `/model` | List available model profiles |
| `/role <name>` | Adopt an agent persona (`/role eli` to revert) |
| `/voice [ptt\|auto] [tools]` | Start voice conversation mode |
| `/config` | Show loaded eli.toml config |
| `/cd <path>` | Change working directory |
| `/think [off\|on\|deep]` | Set thinking level |
| `/approval [auto\|always\|never]` | Set tool approval level |
| `/compact` | Summarise older messages to free context |
| `/status` | Show token usage and context window info |

**Keyboard shortcuts:**

- `Ctrl+C` — cancel current response (stays in session)
- `Ctrl+D` — exit
- `Shift+Tab` — toggle plan mode (Eli plans but doesn't execute)
- `Ctrl+O` — toggle compact mode (collapse thinking/tool output)

---

## Voice mode

```
/voice              # PTT mode (default)
/voice auto         # VAD — speak naturally, pause to send
/voice ptt tools    # PTT with tool access enabled
/voice auto tools   # auto VAD with tool access
```

Voice requires the **eli_voice_server** running on port 1236 (Kokoro ONNX TTS + faster-whisper STT). In PTT mode, hold the configured key (default: Scroll Lock) to record; release to transcribe and send. In auto mode, silence detection triggers the send automatically. Press Escape to exit voice mode.

The `voice_input.py` standalone tool (`claudes_tools/`) provides system-wide push-to-type voice input — it transcribes speech and types the result into whatever window is focused.

---

## Model switching

Eli knows which model profiles are available at startup — descriptions, strengths, weaknesses, and speed are injected from `commands.json` as a system message. Eli can switch models automatically before spawning a sub-agent and restores the original model when done.

The switch goes through the Server Manager control API, so all logging, UI state, and Stop button behaviour remain correct.

---

## Agent queues

Eli can run a sequence of agents — each with its own task, model, and time budget — without manual intervention:

```
Queue two agents:
  1. researcher on the fast model — "what are the tradeoffs of MoE quantization?" (120s)
  2. code-review on the high-quality model — "review _tool_queue_agents in chat.py" (240s)
```

Model switches between agents are skipped when consecutive agents share the same model. The original model is restored after the entire queue completes. Results are written to `sessions/queue_{ts}_{label}/results.json` and browsable with `/queue-results`.

---

## Vision

Eli can analyse images using a local vision-language model. The vision model runs on the same port as text models — the server switches automatically, processes all queued images, then restores the text model.

```
Eli, analyse these screenshots and tell me which UI layout looks cleaner.
```

For batch processing, pass multiple image paths in one call — the model loads once and processes them all before restoring.

**`commands.json` vision config:**

```json
{
  "_meta": {
    "vision_url": "http://192.168.x.x:1234",
    "vision_external": false
  }
}
```

Set `vision_external: true` if your vision model runs on a separate machine — Eli will call it directly without switching the local server.

---

## Configuration

### `commands.json` (gitignored)

Model profiles for the server manager, plus optional metadata. Copy from `commands.example.json`:

```json
{
  "_meta": {
    "vision_url": "http://192.168.x.x:1234",
    "vision_external": false,
    "profiles": {
      "My Model · Quantization": {
        "description": "One-line description.",
        "strengths": "What it excels at",
        "weaknesses": "What to avoid",
        "speed": "~?? t/s",
        "vision": false
      }
    }
  },
  "My Model · Quantization": [
    "path/to/llama-server.exe",
    "-m", "path/to/model.gguf",
    "-ngl", "999", "-c", "32768",
    "-ctk", "q4_1", "-ctv", "q4_1",
    "--no-mmap", "--jinja",
    "-b", "4096", "-ub", "4096", "-t", "16",
    "--parallel", "1",
    "--port", "1234", "--host", "0.0.0.0"
  ]
}
```

Profile metadata (description, strengths, weaknesses, speed) is injected into Eli's context at startup so he can make informed model-selection decisions.

### `eli.toml` (gitignored)

Project-specific config injected as a system message at startup:

```toml
[project]
name = "my-project"

[build]
command = "cmake --build build --preset release"
cwd = "."

[tools]
cmake = "C:\\path\\to\\cmake.exe"
```

Place `eli.toml` in any project root. Run `/config` inside Eli to see what's loaded.

### `USER_PROFILE.md` (gitignored)

Personal info about you — name, background, projects, preferences. Eli reads this to personalize responses. Copy from `USER_PROFILE.example.md`.

---

## Agent profiles

Sub-agents are spawned by Eli for specialized tasks. Profiles live in `agents/`:

| Profile | Purpose |
|---------|---------|
| `code-review` | Review a file for correctness and safety issues |
| `doc-writer` | Write docstrings or README sections |
| `researcher` | Research a library, API, or technical question |
| `test-writer` | Write unit tests |
| `web_designer` | UI/UX and web design feedback |

---

## Skills

Prompt workflows stored in `skills/`. Invoked with `/skillname`:

| Skill | Description |
|-------|-------------|
| `/commit` | Conventional commit message template |
| `/review` | Spawns code-review sub-agent |
| `/research` | Spawns researcher sub-agent |
| `/pr` | Pull request description template |
| `/git-status` | Git status summary |

---

## Credits

- [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) — high-performance llama.cpp fork by ikawrakow
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — upstream project
