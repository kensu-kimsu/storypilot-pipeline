"""Narration synthesis, caption timing, and edit-job assembly.

Azure Speech, Fish Speech, and local Chatterbox are supported.
"""
from __future__ import annotations

import html
import json
import os
import re
import argparse
import wave
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


DEFAULT_EXPRESSIVE_VOICE = "en-US-Ethan:MAI-Voice-2.1"
SUPPORTED_MAI_STYLES = {
    "angry", "confused", "embarrassed", "excited", "jealous", "hopeful",
    "regretful", "relieved", "sad", "softvoice", "surprised", "joyful",
    "shouting", "happy", "neutral", "fearful", "disgusted", "determined",
    "whispering",
}


def emotion_to_style(emotion: str, tone: str = "") -> str:
    """Translate creative-direction tags into supported MAI voice styles."""
    value = f"{emotion} {tone}".lower()
    if any(word in value for word in ("fear", "terror", "alarmed", "dread")):
        return "fearful"
    if any(word in value for word in ("whisper", "secret", "suspense", "hushed")):
        return "softvoice"
    if any(word in value for word in ("intrigu", "curious", "wonder", "hope")):
        return "hopeful"
    if any(word in value for word in ("anger", "furious", "outrage")):
        return "angry"
    if any(word in value for word in ("sad", "grief", "loss", "somber")):
        return "sad"
    if any(word in value for word in ("shock", "surpris", "reveal")):
        return "surprised"
    if any(word in value for word in ("urgent", "forceful", "determined")):
        return "determined"
    if any(word in value for word in ("excited", "energetic", "triumph")):
        return "excited"
    return "neutral"


def _speech_rate(pace: str) -> str:
    return {"slow": "-8%", "measured": "-2%", "brisk": "+5%"}.get(pace, "0%")


def build_ssml(text: str, *, voice: str, emotion: str = "neutral", tone: str = "",
               pace: str = "measured", intensity: float = 0.5) -> str:
    """Create escaped SSML, applying expressive tags only to MAI Voice models."""
    locale = "-".join(voice.split(":", 1)[0].split("-")[:2])
    safe_text = escape(text)
    if ":MAI-Voice-2.1" in voice:
        style = emotion_to_style(emotion, tone)
        if style not in SUPPORTED_MAI_STYLES:
            style = "neutral"
        return (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="http://www.w3.org/2001/mstts" '
            f'xml:lang="{locale}"><voice name="{html.escape(voice, quote=True)}">'
            f'<mstts:express-as style="{style}"><prosody rate="{_speech_rate(pace)}">'
            f'{safe_text}</prosody></mstts:express-as></voice></speak>'
        )
    # Standard neural voices do not expose the MAI emotion styles. Use restrained
    # pacing and punctuation-friendly SSML instead.
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{locale}"><voice name="{html.escape(voice, quote=True)}">'
        f'<prosody rate="{_speech_rate(pace)}">{safe_text}</prosody>'
        '</voice></speak>'
    )


def _fmt_srt(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def words_to_srt(words: list[dict[str, Any]], *, max_words: int = 5, max_duration: float = 2.6) -> str:
    """Group SDK word boundaries into readable, timed English caption cards."""
    cues = []
    group: list[dict[str, Any]] = []
    for word in words:
        if not str(word.get("text", "")).strip():
            continue
        if group and (len(group) >= max_words or float(word["start"]) - float(group[0]["start"]) >= max_duration):
            cues.append(group)
            group = []
        group.append(word)
    if group:
        cues.append(group)
    lines = []
    for index, cue in enumerate(cues, 1):
        start = float(cue[0]["start"])
        end = max(start + 0.08, float(cue[-1].get("end", start + 0.35)))
        caption = " ".join(str(item["text"]).strip() for item in cue)
        lines.append(f"{index}\n{_fmt_srt(start)} --> {_fmt_srt(end)}\n{caption}\n")
    return "\n".join(lines)


def _synthesize_chunk(text: str, *, voice: str, region: str, emotion: str,
                      tone: str, pace: str) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:
        raise RuntimeError("Install Azure Speech with: pip install azure-cognitiveservices-speech") from exc

    key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    if not key or not region:
        raise RuntimeError("Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in local secrets or environment")
    config = speechsdk.SpeechConfig(subscription=key, region=region)
    config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm
    )
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=config, audio_config=None)
    boundaries: list[dict[str, Any]] = []

    def on_word(event: Any) -> None:
        start = float(event.audio_offset) / 10_000_000
        duration = float(getattr(event, "duration", 0) or 0) / 10_000_000
        boundaries.append({"text": str(event.text), "start": start,
                           "end": start + (duration if duration > 0 else 0.28),
                           "text_offset": int(event.text_offset),
                           "word_length": int(event.word_length)})

    synthesizer.synthesis_word_boundary.connect(on_word)
    result = synthesizer.speak_ssml_async(build_ssml(
        text, voice=voice, emotion=emotion, tone=tone, pace=pace
    )).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = speechsdk.SpeechSynthesisCancellationDetails.from_result(result)
        raise RuntimeError(f"Azure speech synthesis failed: {details.reason}: {details.error_details}")
    return bytes(result.audio_data), boundaries


def fish_direction_tag(emotion: str, tone: str = "") -> str:
    """Map creative direction to Fish Speech's inline natural-language tags."""
    value = f"{emotion} {tone}".lower()
    if any(word in value for word in ("fear", "terror", "alarmed", "dread")):
        return "[fearful, tense]"
    if any(word in value for word in ("whisper", "secret", "suspense", "hushed")):
        return "[near-whisper, suspenseful]"
    if any(word in value for word in ("intrigu", "curious", "wonder", "hope")):
        return "[curious, quietly intrigued]"
    if any(word in value for word in ("anger", "furious", "outrage")):
        return "[angry, controlled intensity]"
    if any(word in value for word in ("sad", "grief", "loss", "somber")):
        return "[somber, restrained]"
    if any(word in value for word in ("shock", "surpris", "reveal")):
        return "[surprised, emphatic]"
    if any(word in value for word in ("urgent", "forceful", "determined")):
        return "[determined, urgent]"
    if any(word in value for word in ("excited", "energetic", "triumph")):
        return "[excited, energetic]"
    return "[natural, conversational]"


def _synthesize_fish_chunk(text: str, *, base_url: str, reference_id: str = "",
                            reference_audio: Path | None = None,
                            reference_text: str = "", emotion: str = "neutral",
                            tone: str = "") -> bytes:
    """Call a local Fish Speech S2 API server and return its WAV response."""
    try:
        import requests
        import ormsgpack
    except ImportError as exc:  # pragma: no cover - requests is a project dependency
        raise RuntimeError("Install Fish client support with: pip install -r requirements.txt") from exc
    url = f"{base_url.rstrip('/')}/v1/tts"
    payload: dict[str, Any] = {
        "text": f"{fish_direction_tag(emotion, tone)} {text}",
        "format": "wav",
        "streaming": False,
    }
    if reference_id.strip():
        payload["reference_id"] = reference_id.strip()
    if reference_audio:
        if reference_id.strip():
            raise RuntimeError("Choose either FISH_SPEECH_REFERENCE_ID or reference audio and transcript, not both")
        if not reference_text.strip():
            raise RuntimeError("Set FISH_SPEECH_REFERENCE_TEXT_FILE to the exact transcript for the reference WAV")
        if not reference_audio.is_file():
            raise RuntimeError(f"Fish reference audio file does not exist: {reference_audio}")
        payload["references"] = [{"audio": reference_audio.read_bytes(), "text": reference_text.strip()}]
    try:
        response = requests.post(
            url,
            params={"format": "msgpack"},
            data=ormsgpack.packb(payload),
            headers={"Content-Type": "application/msgpack", "Accept": "application/msgpack"},
            timeout=(10, 1800),
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Cannot reach local Fish Speech server at {url}. Start Fish Speech and confirm its /v1/health endpoint."
        ) from exc
    if not response.ok:
        details = response.text[:1000] if response.text else "empty response"
        raise RuntimeError(f"Fish Speech returned HTTP {response.status_code}: {details}")
    if not response.content.startswith(b"RIFF"):
        raise RuntimeError(
            "Fish Speech did not return WAV audio. Check the local server version and its /v1/tts endpoint."
        )
    return response.content


def _synthesize_chatterbox_chunk(text: str, *, base_url: str, emotion: str,
                                 tone: str, pace: str, intensity: float) -> bytes:
    """Call the local StoryPilot Chatterbox bridge and return its WAV response."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a project dependency
        raise RuntimeError("Install StoryPilot dependencies with: pip install -r requirements.txt") from exc
    payload = {
        "text": text,
        "emotion": emotion,
        "tone": tone,
        "pace": pace,
        "intensity": max(0.0, min(1.0, float(intensity))),
    }
    url = f"{base_url.rstrip('/')}/v1/tts"
    try:
        response = requests.post(url, json=payload, timeout=(10, 1800))
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Cannot reach local Chatterbox at {url}. Start chatterbox_server.py and check /v1/health."
        ) from exc
    if not response.ok:
        details = response.text[:1000] if response.text else "empty response"
        raise RuntimeError(f"Chatterbox returned HTTP {response.status_code}: {details}")
    if not response.content.startswith(b"RIFF"):
        raise RuntimeError("Chatterbox did not return a WAV file; check the local server logs.")
    return response.content


def _estimated_word_boundaries(text: str, duration: float) -> list[dict[str, Any]]:
    """Estimate caption word times when the local TTS server has no alignments."""
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return []
    # Character-weighted estimates are smoother than assigning every word an
    # identical interval. These are explicitly marked approximate in metadata.
    weights = [max(1, len(re.sub(r"[^\w']", "", token))) for token in tokens]
    total = sum(weights)
    elapsed = 0.0
    words = []
    for token, weight in zip(tokens, weights):
        start = duration * elapsed / total
        elapsed += weight
        end = duration * elapsed / total
        words.append({"text": token, "start": start, "end": max(start + 0.04, end)})
    return words


def synthesize_script(script_path: Path, output_dir: Path, *, voice: str | None = None,
                      region: str | None = None, provider: str | None = None,
                      fish_url: str | None = None) -> dict:
    """Synthesize each narration chunk, compose one WAV, and write SRT timings."""
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    chunks = payload.get("narration_chunks", [])
    if not chunks:
        raise ValueError("script.json has no narration_chunks")
    chosen_provider = (provider or os.getenv("NARRATION_PROVIDER") or "azure").strip().lower()
    if chosen_provider not in {"azure", "fish", "chatterbox"}:
        raise ValueError("Narration provider must be 'azure', 'fish', or 'chatterbox'")
    fish_base_url = fish_url or os.getenv("FISH_SPEECH_URL", "http://127.0.0.1:8080").strip()
    chatterbox_base_url = os.getenv("CHATTERBOX_URL", "http://127.0.0.1:8090").strip()
    if chosen_provider == "azure":
        chosen_voice = voice or os.getenv("AZURE_TTS_VOICE") or payload.get("voice_name") or DEFAULT_EXPRESSIVE_VOICE
        chosen_region = region or os.getenv("AZURE_SPEECH_REGION", "").strip()
        if not chosen_region:
            raise RuntimeError("AZURE_SPEECH_REGION is required for Azure narration")
        fish_reference_id = ""
    elif chosen_provider == "fish":
        chosen_voice = voice or os.getenv("FISH_SPEECH_REFERENCE_ID", "").strip() or "Fish Speech S2 Pro default voice"
        chosen_region = "local"
        fish_reference_id = "" if chosen_voice == "Fish Speech S2 Pro default voice" else chosen_voice
        reference_audio_value = os.getenv("FISH_SPEECH_REFERENCE_AUDIO", "").strip()
        fish_reference_audio = Path(reference_audio_value).expanduser() if reference_audio_value else None
        reference_text_file = os.getenv("FISH_SPEECH_REFERENCE_TEXT_FILE", "").strip()
        if fish_reference_audio and not reference_text_file:
            raise RuntimeError("Set FISH_SPEECH_REFERENCE_TEXT_FILE with the exact words spoken in the reference WAV")
        fish_reference_text = Path(reference_text_file).expanduser().read_text(encoding="utf-8") if reference_text_file else ""
        if len(chunks) > 1 and not (fish_reference_id or fish_reference_audio) and os.getenv("FISH_SPEECH_ALLOW_RANDOM_VOICE", "").lower() not in {"1", "true", "yes"}:
            raise RuntimeError(
                "For a consistent narrator across script chunks, set FISH_SPEECH_REFERENCE_ID or both "
                "FISH_SPEECH_REFERENCE_AUDIO and FISH_SPEECH_REFERENCE_TEXT_FILE. "
                "Set FISH_SPEECH_ALLOW_RANDOM_VOICE=1 only for a test."
            )
        try:
            import requests
            health = requests.get(f"{fish_base_url.rstrip('/')}/v1/health", timeout=5)
            health.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Local Fish Speech is not ready at {fish_base_url}. Start the server and verify /v1/health."
            ) from exc
    else:
        chosen_voice = "Chatterbox reference voice"
        chosen_region = "local"
        try:
            import requests
            health = requests.get(f"{chatterbox_base_url.rstrip('/')}/v1/health", timeout=5)
            health.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Local Chatterbox is not ready at {chatterbox_base_url}. Start chatterbox_server.py and verify /v1/health."
            ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "narration.wav"
    words: list[dict[str, Any]] = []
    offsets: list[dict[str, Any]] = []
    params: tuple[int, int, int, int] | None = None
    frame_groups: list[bytes] = []
    elapsed = 0.0
    for index, chunk in enumerate(chunks, 1):
        text = str(chunk.get("text", "")).strip()
        if not text:
            raise ValueError(f"Narration chunk {index} is empty")
        emotion = str(chunk.get("delivery_emotion", "neutral"))
        tone = str(chunk.get("tone", "standard"))
        if chosen_provider == "azure":
            raw, chunk_words = _synthesize_chunk(
                text, voice=chosen_voice, region=chosen_region, emotion=emotion,
                tone=tone, pace=str(chunk.get("delivery_pace", "measured")),
            )
        elif chosen_provider == "fish":
            raw = _synthesize_fish_chunk(
                text, base_url=fish_base_url, reference_id=fish_reference_id,
                reference_audio=fish_reference_audio,
                reference_text=fish_reference_text,
                emotion=emotion, tone=tone,
            )
            chunk_words = []
        else:
            raw = _synthesize_chatterbox_chunk(
                text, base_url=chatterbox_base_url, emotion=emotion, tone=tone,
                pace=str(chunk.get("delivery_pace", "measured")),
                intensity=float(chunk.get("vocal_intensity", chunk.get("intensity", 0.5)) or 0.5),
            )
            chunk_words = []
        import io
        with wave.open(io.BytesIO(raw), "rb") as wav:
            current = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
            if params is None:
                params = current
            if current != params:
                raise RuntimeError(f"{chosen_provider} returned inconsistent WAV formats between narration chunks")
            frames = wav.readframes(wav.getnframes())
            chunk_duration = len(frames) / (wav.getframerate() * wav.getnchannels() * wav.getsampwidth())
        if chosen_provider in {"fish", "chatterbox"}:
            chunk_words = _estimated_word_boundaries(text, chunk_duration)
        frame_groups.append(frames)
        for word in chunk_words:
            word["start"] += elapsed
            word["end"] += elapsed
            word["chunk_index"] = index - 1
            words.append(word)
        offsets.append({"chunk_index": index - 1, "section_title": chunk.get("section_title", ""),
                        "start": round(elapsed, 4), "end": round(elapsed + chunk_duration, 4),
                        "duration": round(chunk_duration, 4), "emotion": chunk.get("delivery_emotion", "neutral"),
                        "tone": chunk.get("tone", "standard"), "voice": chosen_voice})
        elapsed += chunk_duration
        silence = max(0.0, min(2.5, float(chunk.get("pause_after_seconds", 0.25))))
        if silence and params:
            frame_groups.append(b"\0" * round(params[1] * params[2] * silence))
            elapsed += silence

    assert params is not None
    with wave.open(str(audio_path), "wb") as wav_out:
        wav_out.setnchannels(params[0])
        wav_out.setsampwidth(params[1])
        wav_out.setframerate(params[2])
        wav_out.setcomptype(params[3], "not compressed")
        for frames in frame_groups:
            wav_out.writeframesraw(frames)

    # Remove trailing inter-section silence from the public duration estimate.
    if chunks:
        trailing = max(0.0, min(2.5, float(chunks[-1].get("pause_after_seconds", 0.25))))
        elapsed -= trailing
    srt_path = output_dir / "captions.srt"
    srt_path.write_text(words_to_srt(words), encoding="utf-8")
    words_path = output_dir / "word_timestamps.json"
    words_path.write_text(json.dumps(words, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {"provider": chosen_provider, "voice": chosen_voice, "region": chosen_region,
                "word_timing_quality": "approximate" if chosen_provider in {"fish", "chatterbox"} else "provider_word_boundaries",
                "audio_file": str(audio_path.resolve()),
                "duration_seconds": round(elapsed, 3), "caption_file": str(srt_path.resolve()),
                "word_timestamps_file": str(words_path.resolve()), "chunks": offsets,
                "emotion_style_enabled": chosen_provider in {"fish", "chatterbox"} or ":MAI-Voice-2.1" in chosen_voice}
    (output_dir / "narration_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def build_timed_edit_job(script_path: Path, narration: dict, assets_manifest_path: Path | None = None) -> dict:
    """Join synthesized chunk timings and acquired media into creative_director input."""
    script = json.loads(script_path.read_text(encoding="utf-8"))
    research_path = script_path.parent / "research.json"
    research = json.loads(research_path.read_text(encoding="utf-8")) if research_path.is_file() else {}
    manifest = json.loads(assets_manifest_path.read_text(encoding="utf-8")) if assets_manifest_path and assets_manifest_path.exists() else {"assets": [], "unresolved": []}
    selected_by_query: dict[str, list[dict[str, Any]]] = {}
    assets: dict[str, dict[str, Any]] = {}
    for row in manifest.get("assets", []):
        picked = row.get("selected") or {}
        path = picked.get("path")
        asset_id = picked.get("asset_id")
        if not path or not asset_id:
            continue
        category = picked.get("category") or row.get("category")
        kind = {"picture": "image", "generated": "image", "video": "video", "music": "audio", "sfx": "audio"}.get(category, category or "image")
        license_value = str(picked.get("license") or "")
        rights_status = "licensed" if license_value else "unknown"
        assets[str(asset_id)] = {
            "path": path, "kind": kind, "category": category,
            "duration": float(picked.get("duration") or 0), "source": picked.get("source"),
            "source_url": picked.get("source_url"), "license": license_value,
            "creator": picked.get("creator"), "rights_status": rights_status,
            "query": str(row.get("query", "")),
        }
        selected_by_query.setdefault(str(row.get("query", "")).strip().lower(), []).append({"asset_id": str(asset_id), "category": category})

    segments = []
    chunks = script.get("narration_chunks", [])
    timings = narration.get("chunks", [])
    if len(chunks) != len(timings):
        raise ValueError("Narration chunk timings do not align with script chunks")
    for chunk, timing in zip(chunks, timings):
        visual_ids = []
        for query in chunk.get("asset_queries", []):
            options = selected_by_query.get(str(query).strip().lower(), [])
            visual = next((a for a in options if a["category"] in {"picture", "video", "generated"}), None)
            if visual and visual["asset_id"] not in visual_ids:
                visual_ids.append(visual["asset_id"])
        parallax_roles = {}
        for row in manifest.get("assets", []):
            if row.get("segment") != timing["chunk_index"] + 1 or row.get("parallax_role") not in {"portrait_foreground", "parallax_background"}:
                continue
            selected = row.get("selected") or {}
            selected_id = selected.get("asset_id")
            if selected_id:
                parallax_roles[row["parallax_role"]] = str(selected_id)
                if str(selected_id) not in visual_ids:
                    visual_ids.append(str(selected_id))
        sfx_ids, music_ids, sfx_by_cue = [], [], {}
        for cue in chunk.get("sfx_cues", []):
            match = next((a for a in selected_by_query.get(str(cue).strip().lower(), []) if a["category"] == "sfx"), None)
            if match:
                sfx_ids.append(match["asset_id"])
                sfx_by_cue[str(cue)] = match["asset_id"]
        for row in manifest.get("assets", []):
            if row.get("segment") == timing["chunk_index"] + 1 and row.get("category") == "music":
                picked_id = (row.get("selected") or {}).get("asset_id")
                if picked_id:
                    music_ids.append(str(picked_id))
        segments.append({
            "start": timing["start"], "end": timing["end"], "text": chunk.get("text", ""),
            "tone": chunk.get("tone", "standard"), "audio_energy": chunk.get("audio_energy", 0.5),
            "visual_mode": chunk.get("visual_mode", ""),
            "visual_evidence": chunk.get("visual_evidence", ""),
            "visual_rationale": chunk.get("visual_rationale", ""),
            "overlay_text": chunk.get("overlay_text", ""), "visual_note": chunk.get("visual_note", ""),
            "claim_ids": chunk.get("claim_ids", []), "graphic_spec": chunk.get("graphic_spec"),
            "asset_ids": visual_ids, "asset_queries": chunk.get("asset_queries", []),
            "parallax_foreground_asset_id": parallax_roles.get("portrait_foreground"),
            "sfx_candidates": chunk.get("sfx_cues", []), "sfx_asset_ids": sfx_ids, "sfx_by_cue": sfx_by_cue,
            "music_mood": chunk.get("music_mood", "low_tension"), "music_asset_ids": music_ids,
            "delivery_emotion": chunk.get("delivery_emotion", "neutral"),
        })
    # Article screenshots are materialized by pre-production directly into the
    # shared asset directory, so carry those local files into the timed job too.
    request_path = script_path.parent / "asset_requests.json"
    request_manifest = json.loads(request_path.read_text(encoding="utf-8")) if request_path.exists() else {"assets": []}
    screenshot_requirements = []
    for request in request_manifest.get("assets", []):
        if request.get("category") != "news_headline_screenshot":
            continue
        local_path = request.get("local_path") or request.get("image_path")
        if not local_path or not Path(local_path).is_file():
            continue
        asset_id = str(request.get("asset_id"))
        assets[asset_id] = {"path": local_path, "kind": "image", "category": "picture", "duration": 0,
                            "source": request.get("publisher", "news article screenshot"),
                            "source_url": request.get("url"), "license": None, "rights_status": "unknown"}
        section = int(request.get("segment", 0))
        if 1 <= section <= len(segments):
            if asset_id not in segments[section - 1]["asset_ids"]:
                segments[section - 1]["asset_ids"].append(asset_id)
        screenshot_requirements.append({
            "asset_id": asset_id, "query": request.get("search_query", request.get("headline", "news headline")),
            "category": "picture", "segment": section, "purpose": request.get("reason", "News headline screenshot"),
            "selected": {"asset_id": asset_id, "path": local_path, "category": "picture",
                         "source": request.get("publisher", "news article screenshot"), "source_url": request.get("url")},
        })
    job_data_path = script_path.parent / "job.json"
    job_data = json.loads(job_data_path.read_text(encoding="utf-8")) if job_data_path.exists() else {}
    channel = job_data.get("channel", {})
    format_name = script.get("format", job_data.get("format", "long_form"))
    canvas = script.get("canvas") or ({"width": 1080, "height": 1920, "fps": 30} if format_name == "short_form" else {"width": 1920, "height": 1080, "fps": 30})
    return {
        "job_id": script_path.parent.name, "topic": job_data.get("topic", script.get("title_working", script_path.parent.name)),
        "channel_id": job_data.get("channel_id") or channel.get("channel_id") or channel.get("handle"),
        "channel": channel, "visual_brand": job_data.get("visual_brand") or channel.get("visual_brand", {}),
        "format": format_name, "canvas": canvas,
        "narration_file": narration["audio_file"], "narration_duration": narration["duration_seconds"],
        "captions_file": narration["caption_file"], "word_timestamps_file": narration["word_timestamps_file"],
        "assets_manifest": str(assets_manifest_path.resolve()) if assets_manifest_path else None,
        "assets_root": None, "captions_required": True,
        "asset_requirements": [row for row in manifest.get("assets", []) + manifest.get("unresolved", [])
                               if row.get("category") != "news_headline_screenshot"] + screenshot_requirements,
        "segments": segments, "assets": assets,
        "verified_claims": research.get("verified_claims", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesize expressive documentary narration and captions")
    parser.add_argument("script_json", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True, help="Workspace containing secrets.json")
    parser.add_argument("--assets-manifest", type=Path, help="selected_assets.json from preproduction asset acquisition")
    parser.add_argument("--voice", help="Azure voice name or a local Fish reference_id")
    parser.add_argument("--region", help="Azure Speech resource region; defaults to AZURE_SPEECH_REGION")
    parser.add_argument("--provider", choices=("azure", "fish", "chatterbox"), help="Narration provider; defaults to NARRATION_PROVIDER or azure")
    parser.add_argument("--fish-url", help="Local Fish Speech server base URL; defaults to FISH_SPEECH_URL or http://127.0.0.1:8080")
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(args.base_dir / ".env")
    except ImportError:
        pass
    from workspace_setup import configure_api_keys
    provider = args.provider or os.getenv("NARRATION_PROVIDER", "azure").strip().lower()
    if provider == "azure":
        required = ("AZURE_SPEECH_KEY",) if args.region else ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
        configure_api_keys(args.base_dir, required_keys=required)
    elif provider in {"fish", "chatterbox"}:
        from workspace_setup import ensure_workspace
        ensure_workspace(args.base_dir)
    else:
        parser.error("--provider must be azure, fish, or chatterbox")
    result = synthesize_script(args.script_json, args.out_dir, voice=args.voice, region=args.region,
                               provider=provider, fish_url=args.fish_url)
    edit_job = build_timed_edit_job(args.script_json, result, args.assets_manifest)
    edit_job["assets_root"] = str((args.base_dir / "assets").resolve())
    edit_job_path = args.out_dir / "timed_edit_job.json"
    edit_job_path.write_text(json.dumps(edit_job, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Narration: {result['audio_file']} ({result['duration_seconds']:.1f}s)")
    print(f"Captions: {result['caption_file']}")
    print(f"Timed edit job: {edit_job_path.resolve()}")


if __name__ == "__main__":
    main()
