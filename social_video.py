"""Discover public social-video candidates, select a moment, and cut a short clip.

YouTube and TikTok search results are only candidate references. yt-dlp is used
only after a candidate URL has been selected. The source download is temporary;
only the AI-selected, muted excerpt is retained in the reusable media library.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - reported by dependency checker
    requests = None


from cost_controls import record_openai_usage, resolve_model

VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".ogv", ".mpeg", ".mpg"}


def supported_social_url(url: str) -> bool:
    """Allow only canonical video-page URLs; reject credentials, redirects, and other hosts."""
    try:
        parsed = urlparse(str(url).strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return False
        path = parsed.path.rstrip("/")
        if host == "youtu.be" or host.endswith(".youtu.be"):
            return bool(path.strip("/"))
        if host == "youtube.com" or host.endswith(".youtube.com"):
            return ((path == "/watch" and bool(parse_qs(parsed.query).get("v")))
                    or bool(re.fullmatch(r"/(?:shorts|live|embed)/[A-Za-z0-9_-]+", path)))
        if host == "tiktok.com" or host.endswith(".tiktok.com"):
            return bool(re.fullmatch(r"/@[A-Za-z0-9._-]+/video/\d+", path))
        return False
    except (TypeError, ValueError):
        return False


def _model_for(stage: str) -> str:
    """Honor stage routing while preserving the older vision-model setting."""
    if stage == "clip_analysis":
        if os.getenv("OPENAI_MODEL_CLIP_ANALYSIS", "").strip():
            return resolve_model(stage)
        legacy = os.getenv("OPENAI_VISION_MODEL", "").strip()
        if legacy:
            return legacy
    return resolve_model(stage)


def _request_session():
    if requests is None:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt")
    return requests


def search_youtube_videos(query: str, *, limit: int = 3) -> list[dict]:
    """Find candidate videos through the official YouTube Data API."""
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        return []
    req = _request_session()
    response = req.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "q": query[:180], "type": "video",
        "maxResults": min(max(int(limit), 1), 5), "order": "relevance", "key": key,
    }, timeout=30)
    response.raise_for_status()
    candidates = []
    for item in response.json().get("items", []):
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        candidates.append({
            "source": "youtube", "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": str(snippet.get("title", ""))[:200],
            "description": re.sub(r"\s+", " ", str(snippet.get("description", "")))[:500],
            "creator": str(snippet.get("channelTitle", ""))[:120],
            "published_at": snippet.get("publishedAt"), "video_id": video_id,
        })
    return candidates


def _extract_tiktok_urls(response: dict, limit: int) -> list[dict]:
    """Read model text and web-search citations, keeping canonical TikTok video URLs only."""
    chunks: list[str] = []
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        chunks.append(output_text)
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if not isinstance(content, dict):
                continue
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
            for annotation in content.get("annotations", []) if isinstance(content.get("annotations"), list) else []:
                if isinstance(annotation, dict):
                    citation = annotation.get("url_citation", annotation)
                    if isinstance(citation, dict):
                        chunks.extend(str(citation.get(field, "")) for field in ("url", "title"))
    text = "\n".join(chunks)
    matches = re.findall(r"https?://(?:www\.|m\.)?tiktok\.com/@[A-Za-z0-9._-]+/video/\d+[^\s\]\)\"']*", text, flags=re.I)
    candidates = []
    seen = set()
    for url in matches:
        url = url.rstrip(".,;:")
        canonical = url.split("?", 1)[0]
        if canonical in seen or not supported_social_url(canonical):
            continue
        seen.add(canonical)
        candidates.append({"source": "tiktok", "url": canonical, "title": "", "description": ""})
        if len(candidates) >= limit:
            break
    return candidates


def search_tiktok_videos(query: str, *, limit: int = 3) -> list[dict]:
    """Search public web results for TikTok video URLs with OpenAI web search."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return []
    req = _request_session()
    prompt = (
        "Use web search to find public TikTok videos relevant to this documentary scene.\n"
        f"Scene search phrase: {query[:300]}\n"
        f"Return up to {min(max(int(limit), 1), 5)} results as JSON: "
        '{"videos":[{"url":"canonical https://www.tiktok.com/@handle/video/id",'
        '"title":"visible caption/title","reason":"short relevance note"}]}. '
        "Only include URLs you actually found in search results. Do not invent or guess URLs."
    )
    response = req.post("https://api.openai.com/v1/responses", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    }, json={"model": resolve_model("social_search"), "input": prompt,
             "tools": [{"type": "web_search"}]}, timeout=120)
    response.raise_for_status()
    payload = response.json()
    record_openai_usage(payload, stage="social_search", model=resolve_model("social_search"), web_search=True)
    candidates = _extract_tiktok_urls(payload, limit)
    # Preserve model-supplied descriptions where their URL matches a cited URL.
    text = payload.get("output_text", "")
    try:
        parsed = json.loads(text)
        by_url = {str(row.get("url", "")).split("?", 1)[0]: row for row in parsed.get("videos", []) if isinstance(row, dict)}
        for candidate in candidates:
            row = by_url.get(candidate["url"], {})
            candidate["title"] = str(row.get("title", ""))[:200]
            candidate["description"] = str(row.get("reason", ""))[:300]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return candidates


def discover_social_videos(query: str, *, limit_per_platform: int = 2) -> list[dict]:
    """Search configured YouTube and TikTok sources, keeping provider failures isolated."""
    found: list[dict] = []
    for search in (search_youtube_videos, search_tiktok_videos):
        try:
            found.extend(search(query, limit=limit_per_platform))
        except Exception as exc:
            # Never print request exceptions because they can contain API keys.
            print(f"[StoryPilot] {search.__name__} failed ({type(exc).__name__}).", flush=True)
    unique = []
    seen = set()
    for candidate in found:
        url = str(candidate.get("url", ""))
        if supported_social_url(url) and url not in seen:
            seen.add(url)
            unique.append(candidate)
    return unique


def choose_candidate(candidates: list[dict], query: str, purpose: str) -> tuple[dict | None, str]:
    """Ask OpenAI to select a source from search results; deterministic fallback is first result."""
    if not candidates:
        return None, "No social video candidates were found"
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key and len(candidates) > 1:
        req = _request_session()
        prompt = (
            "Choose the single candidate video most likely to show the requested scene. "
            "Use only the supplied metadata. Do not infer unsupported events from titles. "
            "Return JSON {\"index\": integer, \"reason\": string}.\n"
            f"Scene: {query}\nCreative direction: {purpose}\nCandidates: "
            f"{json.dumps(candidates, ensure_ascii=False)}"
        )
        try:
            response = req.post("https://api.openai.com/v1/responses", headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            }, json={"model": resolve_model("social_ranking"), "input": prompt}, timeout=90)
            response.raise_for_status()
            response_data = response.json()
            record_openai_usage(response_data, stage="social_ranking", model=resolve_model("social_ranking"))
            text = response_data.get("output_text", "")
            decision = json.loads(text)
            index = int(decision.get("index", -1))
            if 0 <= index < len(candidates):
                return candidates[index], str(decision.get("reason", "AI ranked candidate by scene relevance"))[:300]
        except Exception as exc:
            print(f"[StoryPilot] Social candidate ranking failed ({type(exc).__name__}); using search rank.", flush=True)
    return candidates[0], "Used provider search ranking"


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(result.stdout.strip())


def _contact_sheet(video: Path, duration: float, output: Path, frames: int = 12) -> list[float]:
    """Extract a small, timestamped contact sheet for visual scene selection."""
    from PIL import Image, ImageDraw, ImageFont

    count = min(max(frames, 4), max(4, int(duration)))
    interval = duration / count
    offset = interval / 2
    with tempfile.TemporaryDirectory(prefix="storypilot_frames_") as tmp:
        pattern = str(Path(tmp) / "frame_%02d.jpg")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(video),
            "-vf", f"fps=1/{interval:.6f},scale=280:158:force_original_aspect_ratio=decrease,pad=280:158:(ow-iw)/2:(oh-ih)/2",
            "-frames:v", str(count), "-q:v", "5", pattern,
        ], capture_output=True, text=True, timeout=120, check=True)
        images = sorted(Path(tmp).glob("frame_*.jpg"))
        if not images:
            raise RuntimeError("No video frames could be extracted for AI analysis")
        columns = 4
        rows = (len(images) + columns - 1) // columns
        cell_w, cell_h = 280, 184
        sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "#111111")
        draw = ImageDraw.Draw(sheet)
        times = []
        font = ImageFont.load_default()
        for index, frame_path in enumerate(images):
            image = Image.open(frame_path).convert("RGB")
            x, y = (index % columns) * cell_w, (index // columns) * cell_h
            sheet.paste(image, (x, y))
            timestamp = min(duration, offset + index * interval)
            times.append(timestamp)
            draw.rectangle((x, y + 158, x + cell_w, y + cell_h), fill="#111111")
            draw.text((x + 8, y + 163), f"Frame {index + 1} · {timestamp:.1f}s", fill="#ffffff", font=font)
        sheet.save(output, format="JPEG", quality=78, optimize=True)
        return times


def analyze_clip_moment(video: Path, *, query: str, purpose: str, openai_key: str | None = None) -> dict:
    """Use a contact sheet and scene context to select a visually relevant moment."""
    from PIL import Image

    api_key = openai_key or os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to analyze social video frames")
    duration = _probe_duration(video)
    if duration < 0.5:
        raise RuntimeError("Source video is too short to produce a clip")
    with tempfile.TemporaryDirectory(prefix="storypilot_contact_") as tmp:
        sheet_path = Path(tmp) / "contact_sheet.jpg"
        sample_times = _contact_sheet(video, duration, sheet_path)
        encoded = base64.b64encode(sheet_path.read_bytes()).decode("ascii")
    prompt = (
        "You are selecting a short B-roll excerpt for an original narrated documentary. "
        "Inspect the timestamped contact sheet. Choose a visually relevant continuous moment that supports the scene; "
        "do not claim the footage proves factual events. If no frame is suitable, return suitable=false. "
        "Return only JSON with suitable (boolean), center_second (number), and reason (string). "
        "Use one of the displayed frame timestamps as the center, or a nearby point that keeps the clip within the visible action.\n"
        f"Scene search phrase: {query}\nCreative direction: {purpose}\n"
        f"Source duration: {duration:.3f} seconds. Sampled frame times: {json.dumps([round(x, 2) for x in sample_times])}."
    )
    req = _request_session()
    response = req.post("https://api.openai.com/v1/responses", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    }, json={
        "model": _model_for("clip_analysis"),
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "high"},
        ]}],
    }, timeout=180)
    response.raise_for_status()
    payload = response.json()
    record_openai_usage(payload, stage="clip_analysis", model=resolve_model("clip_analysis", os.getenv("OPENAI_VISION_MODEL")))
    answer = payload.get("output_text", "")
    if not answer:
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    answer += content.get("text", "")
    match = re.search(r"\{.*\}", answer, flags=re.S)
    if not match:
        raise RuntimeError("AI returned no valid clip-selection JSON")
    decision = json.loads(match.group(0))
    if not decision.get("suitable"):
        raise RuntimeError("AI found no visually suitable moment in the candidate")
    center = float(decision.get("center_second", -1))
    if not 0 <= center <= duration:
        raise RuntimeError("AI selected a time outside the source video")
    return {"center_second": center, "duration_seconds": duration,
            "reason": str(decision.get("reason", ""))[:500], "sampled_frame_times": sample_times}


def download_and_cut(candidate: dict, *, query: str, purpose: str, output: Path,
                     max_clip_seconds: float = 5.0) -> dict:
    """Download one public candidate temporarily, analyze, and retain only a muted excerpt."""
    url = str(candidate.get("url", ""))
    if not supported_social_url(url):
        raise ValueError("Candidate URL must be a public YouTube or TikTok video URL")
    output.parent.mkdir(parents=True, exist_ok=True)
    max_mb = max(25, int(os.getenv("SOCIAL_VIDEO_MAX_DOWNLOAD_MB", "250")))
    max_source_minutes = max(1, int(os.getenv("SOCIAL_VIDEO_MAX_SOURCE_MINUTES", "30")))
    with tempfile.TemporaryDirectory(prefix="storypilot_social_") as tmp:
        temp_root = Path(tmp)
        source_template = str(temp_root / "source.%(ext)s")
        command = [
            sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--no-progress",
            "--max-filesize", f"{max_mb}M", "--match-filter", f"duration <= {max_source_minutes * 60}",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4", "-o", source_template, url,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "yt-dlp failed").strip().splitlines()
            # Do not expose cookies, tokens, or signed URLs if a provider error includes them.
            safe_detail = re.sub(r"https?://\S+", "[url redacted]", detail[-1] if detail else "download failed")[:240]
            raise RuntimeError(f"yt-dlp could not download candidate: {safe_detail}")
        sources = [path for path in temp_root.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES]
        if not sources:
            raise RuntimeError("yt-dlp completed without a recognizable video file")
        source = max(sources, key=lambda path: path.stat().st_size)
        if source.stat().st_size > max_mb * 1024 * 1024:
            raise RuntimeError("Downloaded source exceeded the configured size limit")
        moment = analyze_clip_moment(source, query=query, purpose=purpose)
        source_duration = moment["duration_seconds"]
        clip_length = min(float(max_clip_seconds), source_duration)
        start = max(0.0, min(moment["center_second"] - clip_length / 2, source_duration - clip_length))
        end = start + clip_length
        if not (0 <= start < end <= source_duration and end - start <= max_clip_seconds + 0.001):
            raise RuntimeError("Selected clip time range failed validation")
        cut = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source),
            "-t", f"{clip_length:.3f}", "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ], capture_output=True, text=True, timeout=180, check=False)
        if cut.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg could not cut the selected video segment")
        result_data = {
            "source": candidate.get("source", "social_video"), "source_url": url,
            "title": candidate.get("title", ""), "creator": candidate.get("creator", ""),
            "license": None, "duration": round(clip_length, 3),
            "segment_start_seconds": round(start, 3), "segment_end_seconds": round(end, 3),
            "segment_reason": moment["reason"], "analysis_sample_times": [round(x, 2) for x in moment["sampled_frame_times"]],
            "source_video_duration_seconds": round(source_duration, 3),
            "source_audio_included": False,
        }
        return result_data

