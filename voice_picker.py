"""
voice_picker.py — Audition every Kokoro voice and pick a favourite.

Run with the qwen3-manager venv (no server needed — talks to Kokoro directly):
  .venv/Scripts/python.exe voice_picker.py
"""

import sys
import threading
import tkinter as tk
from tkinter import ttk, font as tkfont
from pathlib import Path

import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE       = Path(__file__).parent
MODEL_PATH  = str(_HERE / "kokoro_models" / "kokoro-v1.0.onnx")
VOICES_PATH = str(_HERE / "kokoro_models" / "voices-v1.0.bin")
SAMPLE_RATE = 24000

DEFAULT_TEXT = (
    "Hey, I'm thinking through a problem and could use a second opinion. "
    "If we front-load the architecture decisions now, we avoid the expensive "
    "rework later — but that means slowing down the initial build. "
    "What's your read on the trade-off?"
)

# ── Voice groups ──────────────────────────────────────────────────────────────
GROUP_LABELS = {
    "af": "American English — Female",
    "am": "American English — Male",
    "bf": "British English — Female",
    "bm": "British English — Male",
    "ef": "Spanish — Female",
    "em": "Spanish — Male",
    "ff": "French — Female",
    "hf": "Hindi — Female",
    "hm": "Hindi — Male",
    "if": "Italian — Female",
    "im": "Italian — Male",
    "jf": "Japanese — Female",
    "jm": "Japanese — Male",
    "pf": "Portuguese — Female",
    "pm": "Portuguese — Male",
    "zf": "Chinese — Female",
    "zm": "Chinese — Male",
}

# ── Colours ───────────────────────────────────────────────────────────────────
BG       = "#1a1a1a"
PANEL    = "#242424"
ACCENT   = "#2a5298"
ACCENT_H = "#3a6ec8"
ACTIVE   = "#1a6e3a"
FG       = "#e0e0e0"
FG_DIM   = "#666666"
FG_HEAD  = "#aaaaaa"
BORDER   = "#333333"


class VoicePicker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Kokoro Voice Picker")
        self.configure(bg=BG)
        self.geometry("700x720")
        self.minsize(600, 500)

        self._kokoro     = None   # loaded in background
        self._playing    = False
        self._stop_flag  = threading.Event()
        self._active_btn = None
        self._active_voice = tk.StringVar(value="—")
        self._status     = tk.StringVar(value="Loading Kokoro model…")
        self._buttons    = {}     # voice_id → Button widget

        self._build_ui()
        self.after(100, self._load_model_bg)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        bold  = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        norm  = tkfont.Font(family="Segoe UI", size=9)
        small = tkfont.Font(family="Segoe UI", size=8)
        head  = tkfont.Font(family="Segoe UI", size=8, weight="bold")

        # ── Top bar ──────────────────────────────────────────────────────────
        top = tk.Frame(self, bg=PANEL, pady=10)
        top.pack(fill="x", padx=0, pady=0)

        tk.Label(top, text="Voice Picker", font=bold, bg=PANEL, fg=FG,
                 padx=16).pack(side="left")

        now_frame = tk.Frame(top, bg=PANEL)
        now_frame.pack(side="right", padx=16)
        tk.Label(now_frame, text="Playing:", font=small, bg=PANEL,
                 fg=FG_DIM).pack(side="left")
        tk.Label(now_frame, textvariable=self._active_voice, font=bold,
                 bg=PANEL, fg="#44cc88", width=18, anchor="w").pack(side="left", padx=(4, 0))

        # ── Sample text ───────────────────────────────────────────────────────
        txt_frame = tk.Frame(self, bg=BG, pady=8)
        txt_frame.pack(fill="x", padx=16)
        tk.Label(txt_frame, text="Sample text:", font=small, bg=BG,
                 fg=FG_DIM).pack(anchor="w")
        self._txt = tk.Text(txt_frame, height=3, font=norm, bg=PANEL, fg=FG,
                            insertbackground=FG, relief="flat",
                            wrap="word", padx=8, pady=6)
        self._txt.insert("1.0", DEFAULT_TEXT)
        self._txt.pack(fill="x", pady=(4, 0))

        # ── Voice list ────────────────────────────────────────────────────────
        list_outer = tk.Frame(self, bg=BG)
        list_outer.pack(fill="both", expand=True, padx=16, pady=8)

        canvas = tk.Canvas(list_outer, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical",
                                  command=canvas.yview)
        self._scroll_frame = tk.Frame(canvas, bg=BG)

        self._scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._canvas = canvas
        self._head_font = head
        self._norm_font = norm

        # ── Status bar ────────────────────────────────────────────────────────
        status_bar = tk.Frame(self, bg=PANEL, pady=6)
        status_bar.pack(fill="x", side="bottom")
        tk.Label(status_bar, textvariable=self._status, font=small,
                 bg=PANEL, fg=FG_DIM, padx=16).pack(side="left")
        tk.Button(status_bar, text="■ Stop", font=small,
                  bg="#5a2020", fg=FG, activebackground="#7a2020",
                  activeforeground=FG, relief="flat", padx=10, pady=2,
                  cursor="hand2", command=self._stop_playback).pack(side="right", padx=16)

    def _populate_voices(self, voices: list[str]):
        """Build the grouped voice button grid (runs on UI thread)."""
        # Group voices
        groups: dict[str, list[str]] = {}
        for v in sorted(voices):
            prefix = v[:2]
            groups.setdefault(prefix, []).append(v)

        for prefix, group_voices in groups.items():
            label = GROUP_LABELS.get(prefix, prefix.upper())

            hdr = tk.Label(self._scroll_frame, text=label,
                           font=self._head_font, bg=BG, fg=FG_HEAD,
                           anchor="w", pady=6)
            hdr.pack(fill="x", padx=4)

            row_frame = tk.Frame(self._scroll_frame, bg=BG)
            row_frame.pack(fill="x", padx=4, pady=(0, 8))

            for i, voice_id in enumerate(group_voices):
                short = voice_id[3:]  # strip prefix, e.g. "am_michael" → "michael"
                btn = tk.Button(
                    row_frame,
                    text=short,
                    font=self._norm_font,
                    bg=ACCENT, fg=FG,
                    activebackground=ACCENT_H, activeforeground=FG,
                    relief="flat", padx=14, pady=6,
                    cursor="hand2",
                    command=lambda v=voice_id: self._play_voice(v),
                )
                btn.grid(row=i // 5, column=i % 5, padx=4, pady=3, sticky="ew")
                self._buttons[voice_id] = btn

            for col in range(5):
                row_frame.columnconfigure(col, weight=1)

        self._status.set(f"{len(voices)} voices ready — click any to audition")

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_model_bg(self):
        def _load():
            try:
                kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
                self._kokoro = kokoro
                voices = kokoro.get_voices()
                self.after(0, self._populate_voices, voices)
            except Exception as e:
                self.after(0, self._status.set, f"Failed to load model: {e}")

        threading.Thread(target=_load, daemon=True).start()

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_voice(self, voice_id: str):
        if self._kokoro is None:
            return
        self._stop_playback()

        # Reset previous active button
        if self._active_btn:
            self._active_btn.config(bg=ACCENT, activebackground=ACCENT_H)

        btn = self._buttons.get(voice_id)
        if btn:
            btn.config(bg=ACTIVE, activebackground=ACTIVE)
            self._active_btn = btn

        self._active_voice.set(voice_id)
        self._status.set(f"Synthesizing {voice_id}…")
        self._stop_flag.clear()
        text = self._txt.get("1.0", "end").strip() or DEFAULT_TEXT

        def _run():
            try:
                samples, _ = self._kokoro.create(
                    text, voice=voice_id, speed=1.0, lang="en-us"
                )
                if self._stop_flag.is_set():
                    return
                audio = (samples * 32767).clip(-32768, 32767).astype(np.int16)
                audio_f = audio.astype(np.float32) / 32768.0
                self.after(0, self._status.set, f"Playing: {voice_id}")
                sd.play(audio_f, SAMPLE_RATE)
                sd.wait()
                if not self._stop_flag.is_set():
                    self.after(0, self._status.set,
                               f"Done — {len(self._buttons)} voices available")
            except Exception as e:
                self.after(0, self._status.set, f"Error: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def _stop_playback(self):
        self._stop_flag.set()
        sd.stop()
        if self._active_btn:
            self._active_btn.config(bg=ACCENT, activebackground=ACCENT_H)
            self._active_btn = None
        self._active_voice.set("—")


if __name__ == "__main__":
    app = VoicePicker()
    app.mainloop()
