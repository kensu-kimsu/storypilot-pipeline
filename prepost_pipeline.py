"""Pre- and post-production stages for the documentary pipeline.

This module creates research/script/asset/metadata packages. It never scrapes or
downloads YouTube media. Search results are discovery references; each reused
asset must have a recorded permission/license before it reaches the editor.
"""
from __future__ import annotations

import argparse
import difflib
import ipaddress
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from workspace_setup import configure_api_keys, ensure_workspace

try:
    import requests
except ImportError:  # pure local QA helpers remain importable without API extras
    requests = None

API_ROOT = "https://api.openai.com/v1"
YT_ROOT = "https://www.googleapis.com/youtube/v3"
HTTP = requests.Session() if requests is not None else None


def _require_requests() -> None:
    if requests is None or HTTP is None:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt")


def _json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "topic"


def _is_public_http_url(url: str) -> bool:
    """Reject non-web and obvious local-network targets before opening a URL."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return address.is_global
    except (TypeError, ValueError):
        return False


def research_screenshot_targets(research: dict, *, limit: int = 3) -> list[dict]:
    """Return unique, public source pages, preferring higher-confidence claims."""
    if limit <= 0:
        return []
    targets, seen = [], set()
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    claims = sorted(research.get("claims", []), key=lambda c: confidence_rank.get(str(c.get("confidence", "low")).lower(), 3))
    for claim in claims:
        url = str(claim.get("url", "")).strip()
        if not _is_public_http_url(url) or url in seen:
            continue
        seen.add(url)
        targets.append({
            "url": url,
            "title": str(claim.get("source_title") or claim.get("publisher") or urlparse(url).netloc),
            "publisher": str(claim.get("publisher") or urlparse(url).netloc),
            "claim_id": claim.get("claim_id"),
            "confidence": claim.get("confidence", "unknown"),
        })
        if len(targets) >= max(0, limit):
            break
    return targets


def capture_research_screenshots(
    research: dict, topic: str, assets_root: Path, *, job_id: str, limit: int = 3
) -> dict:
    """Capture source pages as research references, never as reuse-cleared footage."""
    targets = research_screenshot_targets(research, limit=limit)
    output_dir = assets_root / "research_references" / _slug(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Source screenshots need Playwright. Install it with 'pip install playwright' "
            "then run 'python -m playwright install chromium'."
        ) from exc

    records = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.set_default_navigation_timeout(45000)
        for index, target in enumerate(targets, 1):
            host = _slug(urlparse(target["url"]).netloc)
            stem = f"{_slug(topic)[:45]}_{host[:28]}_source-{index:02d}"
            image_path = output_dir / f"{stem}.png"
            record = {
                **target,
                "image_path": str(image_path),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "purpose": "research reference screenshot; rights for reuse in a published video are not implied",
                "status": "failed",
            }
            try:
                response = page.goto(target["url"], wait_until="domcontentloaded")
                article = page.locator("article").first
                main = page.locator("main").first
                if article.count() and article.is_visible():
                    article.screenshot(path=str(image_path), animations="disabled", timeout=45000)
                    record["capture_scope"] = "article element"
                elif main.count() and main.is_visible():
                    main.screenshot(path=str(image_path), animations="disabled", timeout=45000)
                    record["capture_scope"] = "main element"
                else:
                    page.screenshot(path=str(image_path), full_page=True, animations="disabled", timeout=45000)
                    record["capture_scope"] = "full page fallback"
                record["status"] = "captured"
                record["http_status"] = response.status if response else None
                record["page_title"] = page.title()
            except Exception as exc:  # Some sites block automated browsers or fail to load.
                record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                if image_path.exists():
                    image_path.unlink()
            records.append(record)
        browser.close()

    manifest = {
        "topic": topic,
        "kind": "research_reference_screenshots",
        "notice": "Screenshots document source pages for research. They are not evidence of permission to reuse page content in a video.",
        "screenshots": records,
    }
    _write(output_dir / "research_screenshots.json", manifest)
    return manifest


def openai_response(prompt: str, *, web_search: bool = False, model: str | None = None) -> dict:
    _require_requests()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")
    payload: dict[str, Any] = {
        "model": model or os.environ.get("OPENAI_MODEL", "gpt-5"),
        "input": prompt,
    }
    if web_search:
        payload["tools"] = [{"type": "web_search"}]
    response = HTTP.post(f"{API_ROOT}/responses", headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=240)
    response.raise_for_status()
    return response.json()


def response_text(response: dict) -> str:
    if response.get("output_text"):
        return response["output_text"]
    texts = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                texts.append(content.get("text", ""))
    return "\n".join(texts)


def parse_json_text(value: str) -> dict:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def discover_youtube_videos(niche: str, *, days: int = 90, limit: int = 5, api_key: str | None = None) -> list[dict]:
    """Search public videos and rank by a transparent views/engagement score."""
    api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is required for YouTube topic discovery")
    _require_requests()
    after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    params = {"part": "snippet", "q": niche, "type": "video", "order": "viewCount", "publishedAfter": after, "maxResults": 50, "key": api_key}
    search = HTTP.get(f"{YT_ROOT}/search", params=params, timeout=30)
    search.raise_for_status()
    ids = [item["id"]["videoId"] for item in search.json().get("items", []) if item.get("id", {}).get("videoId")]
    if not ids:
        return []
    result = HTTP.get(f"{YT_ROOT}/videos", params={"part": "snippet,statistics,contentDetails", "id": ",".join(ids), "key": api_key}, timeout=30)
    result.raise_for_status()
    rows = []
    for item in result.json().get("items", []):
        stats = item.get("statistics", {})
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        published = item["snippet"].get("publishedAt", "")
        age_days = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(published.replace("Z", "+00:00"))).days) if published else days
        # Public counters do not expose impressions, watch time, or retention.
        engagement = (likes + comments) / max(views, 1)
        rows.append({
            "video_id": item["id"], "title": item["snippet"].get("title", ""),
            "channel_title": item["snippet"].get("channelTitle", ""), "published_at": published,
            "url": f"https://www.youtube.com/watch?v={item['id']}", "views": views,
            "likes": likes, "comments": comments, "engagement_rate": round(engagement, 6),
            "views_per_day": round(views / age_days, 1),
            "score": round((views / age_days) * (1 + min(engagement * 100, 1)), 1),
        })
    return sorted(rows, key=lambda x: x["score"], reverse=True)[:limit]


def is_duplicate(topic: str, history: list[dict], cutoff: float = 0.78) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]", " ", topic.lower())
    for entry in history:
        prior = re.sub(r"[^a-z0-9 ]", " ", str(entry.get("topic", "")).lower())
        if normalized and difflib.SequenceMatcher(None, normalized, prior).ratio() >= cutoff:
            return True
    return False


def propose_ideas(channel: dict, trending: list[dict], history: list[dict], count: int = 5) -> list[dict]:
    prompt = f"""Create {count} original documentary video ideas for a YouTube channel.
Channel niche: {channel.get('niche')}; audience: {channel.get('audience', 'general audience')}.
Recent high-performing public videos (inspiration only; do not copy titles or story framing):
{json.dumps(trending, ensure_ascii=False)}
Previously covered topics (avoid these): {json.dumps([x.get('topic') for x in history], ensure_ascii=False)}
Return strict JSON {{"ideas":[{{"topic":"specific original topic","angle":"distinctive thesis","why_now":"audience rationale","source_questions":["research question"]}}]}}.
Do not state unverified facts. Ideas should be researchable using primary/credible sources."""
    payload = parse_json_text(response_text(openai_response(prompt)))
    ideas = payload.get("ideas", [])
    return [idea for idea in ideas if idea.get("topic") and not is_duplicate(idea["topic"], history)][:count]


def research_story(topic: str, channel: dict) -> dict:
    prompt = f"""Research a factual documentary story for a {channel.get('niche')} YouTube channel.
Topic: {topic}
Use web search. Prefer primary documents, official records, peer-reviewed research, and reputable reporting. Resolve disputed claims, dates, names, units, and causal claims. Do not fill gaps by guessing.
Return strict JSON: {{"research_brief":"...","claims":[{{"claim_id":"C1","claim":"one checkable factual claim","source_title":"...","url":"https://...","publisher":"...","published_or_accessed":"...","support":"short paraphrase of what the source establishes","confidence":"high|medium|low"}}],"open_questions":["..."]}}.
Every factual claim in the brief must have a source URL. Include enough claims for a 20-minute documentary, and flag conflicting evidence."""
    response = openai_response(prompt, web_search=True)
    data = parse_json_text(response_text(response))
    data["search_response_id"] = response.get("id")
    data["verified_claims"] = [c for c in data.get("claims", []) if c.get("url", "").startswith("http") and c.get("confidence") == "high"]
    return data


def write_script(topic: str, channel: dict, research: dict) -> dict:
    prompt = f"""Write a retention-focused documentary script based ONLY on the supplied research claims. Do not add unsupported facts. Attach source claim IDs to every factual paragraph. Preserve qualifications and uncertainty. Cite claims inline as [C1].
Channel: {channel.get('name', channel.get('handle', ''))}; niche: {channel.get('niche')}; voice/tone: {channel.get('tone', 'cinematic, clear, curious')}.
Topic: {topic}. Target >=20 minutes spoken narration (roughly 2,600-3,000 words, but prioritize coherent pacing). Use a strong opening, escalating questions, clear act progression, payoff, and an ending that leads naturally to another video without an artificial cliffhanger. Include visual/tone/audio-energy tags per segment.
Research ledger: {json.dumps(research.get('claims', []), ensure_ascii=False)}
Return strict JSON {{"title_working":"...","logline":"...","narration_chunks":[{{"text":"...","claim_ids":["C1"],"tone":"...","audio_energy":0.5,"visual_note":"...","asset_queries":["..."] ,"music_mood":"...","sfx_cues":["...", "..."]}}],"shorts_script":{{"text":"standalone complete 30-second short, about 60-75 words","hook":"...","payoff":"...","claim_ids":["C1"]}}}}."""
    script = parse_json_text(response_text(openai_response(prompt)))
    claim_ids = {c.get("claim_id") for c in research.get("claims", [])}
    used = {cid for chunk in script.get("narration_chunks", []) for cid in chunk.get("claim_ids", [])}
    used.update(script.get("shorts_script", {}).get("claim_ids", []))
    script["unsupported_claim_ids"] = sorted(used - claim_ids)
    script["approx_word_count"] = sum(len(c.get("text", "").split()) for c in script.get("narration_chunks", []))
    script["target_met_estimate"] = script["approx_word_count"] >= 2500
    return script


def asset_research(script: dict, out_dir: Path) -> dict:
    """Compile the AI creative brief into one per-video downloader handoff."""
    requests_list, seen = [], set()
    for i, chunk in enumerate(script.get("narration_chunks", []), 1):
        for query_index, query in enumerate(chunk.get("asset_queries", [])):
            category = "video" if query_index % 2 == 0 else "picture"
            key = (str(query).strip().lower(), category)
            if not key or key in seen:
                continue
            seen.add(key)
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": query,
                                 "category": category, "segment": i,
                                 "purpose": chunk.get("visual_note", "Support the narration in this section"),
                                 "suggested_sources": ["Pixabay API", "Wikimedia Commons (no key)", "Internet Archive (no key)", "Library of Congress (no key)", "NASA Image and Video Library (no key)", "official company newsroom"],
                                 "source_url": None, "local_path": None,
                                 "rights_status": "unknown", "max_duration_seconds": 5 if category == "video" else None,
                                 "license_evidence": None, "download_status": "not_downloaded"})
        music_query = str(chunk.get("music_mood", "")).strip()
        if music_query:
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": music_query,
                "category": "music", "segment": i, "purpose": "Background music matched to section mood",
                "suggested_sources": ["YouTube Audio Library local catalog"], "download_status": "match_local_catalog"})
        cues = chunk.get("sfx_cues", chunk.get("sfx_candidates", []))
        for cue in cues:
            cue = str(cue).strip()
            if not cue or cue.lower() == "none":
                continue
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": cue,
                "category": "sfx", "segment": i, "purpose": "Sound effect matched to the scene direction",
                "suggested_sources": ["YouTube Audio Library local catalog"], "download_status": "match_local_catalog"})
    ledger = {"policy": "AI provides per-scene needs; Python resolves, downloads, caches and records reusable files. Discovery is not reuse permission.", "assets": requests_list}
    _write(out_dir / "asset_requests.json", ledger)
    return ledger


def search_asset_sources(ledger_path: Path, *, max_results: int = 3) -> dict:
    """Attach candidates from Wikimedia Commons, IA, LOC and NASA (no keys)."""
    ledger = _json(ledger_path, {"assets": []})
    from media_library import MediaLibrary
    library = MediaLibrary(ledger_path.parents[2] / "assets")
    for asset in ledger.get("assets", []):
        if asset.get("category") not in {"picture", "video"}:
            continue
        asset["candidates"] = library.search_public_archives(asset["query"], asset["category"], limit=max_results)
    _write(ledger_path, ledger)
    return ledger


def make_metadata(topic: str, channel: dict, script: dict) -> dict:
    prompt = f"""Create truthful YouTube packaging for this documentary. No unsupported promise, false urgency, or claim beyond the script.
Topic: {topic}; channel: {channel.get('name', channel.get('handle'))}.
Script summary: {script.get('logline')}\nReturn strict JSON {{"title_variants":[{{"title":"...","hypothesis":"..."}} x 3],"description":"...","tags":["..."],"thumbnail_briefs":[{{"concept":"...","composition":"...","text":"2-4 words","contrast_angle":"..."}} x 2]}}. Keep titles distinct hypotheses; thumbnails readable at mobile size."""
    data = parse_json_text(response_text(openai_response(prompt)))
    data["testing_note"] = "For native YouTube test, use YouTube Studio's concurrent test when available. Do not sequentially swap manually and call it a controlled A/B test. API support for starting the native experiment is not assumed."
    return data


def generate_thumbnail(prompt: str, path: Path, *, size: str = "1536x1024") -> bool:
    """Optional OpenAI image generation; output requires crop/layout QA at 16:9."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return False
    _require_requests()
    response = HTTP.post(f"{API_ROOT}/images/generations", headers={"Authorization": f"Bearer {key}"}, json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": size, "quality": "high", "n": 1}, timeout=300)
    response.raise_for_status()
    import base64
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(response.json()["data"][0]["b64_json"]))
    return True


def prepare_job(channel: dict, topic: str, base_dir: Path, *, generate_thumbnails: bool = False) -> Path:
    job_id = f"{_slug(channel.get('handle', 'channel'))}_{_slug(topic)}"
    out = base_dir / "jobs" / job_id
    out.mkdir(parents=True, exist_ok=True)
    research = research_story(topic, channel)
    script = write_script(topic, channel, research)
    assets = asset_research(script, out)
    metadata = make_metadata(topic, channel, script)
    _write(out / "research.json", research)
    _write(out / "script.json", script)
    _write(out / "metadata_ab_test.json", metadata)
    _write(out / "job.json", {"job_id": job_id, "channel": channel, "topic": topic, "status": "preproduction_ready",
                                 "research_file": "research.json", "script_file": "script.json", "asset_manifest": "asset_requests.json",
                                 "metadata_file": "metadata_ab_test.json", "human_polish_required": True})
    if generate_thumbnails:
        for index, concept in enumerate(metadata.get("thumbnail_briefs", [])[:2], 1):
            image_prompt = f"YouTube documentary thumbnail, 16:9 composition, high contrast, topic: {topic}. {concept.get('composition','')}. Visual idea: {concept.get('concept','')}. Leave clear negative space for short text: {concept.get('text','')}. Avoid logos and tiny text."
            generate_thumbnail(image_prompt, out / f"thumbnail_{index}.png")
    return out


def qa_package(job_dir: Path, final_files: list[Path] | None = None) -> list[str]:
    errors = []
    required = ["research.json", "script.json", "asset_requests.json", "metadata_ab_test.json"]
    for name in required:
        if not (job_dir / name).exists():
            errors.append(f"Missing {name}")
    script = _json(job_dir / "script.json", {})
    if script.get("unsupported_claim_ids"):
        errors.append("Script references claim IDs absent from research ledger")
    if not script.get("target_met_estimate"):
        errors.append("Long-form script estimate is under 2,500 words; confirm 20-minute narration before editing")
    for claim in _json(job_dir / "research.json", {}).get("claims", []):
        if not claim.get("url", "").startswith("http"):
            errors.append(f"Claim {claim.get('claim_id')} has no source URL")
    for asset in _json(job_dir / "asset_requests.json", {}).get("assets", []):
        if asset.get("local_path") and asset.get("rights_status") not in {"owned", "licensed", "public_domain", "permission"}:
            errors.append(f"Asset {asset.get('asset_id')} has no documented reuse status")
    if final_files is not None:
        for file in final_files:
            if not file.exists() or file.stat().st_size < 5 * 1024 * 1024:
                errors.append(f"Missing or implausibly small final video: {file}")
    return errors


def record_history(history_path: Path, channel_id: str, topic: str, video_id: str | None = None) -> None:
    history = _json(history_path, [])
    history.append({"channel_id": channel_id, "topic": topic, "video_id": video_id, "completed_at": datetime.now(timezone.utc).isoformat()})
    _write(history_path, history)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research, script, asset brief, metadata and thumbnail preproduction")
    parser.add_argument("--channels", type=Path, required=True, help="JSON array of channel configs")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--channel", help="Channel handle to process")
    parser.add_argument("--topic", help="Skip discovery and prepare this topic")
    parser.add_argument("--make-thumbnails", action="store_true", help="Call the configured OpenAI image model")
    parser.add_argument("--capture-research-screenshots", action="store_true", help="Capture full-page screenshots of up to three cited source pages into assets/research_references")
    parser.add_argument("--screenshot-limit", type=int, default=3, help="Maximum cited pages to capture (default: 3)")
    parser.add_argument("--discover-assets", action="store_true", help="Search Pixabay and public archives for candidate URLs")
    parser.add_argument("--acquire-assets", action="store_true", help="Download/reuse Pixabay or public archive visuals, generate missing stills, and match local audio")
    parser.add_argument("--youtube-audio-library-dir", type=Path, help="Folder containing downloaded YouTube Audio Library files; expected subfolders background_music/ and sfx/")
    args = parser.parse_args()
    workspace = ensure_workspace(args.base_dir)
    required_keys = ("OPENAI_API_KEY",) if args.topic else ("OPENAI_API_KEY", "YOUTUBE_API_KEY")
    api_status = configure_api_keys(args.base_dir, required_keys=required_keys)
    if api_status["missing_optional"]:
        print("Optional providers not configured (their features will be skipped): " + ", ".join(api_status["missing_optional"]))
    print(f"Production folders ready: {workspace['root']}")
    channels = _json(args.channels, [])
    channel = next((c for c in channels if not args.channel or c.get("handle") == args.channel), None)
    if channel is None:
        raise SystemExit("No matching channel in channels JSON")
    history_path = args.base_dir / "history.json"
    history = _json(history_path, [])
    topic = args.topic
    if not topic:
        trending = discover_youtube_videos(channel.get("niche", ""))
        _write(args.base_dir / "discovery" / f"{_slug(channel.get('handle','channel'))}.json", trending)
        ideas = propose_ideas(channel, trending, history)
        _write(args.base_dir / "discovery" / "ideas.json", {"channel": channel, "ideas": ideas})
        if not ideas:
            raise SystemExit("No non-duplicate topic ideas generated")
        topic = ideas[0]["topic"]
    job_dir = prepare_job(channel, topic, args.base_dir, generate_thumbnails=args.make_thumbnails)
    if args.capture_research_screenshots:
        research = _json(job_dir / "research.json", {})
        captures = capture_research_screenshots(
            research, topic, args.base_dir / "assets", job_id=job_dir.name,
            limit=max(0, args.screenshot_limit),
        )
        successful = sum(item.get("status") == "captured" for item in captures["screenshots"])
        print(f"Research reference screenshots: {successful}/{len(captures['screenshots'])} captured")
    if args.discover_assets:
        search_asset_sources(job_dir / "asset_requests.json")
    if args.acquire_assets:
        from media_library import MediaLibrary, acquire_asset_requests
        shared_assets = MediaLibrary(args.base_dir / "assets")
        if args.youtube_audio_library_dir:
            for category in ("music", "sfx"):
                source_folder = args.youtube_audio_library_dir / category
                if source_folder.exists():
                    shared_assets.import_audio_library_folder(source_folder, category)
        requests_manifest = _json(job_dir / "asset_requests.json", {"assets": []})
        pack = acquire_asset_requests(requests_manifest, shared_assets)
        _write(job_dir / "selected_assets.json", pack)
    print(f"Preproduction package ready: {job_dir}")
    print("Next: make narration, render the timeline, and run final QA. Check selected_assets.json for any requested assets that could not be matched.")


if __name__ == "__main__":
    main()
