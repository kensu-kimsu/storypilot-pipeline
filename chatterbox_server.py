"""Small localhost HTTP bridge for the official Chatterbox Python package.

Run this file from a Python 3.11 environment with chatterbox-tts installed.
The StoryPilot application posts one script chunk at a time to /v1/tts.
"""
from __future__ import annotations

import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODEL: Any = None
TORCHAUDIO: Any = None
REFERENCE_AUDIO: Path | None = None
DEVICE = "cpu"
PITCH_SHIFT_SEMITONES = -2.0
MODEL_LOCK = threading.Lock()


def direction_parameters(emotion: str, tone: str, pace: str, intensity: float) -> tuple[float, float]:
    """Map creative direction to deliberately contrasting Chatterbox controls.

    Chatterbox exposes a general exaggeration control, not named emotion
    classes. Keep neutral delivery restrained and push expressive beats higher
    so the difference is audible. These are starting points; the model can
    still interpret a given line subtly.
    """
    value = f"{emotion} {tone}".lower().replace("_", " ")
    intensity = max(0.0, min(1.0, intensity))
    if any(word in value for word in ("fear", "suspense", "tense", "urgent", "shock", "dramatic", "alarm")):
        # Leave room for a suspense beat to build across consecutive chunks.
        # A lower ceiling avoids turning a tense read into a shout.
        exaggeration = 0.48 + 0.36 * intensity
    elif any(word in value for word in ("curious", "intrigu", "wonder", "reveal", "anticipat")):
        exaggeration = 0.56 + 0.30 * intensity
    elif any(word in value for word in ("sad", "somber", "calm", "reflective", "solemn")):
        exaggeration = 0.25 + 0.16 * intensity
    elif any(word in value for word in ("excited", "energetic", "angry", "forceful", "triumphant")):
        exaggeration = 0.72 + 0.24 * intensity
    else:
        exaggeration = 0.25 + 0.15 * intensity
    exaggeration = max(0.25, min(0.85, exaggeration))

    # Resemble documents lower CFG as a way to temper faster, exaggerated speech.
    # This is a style control, not a precise words-per-minute setting.
    pace = pace.strip().lower()
    cfg_weight = {"slow": 0.28, "measured": 0.42, "brisk": 0.60}.get(pace, 0.42)
    if "suspense" in value or "dramatic" in value:
        cfg_weight = min(cfg_weight, 0.28)
    return exaggeration, cfg_weight


class ChatterboxHandler(BaseHTTPRequestHandler):
    server_version = "StoryPilotChatterbox/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[Chatterbox] " + fmt % args, flush=True)

    def _json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/health":
            self._json(200, {"status": "ok", "provider": "chatterbox", "device": DEVICE,
                             "reference_ready": bool(REFERENCE_AUDIO and REFERENCE_AUDIO.is_file())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/tts":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                self._json(413, {"error": "request body must be between 1 byte and 2 MB"})
                return
            payload = json.loads(self.rfile.read(length))
            text = str(payload.get("text", "")).strip()
            if not text:
                self._json(400, {"error": "text is required"})
                return
            if len(text) > 12_000:
                self._json(413, {"error": "split narration into chunks of 12,000 characters or fewer"})
                return
            emotion = str(payload.get("emotion", "neutral"))
            tone = str(payload.get("tone", ""))
            pace = str(payload.get("pace", "measured"))
            intensity = float(payload.get("intensity", 0.5))
            exaggeration, cfg_weight = direction_parameters(emotion, tone, pace, intensity)

            with MODEL_LOCK:
                audio = MODEL.generate(
                    text,
                    audio_prompt_path=str(REFERENCE_AUDIO) if REFERENCE_AUDIO else None,
                    exaggeration=exaggeration,
                    cfg_weight=cfg_weight,
                )
                if PITCH_SHIFT_SEMITONES:
                    audio = TORCHAUDIO.functional.pitch_shift(
                        audio.detach(),
                        sample_rate=MODEL.sr,
                        n_steps=PITCH_SHIFT_SEMITONES,
                    )
            buffer = io.BytesIO()
            TORCHAUDIO.save(buffer, audio.detach().cpu(), MODEL.sr, format="wav")
            body = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(
                f"[Chatterbox] emotion={emotion!r} tone={tone!r} pace={pace!r} "
                f"intensity={intensity:.2f}; synthesized {len(text)} characters; "
                f"exaggeration={exaggeration:.2f}, cfg={cfg_weight:.2f}, "
                f"pitch_shift={PITCH_SHIFT_SEMITONES:.1f} semitones",
                flush=True,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # report details to the server console, not the network client
            print(f"[Chatterbox] Synthesis failed: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"error": "synthesis failed; inspect the Chatterbox server terminal"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Chatterbox server for StoryPilot")
    parser.add_argument("--host", default="127.0.0.1", help="Bind locally by default; use only localhost")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--pitch-shift-semitones", type=float, default=-2.0,
        help="Lower the generated voice pitch while preserving duration; use 0 to disable (suggested range -1 to -3)",
    )
    parser.add_argument("--reference-audio", type=Path,
                        help="Optional clean voice reference WAV; use only a voice you have permission to use")
    args = parser.parse_args()

    global MODEL, TORCHAUDIO, REFERENCE_AUDIO, DEVICE, PITCH_SHIFT_SEMITONES
    try:
        import torch
        import torchaudio
        from chatterbox.tts import ChatterboxTTS
    except ImportError as exc:
        raise SystemExit("Install Chatterbox in this Python environment with: pip install chatterbox-tts") from exc
    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA is unavailable. Check the NVIDIA driver and PyTorch CUDA install, or run with --device cpu.")
    REFERENCE_AUDIO = args.reference_audio.expanduser().resolve() if args.reference_audio else None
    if REFERENCE_AUDIO and not REFERENCE_AUDIO.is_file():
        raise SystemExit(f"Reference WAV not found: {REFERENCE_AUDIO}")
    DEVICE = args.device
    if not -4.0 <= args.pitch_shift_semitones <= 0.0:
        raise SystemExit("--pitch-shift-semitones must be between -4 and 0")
    PITCH_SHIFT_SEMITONES = args.pitch_shift_semitones
    TORCHAUDIO = torchaudio
    print(f"Loading Chatterbox on {DEVICE}; first launch downloads model weights.", flush=True)
    MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
    print(f"Ready at http://{args.host}:{args.port}/v1/health", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), ChatterboxHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Chatterbox server.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
