"""
eli_voice_server.py — Kokoro ONNX TTS + faster-whisper STT server for Eli.

Endpoints:
  POST /play          Synthesize and play audio (blocking)
  POST /tts           Synthesize, return raw int16 PCM
  POST /tts/stream    Synthesize per-sentence, stream PCM chunks
  POST /transcribe    Transcribe raw int16 PCM → {"text": "..."}
  POST /voice         Switch active voice
  GET  /voices        List all available voices
  GET  /              Health / info
"""

import re
import struct
import numpy as np
import sounddevice as sd
from pathlib import Path
from typing import List

from kokoro_onnx import Kokoro
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE        = Path(__file__).parent
MODEL_PATH   = str(_HERE / "kokoro_models" / "kokoro-v1.0.onnx")
VOICES_PATH  = str(_HERE / "kokoro_models" / "voices-v1.0.bin")

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_VOICE = "am_eric"
TTS_SPEED     = 1.0   # 1.0 = natural pace. Lower = faster, higher = slower.
SAMPLE_RATE   = 24000  # Kokoro outputs 24 kHz

# ── TTS engine ────────────────────────────────────────────────────────────────
_kokoro       = Kokoro(MODEL_PATH, VOICES_PATH)
current_voice = DEFAULT_VOICE


def _synthesize(text: str, voice: str = None, speed: float = None) -> bytes:
    """Synthesize text → raw int16 PCM bytes at 24 kHz."""
    v = voice or current_voice
    s = speed if speed is not None else TTS_SPEED
    samples, _ = _kokoro.create(text, voice=v, speed=s, lang="en-us")
    pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
    return pcm.tobytes()


def _play(audio_bytes: bytes) -> None:
    """Play raw int16 PCM (blocking)."""
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    sd.play(audio, SAMPLE_RATE)
    sd.wait()


# ── Whisper STT (lazy) ────────────────────────────────────────────────────────
_whisper_model = None

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


# ── Sentence splitter ─────────────────────────────────────────────────────────
_SENT = re.compile(r'([^.!?;,\n]+[.!?;,\n]+|[^.!?;,\n]+$)')

def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT.findall(text) if s.strip()] if text.strip() else []


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Eli Voice Server", version="2.0.0")

class TTSRequest(BaseModel):
    text: str


@app.get("/")
async def root():
    return {
        "service": "Eli Voice Server",
        "version": "2.0.0",
        "engine": "kokoro-onnx",
        "current_voice": current_voice,
        "sample_rate": SAMPLE_RATE,
        "endpoints": ["/", "/play", "/tts", "/tts/stream", "/transcribe", "/voice", "/voices"],
    }


@app.post("/play")
async def play_text(request: TTSRequest):
    """Synthesize and play immediately (blocks until audio finishes)."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text must not be empty")
    try:
        _play(_synthesize(request.text))
        return {"status": "ok", "voice": current_voice}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playback failed: {e}")


@app.post("/tts")
async def synthesize_full(request: TTSRequest):
    """Synthesize and return raw int16 PCM bytes."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text must not be empty")
    try:
        return Response(
            content=_synthesize(request.text),
            media_type="audio/basic",
            headers={"X-Sample-Rate": str(SAMPLE_RATE), "X-Num-Channels": "1", "X-Sample-Width": "2"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")


@app.post("/tts/stream")
async def synthesize_stream(request: TTSRequest):
    """Stream one PCM chunk per sentence — lowest latency."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text must not be empty")
    return StreamingResponse(
        (_synthesize(s) for s in _sentences(request.text)),
        media_type="audio/basic",
        headers={"X-Sample-Rate": str(SAMPLE_RATE), "X-Num-Channels": "1", "X-Sample-Width": "2"},
    )


@app.post("/transcribe")
async def transcribe_audio(request: Request, sample_rate: int = 16000):
    """Transcribe raw int16 mono PCM bytes → {"text": "..."}."""
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="No audio data received")
    audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _get_whisper().transcribe(audio_np, language="en")
    return {"text": " ".join(s.text for s in segments).strip()}


@app.post("/voice")
async def set_voice(voice_id: str):
    """Switch the active voice. Must be a valid Kokoro voice name."""
    global current_voice
    available = _kokoro.get_voices()
    if voice_id not in available:
        raise HTTPException(status_code=400, detail=f"Unknown voice: {voice_id!r}. Available: {available}")
    current_voice = voice_id
    return {"ok": True, "voice": current_voice}


@app.get("/voices")
async def list_voices():
    """List all available Kokoro voices."""
    return {"voices": _kokoro.get_voices(), "current": current_voice}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=1236)
