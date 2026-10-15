Warning: truncated output (original token count: 26074)
Total output lines: 1691

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
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from workspace_setup import configure_api_keys, ensure_workspace

try:
    import requests
except ImportError:  # pure local QA helpers remain importable without API extras
    requests = None

from cost_controls import fallback_model_env, load_checkpoint, record_openai_usage, resolve_model, save_checkpoint, signature as stage_signature

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


def unfinished_jobs(base_dir: Path) -> list[tuple[Path, str]]:
    """Find job folders without a passed final render QA result."""
    jobs_root = Path(base_dir) / "jobs"
    if not jobs_root.is_dir():
        return []
    pending = []
    for folder in sorted((p for p in jobs_root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime):
        metadata_path = folder / "job.json"
        if not metadata_path.is_file():
            if any(folder.iterdir()):
                pending.append((folder, "incomplete package"))
            continue
        status = str(_json(metadata_path, {}).get("status", "in_progress")).lower()
        if status not in {"completed", "qa_passed"}:
            pending.append((folder, status))
    return pending


def require_no_unfinished_jobs(base_dir: Path) -> None:
    pending = unfinished_jobs(base_dir)
    if pending:
        path, status = pending[0]
        raise RuntimeError(
            f"A previous topic is still unfinished ({status}): {path}. "
            "Complete its asset retry, narration, render, and QA before starting another topic."
        )


def retry_job_assets(job_dir: Path, base_dir: Path, youtube_audio_library_dir: Path | None = None) -> dict:
    """Retry unresolved requests in-place without regenerating research or script."""
    from media_library import MediaLibrary, acquire_asset_requests

    job_dir = Path(job_dir).expanduser().resolve()
    base_dir = Path(base_dir).expanduser().resolve()
    source_path = job_dir / "asset_requests.json"
    selected_path = job_dir / "selected_assets.json"
    if not source_path.is_file():
        raise FileNotFoundError(f"Job has no asset_requests.json: {source_path}")
    source = _json(source_path, {"assets": []})
    prior = _json(selected_path, {"assets": [], "unresolved": []})
    request_by_id = {str(row.get("asset_id")): row for row in source.get("assets", [])}
    unresolved_ids = {str(row.get("asset_id")) for row in prior.get("unresolved", [])}
    existing_rows = {str(row.get("asset_id")): row for row in prior.get("assets", [])}
    if not selected_path.is_file():
        unresolved_ids.update(request_by_id)
    # Retry requests omitted by an older or partial manifest as well.
    unresolved_ids.update(set(request_by_id) - set(existing_rows) - unresolved_ids)
    for asset_id, row in existing_rows.items():
        selected = row.get("selected") or {}
        if not selected.get("path") or not Path(selected["path"]).is_file():
            unresolved_ids.add(asset_id)
    pending = []
    for asset_id, original in request_by_id.items():
        if asset_id not in unresolved_ids:
            continue
        request = dict(original)
        request["candidates"] = []
        request["social_search_performed"] = False
        pending.append(request)
    if not pending:
        return prior

    # A headline screenshot is generated on demand during preproduction. Retry
    # that same search-and-capture workflow here instead of treating the
    # screenshot as a file that the user must supply manually.
    screenshot_pending = [row for row in pending if row.get("category") == "news_headline_screenshot"]
    if screenshot_pending:
        job_record = _json(job_dir / "job.json", {})
        script = _json(job_dir / "script.json", {})
        script_requests = {
            row["asset_id"]: row for row in _article_screenshot_requests(script)
        }
        screenshot_chunks = []
        retryable = []
        for row in screenshot_pending:
            asset_id = str(row.get("asset_id", ""))
            script_row = script_requests.get(asset_id, {})
            search_query = str(row.get("search_query") or row.get("query") or script_row.get("search_query") or "").strip()
            keywords = row.get("highlight_keywords") or script_row.get("highlight_keywords") or row.get("keywords") or []
            keywords = [str(value).strip() for value in keywords if str(value).strip()]
            if not search_query or not keywords:
                row.update(status="failed", error="Screenshot request is missing a search query or highlight keywords in asset_requests.json and script.json")
                continue
            retryable.append(row)
            screenshot_chunks.append({"news_article_screenshot": {
                "asset_id": asset_id,
                "search_query": search_query,
                "highlight_keywords": keywords,
                "reason": row.get("reason") or script_row.get("reason", ""),
            }})

        if retryable:
            topic = str(job_record.get("topic") or "").strip()
            if not topic:
                for row in retryable:
                    row.update(status="failed", error="Cannot retry screenshot: job.json has no topic")
            else:
                print(f"[StoryPilot] Retrying {len(retryable)} requested news headline screenshot(s) with Google News RSS and capture.", flush=True)
                try:
                    capture_manifest = fulfill_news_headline_requests(
                        topic, {"narration_chunks": screenshot_chunks}, base_dir / "assets",
                        job_id=job_dir.name, research=_json(job_dir / "research.json", {}),
                    )
                    captures = {str(item.get("asset_id")): item for item in capture_manifest.get("screenshots", [])}
                    for row in retryable:
                        captured = captures.get(str(row.get("asset_id")))
                        if captured:
                            row.update({key: captured[key] for key in (
                                "status", "url", "publisher", "headline", "image_path", "highlighted_keywords",
                                "captured_at", "error", "searched", "published_date", "http_status",
                            ) if key in captured})
                            if captured.get("status") == "captured":
                                row["local_path"] = captured.get("image_path")
                        else:
                            row.update(status="failed", error="Headline screenshot retry returned no result")
                except Exception as exc:
                    for row in retryable:
                        row.update(status="failed", error=f"Screenshot retry failed: {type(exc).__name__}: {str(exc)[:250]}")

        # Persist the capture metadata to the source manifest so later retries
        # and QA can explain what happened, even if no screenshot was captured.
        for row in screenshot_pending:
            original = request_by_id.get(str(row.get("asset_id")))
            if original is not None:
                original.update({key: row[key] for key in (
                    "status", "error", "search_query", "highlight_keywords", "url", "publisher", "headline",
                    "image_path", "local_path", "highlighted_keywords", "captured_at", "searched",
                    "published_date", "http_status",
                ) if key in row})
        _write(source_path, source)
        # Refresh from the updated source rows before passing them to acquisition.
        pending_by_id = {str(row.get("asset_id")): row for row in pending}
        pending = [pending_by_id.get(str(row.get("asset_id")), row) for row in pending]

    library = MediaLibrary(base_dir / "assets")
    if youtube_audio_library_dir:
        audio_root = Path(youtube_audio_library_dir).expanduser()
        for category, aliases in (("music", ("music", "background_music")), ("sfx", ("sfx",))):
            for folder_name in aliases:
                source_folder = audio_root / folder_name
                if source_folder.is_dir():
                    imported = library.import_audio_library_folder(source_folder, category)
                    print(f"[StoryPilot] Imported {len(imported)} local YouTube Audio Library {category} file(s).", flush=True)
                    break
    pack = acquire_asset_requests({"assets": pending}, library)
    merged_assets = dict(existing_rows)
    merged_unresolved = {str(row.get("asset_id")): row for row in prior.get("unresolved", [])}
    for row in pack.get("assets", []):
        asset_id = str(row.get("asset_id"))
        merged_assets[asset_id] = row
        merged_unresolved.pop(asset_id, None)
    for row in pack.get("unresolved", []):
        merged_unresolved[str(row.get("asset_id"))] = row
    result = dict(prior)
    result["assets"] = list(merged_assets.values())
    result["unresolved"] = list(merged_unresolved.values())
    result["retry_summary"] = {"retried": len(pending), "resolved_this_retry": len(pack.get("assets", [])),
                               "still_unresolved": len(result["unresolved"]),
                               "completed_at": datetime.now(timezone.utc).isoformat()}
    _write(selected_path, result)
    return result


def mark_job_render_status(job_json_path: Path, render_result: dict, edit_job: dict) -> None:
    """Unlock the next topic only after a final render passes QA."""
    assets_manifest = edit_job.get("assets_manifest")
    source_job_dir = Path(assets_manifest).resolve().parent if assets_manifest else Path(job_json_path).resolve().parent
    source_job = source_job_dir / "job.json"
    if not source_job.is_file():
        return
    metadata = _json(source_job, {})
    outputs = metadata.setdefault("rendered_outputs", {})
    format_name = str(edit_job.get("format", "long_form"))
    video_path = render_result.get("video_path")
    report_path = str(Path(video_path).with_name(Path(video_path).stem + "_qa.json")) if video_path else None
    outputs[format_name] = {"status": render_result.get("status", "failed"), "video_path": video_path,
                            "qa_report": report_path, "completed_at": datetime.now(timezone.utc).isoformat()}
    if outputs.get("long_form", {}).get("status") == "passed":
        metadata["status"] = "completed"
        metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    elif render_result.get("status") == "passed":
        metadata["status"] = "preproduction_ready"
    else:
        metadata["status"] = "qa_failed"
    _write(source_job, metadata)


class _ConsoleProgress:
    """Live main and indeterminate subtask bars for long-running stages."""
    def __init__(self, estimated_total_seconds: float = 900.0):
        self.started = time.monotonic()
        self.estimated_total = estimated_total_seconds
        self.percent = 0.0
        self.label = "Starting preproduction"
        self.subtask = "Preparing"
        self.subtask_started = self.started
        self.frame = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"

    def set_estimate(self, seconds: float) -> None:
        with self.lock:
            self.estimated_total = max(60.0, seconds)

    def update(self, message: str, percent: float) -> None:
        with self.lock:
            self.label = message[:36]
            self.percent = max(self.percent, min(100.0, percent))
            self.subtask = "Preparing next step"
            self.subtask_started = time.monotonic()
        self.render()

    def update_subtask(self, message: str) -> None:
        """Show the current operation without pretending to know its percent."""
        with self.lock:
            if message != self.subtask:
                self.subtask = message[:64]
                self.subtask_started = time.monotonic()
        self.render()

    def render(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started
            percent = self.percent
            expected = self.estimated_total
            label = self.label
            subtask = self.subtask
            subtask_elapsed = time.monotonic() - self.subtask_started
            frame = self.frame
            self.frame += 1
        nominal_done = expected * percent / 100.0
        scale = min(2.5, max(0.5, elapsed / nominal_done)) if nominal_done >= 20 else 1.0
        remaining = expected * (1.0 - percent / 100.0) * scale
        filled = int(12 * percent / 100.0)
        main_bar = "=" * filled + " " * (12 - filled)
        phase = frame % 14
        phase = phase if phase <= 7 else 14 - phase
        sub_bar = "[" + "." * phase + "===" + "." * (7 - phase) + "]"

        def compact(seconds: float) -> str:
            seconds = max(0, int(seconds))
            minutes, seconds = divmod(seconds, 60)
            return f"{minutes}m{seconds:02d}s"

        # Leave one terminal column unused to avoid auto-wrap. Once a line
        # wraps, carriage return alone cannot return to its first row.
        columns = shutil.get_terminal_size((120, 24)).columns
        width = max(40, columns - 1)
        overall = (f"[StoryPilot] [{main_bar}] {percent:4.0f}% | {compact(elapsed)} "
                   f"| ETA {compact(remaining)} | {label[:12]}")
        tail_prefix = f" | {sub_bar} "
        tail_suffix = f" +{compact(subtask_elapsed)}" if width >= 150 else ""
        available = max(0, width - len(overall) - len(tail_prefix) - len(tail_suffix))
        status = overall + tail_prefix + subtask[:available] + tail_suffix
        sys.stdout.write("\x1b[2K\r" + status[:width].ljust(width))
        sys.stdout.flush()

    def _heartbeat(self) -> None:
        while not self.stop_event.wait(2.0):
            self.render()

    def finish(self) -> None:
        self.update("Preproduction complete", 100.0)
        self.stop_event.set()
        self.thread.join(timeout=0.2)
        sys.stdout.write("\n")
        sys.stdout.flush()


_RUN_PROGRESS: _ConsoleProgress | None = None


def _progress(message: str, percent: float | None = None) -> None:
    """Print a stage update and refresh the live progress bar."""
    if percent is None:
        markers = (
            ("Setup:", 1), ("Channel selected:", 3), ("Discovery 1/2:", 5),
            ("Discovery 1/2 complete:", 10), ("Discovery 2/2 complete:", 14),
            ("Using supplied topic:", 14), ("Research 1/5:", 20),
            ("Research 2/5:", 34), ("Research verification complete:", 42),
            ("Script 3/5:", 45), ("Script complete:", 54), ("Assets 4/5:", 57),
            ("News visuals:", 60), ("Packaging 5/5:", 63),
            ("Asset discovery:", 75), ("Asset acquisition:", 83),
            ("Asset acquisition complete:", 99), ("Preproduction complete.", 100),
        )
        percent = next((value for marker, value in reversed(markers) if marker in message), 3)
        if message.startswith("Thumbnail "):
            match = re.search(r"Thumbnail (\d+)/(\d+)", message)
            if match:
                percent = 63 + 12 * int(match.group(1)) / max(1, int(match.group(2)))
    if _RUN_PROGRESS is not None:
        _RUN_PROGRESS.update(message, percent)
    print(f"\n[StoryPilot] {message}", flush=True)


def _subprogress(message: str) -> None:
    """Update the animated subtask bar during a blocking operation."""
    if _RUN_PROGRESS is not None:
        _RUN_PROGRESS.update_subtask(message)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "topic"


def get_niche_config(niche: str, prompts_file: Path | None = None) -> dict:
    """Load the best matching niche guidance, with a safe generic fallback."""
    path = prompts_file or Path(__file__).with_name("niche_prompts.json")
    configs = _json(path, {})
    query = re.sub(r"[^a-z0-9]+", " ", niche.lower()).strip()
    for name, config in configs.items():
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if query == key or key in query or query in key:
            return config
    return {
        "voice_name": "en-US-Ethan:MAI-Voice-2.1",
        "narration_direction": "Natural, clear English. Match the scene's emotional tone without forced slang or exaggerated delivery.",
        "default_search_fallback": "documentary topic",
        "research_prompt": "Prioritise company filings, primary documents, reputable reporting, and independent corroboration. Flag uncertainty and conflicting accounts.",
        "system_prompt": "Tell a clear, evidence-led story with a strong beginning, a logical build, and a satisfying factual conclusion.",
        "retention_guidance": "Use meaningful discoveries and pay off key questions to maintain curiosity; do not invent suspense.",
    }


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


NEWS_PUBLISHER_DOMAINS = {
    "abc.net.au", "abcnews.go.com", "apnews.com", "arstechnica.com", "bbc.com",
    "bbc.co.uk", "bloomberg.com", "cnbc.com", "cnn.com", "ft.com", "theguardian.com",
    "nytimes.com", "npr.org", "reuters.com", "smh.com.au", "theverge.com", "time.com",
    "washingtonpost.com", "wsj.com", "wired.com", "techcrunch.com", "news.com.au", "etf.com",
}


def _is_approved_news_url(url: str) -> bool:
    if not _is_public_http_url(url):
        return False
    host = urlparse(url).hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in NEWS_PUBLISHER_DOMAINS)


def _article_screenshot_requests(script: dict) -> list[dict]:
    """Extract only headline captures explicitly requested by the AI creative brief."""
    requests_list = []
    for segment, chunk in enumerate(script.get("narration_chunks", []), 1):
        request = chunk.get("news_article_screenshot")
        if not isinstance(request, dict) or not request.get("search_query"):
            continue
        keywords = [str(word).strip() for word in request.get("highlight_keywords", []) if str(word).strip()]
        if not keywords:
            continue
        requests_list.append({
            "asset_id": str(request.get("asset_id") or f"news_headline_{len(requests_list)+1:02d}"),
            "category": "news_headline_screenshot",
            "segment": segment,
            "search_query": str(request["search_query"]).strip(),
            "highlight_keywords": keywords,
            "reason": str(request.get("reason", "")),
            "status": "awaiting_search",
        })
        if len(requests_list) >= 2:
            break
    return requests_list


def find_news_article(topic: str, request: dict) -> dict | None:
    """Search the public Google News RSS feed from Python, without OpenAI API calls.

    Google News RSS is an undocumented interface, so this is keyless but may
    change or rate-limit requests. The returned article link is opened by
    Playwright later, where its final publisher domain and visible headline are
    checked before a screenshot is saved.
    """
    _require_requests()
    verified_url = str(request.get("verified_article_url") or "").strip()
    if verified_url and _is_approved_news_url(verified_url):
        return {
            "headline": str(request.get("verified_article_title") or "").strip(),
            "url": verified_url,
            "publisher": str(request.get("verified_article_publisher") or urlparse(verified_url).hostname),
            "published_date": "from verified research source",
            "search_provider": "verified_research_source",
        }
    search_query = str(request.get("search_query") or "").strip()
    if not search_query:
        return None
    response = HTTP.get(
        "https://news.google.com/rss/search",
        params={"q": search_query, "hl": "en-AU", "gl": "AU", "ceid": "AU:en"},
        headers={"User-Agent": "StoryPilot/1.0 (+headline research)"},
        timeout=30,
    )
    response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ValueError(f"Google News returned invalid RSS/XML: {exc}") from exc

    keywords = [str(value).strip().casefold() for value in request.get("highlight_keywords", []) if str(value).strip()]
    for item in root.findall(".//item")[:30]:
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        source_node = item.find("source")
        publisher = " ".join((source_node.text if source_node is not None and source_node.text else "").split())
        publisher_url = source_node.get("url", "") if source_node is not None else ""
        if not title or not link or not _is_approved_news_url(publisher_url):
            continue
        # Google News often appends the publisher after a final dash. Remove it
        # only when it matches the structured source element.
        if publisher and title.casefold().endswith(f" - {publisher}".casefold()):
            title = title[: -(len(publisher) + 3)].strip()
        if keywords and not any(keyword in title.casefold() for keyword in keywords):
            continue
        return {
            "headline": title,
            # Google News links redirect to the publisher. _capture_headline()
            # validates the final browser URL against the publisher allowlist.
            "url": link,
            "publisher": publisher or urlparse(publisher_url).hostname,
            "published_date": (item.findtext("pubDate") or "unknown").strip(),
            "search_provider": "google_news_rss",
        }
    return None


def _verified_news_source_for_request(research: dict, request: dict) -> dict | None:
    """Reuse a matching, already-verified source URL before querying RSS again."""
    query = str(request.get("search_query") or "")
    requested_domains = {
        match.group(1).lower().removeprefix("www.")
        for match in re.finditer(r"(?:https?://)?(?:www\.)?([a-z0-9-]+\.[a-z]{2,})", query, re.I)
    }
    requested_domains = {domain for domain in requested_domains if domain in NEWS_PUBLISHER_DOMAINS}
    query_terms = set(re.findall(r"[a-z0-9]+", query.lower())) - {
        "the", "and", "for", "from", "with", "sets", "record", "article", "headline", "news", "com",
        "september", "october", "november", "december", "january", "february", "march", "april", "may", "june", "july", "august",
    }
    keyword_terms = {
        token for keyword in request.get("highlight_keywords", [])
        for token in re.findall(r"[a-z0-9]+", str(keyword).lower())
    }
    claim_rows = research.get("verified_claims", []) if isinstance(research, dict) else []
    candidates = []
    for claim in claim_rows:
        if not isinstance(claim, dict) or claim.get("verification_status") not in {"verified", "qualified"}:
            continue
        base_text = " ".join(str(claim.get(key, "")) for key in ("claim", "source_title", "support", "publisher", "verification_note"))
        sources = [{"url": claim.get("url"), "title": claim.get("source_title"), "publisher": claim.get("publisher") }]
        sources.extend(claim.get("checked_sources", []) if isinstance(claim.get("checked_sources"), list) else [])
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if not _is_approved_news_url(url):
                continue
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
            if requested_domains and not any(host == domain or host.endswith("." + domain) for domain in requested_domains):
                continue
            context = f"{base_text} {source.get('title', '')} {source.get('support', '')} {url}".lower()
            context_terms = set(re.findall(r"[a-z0-9]+", context))
            keyword_hits = len(keyword_terms & context_terms)
            query_hits = len(query_terms & context_terms)
            # Require direct evidence that this source concerns the specific
            # requested fact; domain match alone must never select an article.
            if keyword_hits == 0 and query_hits < 2:
                continue
            score = keyword_hits * 4 + query_hits
            candidates.append((score, {
                "url": url,
                "title": str(source.get("title") or claim.get("source_title") or ""),
                "publisher": str(source.get("publisher") or claim.get("publisher") or host),
            }))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _keyword_match_pattern(keyword: str) -> re.Pattern:
    """Match requested wording while allowing harmless decimal precision in amounts."""
    tokens = re.findall(r"\$?\d[\d,]*(?:\.\d+)?|[A-Za-z][A-Za-z0-9’'-]*", str(keyword))
    if not tokens:
        return re.compile(re.escape(str(keyword)), re.I)
    parts = []
    for token in tokens:
        if re.fullmatch(r"\$?\d[\d,]*(?:\.\d+)?", token):
            currency = r"\$?\s*" if token.startswith("$") else ""
            number = token.lstrip("$").replace(",", "")
            number_pattern = re.escape(number)
            if "." not in number:
                number_pattern += r"(?:\.\d+)?"
            parts.append(currency + number_pattern)
        else:
            parts.append(re.escape(token))
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)


def _matched_keyword_text(text: str, keywords: list[str]) -> list[str]:
    matches = []
    for keyword in keywords:
        match = _keyword_match_pattern(keyword).search(text)
        if match and match.group(0) not in matches:
            matches.append(match.group(0))
    return matches


def _capture_headline(candidate: dict, keywords: list[str], image_path: Path) -> dict:
    """Capture the visible article headline and, if needed, its matching lead paragraph."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1400}, device_scale_factor=1)
            page.set_default_navigation_timeout(45000)
            response = page.goto(candidate["url"], wait_until="domcontentloaded")
            if not _is_approved_news_url(page.url):
                raise ValueError("News page redirected outside the approved publisher list")
            headline = page.locator("h1").first
            if not headline.count() or not headline.is_visible():
                raise ValueError("Article page has no visible headline heading (h1)")
            actual_headline = " ".join(headline.inner_text().split())
            if not actual_headline:
                raise ValueError("Article page headline is empty")
            matched_text = _matched_keyword_text(actual_headline, keywords)
            highlight_location = "headline"
            target = headline
            if not matched_text:
                paragraphs = page.locator("article p, main p")
                for index in range(min(paragraphs.count(), 16)):
                    paragraph = paragraphs.nth(index)
                    text = " ".join(paragraph.inner_text().split())
                    matched_text = _matched_keyword_text(text, keywords)
                    if matched_text:
                        target = paragraph
                        highlight_location = "lead_paragraph"
                        break
            if not matched_text:
                raise ValueError("Requested keyword was not present in the article headline or lead paragraphs")
            result = target.evaluate(
                """(el, words) => {
                  const terms = words.filter(Boolean).sort((a, b) => b.length - a.length);
                  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                  const nodes = [];
                  while (walker.nextNode()) nodes.push(walker.currentNode);
                  const matched = new Set();
                  for (const node of nodes) {
                    const text = node.nodeValue || '';
                    const lower = text.toLowerCase();
                    const fragment = document.createDocumentFragment();
                    let cursor = 0;
                    while (cursor < text.length) {
                      let bestStart = -1, bestTerm = '';
                      for (const term of terms) {
                        const at = lower.indexOf(term.toLowerCase(), cursor);
                        if (at >= 0 && (bestStart < 0 || at < bestStart || (at === bestStart && term.length > bestTerm.length))) {
                          bestStart = at;
                          bestTerm = term;
                        }
                      }
                      if (bestStart < 0) break;
                      fragment.append(document.createTextNode(text.slice(cursor, bestStart)));
                      const matchedText = text.slice(bestStart, bestStart + bestTerm.length);
                      const mark = document.createElement('mark');
                      mark.textContent = matchedText;
                      mark.style.cssText = 'background:#ffe600;color:#111;padding:0 .04em;border-radius:.08em;box-decoration-break:clone;-webkit-box-decoration-break:clone';
                      fragment.append(mark);
                      matched.add(matchedText);
                      cursor = bestStart + bestTerm.length;
                    }
                    if (cursor > 0) {
                      fragment.append(document.createTextNode(text.slice(cursor)));
                      node.parentNode.replaceChild(fragment, node);
                    }
                  }
                  return [...matched];
            …10074 tokens truncated…"NASA video library (no key)"],
                                 "reference_urls": reference_urls, "source_url": None, "local_path": None,
                                 "rights_status": "unknown", "max_duration_seconds": 5,
                                 "license_evidence": None, "download_status": "not_downloaded"})
        music_query = str(chunk.get("music_mood", "")).strip()
        if music_query:
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": music_query,
                "category": "music", "segment": i, "purpose": "Background music matched to section mood",
                "suggested_sources": ["Freesound API", "reusable local audio catalog"], "download_status": "search_freesound_or_local_catalog"})
        cues = chunk.get("sfx_cues", chunk.get("sfx_candidates", []))
        for cue in cues:
            cue = str(cue).strip()
            if not cue or cue.lower() == "none":
                continue
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": cue,
                "category": "sfx", "segment": i, "purpose": "Sound effect matched to the scene direction",
                "suggested_sources": ["Freesound API", "reusable local audio catalog"], "download_status": "search_freesound_or_local_catalog"})
    requests_list.extend(_article_screenshot_requests(script))
    ledger = {"policy": "AI provides scene-specific needs; Python searches configured sources, downloads selected candidates, asks AI to choose a relevant timestamped excerpt, cuts it to at most five seconds, and records source metadata. Discovery is not reuse permission.", "assets": requests_list}
    _write(out_dir / "asset_requests.json", ledger)
    return ledger


def search_asset_sources(ledger_path: Path, *, max_results: int = 3) -> dict:
    """Attach video/image candidates from public archives and social video search."""
    ledger = _json(ledger_path, {"assets": []})
    from media_library import MediaLibrary
    from social_video import discover_social_videos
    library = MediaLibrary(ledger_path.parents[2] / "assets")
    assets = [asset for asset in ledger.get("assets", []) if asset.get("category") in {"picture", "video"}]
    total = len(assets)
    social_limit = max(0, int(os.getenv("SOCIAL_VIDEO_SEARCHES_PER_RUN", "20")))
    social_queries: set[str] = set()
    for index, asset in enumerate(assets, 1):
        pct = 75 + 7 * index / max(1, total)
        _progress(f"Asset search {index}/{total}: {asset.get('category')} — {asset.get('query', '')[:60]}", pct)
        candidates = library.search_public_archives(asset["query"], asset["category"], limit=max_results)
        if asset.get("category") == "video":
            query_key = str(asset.get("query", "")).strip().casefold()
            social = []
            if query_key not in social_queries and len(social_queries) < social_limit:
                social_queries.add(query_key)
                social = discover_social_videos(asset["query"], limit_per_platform=min(max_results, 2))
            # Put platform search results first: the acquisition stage tries these
            # and retains only the AI-selected five-second excerpt.
            asset["candidates"] = social + candidates
            asset["social_search_performed"] = query_key in social_queries
        else:
            asset["candidates"] = candidates
    _write(ledger_path, ledger)
    return ledger


def make_metadata(topic: str, channel: dict, script: dict) -> dict:
    prompt = f"""Create truthful YouTube packaging for this documentary. No unsupported promise, false urgency, or claim beyond the verified story.
Topic: {topic}; channel: {channel.get('name', channel.get('handle'))}.
Script summary: {script.get('logline')}\nReturn strict JSON {{"title_variants":[{{"title":"...","hypothesis":"..."}} x 3],"description":"...","tags":["..."],"thumbnail_briefs":[{{"subject":"specific recognizable person, brand, or company from this story","subject_type":"person|brand|company","recognition_cues":"distinctive verified appearance, product, colors, or logo","concept":"...","composition":"...","text":"2-3 word curiosity hook, not the title","contrast_angle":"..."}}]}} with exactly ONE thumbnail brief. Choose a recognizable named person, brand, or company central to the story as the main focal subject. Do not substitute a generic silhouette if an actual subject is identifiable from the topic and script. The actual source image will be placed in the thumbnail directly, so select a subject that can be found in Wikimedia Commons or another public image archive. Keep title variants distinct hypotheses. The thumbnail should read on a phone and create a truthful curiosity gap without repeating the title."""
    _subprogress("Packaging: writing titles and thumbnail brief")
    data = parse_json_text(response_text(openai_response(prompt, stage="metadata")))
    data["thumbnail_briefs"] = data.get("thumbnail_briefs", [])[:1]
    data["testing_note"] = "For native YouTube test, use YouTube Studio's concurrent test when available. Do not sequentially swap manually and call it a controlled A/B test. API support for starting the native experiment is not assumed."
    return data


def resolve_thumbnail_reference(subject: str, subject_type: str, assets_dir: Path) -> dict | None:
    """Find or download a real subject image from keyless public image sources."""
    from media_library import MediaLibrary

    subject = str(subject or "").strip()
    if not subject:
        return None
    library = MediaLibrary(assets_dir)
    descriptor = "portrait" if str(subject_type).lower() == "person" else "logo"
    terms = {
        word for word in re.findall(r"[a-z0-9]+", subject.lower())
        if len(word) > 1 and word not in {"inc", "ltd", "limited", "corp", "corporation", "company", "group", "plc"}
    }
    if not terms:
        terms = set(re.findall(r"[a-z0-9]+", subject.lower()))

    def rank(asset: dict) -> tuple[float, int]:
        metadata = asset.get("metadata") or {}
        title = str(asset.get("title") or metadata.get("title") or "")
        description = str(metadata.get("description") or metadata.get("ImageDescription") or "")
        # Do not count search-query tags as proof that the file depicts the subject.
        haystack = f"{title} {description} {asset.get('creator', '')}".lower()
        words = set(re.findall(r"[a-z0-9]+", haystack))
        score = len(terms & words) / max(1, len(terms))
        if subject.lower() in haystack:
            score = max(score, 1.0)
        path = Path(str(asset.get("path", "")))
        real_source = asset.get("source") in {
            "wikimedia_commons", "internet_archive", "library_of_congress", "nasa_images", "pixabay",
        }
        if not real_source or not path.is_file():
            score = 0.0
        # Prefer a Commons photo/logo when relevance scores are tied.
        source_rank = 1 if asset.get("source") == "wikimedia_commons" else 0
        return score, source_rank

    def choose(rows: list[dict]) -> dict | None:
        usable = [(rank(row), row) for row in rows]
        usable = [(score, row) for score, row in usable if score[0] >= 0.5]
        if not usable:
            return None
        (score, _), row = max(usable, key=lambda item: item[0])
        metadata = row.get("metadata") or {}
        return {**row, "title": row.get("title") or metadata.get("title", ""),
                "subject_match_score": round(score, 3), "reference_query": subject}

    reused = choose(library.find_reusable(subject, category="picture", limit=20))
    if reused:
        return reused

    query = f"{subject} {descriptor}"
    try:
        downloaded = library.download_public_archives(query, category="picture", limit=5)
    except Exception as exc:
        _progress(f"Thumbnail reference search failed ({type(exc).__name__}).")
        return None
    selected = choose(downloaded)
    if selected:
        selected["reference_query"] = query
    return selected


def _thumbnail_font(size: int):
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_thumbnail_from_reference(reference: dict, concept: dict, path: Path,
                                     visual_brand: dict | None = None) -> None:
    """Compose a 1280x720 thumbnail around the downloaded real subject image.

    This intentionally uses Pillow instead of a paid image-generation endpoint:
    the subject's source pixels remain intact and recognizable.
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    source_path = Path(reference["path"])
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = 1280, 720
    background = ImageOps.fit(
        source.convert("RGB"), (width, height),
        method=Image.Resampling.LANCZOS, centering=(0.5, 0.4),
    ).filter(ImageFilter.GaussianBlur(28))
    background = ImageEnhance.Brightness(background).enhance(0.28).convert("RGBA")
    overlay = Image.new("RGBA", (width, height), (5, 8, 14, 88))
    canvas = Image.alpha_composite(background, overlay)

    brand = visual_brand or {}
    accent = str(brand.get("brand_accent", "#FFD34E"))
    subject_type = str(concept.get("subject_type", "person")).lower()
    title = str(reference.get("title", "")).lower()
    is_logo = subject_type in {"brand", "company"} and "logo" in title
    left, top, box_w, box_h = 38, 38, 735, 644
    if is_logo:
        card = Image.new("RGBA", (box_w, box_h), (245, 245, 240, 255))
        logo = ImageOps.contain(source, (box_w - 70, box_h - 70), method=Image.Resampling.LANCZOS)
        card.alpha_composite(logo, ((box_w - logo.width) // 2, (box_h - logo.height) // 2))
    else:
        photo = ImageOps.fit(
            source.convert("RGB"), (box_w, box_h),
            method=Image.Resampling.LANCZOS, centering=(0.5, 0.36 if subject_type == "person" else 0.5),
        )
        card = photo.convert("RGBA")

    # A small dark shadow and a bright edge separate the authentic source photo
    # from the atmospheric background without changing the person's appearance.
    shadow = Image.new("RGBA", (box_w + 24, box_h + 24), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rectangle((12, 12, box_w + 11, box_h + 11), fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    canvas.alpha_composite(shadow, (left - 8, top - 4))
    canvas.alpha_composite(card, (left, top))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((left, top, left + box_w - 1, top + box_h - 1), outline=accent, width=5)

    # The right-side copy is the one story clue; it is real typography rendered
    # locally, so it stays crisp and does not depend on image-model text.
    draw.rectangle((812, 92, 1210, 101), fill=accent)
    phrase = " ".join(str(concept.get("text", "")).split()[:3]).strip()
    if phrase:
        lines: list[str] = []
        current = ""
        test_draw = ImageDraw.Draw(canvas)
        font_size = 100
        while font_size >= 50:
            font = _thumbnail_font(font_size)
            lines = []
            current = ""
            for word in phrase.split():
                candidate = (current + " " + word).strip()
                if current and test_draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] > 400:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            if len(lines) <= 3 and max(test_draw.textbbox((0, 0), line, font=font, stroke_width=2)[2] for line in lines) <= 400:
                break
            font_size -= 6
        y = 235 if len(lines) <= 2 else 180
        for line in lines:
            draw.text((812, y), line, font=font, fill="#FFFFFF",
                      stroke_width=5, stroke_fill="#07090D")
            y += font_size + 12
        draw.rectangle((812, min(y + 16, 640), 1000, min(y + 22, 646)), fill=accent)

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(path, format="PNG")


def _cached_job_stage(out: Path, stage: str, inputs: Any, filename: str, producer) -> Any:
    """Load a completed stage when its exact inputs and model match."""
    stage_sig = stage_signature({
        "cache_version": 2, "stage": stage, "model": resolve_model(stage),
        "fallback_model": os.getenv(fallback_model_env(stage), ""),
        "inputs": inputs,
    })
    cached = load_checkpoint(out, stage, stage_sig, filename)
    if cached is not None:
        _progress(f"Resume: reusing cached {stage} result.")
        return cached
    result = producer()
    cacheable = True
    if stage == "research":
        cacheable = bool(result.get("claims"))
    elif stage == "verification":
        cacheable = result.get("verification_summary", {}).get("status") == "passed"
    elif stage == "scripting":
        cacheable = not result.get("unsupported_claim_ids") and bool(result.get("target_met_estimate"))
    if cacheable:
        save_checkpoint(out, stage, stage_sig, filename, result)
    else:
        _progress(f"Checkpoint skipped: {stage} output did not pass its quality checks.")
    return result


def prepare_job(channel: dict, topic: str, base_dir: Path, *, generate_thumbnails: bool = False,
                niche_config: dict | None = None) -> Path:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    job_id = f"{_slug(channel.get('handle', 'channel'))}_{_slug(topic)}"
    out = base_dir / "jobs" / job_id
    out.mkdir(parents=True, exist_ok=True)
    job_record_path = out / "job.json"
    initial_record = _json(job_record_path, {})
    started_at = initial_record.get("started_at") or datetime.now(timezone.utc).isoformat()
    _write(job_record_path, {**initial_record, "job_id": job_id, "channel": channel,
                             "channel_id": channel.get("channel_id"), "topic": topic,
                             "status": "in_progress", "started_at": started_at})
    os.environ["STORYPILOT_ACTIVE_JOB_DIR"] = str(out.resolve())

    _progress("Research 1/5: searching for sourced claims; this may take a few minutes.")
    research_inputs = {"topic": topic, "niche": channel.get("niche"), "research_prompt": niche_config.get("research_prompt")}
    research = _cached_job_stage(
        out, "research", research_inputs, "research_raw.json",
        lambda: research_story(topic, channel, niche_config),
    )

    _progress(f"Research 2/5: independently checking {len(research.get('claims', []))} claims.")
    verification_inputs = {"topic": topic, "research": research, "research_prompt": niche_config.get("research_prompt")}
    research = _cached_job_stage(
        out, "verification", verification_inputs, "research.json",
        lambda: verify_research_claims(topic, research, niche_config),
    )
    verified_count = len(research.get("verified_claims", []))
    _progress(f"Research verification complete: {verified_count} claims passed or were qualified.")

    _progress("Script 3/5: writing the documentary and complete short-form story.")
    script_inputs = {"topic": topic, "channel": channel, "verified_research": research,
                     "system_prompt": niche_config.get("system_prompt"),
                     "retention_guidance": niche_config.get("retention_guidance"),
                     "narration_direction": niche_config.get("narration_direction")}
    script = _cached_job_stage(
        out, "scripting", script_inputs, "script.json",
        lambda: write_script(topic, channel, research, niche_config),
    )
    _progress(f"Script complete: {script.get('approx_word_count', 0)} estimated narration words.")

    _progress("Assets 4/5: preparing scene-specific visual, music, and sound-effect requests.")
    asset_manifest = asset_research(script, out)
    if _RUN_PROGRESS is not None:
        visual_count = sum(asset.get("category") in {"picture", "video"} for asset in asset_manifest.get("assets", []))
        thumbnail_count = 1 if generate_thumbnails else 0
        _RUN_PROGRESS.set_estimate(480 + visual_count * 45 + thumbnail_count * 120)
    screenshot_requests = _article_screenshot_requests(script)
    if screenshot_requests:
        _progress(f"News visuals: capturing {len(screenshot_requests)} requested article headline(s).")
    screenshots = fulfill_news_headline_requests(
        topic, script, base_dir / "assets", job_id=job_id, research=research
    )
    screenshot_by_id = {row.get("asset_id"): row for row in screenshots.get("screenshots", [])}
    for asset in asset_manifest["assets"]:
        if asset.get("category") != "news_headline_screenshot":
            continue
        result = screenshot_by_id.get(asset["asset_id"], {})
        asset.update({key: result[key] for key in ("url", "publisher", "headline", "image_path", "status", "error", "highlighted_keywords") if key in result})
        if result.get("status") == "captured":
            asset["local_path"] = result["image_path"]
    _write(out / "asset_requests.json", asset_manifest)

    _progress("Packaging 5/5: generating titles, description, tags, and thumbnail concepts.")
    metadata_inputs = {"topic": topic, "channel": channel, "script": script}
    metadata = _cached_job_stage(
        out, "metadata", metadata_inputs, "metadata_ab_test.json",
        lambda: make_metadata(topic, channel, script),
    )
    job_record = {"job_id": job_id, "channel": channel, "channel_id": channel.get("channel_id"),
                  "visual_brand": channel.get("visual_brand", {}), "topic": topic, "status": "preproduction_ready",
                  "started_at": started_at, "preproduction_completed_at": datetime.now(timezone.utc).isoformat(),
                  "research_file": "research.json", "script_file": "script.json", "asset_manifest": "asset_requests.json",
                  "metadata_file": "metadata_ab_test.json", "human_polish_required": False,
                  "thumbnail_requested": bool(generate_thumbnails)}
    if generate_thumbnails:
        concepts = metadata.get("thumbnail_briefs", [])[:1]
        reference_record = {"status": "missing", "message": "No thumbnail subject brief was returned."}
        if concepts:
            concept = concepts[0]
            subject = str(concept.get("subject", "")).strip()
            subject_type = str(concept.get("subject_type", "person")).strip().lower()
            _progress(f"Thumbnail reference: searching keyless public archives for {subject or 'the named subject'}.")
            reference = resolve_thumbnail_reference(subject, subject_type, base_dir / "assets")
            if reference:
                try:
                    create_thumbnail_from_reference(reference, concept, out / "thumbnail_1.png", channel.get("visual_brand", {}))
                    reference_record = {
                        "status": "ready", "subject": subject, "subject_type": subject_type,
                        "recognition_cues": concept.get("recognition_cues", ""),
                        "image_path": reference.get("path"), "asset_id": reference.get("asset_id"),
                        "source": reference.get("source"), "source_url": reference.get("source_url"),
                        "license": reference.get("license"), "creator": reference.get("creator"),
                        "match_score": reference.get("subject_match_score"),
                        "composition_method": "local Pillow composition using the downloaded source image directly",
                    }
                    job_record["thumbnail_status"] = "ready"
                    _progress("Thumbnail 1/1: composed locally from the real subject image.")
                except Exception as exc:
                    reference_record = {"status": "composition_failed", "subject": subject,
                                        "subject_type": subject_type, "message": f"{type(exc).__name__}: {exc}"}
                    job_record["thumbnail_status"] = "failed"
                    _progress(f"Thumbnail composition failed ({type(exc).__name__}).")
            else:
                reference_record = {"status": "subject_image_not_found", "subject": subject,
                                    "subject_type": subject_type,
                                    "message": "No sufficiently relevant real image was found in the public image sources; no synthetic substitute was created."}
                job_record["thumbnail_status"] = "missing_reference"
                _progress(f"No reliable public image found for {subject or 'the requested subject'}; thumbnail was not fabricated.")
        _write(out / "thumbnail_reference.json", reference_record)
    _write(out / "job.json", job_record)
    return out

def qa_package(job_dir: Path, final_files: list[Path] | None = None) -> list[str]:
    errors = []
    required = ["research.json", "script.json", "asset_requests.json", "metadata_ab_test.json"]
    for name in required:
        if not (job_dir / name).exists():
            errors.append(f"Missing {name}")
    job_record = _json(job_dir / "job.json", {})
    if job_record.get("thumbnail_requested"):
        thumbnail_path = job_dir / "thumbnail_1.png"
        reference = _json(job_dir / "thumbnail_reference.json", {})
        if not thumbnail_path.is_file():
            errors.append("Requested thumbnail is missing")
        if reference.get("status") != "ready":
            errors.append(f"Thumbnail reference is not ready: {reference.get('status', 'missing')}")
        reference_path = Path(reference.get("image_path", "")) if reference.get("image_path") else None
        if not reference_path or not reference_path.is_file():
            errors.append("Thumbnail reference image file is missing")
        if reference.get("source") not in {"wikimedia_commons", "internet_archive", "library_of_congress", "nasa_images", "pixabay"}:
            errors.append("Thumbnail does not use a real downloaded source image")
    script = _json(job_dir / "script.json", {})
    if script.get("unsupported_claim_ids"):
        errors.append("Script references claim IDs absent from research ledger")
    verified_ids = {claim.get("claim_id") for claim in _json(job_dir / "research.json", {}).get("verified_claims", [])}
    if _json(job_dir / "research.json", {}).get("verification_summary", {}).get("status") != "passed":
        errors.append("Research claims did not pass the independent verification stage")
    script_ids = {claim_id for chunk in script.get("narration_chunks", []) for claim_id in chunk.get("claim_ids", [])}
    script_ids.update(script.get("shorts_script", {}).get("claim_ids", []))
    if script_ids - verified_ids:
        errors.append("Script includes claims that are not marked verified or qualified")
    if not script.get("target_met_estimate"):
        errors.append("Long-form script estimate is under 2,500 words; confirm 20-minute narration before editing")
    for claim in _json(job_dir / "research.json", {}).get("claims", []):
        if not claim.get("url", "").startswith("http"):
            errors.append(f"Claim {claim.get('claim_id')} has no source URL")
    for asset in _json(job_dir / "asset_requests.json", {}).get("assets", []):
        if asset.get("local_path") and asset.get("rights_status") not in {"owned", "licensed", "public_domain", "permission"}:
            errors.append(f"Asset {asset.get('asset_id')} has no documented reuse status")
    selected_path = job_dir / "selected_assets.json"
    if selected_path.exists():
        selected_pack = _json(selected_path, {})
        video_suffixes = {".mp4", ".webm", ".ogv", ".mov", ".mkv", ".mpeg", ".mpg"}
        for row in selected_pack.get("assets", []):
            if row.get("category") != "video":
                continue
            chosen = row.get("selected") or {}
            local = Path(chosen.get("path", "")) if chosen.get("path") else None
            if not local or not local.is_file():
                errors.append(f"Video request {row.get('asset_id')} has no downloaded local clip")
            elif local.suffix.lower() not in video_suffixes:
                errors.append(f"Video request {row.get('asset_id')} resolved to a non-video file: {local.name}")
            details = chosen.get("metadata") or {}
            if chosen.get("source") in {"youtube", "tiktok"}:
                try:
                    start = float(details["segment_start_seconds"])
                    end = float(details["segment_end_seconds"])
                    maximum = float(row.get("max_duration_seconds", 5))
                    if start < 0 or end <= start or end - start > maximum + 0.01:
                        errors.append(f"Social video request {row.get('asset_id')} has an invalid or over-limit selected time range")
                except (KeyError, TypeError, ValueError):
                    errors.append(f"Social video request {row.get('asset_id')} has no validated AI-selected time range")
        for row in selected_pack.get("unresolved", []):
            if row.get("category") == "video":
                errors.append(f"Required video clip unresolved for {row.get('asset_id')}: {row.get('query', '')}")
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
    global _RUN_PROGRESS
    parser = argparse.ArgumentParser(description="Research, script, asset brief, metadata and thumbnail preproduction")
    parser.add_argument("--channels", type=Path, required=True, help="JSON array of channel configs")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--channel", help="Channel handle to process")
    parser.add_argument("--topic", help="Skip discovery and prepare this topic")
    parser.add_argument("--niche-prompts", type=Path, default=Path(__file__).with_name("niche_prompts.json"), help="Niche-specific research, script and narrator instructions")
    parser.add_argument("--make-thumbnails", action="store_true", help="Call the configured OpenAI image model")
    parser.add_argument("--discover-assets", action="store_true", help="Search YouTube, TikTok web results, Pixabay, and public archives for scene-specific media candidates")
    parser.add_argument("--acquire-assets", action="store_true", help="Download/reuse real video clips; AI selects and FFmpeg cuts a relevant excerpt up to five seconds")
    parser.add_argument("--youtube-audio-library-dir", type=Path, help="Folder containing downloaded YouTube Audio Library files; expected subfolders background_music/ and sfx/")
    args = parser.parse_args()
    try:
        require_no_unfinished_jobs(args.base_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    _RUN_PROGRESS = _ConsoleProgress()
    _progress("Setup: preparing workspace and loading API credentials.")
    workspace = ensure_workspace(args.base_dir)
    required_keys = ("OPENAI_API_KEY",) if args.topic else ("OPENAI_API_KEY", "YOUTUBE_API_KEY")
    api_status = configure_api_keys(args.base_dir, required_keys=required_keys)
    if api_status["missing_optional"]:
        print("Optional providers not configured (their features will be skipped): " + ", ".join(api_status["missing_optional"]))
    print(f"Production folders ready: {workspace['root']}", flush=True)
    channels = _json(args.channels, [])
    channel = next((c for c in channels if not args.channel or c.get("handle") == args.channel), None)
    if channel is None:
        raise SystemExit("No matching channel in channels JSON")
    _progress(f"Channel selected: {channel.get('name', channel.get('handle'))} ({channel.get('niche', 'niche not set')}).")
    history_path = args.base_dir / "history.json"
    history = _json(history_path, [])
    topic = args.topic
    if not topic:
        _progress("Discovery 1/2: searching YouTube for videos from the last 90 days; waiting for YouTube API.")
        trending = discover_youtube_videos(channel.get("niche", ""))
        _progress(f"Discovery 1/2 complete: ranked {len(trending)} recent videos. Asking OpenAI for five topic ideas.")
        _write(args.base_dir / "discovery" / f"{_slug(channel.get('handle','channel'))}.json", trending)
        ideas = propose_ideas(channel, trending, history)
        _write(args.base_dir / "discovery" / "ideas.json", {"channel": channel, "ideas": ideas})
        if not ideas:
            raise SystemExit("No non-duplicate topic ideas generated")
        topic = ideas[0]["topic"]
        _progress(f"Discovery 2/2 complete: selected topic — {topic}")
    else:
        _progress(f"Using supplied topic: {topic}")
    niche_config = get_niche_config(channel.get("niche", ""), args.niche_prompts)
    job_dir = prepare_job(channel, topic, args.base_dir, generate_thumbnails=args.make_thumbnails,
                          niche_config=niche_config)
    if args.discover_assets:
        _progress("Asset discovery: searching YouTube, TikTok web results, and public image/video archives.")
        search_asset_sources(job_dir / "asset_requests.json")
    if args.acquire_assets:
        from media_library import MediaLibrary, acquire_asset_requests
        shared_assets = MediaLibrary(args.base_dir / "assets")
        if args.youtube_audio_library_dir:
            for category, aliases in (("music", ("music", "background_music")), ("sfx", ("sfx",))):
                for folder_name in aliases:
                    source_folder = args.youtube_audio_library_dir / folder_name
                    if source_folder.is_dir():
                        imported = shared_assets.import_audio_library_folder(source_folder, category)
                        _progress(f"Audio library: indexed {len(imported)} {category} files from {source_folder.name}.")
                        break
        requests_manifest = _json(job_dir / "asset_requests.json", {"assets": []})
        _progress(f"Asset acquisition: resolving {len(requests_manifest.get('assets', []))} scene requests; one update will appear per asset.")
        def asset_progress(index: int, total: int, message: str) -> None:
            if _RUN_PROGRESS is not None:
                pct = 83 + 15 * index / max(1, total)
                _RUN_PROGRESS.update(f"Asset acquisition {index}/{total}", pct)
                _RUN_PROGRESS.update_subtask(message)
        pack = acquire_asset_requests(requests_manifest, shared_assets, progress_callback=asset_progress)
        _write(job_dir / "selected_assets.json", pack)
        _progress(f"Asset acquisition complete: {len(pack.get('assets', []))} resolved; {len(pack.get('unresolved', []))} unresolved.")
    _progress("Preproduction complete. Job package is ready for narration.")
    print(f"Preproduction package ready: {job_dir}")
    print("Next: make narration, render the timeline, and run final QA. Check selected_assets.json for any requested assets that could not be matched.")
    if _RUN_PROGRESS is not None:
        _RUN_PROGRESS.finish()


if __name__ == "__main__":
    main()
