#!/usr/bin/env python3
"""
Open LLM Server Manager
Desktop UI for managing ik_llama.cpp server instances.
"""

import tkinter as tk
from tkinter import ttk
import subprocess, threading, queue, json, datetime, shlex, os, re, collections, ctypes
import urllib.request, urllib.error, http.server

# ── Configuration ─────────────────────────────────────────────────────────────
BINARY        = "llama-server"  # override: add your llama-server path to commands.json
PORT          = 1234
CONTROL_PORT  = 1235             # loopback-only control API for chat.py
URL           = f"http://localhost:{PORT}"
HB_INTERVAL   = 300    # full heartbeat every 5 min
MAX_LOG_LINES = 8000   # trim log when it exceeds this many lines
COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "commands.json")
UI_PREFS_FILE = os.path.join(os.path.dirname(__file__), "ui_prefs.json")

MODELS_DEFAULT = {
    "My Model  ·  ?? t/s  ·  Notes": [
        BINARY,
        "-m", r"C:\path\to\model.gguf",
        "-ngl", "999", "-c", "32768",
        "-ctk", "q4_1", "-ctv", "q4_1",
        "--no-mmap", "--jinja",
        "-b", "4096", "-ub", "4096", "-t", "16",
        "--parallel", "1",
        "--port", str(PORT), "--host", "0.0.0.0",
    ],
}

_DEFAULT_NEW_CMD = [
    BINARY, "-m", r"C:\path\to\model.gguf",
    "-ngl", "999", "--n-cpu-moe", "22", "-c", "81920",
    "-ctk", "q4_1", "-ctv", "q4_1", "--no-mmap", "--jinja",
    "-b", "4096", "-ub", "4096", "-t", "16",
    "--parallel", "2",
    "--port", str(PORT), "--host", "0.0.0.0",
]

_RE_PRE_TIMING   = re.compile(
    r'prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second', re.I)
_RE_GEN_TIMING   = re.compile(
    r'eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second', re.I)
_RE_TOTAL_TIMING = re.compile(r'total time\s*=\s*([\d.]+)\s*ms', re.I)

# ── Colours ────────────────────────────────────────────────────────────────────
BG     = "#1e1e1e"
PANEL  = "#252526"
BORDER = "#3e3e42"
FG     = "#d4d4d4"
FG_DIM = "#808080"
GREEN  = "#4ec9b0"
RED    = "#f44747"
YELLOW = "#dcdcaa"
BLUE   = "#569cd6"
PURPLE = "#c678dd"
LOG_BG = "#0d0d0d"
GRAPH_FILL = "#0a2520"   # dark teal fill under curve

# ── System stats helpers (stdlib only) ────────────────────────────────────────
class _MEMSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength",                ctypes.c_ulong),
        ("dwMemoryLoad",            ctypes.c_ulong),
        ("ullTotalPhys",            ctypes.c_ulonglong),
        ("ullAvailPhys",            ctypes.c_ulonglong),
        ("ullTotalPageFile",        ctypes.c_ulonglong),
        ("ullAvailPageFile",        ctypes.c_ulonglong),
        ("ullTotalVirtual",         ctypes.c_ulonglong),
        ("ullAvailVirtual",         ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual",ctypes.c_ulonglong),
    ]

def _get_ram():
    """Returns (used_gb, total_gb, pct) via Windows GlobalMemoryStatusEx."""
    stat = _MEMSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    total = stat.ullTotalPhys / 1024**3
    used  = (stat.ullTotalPhys - stat.ullAvailPhys) / 1024**3
    return used, total, stat.dwMemoryLoad

def _get_gpu_stats():
    """Returns (vram_used_mib, vram_total_mib, gpu_load_pct, gpu_power_w, gpu_temp_c)
    via nvidia-smi, or (None, None, None, None, None) on failure."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            return int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]), int(parts[4])
    except Exception:
        pass
    return None, None, None, None, None




def _load_models() -> tuple[dict, str | None]:
    """Return (models_dict, default_name_or_None)."""
    if os.path.exists(COMMANDS_FILE):
        try:
            with open(COMMANDS_FILE) as f:
                saved = json.load(f)
            default = saved.get("_default") if isinstance(saved.get("_default"), str) else None
            models = {k: v for k, v in saved.items()
                      if not k.startswith("_") and isinstance(v, list)}
            if models:
                return models, default
        except Exception:
            pass
    return dict(MODELS_DEFAULT), None


def _save_models(models):
    with open(COMMANDS_FILE, "w") as f:
        json.dump(models, f, indent=2)


def _load_prefs() -> dict:
    try:
        with open(UI_PREFS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_prefs(prefs: dict) -> None:
    with open(UI_PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def _detect_engine(cmd: list) -> str:
    """Return the engine tag for a command list.

    Tags:
      "llama" — Windows llama-server / ik_llama.cpp binary
      "wsl"   — anything launched via wsl.exe (vLLM, etc.)
    """
    if not cmd:
        return "llama"
    exe = os.path.basename(cmd[0]).lower().replace(".exe", "")
    if exe == "wsl":
        return "wsl"
    return "llama"


def _cmd_to_str(cmd: list) -> str:
    def _q(s):
        # Quote args that contain spaces (e.g. bash -c "..." for WSL commands).
        # Safe for llama entries too — their args never contain spaces.
        return f'"{s}"' if " " in s and not (s.startswith('"') and s.endswith('"')) else s
    parts = []
    i = 0
    while i < len(cmd):
        arg = cmd[i]
        if arg.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
            parts.append(f"{arg} {_q(cmd[i+1])}")
            i += 2
        else:
            parts.append(_q(arg))
            i += 1
    return " \\\n  ".join(parts)


def _str_to_cmd(s: str) -> list:
    cleaned = s.replace("\\\n", " ").replace("\\\r\n", " ")
    tokens = shlex.split(cleaned, posix=False)
    # Strip outer quotes that _cmd_to_str adds for args containing spaces.
    # Has no effect on unquoted llama-server args.
    result = []
    for t in tokens:
        if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
            result.append(t[1:-1])
        else:
            result.append(t)
    return result


# ── App ────────────────────────────────────────────────────────────────────────
class ServerManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Open LLM Server Manager")
        self.configure(bg=BG)
        self.minsize(660, 740)
        _prefs = _load_prefs()
        self.geometry(_prefs.get("window_geometry", "760x940"))

        self._models, self._default_model = _load_models()
        self._proc        = None
        self._voice_proc  = None
        self._telegram_proc = None
        self._log_q       = queue.Queue()
        self._running     = False
        self._external    = False
        self._engine      = "llama"   # "llama" | "wsl" — set on each start
        self._hb_after    = None
        self._cd_after    = None
        self._hb_secs  = 0
        self._tps_gen  = 0.0
        self._tps_pre  = 0.0

        # Rolling graph data (main thread only, max 100 points)
        self._graph_data = collections.deque(maxlen=100)

        # CPU usage tracking — delta between successive GetSystemTimes calls
        self._cpu_prev_idle  = 0
        self._cpu_prev_total = 0

        # Accumulator for the 3-line timing block (background thread only)
        self._pend_pre_tps = 0.0
        self._pend_pre_n   = 0
        self._pend_pre_ms  = 0.0
        self._pend_gen_tps = 0.0
        self._pend_gen_n   = 0
        self._pend_gen_ms  = 0.0

        self._build_ui()
        self._poll_log()
        self._startup_probe()
        self._poll_sysinfo()
        self._start_control_server()

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._style_ttk()

        # Header
        hdr = tk.Frame(self, bg=PANEL, padx=14, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Open LLM Server Manager", bg=PANEL, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(hdr, text=f"port {PORT}", bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="right", padx=4)

        # Model selector
        sel = tk.Frame(self, bg=BG, padx=14, pady=8)
        sel.pack(fill="x")

        sel_hdr = tk.Frame(sel, bg=BG)
        sel_hdr.pack(fill="x")
        tk.Label(sel_hdr, text="Model", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", anchor="w")
        self._small_btn(sel_hdr, "＋ Add Model", "#1a5c3a",
                        self._add_model).pack(side="right")

        _default = self._default_model if self._default_model in self._models else list(self._models.keys())[0]
        self._model_var = tk.StringVar(value=_default)
        self._combo = ttk.Combobox(sel, textvariable=self._model_var,
                                   values=list(self._models.keys()),
                                   state="readonly", font=("Segoe UI", 9))
        self._combo.pack(fill="x", pady=(3, 0))
        self._combo.bind("<<ComboboxSelected>>", self._on_model_change)

        # Command preview
        cmd_frame = tk.Frame(self, bg=BG, padx=14, pady=4)
        cmd_frame.pack(fill="x")

        cmd_hdr = tk.Frame(cmd_frame, bg=BG)
        cmd_hdr.pack(fill="x")
        tk.Label(cmd_hdr, text="Start Command", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left", anchor="w")
        cmd_btns = tk.Frame(cmd_hdr, bg=BG)
        cmd_btns.pack(side="right")
        self._small_btn(cmd_btns, "Save Changes",    "#1a6fa3", self._save_command).pack(side="left", padx=(0, 4))
        self._small_btn(cmd_btns, "Restore Default", "#555",    self._restore_default).pack(side="left")

        cmd_inner = tk.Frame(cmd_frame, bg=LOG_BG, bd=1, relief="flat")
        cmd_inner.pack(fill="x", pady=(4, 0))
        sb_cmd = tk.Scrollbar(cmd_inner, bg=PANEL, troughcolor=BG,
                              activebackground=BORDER, width=10, relief="flat")
        self._cmd_text = tk.Text(cmd_inner, bg=LOG_BG, fg="#ce9178",
                                 font=("Consolas", 8), relief="flat", bd=6,
                                 height=6, wrap="none", yscrollcommand=sb_cmd.set,
                                 insertbackground=FG, selectbackground="#264f78")
        sb_cmd.config(command=self._cmd_text.yview)
        sb_cmd.pack(side="right", fill="y")
        self._cmd_text.pack(side="left", fill="both", expand=True)
        self._refresh_cmd_text()

        # Buttons — row 1: server controls
        btns1 = tk.Frame(self, bg=BG, padx=14, pady=4)
        btns1.pack(fill="x")
        self._btn_start = self._btn(btns1, "▶  Start", "#27ae60", self._start_server)
        self._btn_start.pack(side="left", padx=(0, 6))
        self._btn_stop = self._btn(btns1, "■  Stop", "#c0392b", self._stop_server)
        self._btn_stop.pack(side="left", padx=(0, 6))
        self._btn_stop.config(state="disabled")
        self._btn(btns1, "⟳  Check Now", "#1a6fa3", self._check_now).pack(side="left", padx=(0, 6))

        # Buttons — row 2: clients + telegram
        btns2 = tk.Frame(self, bg=BG, padx=14, pady=4)
        btns2.pack(fill="x")
        self._btn_chat = self._btn(btns2, "💬  Open Chat GUI", "#5a3a7e", self._open_chat)
        self._btn_chat.pack(side="left", padx=(0, 6))
        self._btn_chat.config(state="disabled")
        self._btn_chat_tui = self._btn(btns2, "⌨  Open Chat TUI", "#5a3a7e", self._open_chat_tui)
        self._btn_chat_tui.pack(side="left", padx=(0, 6))
        self._btn_chat_tui.config(state="disabled")
        self._btn_telegram = self._btn(btns2, "🤖  Telegram", "#1a6fa3", self._toggle_telegram)
        self._btn_telegram.pack(side="left", padx=(0, 10))
        self._auto_telegram = tk.BooleanVar(value=_load_prefs().get("auto_telegram", False))
        self._chk_telegram = tk.Checkbutton(
            btns2, text="Auto-start Telegram", variable=self._auto_telegram,
            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
            font=("Segoe UI", 9), relief="flat", bd=0,
            command=self._on_auto_telegram_toggle,
        )
        self._chk_telegram.pack(side="left")

        # Status card — left: model stats  |  right: system stats
        card = tk.Frame(self, bg=PANEL, padx=14, pady=10)
        card.pack(fill="x", padx=14, pady=(4, 4))

        left = tk.Frame(card, bg=PANEL)
        left.pack(side="left", fill="both", expand=True)

        tk.Frame(card, bg=BORDER, width=1).pack(side="left", fill="y", padx=10)

        right = tk.Frame(card, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)

        # Left column — server/model info
        self._lbl_dot, self._lbl_status = self._make_status_row(left, 0)
        self._lbl_model  = self._make_label_row(left, "Running",  1, FG)
        self._lbl_speed  = self._make_label_row(left, "Speed",    2, GREEN)
        self._lbl_last   = self._make_label_row(left, "Last req", 3, PURPLE)
        self._lbl_cd     = self._make_label_row(left, "Next chk", 4, FG_DIM)
        self._lbl_voice, self._cmb_voice = self._make_voice_row(left, 5)

        # Right column — system resources
        tk.Label(right, text="System", bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).grid(
                     row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self._lbl_sys_ram  = self._make_sys_row(right, "RAM",   1)
        self._lbl_sys_vram = self._make_sys_row(right, "VRAM",  2)
        self._lbl_sys_gpu  = self._make_sys_row(right, "GPU",   3)
        self._lbl_sys_cpu  = self._make_sys_row(right, "CPU",   4)
        self._lbl_sys_pwr  = self._make_sys_row(right, "Power", 5)

        # t/s graph
        graph_outer = tk.Frame(self, bg=BG, padx=14)
        graph_outer.pack(fill="x", pady=(0, 4))
        graph_hdr = tk.Frame(graph_outer, bg=BG)
        graph_hdr.pack(fill="x")
        tk.Label(graph_hdr, text="Generation t/s  (last 100 requests)",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8, "bold")).pack(side="left")
        self._graph = tk.Canvas(graph_outer, bg=LOG_BG, height=90,
                                highlightthickness=0, relief="flat")
        self._graph.pack(fill="x", pady=(3, 0))
        self._graph.bind("<Configure>", lambda _e: self._redraw_graph())

        # Log
        log_outer = tk.Frame(self, bg=BG, padx=14, pady=4)
        log_outer.pack(fill="both", expand=True)
        tk.Label(log_outer, text="Server Log", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        log_frame = tk.Frame(log_outer, bg=LOG_BG, bd=1, relief="flat")
        log_frame.pack(fill="both", expand=True, pady=(3, 0))
        sb = tk.Scrollbar(log_frame, bg=PANEL, troughcolor=BG,
                          activebackground=BORDER, width=10, relief="flat")
        self._log = tk.Text(log_frame, bg=LOG_BG, fg=FG, font=("Consolas", 8),
                            relief="flat", bd=6, state="disabled", wrap="word",
                            yscrollcommand=sb.set, selectbackground="#264f78",
                            insertbackground=FG)
        sb.config(command=self._log.yview)
        sb.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)

        self._log.tag_config("info",   foreground=FG_DIM)
        self._log.tag_config("ok",     foreground=GREEN)
        self._log.tag_config("err",    foreground=RED)
        self._log.tag_config("beat",   foreground=BLUE)
        self._log.tag_config("warn",   foreground=YELLOW)
        self._log.tag_config("timing", foreground=PURPLE)
        self._log.tag_config("ts",     foreground="#555555")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _style_ttk(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                    foreground=FG, selectbackground=PANEL, selectforeground=FG,
                    bordercolor=BORDER, arrowcolor=FG_DIM, padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)],
              foreground=[("readonly", FG)])

    @staticmethod
    def _btn(parent, text, bg, cmd):
        return tk.Button(parent, text=text, bg=bg, fg="white",
                         font=("Segoe UI", 9, "bold"), relief="flat",
                         padx=14, pady=6, cursor="hand2", command=cmd,
                         activebackground=bg, activeforeground="white")

    @staticmethod
    def _small_btn(parent, text, bg, cmd):
        return tk.Button(parent, text=text, bg=bg, fg=FG,
                         font=("Segoe UI", 8), relief="flat",
                         padx=8, pady=3, cursor="hand2", command=cmd,
                         activebackground=bg, activeforeground="white")

    def _make_status_row(self, parent, row):
        f = tk.Frame(parent, bg=PANEL)
        f.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        tk.Label(f, text="Status", bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8), width=10, anchor="w").pack(side="left")
        dot = tk.Label(f, text="●", bg=PANEL, fg=RED, font=("Segoe UI", 10))
        dot.pack(side="left")
        status = tk.Label(f, text="  Stopped", bg=PANEL, fg=FG, font=("Segoe UI", 9))
        status.pack(side="left")
        return dot, status

    def _make_label_row(self, parent, label, row, fg):
        f = tk.Frame(parent, bg=PANEL)
        f.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        tk.Label(f, text=label, bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8), width=10, anchor="w").pack(side="left")
        val = tk.Label(f, text="—", bg=PANEL, fg=fg, font=("Segoe UI", 9))
        val.pack(side="left")
        return val

    def _make_voice_row(self, parent, row):
        f = tk.Frame(parent, bg=PANEL)
        f.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        tk.Label(f, text="Voice", bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8), width=10, anchor="w").pack(side="left")
        status = tk.Label(f, text="—", bg=PANEL, fg=FG_DIM, font=("Segoe UI", 9))
        status.pack(side="left")
        cmb = ttk.Combobox(f, state="disabled", width=28, font=("Segoe UI", 9))
        cmb.pack(side="left", padx=(6, 0))
        cmb.bind("<<ComboboxSelected>>", self._on_voice_selected)
        return status, cmb

    def _make_sys_row(self, parent, label, row):
        tk.Label(parent, text=label, bg=PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8), width=6, anchor="w").grid(
                     row=row, column=0, sticky="w", pady=1)
        val = tk.Label(parent, text="—", bg=PANEL, fg=FG, font=("Segoe UI", 9))
        val.grid(row=row, column=1, sticky="w", pady=1)
        return val

    # ── Graph ────────────────────────────────────────────────────────────────
    def _redraw_graph(self):
        c = self._graph
        c.delete("all")
        data = list(self._graph_data)
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 4 or h < 4:
            return

        PAD_L, PAD_R, PAD_T, PAD_B = 36, 8, 6, 4
        pw = w - PAD_L - PAD_R   # plot width
        ph = h - PAD_T - PAD_B   # plot height

        # Background
        c.create_rectangle(0, 0, w, h, fill=LOG_BG, outline="")

        if not data:
            c.create_text(w // 2, h // 2, text="no data yet",
                          fill=FG_DIM, font=("Consolas", 8))
            return

        top = max(data) * 1.15 or 1.0

        def px(i):
            """x pixel for data index i in a window of up to 100 points."""
            n = len(data)
            if n == 1:
                return PAD_L + pw // 2
            return PAD_L + int(i * pw / (n - 1))

        def py(v):
            return PAD_T + ph - int(v / top * ph)

        # Horizontal grid lines at 25 / 50 / 75 / 100 %
        for frac in (0.25, 0.5, 0.75, 1.0):
            gy   = PAD_T + ph - int(frac * ph)
            gval = top * frac
            c.create_line(PAD_L, gy, w - PAD_R, gy, fill="#1e2e2a", width=1)
            c.create_text(PAD_L - 3, gy, text=f"{gval:.0f}",
                          anchor="e", fill=FG_DIM, font=("Consolas", 7))

        # Filled area under curve
        if len(data) >= 2:
            poly = [PAD_L, PAD_T + ph]
            for i, v in enumerate(data):
                poly += [px(i), py(v)]
            poly += [px(len(data) - 1), PAD_T + ph]
            c.create_polygon(poly, fill=GRAPH_FILL, outline="")

        # Line
        if len(data) == 1:
            x0, y0 = px(0), py(data[0])
            c.create_oval(x0 - 2, y0 - 2, x0 + 2, y0 + 2, fill=GREEN, outline="")
        else:
            pts = []
            for i, v in enumerate(data):
                pts += [px(i), py(v)]
            c.create_line(pts, fill=GREEN, width=1, smooth=False)

        # Current value label at right edge
        last_y = py(data[-1])
        c.create_text(w - PAD_R, last_y - 2, text=f"{data[-1]:.1f}",
                      anchor="se", fill=GREEN, font=("Consolas", 7, "bold"))

    # ── Command edit ────────────────────────────────────────────────────────
    def _refresh_cmd_text(self):
        name = self._model_var.get()
        cmd  = self._models.get(name, [])
        self._cmd_text.config(state="normal")
        self._cmd_text.delete("1.0", "end")
        self._cmd_text.insert("1.0", _cmd_to_str(cmd))

    def _on_model_change(self, _event=None):
        self._refresh_cmd_text()

    def _save_command(self):
        name = self._model_var.get()
        raw  = self._cmd_text.get("1.0", "end").strip()
        try:
            cmd = _str_to_cmd(raw)
        except Exception as e:
            self._write_log(f"Parse error: {e}", "err")
            return
        self._models[name] = cmd
        _save_models(self._models)
        self._write_log(f"Saved command for: {name}", "beat")

    def _restore_default(self):
        name = self._model_var.get()
        if name in MODELS_DEFAULT:
            self._models[name] = list(MODELS_DEFAULT[name])
            self._refresh_cmd_text()
            _save_models(self._models)
            self._write_log(f"Restored default for: {name}", "warn")

    # ── Add Model dialog ─────────────────────────────────────────────────────
    def _add_model(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add Model")
        dlg.configure(bg=BG)
        dlg.geometry("700x440")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Model Name", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(14, 0))
        name_var   = tk.StringVar()
        name_entry = tk.Entry(dlg, textvariable=name_var, bg=PANEL, fg=FG,
                              insertbackground=FG, font=("Segoe UI", 9),
                              relief="flat", bd=6)
        name_entry.pack(fill="x", padx=14, pady=(3, 0))
        name_entry.focus_set()

        tk.Label(dlg, text="Start Command", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(10, 0))
        cmd_frame = tk.Frame(dlg, bg=LOG_BG)
        cmd_frame.pack(fill="both", expand=True, padx=14, pady=(3, 0))
        sb_dlg = tk.Scrollbar(cmd_frame, bg=PANEL, troughcolor=BG,
                              activebackground=BORDER, width=10, relief="flat")
        cmd_text = tk.Text(cmd_frame, bg=LOG_BG, fg="#ce9178",
                           font=("Consolas", 8), relief="flat", bd=6,
                           wrap="none", insertbackground=FG,
                           selectbackground="#264f78",
                           yscrollcommand=sb_dlg.set)
        sb_dlg.config(command=cmd_text.yview)
        sb_dlg.pack(side="right", fill="y")
        cmd_text.pack(side="left", fill="both", expand=True)
        cmd_text.insert("1.0", _cmd_to_str(_DEFAULT_NEW_CMD))

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=14, pady=10)

        def _do_add():
            name = name_var.get().strip()
            if not name:
                name_entry.config(bg="#4a1a1a")
                return
            raw = cmd_text.get("1.0", "end").strip()
            try:
                cmd = _str_to_cmd(raw)
            except Exception as e:
                self._log_put(f"Parse error: {e}", "err")
                return
            self._models[name] = cmd
            _save_models(self._models)
            self._combo["values"] = list(self._models.keys())
            self._model_var.set(name)
            self._refresh_cmd_text()
            self._write_log(f"Added model: {name}", "beat")
            dlg.destroy()

        self._btn(btn_row, "Add Model", "#27ae60", _do_add).pack(side="left", padx=(0, 6))
        self._btn(btn_row, "Cancel", "#555555", dlg.destroy).pack(side="left")
        dlg.bind("<Return>", lambda e: _do_add())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ── Logging ─────────────────────────────────────────────────────────────
    def _log_put(self, text, tag="info"):
        self._log_q.put((text, tag))

    def _poll_log(self):
        try:
            while True:
                text, tag = self._log_q.get_nowait()

                if tag == "__timing__":
                    d       = json.loads(text)
                    gen     = d.get("gen", 0.0)
                    pre     = d.get("pre", 0.0)
                    gen_n   = d.get("gen_n", 0)
                    pre_n   = d.get("pre_n", 0)
                    total_s = d.get("total_ms", 0.0) / 1000
                    ctx_n   = gen_n + pre_n

                    self._tps_gen = gen
                    self._tps_pre = pre
                    self._lbl_speed.config(
                        text=f"gen {gen:.1f} t/s  ·  prefill {pre:.0f} t/s")
                    self._lbl_last.config(
                        text=f"{gen_n} tok out  ·  {ctx_n} ctx  ·  {total_s:.1f}s")

                    # Update graph
                    if gen > 0:
                        self._graph_data.append(gen)
                        self._redraw_graph()

                    self._write_log(
                        f"⚡ gen {gen:.1f} t/s  ·  prefill {pre:.0f} t/s"
                        f"  ·  {gen_n} tok  ·  {total_s:.1f}s",
                        "timing")
                    continue

                self._write_log(text, tag)
                if "HTTP server listening" in text or "server is listening on" in text:
                    self._set_running()
                elif "model loaded" in text:
                    self._log_put("Model loaded — waiting for HTTP…", "ok")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _write_log(self, text, tag="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log.config(state="normal")
        self._log.insert("end", f"[{ts}] ", "ts")
        self._log.insert("end", text + "\n", tag)
        # Keep the buffer bounded — drop oldest lines when over the cap
        end_line = int(self._log.index("end-1c").split(".")[0])
        if end_line > MAX_LOG_LINES:
            self._log.delete("1.0", f"{end_line - MAX_LOG_LINES}.0")
        self._log.config(state="disabled")
        self._log.see("end")

    # ── Server control ───────────────────────────────────────────────────────
    def _start_server(self):
        if self._running:
            return
        name = self._model_var.get()
        raw = self._cmd_text.get("1.0", "end").strip()
        try:
            cmd = _str_to_cmd(raw)
        except Exception as e:
            self._log_put(f"Command parse error: {e}", "err")
            return

        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

        self._engine = _detect_engine(cmd)
        self._log_put(f"Starting {name} [{self._engine}]", "beat")
        self._btn_start.config(state="disabled")
        self._combo.config(state="disabled")
        self._lbl_status.config(text="  Loading…")
        self._lbl_dot.config(fg=YELLOW)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._running  = True
            self._external = False
            self._btn_stop.config(state="normal")
            threading.Thread(target=self._read_proc, daemon=True).start()
            if self._engine == "wsl":
                # WSL processes don't forward stdout reliably — poll the API directly
                self._log_put("WSL engine: polling /v1/models every 15 s (up to 10 min)…", "info")
                threading.Thread(target=self._wsl_startup_poll, daemon=True).start()
            else:
                self._schedule_heartbeat(45)
        except Exception as e:
            self._log_put(f"Failed to start: {e}", "err")
            self._btn_start.config(state="normal")
            self._combo.config(state="readonly")
            self._lbl_status.config(text="  Error")
            self._lbl_dot.config(fg=RED)

    def _stop_server(self):
        self._cancel_all_timers()
        if self._proc:
            self._log_put("Terminating server process…", "warn")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            if self._engine == "wsl":
                self._kill_wsl_server()
        else:
            if self._engine == "wsl":
                self._kill_wsl_server()
            else:
                self._log_put("Killing external llama-server…", "warn")
                subprocess.run(
                    ["powershell", "-Command",
                     "Stop-Process -Name llama-server -Force -ErrorAction SilentlyContinue"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        self._stop_voice_server()
        if self._auto_telegram.get():
            self._stop_telegram()
        self._reset_ui()
        self._log_put("Server stopped", "err")

    def _kill_wsl_server(self):
        """Kill any process listening on PORT inside WSL."""
        self._log_put(f"Killing WSL server on port {PORT}…", "warn")
        subprocess.run(
            ["wsl", "bash", "-c",
             f"kill $(lsof -t -i:{PORT} 2>/dev/null) 2>/dev/null; "
             f"pkill -f 'api_server' 2>/dev/null; true"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=8,
        )

    def _reset_ui(self):
        self._running  = False
        self._external = False
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._btn_chat.config(state="disabled")
        self._btn_chat_tui.config(state="disabled")
        self._combo.config(state="readonly")
        self._lbl_dot.config(fg=RED)
        self._lbl_status.config(text="  Stopped")
        self._lbl_model.config(text="—")
        self._lbl_speed.config(text="—")
        self._lbl_last.config(text="—")
        self._lbl_cd.config(text="—")

    def _set_running(self):
        self._lbl_dot.config(fg=GREEN)
        self._lbl_status.config(text="  Running")
        self._btn_chat.config(state="normal")
        self._btn_chat_tui.config(state="normal")
        self._start_voice_server()
        if self._auto_telegram.get():
            self._start_telegram()

    def _wsl_startup_poll(self):
        """Background thread: poll /v1/models every 15 s until ready or 10 min elapsed."""
        import time
        interval = 15      # seconds between probes
        max_tries = 40     # 40 × 15 s = 600 s = 10 min
        for attempt in range(1, max_tries + 1):
            time.sleep(interval)
            if not self._running:
                return  # server was stopped while we were waiting
            result = self._probe()
            if result["ok"]:
                self.after(0, lambda r=result: self._apply_wsl_ready(r))
                return
            remaining = (max_tries - attempt) * interval
            self.after(0, lambda a=attempt, rem=remaining: self._log_put(
                f"WSL probe {a}/{max_tries} — not ready yet, {rem}s remain", "info"
            ))
        # Gave up
        self.after(0, lambda: self._log_put(
            "WSL startup timeout (10 min) — server did not respond", "err"
        ))

    def _apply_wsl_ready(self, result):
        """Called on the Tk thread when WSL probe first succeeds."""
        model_short = result["model"]
        self._lbl_model.config(text=model_short)
        self._log_put(f"✓  {model_short}  ·  vLLM server ready", "ok")
        self._set_running()
        self._schedule_heartbeat()   # switch to normal 5-min heartbeat cycle

    # ── Voice server lifecycle ───────────────────────────────────────────────
    _VOICE_PYTHON = r"C:\Users\timob\claude-projects\qwen3-manager\.venv\Scripts\python.exe"
    _VOICE_CWD    = r"C:\Users\timob\claude-projects\qwen3-manager"

    def _start_voice_server(self) -> None:
        if self._voice_proc and self._voice_proc.poll() is None:
            return  # already running
        try:
            cmd = [self._VOICE_PYTHON, "-m", "uvicorn", "eli_voice_server:app",
                   "--host", "127.0.0.1", "--port", "1236"]
            self._voice_proc = subprocess.Popen(
                cmd, cwd=self._VOICE_CWD,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._read_voice_proc, daemon=True).start()
            self._lbl_voice.config(text="starting…")
        except Exception as e:
            self._log_put(f"[voice] Failed to start: {e}", "err")
            self._lbl_voice.config(text="error")

    def _stop_voice_server(self) -> None:
        if self._voice_proc and self._voice_proc.poll() is None:
            # Ask the server to stop audio and exit cleanly before hard-killing
            try:
                import urllib.request
                urllib.request.urlopen(
                    urllib.request.Request("http://127.0.0.1:1236/shutdown", data=b"", method="POST"),
                    timeout=2,
                )
            except Exception:
                pass
            try:
                self._voice_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._voice_proc.terminate()
                try:
                    self._voice_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._voice_proc.kill()
        self._voice_proc = None
        self._on_voice_server_stopped()

    def _read_voice_proc(self) -> None:
        for line in self._voice_proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            ll = line.lower()
            if "application startup complete" in ll or "uvicorn running" in ll:
                self._log_q.put(("[voice] Ready", "ok"))
                self.after(0, lambda: self._lbl_voice.config(text="running"))
                self.after(500, self._fetch_voices)   # small delay for server readiness
            elif "error" in ll:
                self._log_q.put((f"[voice] {line}", "err"))
            else:
                self._log_q.put((f"[voice] {line}", "info"))
        self._log_q.put(("[voice] Process exited", "warn"))
        self.after(0, self._on_voice_server_stopped)

    def _on_voice_server_stopped(self) -> None:
        self._lbl_voice.config(text="stopped")
        self._cmb_voice.config(state="disabled")
        self._cmb_voice.set("")

    def _fetch_voices(self) -> None:
        """Fetch voice list from TTS server, populate the combobox, restore saved preference."""
        import urllib.request, json as _json
        try:
            with urllib.request.urlopen("http://127.0.0.1:1236/voices", timeout=3) as r:
                data = _json.loads(r.read())
            voices = data.get("voices", [])
            if not voices:
                return
            self._cmb_voice["values"] = voices
            self._cmb_voice.config(state="readonly")
            saved = _load_prefs().get("voice")
            target = saved if saved in voices else voices[0]
            self._cmb_voice.set(target)
            # Always apply to server so it matches UI (server resets on restart)
            threading.Thread(target=self._set_voice_bg, args=(target,), daemon=True).start()
        except Exception as e:
            self._log_put(f"[voice] Could not fetch voice list: {e}", "warn")

    def _on_voice_selected(self, _event=None) -> None:
        """Called when user picks a voice from the combobox."""
        raw = self._cmb_voice.get()
        if not raw:
            return
        self._lbl_voice.config(text="switching…")
        threading.Thread(target=self._set_voice_bg, args=(raw, True), daemon=True).start()

    def _set_voice_bg(self, voice_id: str, save: bool = False) -> None:
        """POST /voice to the TTS server (background thread)."""
        import urllib.request, urllib.parse, json as _json
        try:
            url = f"http://127.0.0.1:1236/voice?voice_id={urllib.parse.quote(voice_id)}"
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                _json.loads(r.read())
            if save:
                prefs = _load_prefs()
                prefs["voice"] = voice_id
                _save_prefs(prefs)
            self._log_q.put((f"[voice] Switched to {voice_id}", "ok"))
            self.after(0, lambda: self._lbl_voice.config(text="running"))
        except Exception as e:
            self._log_q.put((f"[voice] Switch failed: {e}", "err"))
            self.after(0, lambda: self._lbl_voice.config(text="error"))

    def _read_proc(self):
        for line in self._proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if self._engine == "llama" and self._try_parse_tps(line):
                continue
            ll = line.lower()
            if "error" in ll or "failed" in ll:
                tag = "err"
            elif "listening" in ll or "model loaded" in ll:
                tag = "ok"
            elif "warning" in ll or "warn" in ll:
                tag = "warn"
            else:
                tag = "info"
            self._log_q.put((line, tag))
        self._log_q.put(("Process exited", "err"))
        self.after(0, self._on_proc_exit)

    def _try_parse_tps(self, line: str) -> bool:
        """Parse timing data from a server log line.

        Returns True if the line is part of the timing block (suppressed from
        raw log; a synthesized ⚡ summary is emitted via __timing__ instead).
        """
        # JSON structured logging
        try:
            obj = json.loads(line)
            t   = obj.get("timings", {})
            gen = t.get("predicted_per_second", 0.0)
            if gen > 0:
                self._log_q.put((json.dumps({
                    "gen":      gen,
                    "pre":      t.get("prompt_per_second", 0.0),
                    "gen_n":    t.get("predicted_n", 0),
                    "pre_n":    t.get("prompt_n", 0),
                    "total_ms": t.get("predicted_ms", 0.0) + t.get("prompt_ms", 0.0),
                }), "__timing__"))
                return True
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        # Classic 3-line block
        if "slot print_timing" in line.lower():
            return True

        m = _RE_PRE_TIMING.search(line)
        if m:
            self._pend_pre_ms  = float(m.group(1))
            self._pend_pre_n   = int(m.group(2))
            self._pend_pre_tps = float(m.group(3))
            return True

        # _RE_GEN_TIMING also matches "prompt eval time" lines, but those
        # were already handled above and returned — so this only runs for pure gen lines.
        m = _RE_GEN_TIMING.search(line)
        if m:
            self._pend_gen_ms  = float(m.group(1))
            self._pend_gen_n   = int(m.group(2))
            self._pend_gen_tps = float(m.group(3))
            return True

        m = _RE_TOTAL_TIMING.search(line)
        if m:
            self._log_q.put((json.dumps({
                "gen":      self._pend_gen_tps,
                "pre":      self._pend_pre_tps,
                "gen_n":    self._pend_gen_n,
                "pre_n":    self._pend_pre_n,
                "total_ms": float(m.group(1)),
            }), "__timing__"))
            return True

        return False

    def _on_proc_exit(self):
        self._proc = None
        self._cancel_all_timers()
        self._reset_ui()

    # ── Timers ──────────────────────────────────────────────────────────────
    def _cancel_all_timers(self):
        for attr in ("_hb_after", "_cd_after"):
            h = getattr(self, attr, None)
            if h:
                self.after_cancel(h)
                setattr(self, attr, None)

    def _schedule_heartbeat(self, delay=HB_INTERVAL):
        if self._hb_after:
            self.after_cancel(self._hb_after)
        self._hb_secs = delay
        self._tick_countdown()
        self._hb_after = self.after(delay * 1000, self._run_heartbeat)

    def _tick_countdown(self):
        if self._hb_secs > 0 and (self._running or self._external):
            m, s = divmod(self._hb_secs, 60)
            self._lbl_cd.config(text=f"{m}:{s:02d}")
            self._hb_secs -= 1
            self._cd_after = self.after(1000, self._tick_countdown)

    # ── Full heartbeat (every 5 min) ────────────────────────────────────────
    def _run_heartbeat(self):
        self._log_put("Heartbeat…", "beat")
        threading.Thread(target=self._probe_thread, args=(True,), daemon=True).start()

    def _check_now(self):
        self._cancel_all_timers()
        self._log_put("Manual check…", "beat")
        threading.Thread(target=self._probe_thread, args=(True,), daemon=True).start()

    def _probe_thread(self, reschedule=False):
        result = self._probe()
        self.after(0, lambda: self._apply_probe(result, reschedule))

    def _probe(self):
        try:
            req = urllib.request.Request(f"{URL}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            model_path  = data["data"][0]["id"] if data.get("data") else "unknown"
            model_short = model_path.replace("\\", "/").split("/")[-1]
            return {"ok": True, "model": model_short}
        except urllib.error.URLError:
            return {"ok": False, "error": "server unreachable"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _apply_probe(self, result, reschedule=False):
        if result["ok"]:
            self._lbl_model.config(text=result["model"])
            self._lbl_dot.config(fg=GREEN)
            self._lbl_status.config(text="  Running")
            self._log_put(f"✓  {result['model']}  ·  server alive", "ok")
        else:
            self._log_put(f"✗  {result.get('error', 'probe failed')}", "err")
            if not self._running:
                self._lbl_dot.config(fg=RED)
                self._lbl_status.config(text="  Stopped")

        if reschedule and (self._running or self._external):
            self._schedule_heartbeat()

    # ── Startup probe ────────────────────────────────────────────────────────
    def _startup_probe(self):
        def _go():
            r = self._probe()
            self.after(0, lambda: self._apply_startup(r))
        threading.Thread(target=_go, daemon=True).start()

    def _apply_startup(self, result):
        if result["ok"]:
            self._external = True
            self._running  = True
            # Infer engine from whichever profile is currently selected
            try:
                cmd = _str_to_cmd(self._cmd_text.get("1.0", "end").strip())
                self._engine = _detect_engine(cmd)
            except Exception:
                self._engine = "llama"
            self._lbl_model.config(text=result["model"])
            self._lbl_dot.config(fg=GREEN)
            self._lbl_status.config(text="  Running (external)")
            self._btn_stop.config(state="normal")
            self._btn_chat.config(state="normal")
            self._btn_chat_tui.config(state="normal")
            self._log_put(f"Detected running server: {result['model']}", "beat")
            self._schedule_heartbeat()
        else:
            self._log_put("No server detected on startup", "info")

    # ── System stats polling (every 2 s) ────────────────────────────────────
    def _poll_sysinfo(self):
        threading.Thread(target=self._sysinfo_thread, daemon=True).start()

    def _sysinfo_thread(self):
        ram_used, ram_total, ram_pct              = _get_ram()
        vram_used, vram_total, gpu_load, gpu_w, gpu_t = _get_gpu_stats()
        cpu_pct                                   = self._cpu_delta()
        self.after(0, lambda: self._apply_sysinfo(
            ram_used, ram_total, ram_pct, vram_used, vram_total, gpu_load, cpu_pct,
            gpu_w, gpu_t))
        self.after(2000, self._poll_sysinfo)

    def _cpu_delta(self) -> float:
        """Compute CPU % since the last call using Windows GetSystemTimes."""
        class _FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
        idle, kernel, user = _FT(), _FT(), _FT()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        def ft(s): return s.hi * 0x100000000 + s.lo
        idle_v   = ft(idle)
        total_v  = ft(kernel) + ft(user)   # kernel includes idle
        d_idle   = idle_v  - self._cpu_prev_idle
        d_total  = total_v - self._cpu_prev_total
        self._cpu_prev_idle  = idle_v
        self._cpu_prev_total = total_v
        if d_total == 0:
            return 0.0
        return max(0.0, min(100.0, (d_total - d_idle) / d_total * 100))

    def _apply_sysinfo(self, ram_used, ram_total, ram_pct,
                       vram_used, vram_total, gpu_load, cpu_pct,
                       gpu_w, gpu_t):
        self._lbl_sys_ram.config(
            text=f"{ram_used:.1f} / {ram_total:.0f} GB  ({ram_pct}%)")

        if vram_used is not None:
            vram_pct = vram_used * 100 // vram_total
            self._lbl_sys_vram.config(
                text=f"{vram_used:,} / {vram_total:,} MiB  ({vram_pct}%)")
            gpu_text = f"{gpu_load}%"
            if gpu_t is not None:
                gpu_text += f"  ·  {gpu_t}°C"
            self._lbl_sys_gpu.config(text=gpu_text)
            self._lbl_sys_pwr.config(
                text=f"{gpu_w:.0f} W" if gpu_w is not None else "—")

        self._lbl_sys_cpu.config(text=f"{cpu_pct:.0f}%")

    # ── Control API (loopback HTTP on CONTROL_PORT) ──────────────────────────
    def _start_control_server(self):
        """Start a tiny HTTP server on localhost:CONTROL_PORT for chat.py integration.

        Endpoints:
          GET  /api/status   → {running, model, external}
          GET  /api/profiles → [profile names from commands.json]
          POST /api/stop     → triggers _stop_server() on the UI thread
          POST /api/start    → body: {"profile": "..."} — selects model and triggers _start_server()

        All UI mutations are dispatched via self.after(0, ...) to stay on the Tkinter thread.
        """
        mgr = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silence default stderr logging
                pass

            def _send(self, code: int, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/status":
                    data = json.dumps({
                        "running":  mgr._running,
                        "model":    mgr._model_var.get() if mgr._running else None,
                        "external": mgr._external,
                    }).encode()
                    self._send(200, data)
                elif self.path == "/api/profiles":
                    self._send(200, json.dumps(list(mgr._models.keys())).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}

                if self.path == "/api/stop":
                    mgr.after(0, mgr._stop_server)
                    self._send(200, b'{"ok":true}')

                elif self.path == "/api/start":
                    profile = body.get("profile", "")
                    if profile in mgr._models:
                        def _do():
                            mgr._model_var.set(profile)
                            mgr._combo["values"] = list(mgr._models.keys())
                            mgr._refresh_cmd_text()
                            mgr._start_server()
                        mgr.after(0, _do)
                        self._send(200, b'{"ok":true}')
                    else:
                        available = list(mgr._models.keys())
                        msg = json.dumps({"error": f"unknown profile: {profile}",
                                          "available": available}).encode()
                        self._send(400, msg)

                elif self.path == "/api/timing":
                    # Posted by chat.py after each completed response with gen/prefill stats
                    mgr._log_q.put((json.dumps(body), "__timing__"))
                    self._send(200, b'{"ok":true}')

                else:
                    self._send(404, b'{"error":"not found"}')

        srv = http.server.HTTPServer(("127.0.0.1", CONTROL_PORT), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ── Open Chat ────────────────────────────────────────────────────────────
    def _open_chat(self):
        here = os.path.dirname(os.path.abspath(__file__))
        python = os.path.join(here, ".venv", "Scripts", "python.exe")
        main   = os.path.join(here, "qt", "main.py")
        subprocess.Popen(
            [python, main, "--continue"],
            cwd=here,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _open_chat_tui(self):
        here = os.path.dirname(os.path.abspath(__file__))
        python = os.path.join(here, ".venv", "Scripts", "python.exe")
        chat   = os.path.join(here, "chat.py")
        subprocess.Popen(
            ["cmd", "/c", "start", "cmd", "/k", python, chat, "--continue"],
            cwd=here,
        )

    # ── Telegram bot lifecycle ───────────────────────────────────────────────
    def _toggle_telegram(self):
        if self._telegram_proc and self._telegram_proc.poll() is None:
            self._stop_telegram()
        else:
            self._start_telegram()

    def _start_telegram(self) -> None:
        if self._telegram_proc and self._telegram_proc.poll() is None:
            return  # already running
        here = os.path.dirname(os.path.abspath(__file__))
        python = os.path.join(here, ".venv", "Scripts", "python.exe")
        try:
            self._telegram_proc = subprocess.Popen(
                [python, "-m", "telegram_bot.main"],
                cwd=here,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._read_telegram_proc, daemon=True).start()
            self._btn_telegram.config(text="🤖  Stop Telegram")
            self._log_put("[telegram] Bot started", "ok")
        except Exception as e:
            self._log_put(f"[telegram] Failed to start: {e}", "err")

    def _stop_telegram(self) -> None:
        if self._telegram_proc and self._telegram_proc.poll() is None:
            self._telegram_proc.terminate()
            try:
                self._telegram_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._telegram_proc.kill()
        self._telegram_proc = None
        self._btn_telegram.config(text="🤖  Telegram")
        self._log_put("[telegram] Bot stopped", "warn")

    def _read_telegram_proc(self) -> None:
        proc = self._telegram_proc
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._log_put(f"[telegram] {line}", "info")
        # process exited
        if self._telegram_proc is proc:
            self._telegram_proc = None
            self.after(0, lambda: self._btn_telegram.config(text="🤖  Telegram"))
            self._log_put("[telegram] Bot exited", "warn")

    def _on_auto_telegram_toggle(self):
        prefs = _load_prefs()
        prefs["auto_telegram"] = self._auto_telegram.get()
        _save_prefs(prefs)

    # ── Close ────────────────────────────────────────────────────────────────
    def _on_close(self):
        # Persist window geometry before closing
        prefs = _load_prefs()
        prefs["window_geometry"] = self.geometry()
        _save_prefs(prefs)

        if self._running and not self._external:
            self._stop_server()
        else:
            self._stop_voice_server()
        self._stop_telegram()
        self.destroy()


if __name__ == "__main__":
    app = ServerManager()
    app.mainloop()
