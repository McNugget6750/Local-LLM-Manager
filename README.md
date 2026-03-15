# Local LLM Manager

A local LLM chat CLI + server manager GUI for [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) (a high-performance fork of llama.cpp).

Includes **Eli** — a coding assistant persona with tool use, sub-agents, plan mode, slash commands, and persistent memory.

---

## What this is

- **`server_manager.py`** — Tkinter GUI for launching and monitoring llama-server instances. Tracks t/s, VRAM, GPU load, RAM. Supports multiple named model profiles loaded from `commands.json`.
- **`chat.py`** — Terminal chat client (Eli). Connects to a running llama-server, supports tool use (bash, file read/write/edit, glob, grep, web search/fetch), slash commands, plan mode, and sub-agents.

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

:: Start Eli chat (in a separate terminal, after server is running)
chat.bat
```

---

## Usage

### Server manager

```bat
run.bat
```

Opens the GUI. Select a model profile, click **Start**. The GUI monitors t/s, VRAM, GPU, RAM, and CPU in real time. Add new model profiles with **+ Add Model** — they are saved to `commands.json`.

### Eli chat CLI

```bat
chat.bat
```

Connects to `http://localhost:1234` by default. Type naturally or use slash commands.

**Slash commands:**

| Command | Description |
|---------|-------------|
| `/skills` | List available skills |
| `/commit` | Generate a conventional commit message |
| `/review <file>` | Spawn code-review sub-agent on a file |
| `/research <topic>` | Spawn researcher sub-agent |
| `/model` | List or switch models |
| `/config` | Show loaded eli.toml config |
| `/cd <path>` | Change working directory |

**Keyboard shortcuts:**

- `Ctrl+C` — cancel current response (stays in session)
- `Ctrl+D` — exit
- `Shift+Tab` — toggle plan mode (Eli plans but doesn't execute)

---

## Configuration

### `commands.json` (gitignored)

Model profiles for the server manager. Copy from `commands.example.json` and add your own:

```json
{
  "Qwen3-30B  ·  237 t/s  ·  Q4_K_M": [
    "C:\\path\\to\\llama-server.exe",
    "-m", "C:\\path\\to\\model.gguf",
    "-ngl", "999", "-c", "128000",
    "-ctk", "q4_1", "-ctv", "q4_1",
    "--no-mmap", "--jinja",
    "-b", "4096", "-ub", "4096", "-t", "16",
    "--parallel", "1",
    "--port", "1234", "--host", "0.0.0.0"
  ]
}
```

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
