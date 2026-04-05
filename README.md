# Local LLM Manager

A local LLM chat GUI + server manager supporting multiple inference backends:
[ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp), [llama.cpp](https://github.com/ggml-org/llama.cpp), and [vLLM](https://github.com/vllm-project/vllm) (via WSL).

Includes **Eli** — a coding assistant persona with tool use, sub-agents, agent queues, vision analysis, voice I/O, plan mode, slash commands, and persistent session state.

---

## What this is

- **`server_manager.py`** — Tkinter GUI for launching and monitoring inference server instances. Tracks t/s, VRAM, GPU load, RAM, and CPU in real time. Supports multiple named model profiles from `commands.json` across different backends. Auto-detects the engine type from the launch command — llama.cpp binaries and WSL-hosted vLLM servers are handled transparently. Includes a loopback control API so Eli can switch models automatically. Manages the voice server lifecycle.
- **`qt/main.py`** — Qt chat GUI (primary interface). Full-featured chat window with file explorer, code editor, agent output tab, token bar, slash command autocomplete, Knight Rider activity indicator, and session management. Slash command output (e.g. `/help`, `/role`) renders as formatted HTML panels in the chat view when the GUI is open. Launch via `qt/run.bat` or click **Open Chat** in the server manager.
- **`chat.py`** — Terminal chat client (Eli). Same backend as the Qt GUI — use this if you prefer a terminal interface.

---

## Supported backends

All backends expose an OpenAI-compatible `/v1` API on the configured port. The server manager auto-detects the engine from the launch command and handles start/stop correctly for each.

| Backend | Engine tag | Use case |
|---------|-----------|----------|
| ik_llama.cpp / llama.cpp | `llama` | GGUF models, Windows-native, CPU+GPU offload |
| vLLM (WSL) | `wsl` | HuggingFace safetensors, NVFP4/FP8 quants, Blackwell GPU |

**llama.cpp entry** (`commands.json`):
```json
"My Model · Q6_K": [
  "..\\llama.cpp\\build\\bin\\Release\\llama-server.exe",
  "-m", "path\\to\\model.gguf",
  "-ngl", "999", "-c", "32768",
  "-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on",
  "--no-mmap", "--jinja",
  "-b", "4096", "-ub", "4096", "-t", "16",
  "--parallel", "2", "--port", "1234", "--host", "0.0.0.0"
]
```

**vLLM (WSL) entry** (`commands.json`):
```json
"My Model · NVFP4 [vLLM]": [
  "wsl", "--exec", "bash", "-c",
  "/home/user/miniconda3/bin/conda run -n vllm-env python -m vllm.entrypoints.openai.api_server --model /mnt/c/path/to/model --quantization modelopt --dtype bfloat16 --max-model-len 8192 --max-num-seqs 2 --port 1234 --host 0.0.0.0"
]
```

---

## Requirements

- Python 3.11+
- At least one inference backend:
  - [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) or [llama.cpp](https://github.com/ggml-org/llama.cpp) built — `llama-server.exe` configured in `commands.json`
  - Or: WSL2 + Ubuntu + vLLM installed in a conda environment
- GGUF or HuggingFace model files
- Windows (server_manager.py uses Windows APIs for GPU/RAM stats)
- NVIDIA GPU recommended; NVFP4 requires Blackwell (RTX 5000/6000 series, B100/B200)

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
:: Edit commands.json — fill in your backend binary path and model paths

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

Once a server is running, click **Open Chat** to launch the Qt chat GUI with `--continue` (resumes last session automatically). The voice server starts and stops alongside the inference server.

The server manager also exposes a loopback control API on port 1235. Eli uses this to switch models automatically when running agents on different model profiles — the GUI stays in sync with start/stop state throughout.

### Qt chat GUI

```bat
qt\run.bat               # new session
qt\run.bat --continue    # resume last session with all settings restored
qt\run.bat --resume name # resume a specific named session
```

Connects to `http://localhost:1234` by default. The file explorer on the left roots at the drive level — double-click a directory to set it as the working directory. The editor panel supports syntax highlighting, excerpts, and line references. The Agent tab streams sub-agent output separately.

Slash command output (e.g. `/help`, `/role`, `/model`, `/status`) renders as styled HTML panels directly in the chat view when the GUI is open. In terminal-only mode the same output goes to the Rich console as before.

### Eli chat CLI (terminal)

```bat
chat.bat           # new session
chat.bat --continue  # resume last session with all settings restored
```

Same backend as the Qt GUI. Use this if you prefer a terminal interface.

**Session persistence** — think level, compact mode, approval level, model, active role, and working directory are saved with each session and restored on `--continue` or `/resume`.

**Slash commands:**

| Command | Description |
|---------|-------------|
| `/skills` | List available skills with triggers |
| `/commit` | Generate a conventional commit message |
| `/review <file>` | Deep code review sub-agent (reads code + callers + tests) |
| `/research <topic>` | Skeptical research sub-agent (3-pass protocol) |
| `/plan <feature>` | Implementation planning sub-agent |
| `/code <task>` | Production code writing sub-agent |
| `/queue-results [label]` | List recent agent queue runs or show one by label |
| `/model [name]` | List available model profiles or switch to one |
| `/role <name>` | Adopt an agent persona (`/role eli` to revert) |
| `/voice [ptt\|auto] [tools]` | Start voice conversation mode |
| `/config` | Show loaded eli.toml config |
| `/cd <path>` | Change working directory |
| `/think [off\|on\|deep]` | Set thinking level |
| `/approval [auto\|ask-writes\|ask-all\|yolo]` | Set tool approval level |
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

Voice requires the **eli_voice_server** running on port 1236 (Kokoro ONNX TTS + faster-whisper STT). Start it standalone with `voice_server.bat` — no model loaded in the server manager needed for TTS/STT alone. In PTT mode, hold the configured key to record; release to transcribe and send. In auto mode, silence detection triggers the send automatically. Press Escape to exit voice mode.

The `voice_input.py` standalone tool provides system-wide push-to-type voice input (Insert key PTT by default). It types transcriptions into any target window you pick, with optional auto-submit.

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

Model profiles for the server manager, plus optional metadata. Copy from `commands.example.json`. Supports both llama.cpp (Windows binary) and vLLM (WSL) entries — the engine is auto-detected from the first token of the command.

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
| `code-researcher` | Targeted code research and API lookup |
| `code-review` | Review a file for correctness and safety issues |
| `doc-writer` | Write docstrings or README sections |
| `expert_coder` | Production code implementation |
| `researcher` | Research a library, API, or technical question |
| `test-writer` | Write unit tests |
| `web_designer` | UI/UX and web design feedback |

---

## Skills

Prompt workflows stored in `skills/`. Invoked with `/skillname` or triggered automatically when your message matches a skill's trigger list. Use `/skills` to list all available skills with their triggers.

| Skill | Spawns agent | Description |
|-------|:------------:|-------------|
| `/research` | yes | 3-pass skeptical research protocol — initial sweep, cross-examination, synthesis |
| `/plan` | yes | Implementation planning — reads codebase, evaluates approaches, produces ordered task checklist |
| `/code` | yes | Production code writing — reads first, designs, implements, writes tests, self-reviews |
| `/review` | yes | Deep code review — reads code, callers, and tests; reports issues by severity with fixes |
| `/commit` | no | Conventional commit message template |
| `/pr` | no | Pull request description template |
| `/git-status` | no | Git status summary |

Skills that spawn agents support a `max_iterations` frontmatter field to control how long they run. The hard ceiling is 50 iterations.

Skill files use YAML frontmatter:

```yaml
---
name: my-skill
description: One-line description shown in /skills
spawn_agent: true
think_level: deep
max_iterations: 20
triggers: [keyword, another keyword]
context_files: [path/to/extra.md]
---
```

---

## Credits

- [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) — high-performance llama.cpp fork by ikawrakow
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — upstream project
- [vLLM](https://github.com/vllm-project/vllm) — GPU-accelerated inference for HuggingFace models
