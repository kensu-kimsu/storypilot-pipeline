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


class _ConsoleProgress:
    """Live console bar with a rough, dynamically scaled time estimate."""
    def __init__(self, estimated_total_seconds: float = 900.0):
        self.started = time.monotonic()
        self.estimated_total = estimated_total_seconds
        self.percent = 0.0
        self.label = "Starting preproduction"
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
        self.render()

    def render(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started
            percent = self.percent
            expected = self.estimated_total
            label = self.label
        nominal_done = expected * percent / 100.0
        scale = min(2.5, max(0.5, elapsed / nominal_done)) if nominal_done >= 20 else 1.0
        remaining = expected * (1.0 - percent / 100.0) * scale
        filled = int(20 * percent / 100.0)
        bar = "=" * filled + " " * (20 - filled)
        status = (f"\r[StoryPilot] [{bar}] {percent:5.1f}% | elapsed {self._duration(elapsed)} "
                  f"| ETA ~{self._duration(remaining)} | {label}")
        sys.stdout.write(status[:120].ljust(120))
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
    "washingtonpost.com", "wsj.com", "wired.com", "techcrunch.com", "news.com.au",
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
            "asset_id": f"news_headline_{len(requests_list)+1:02d}",
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
    """Use OpenAI web search for a headline only when the creative brief asks for one."""
    prompt = f"""Find one real article from an established news publisher for a documentary visual.
Topic: {topic}
Article search query: {request['search_query']}
The article should visibly show at least one of these terms (or a clear inflection) in its headline: {json.dumps(request['highlight_keywords'])}.
Search the web. Choose a directly accessible article from one of these publishers only: {', '.join(sorted(NEWS_PUBLISHER_DOMAINS))}.
Return strict JSON: {{"article":null}} if no verifiable match exists; otherwise {{"article":{{"headline":"exact published headline","url":"direct article URL","publisher":"publisher","published_date":"date or unknown"}}}}.
Do not invent headlines, URLs, dates, or citations. Do not return a search results page or a homepage."""
    response = openai_response(prompt, web_search=True, stage="news_search")
    data = parse_json_text(response_text(response))
    article = data.get("article")
    if not isinstance(article, dict):
        return None
    if not article.get("headline") or not _is_approved_news_url(str(article.get("url", ""))):
        return None
    return {
        "headline": str(article["headline"]).strip(),
        "url": str(article["url"]).strip(),
        "publisher": str(article.get("publisher") or urlparse(article["url"]).hostname),
        "published_date": article.get("published_date", "unknown"),
    }


def _capture_headline(candidate: dict, keywords: list[str], image_path: Path) -> dict:
    """Capture only the page's visible H1, after highlighting requested keyword matches."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
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
            result = headline.evaluate(
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
                }""",
                keywords,
            )
            if not result:
                raise ValueError("None of the requested topic keywords appear in the article headline")
            headline.screenshot(path=str(image_path), animations="disabled", timeout=45000)
        finally:
            browser.close()
    return {"headline": actual_headline, "highlighted_keywords": result, "http_status": response.status if response else None}


def fulfill_news_headline_requests(topic: str, script: dict, assets_root: Path, *, job_id: str) -> dict:
    """Search and capture no articles unless the script's creative direction requests them."""
    requests_list = _article_screenshot_requests(script)
    if not requests_list:
        return {"screenshots": [], "searched": False}

    output_dir = assets_root / "screenshots" / _slug(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for request in requests_list:
        record = {**request, "searched": True, "captured_at": datetime.now(timezone.utc).isoformat()}
        try:
            article = find_news_article(topic, request)
            if not article:
                record.update(status="not_found", error="No matching article found from the approved news publishers")
                records.append(record)
                continue
            filename = f"{_slug(topic)[:35]}_{_slug(article['publisher'])[:24]}_{_slug(article['headline'])[:50]}.png"
            image_path = output_dir / filename
            captured = _capture_headline(article, request["highlight_keywords"], image_path)
            record.update(article, **captured, image_path=str(image_path), status="captured")
        except Exception as exc:
            record.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        records.append(record)
    manifest = {
        "topic": topic,
        "searched": True,
        "screenshot_count": sum(row.get("status") == "captured" for row in records),
        "screenshots": records,
    }
    _write(output_dir / "article_screenshots.json", manifest)
    return manifest


def openai_response(prompt: str, *, web_search: bool = False, model: str | None = None,
                    stage: str = "general") -> dict:
    """Call Responses API with stage routing, usage logging, and optional JSON fallback."""
    _require_requests()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")
    selected_model = resolve_model(stage, model)
    payload: dict[str, Any] = {"model": selected_model, "input": prompt}
    if web_search:
        payload["tools"] = [{"type": "web_search"}]
    headers = {"Authorization": f"Bearer {key}"}
    response = HTTP.post(f"{API_ROOT}/responses", headers=headers, json=payload, timeout=240)
    response.raise_for_status()
    data = response.json()

    # Optional fallback runs only when a configured low-risk JSON stage returns
    # malformed JSON. It never silently downgrades research or fact-checking.
    fallback_model = os.getenv(f"OPENAI_MODEL_{stage.upper()}_FALLBACK", "").strip()
    if fallback_model and stage in {"ideation", "research", "verification", "scripting", "metadata", "news_search", "social_ranking"}:
        try:
            parse_json_text(response_text(data))
        except (json.JSONDecodeError, TypeError, ValueError):
            # Both requests are billable when malformed JSON triggers a retry.
            record_openai_usage(data, stage=stage, model=selected_model, web_search=web_search)
            fallback_response = HTTP.post(
                f"{API_ROOT}/responses", headers=headers,
                json={**payload, "model": fallback_model}, timeout=240,
            )
            fallback_response.raise_for_status()
            data = fallback_response.json()
            selected_model = fallback_model
    record_openai_usage(data, stage=stage, model=selected_model, web_search=web_search)
    return data


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
    payload = parse_json_text(response_text(openai_response(prompt, stage="ideation")))
    ideas = payload.get("ideas", [])
    return [idea for idea in ideas if idea.get("topic") and not is_duplicate(idea["topic"], history)][:count]


def research_story(topic: str, channel: dict, niche_config: dict | None = None) -> dict:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    prompt = f"""Research a factual documentary story for a {channel.get('niche')} YouTube channel.
Topic: {topic}
Use web search. Build a source-backed evidence ledger for a 20-minute documentary. Aim for at least eight credible, distinct sources when the topic supports it, including primary records where available. Each factual claim must have a direct source URL and a short note about what that source establishes. Separate facts, estimates, allegations, interpretations, and unresolved questions. Never fill gaps by guessing. For claims that identify real places relevant to a route or event, optionally include geo_points with exact decimal latitude/longitude and a source URL that explicitly supports each coordinate; omit coordinates you cannot source.
Niche research guidance: {niche_config.get('research_prompt', '')}
Return strict JSON: {{"research_brief":"...","claims":[{{"claim_id":"C1","claim":"one checkable factual claim","source_title":"...","url":"https://...","publisher":"...","published_or_accessed":"...","source_type":"primary|official|academic|reporting|archive|other","support":"short paraphrase of the relevant evidence","confidence":"high|medium|low","caveat":"qualification or empty string","geo_points":[{{"name":"place name in the claim","latitude":0.0,"longitude":0.0,"source_url":"direct URL supporting these coordinates"}}]}}],"open_questions":["..."]}}.
The brief may contain only claims represented in the ledger. Prioritise original records over summaries and flag conflicting evidence explicitly."""
    response = openai_response(prompt, web_search=True, stage="research")
    data = parse_json_text(response_text(response))
    data["search_response_id"] = response.get("id")
    data["verified_claims"] = []
    return data


def verify_research_claims(topic: str, research: dict, niche_config: dict | None = None) -> dict:
    """Independently check the proposed claim ledger before any script is written."""
    niche_config = niche_config or {}
    claims = research.get("claims", [])
    if not claims:
        research["verified_claims"] = []
        research["verification_summary"] = {"status": "failed", "reason": "No research claims were returned"}
        return research
    prompt = f"""Fact-check these proposed documentary claims before script writing.
Topic: {topic}
Niche guidance: {niche_config.get('research_prompt', '')}
Use web search to inspect the cited source and seek independent corroboration. Check dates, names, amounts, definitions, attribution, causation, and qualifications. A claim is verified only when the cited material supports it. Mark qualified if accurate only with an explicit caveat. Mark unverified when evidence is missing or weak; disputed when reliable sources conflict. Do not infer certainty from a source's reputation alone. For every proposed geo_point, separately check the exact coordinates and return checked_geo_points only when a source directly supports the place and coordinates. Return the exact decimal values and checked source URL; omit unsupported points.
Claims: {json.dumps(claims, ensure_ascii=False)}
Return strict JSON: {{"results":[{{"claim_id":"C1","status":"verified|qualified|unverified|disputed","verification_note":"what the sources support or fail to support","checked_sources":[{{"title":"...","url":"...","support":"short evidence summary"}}],"checked_geo_points":[{{"name":"...","latitude":0.0,"longitude":0.0,"source_url":"https://..."}}]}}]}}.
Provide at least one checked source for each verified or qualified claim. Do not invent source URLs."""
    response = openai_response(prompt, web_search=True, stage="verification")
    checked = parse_json_text(response_text(response)).get("results", [])
    by_id = {str(item.get("claim_id")): item for item in checked if item.get("claim_id")}
    verified = []
    counts = {"verified": 0, "qualified": 0, "unverified": 0, "disputed": 0}
    for claim in claims:
        result = by_id.get(str(claim.get("claim_id")), {})
        status = str(result.get("status", "unverified")).lower()
        sources = result.get("checked_sources", [])
        valid_sources = [
            source for source in sources
            if isinstance(source, dict) and str(source.get("url", "")).startswith(("http://", "https://"))
        ] if isinstance(sources, list) else []
        if status in {"verified", "qualified"} and not valid_sources:
            status = "unverified"
        if status not in counts:
            status = "unverified"
        claim["verification_status"] = status
        claim["verification_note"] = str(result.get("verification_note", "No verification result returned"))
        claim["checked_sources"] = valid_sources
        proposed_points = claim.get("geo_points", []) if isinstance(claim.get("geo_points", []), list) else []
        checked_points = result.get("checked_geo_points", []) if isinstance(result.get("checked_geo_points", []), list) else []
        accepted_points = []
        for proposed in proposed_points:
            if not isinstance(proposed, dict):
                continue
            for checked_point in checked_points:
                if not isinstance(checked_point, dict):
                    continue
                try:
                    exact_match = (
                        str(proposed.get("name", "")).casefold() == str(checked_point.get("name", "")).casefold()
                        and float(proposed.get("latitude")) == float(checked_point.get("latitude"))
                        and float(proposed.get("longitude")) == float(checked_point.get("longitude"))
                        and str(proposed.get("source_url", "")) == str(checked_point.get("source_url", ""))
                        and str(proposed.get("source_url", "")).startswith(("http://", "https://"))
                        and str(proposed.get("source_url", "")) in {str(row.get("url", "")) for row in valid_sources}
                        and -90 <= float(proposed.get("latitude")) <= 90
                        and -180 <= float(proposed.get("longitude")) <= 180
                    )
                except (TypeError, ValueError):
                    exact_match = False
                if exact_match:
                    accepted_points.append({"name": str(proposed["name"]).strip(), "latitude": float(proposed["latitude"]), "longitude": float(proposed["longitude"]), "source_url": str(proposed["source_url"])})
                    break
        claim["verified_geo_points"] = accepted_points
        counts[status] += 1
        if status in {"verified", "qualified"} and str(claim.get("url", "")).startswith(("http://", "https://")):
            verified.append(claim)
    research["verified_claims"] = verified
    research["verification_response_id"] = response.get("id")
    research["verification_summary"] = {
        "status": "passed" if verified else "failed",
        "claim_counts": counts,
        "verified_claim_ids": [claim.get("claim_id") for claim in verified],
    }
    return research


def validate_graphic_spec(spec: Any, chunk: dict, verified_claims: list[dict]) -> tuple[dict | None, str | None]:
    """Accept only compact graphic specs supported by claims assigned to a scene."""
    if spec in (None, {}):
        return None, None
    if not isinstance(spec, dict) or spec.get("kind") not in {"chart", "counter", "route_schematic", "geographic_map", "document_highlight"}:
        return None, "Unsupported graphic kind"
    expected_mode = {"chart": "animated_chart", "counter": "kinetic_fact", "route_schematic": "animated_map", "geographic_map": "animated_map", "document_highlight": "document_focus"}[spec["kind"]]
    if chunk.get("visual_mode") != expected_mode:
        return None, f"{spec['kind']} graphics require visual_mode={expected_mode}"
    allowed_ids = {str(value) for value in chunk.get("claim_ids", [])}
    claim_rows = {str(row.get("claim_id")): row for row in verified_claims}
    claims = {claim_id: str(row.get("claim", "")) for claim_id, row in claim_rows.items()}
    if not allowed_ids:
        return None, "Graphic spec has no claim IDs assigned to this scene"
    graphic = dict(spec)
    graphic["title"] = str(graphic.get("title", ""))[:90]

    def numeric_value(text: str, claim_id: str) -> float | None:
        source = claims.get(claim_id, "")
        if claim_id not in allowed_ids or not source:
            return None
        normalized_source = re.sub(r"\s+", " ", source.casefold())
        normalized_text = re.sub(r"\s+", " ", text.casefold()).strip()
        number_match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", normalized_text)
        if not number_match or normalized_text not in normalized_source:
            return None
        try:
            value = float(number_match.group(0).replace(",", ""))
            suffix = normalized_text[number_match.end():]
            if re.search(r"\b(trillion|trn|tn)\b", suffix):
                value *= 1_000_000_000_000
            elif re.search(r"\b(billion|bn)\b", suffix):
                value *= 1_000_000_000
            elif re.search(r"\b(million|mn)\b", suffix):
                value *= 1_000_000
            elif re.search(r"\b(thousand|k)\b", suffix):
                value *= 1_000
            return value
        except ValueError:
            return None

    if graphic["kind"] == "chart":
        rows = graphic.get("items", [])
        if not isinstance(rows, list) or not 2 <= len(rows) <= 6:
            return None, "Charts require two to six evidence-backed data items"
        normalized = []
        unit_signatures = set()
        for row in rows:
            if not isinstance(row, dict):
                return None, "Chart item is not an object"
            label = str(row.get("label", "")).strip()[:28]
            value = str(row.get("value", "")).strip()[:32]
            claim_id = str(row.get("claim_id", ""))
            number = numeric_value(value, claim_id)
            source_claim = claims.get(claim_id, "")
            if not label or label.casefold() not in source_claim.casefold() or number is None or number < 0:
                return None, f"Chart label/value {label!r} / {value!r} is not stated in its assigned verified claim"
            unit = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value)
            suffix = value[unit.end():] if unit else value
            suffix = re.sub(r"\b(trillion|trn|tn|billion|bn|million|mn|thousand|k)\b", "", suffix.casefold())
            unit_signatures.add(re.sub(r"[^a-z%$]", "", suffix))
            normalized.append({"label": label, "value": value, "numeric_value": number, "claim_id": claim_id})
        if len(unit_signatures) > 1:
            return None, "Chart values use different units; only comparable measures can be graphed together"
        graphic["items"] = normalized
    elif graphic["kind"] == "counter":
        value = str(graphic.get("value", "")).strip()[:32]
        claim_id = str(graphic.get("claim_id", ""))
        number = numeric_value(value, claim_id)
        if number is None:
            return None, f"Counter value {value!r} is not stated in its assigned verified claim"
        graphic["numeric_value"] = number
    elif graphic["kind"] == "route_schematic":
        places = graphic.get("places", [])
        if not isinstance(places, list) or not 2 <= len(places) <= 6:
            return None, "Route schematics require two to six sourced place labels"
        normalized_places = []
        for place in places:
            if not isinstance(place, dict):
                return None, "Route place is not an object"
            name = str(place.get("name", "")).strip()[:28]
            claim_id = str(place.get("claim_id", ""))
            source = claims.get(claim_id, "")
            if claim_id not in allowed_ids or not name or name.casefold() not in source.casefold():
                return None, f"Route place {name!r} is not stated in its assigned verified claim"
            normalized_places.append({"name": name, "claim_id": claim_id})
        route_claim_ids = {place["claim_id"] for place in normalized_places}
        if len(route_claim_ids) != 1:
            return None, "Route order must be supported by one verified claim"
        route_source = claims[next(iter(route_claim_ids))].casefold()
        route_positions = [route_source.find(place["name"].casefold()) for place in normalized_places]
        if route_positions != sorted(route_positions):
            return None, "Route order does not match the order stated in its verified claim"
        graphic["places"] = normalized_places
        graphic["schematic_only"] = True
    elif graphic["kind"] == "geographic_map":
        points = graphic.get("points", [])
        if not isinstance(points, list) or not 2 <= len(points) <= 8:
            return None, "Geographic maps require two to eight independently checked locations"
        normalized_points = []
        route_claim_ids = set()
        for point in points:
            if not isinstance(point, dict):
                return None, "Map point is not an object"
            name = str(point.get("name", "")).strip()[:36]
            claim_id = str(point.get("claim_id", ""))
            claim = claim_rows.get(claim_id, {})
            if claim_id not in allowed_ids or not name or name.casefold() not in claims.get(claim_id, "").casefold():
                return None, "Map point is missing its assigned verified claim or place name"
            match = next((row for row in claim.get("verified_geo_points", []) if str(row.get("name", "")).casefold() == name.casefold()), None)
            try:
                lat, lon = float(point.get("latitude")), float(point.get("longitude"))
            except (TypeError, ValueError):
                return None, f"Map coordinates for {name!r} are invalid"
            if not match or lat != float(match["latitude"]) or lon != float(match["longitude"]):
                return None, f"Map coordinates for {name!r} were not independently verified"
            normalized_points.append({"name": name, "latitude": lat, "longitude": lon, "claim_id": claim_id, "source_url": match["source_url"]})
            route_claim_ids.add(claim_id)
        if len(route_claim_ids) != 1:
            return None, "Geographic route points must come from one verified claim"
        route_source = claims[next(iter(route_claim_ids))].casefold()
        route_positions = [route_source.find(point["name"].casefold()) for point in normalized_points]
        if route_positions != sorted(route_positions):
            return None, "Geographic route order does not match the order stated in its verified claim"
        if any(abs(right["longitude"] - left["longitude"]) > 180 for left, right in zip(normalized_points, normalized_points[1:])):
            return None, "Routes crossing the international date line are not supported by this map template"
        graphic["points"] = normalized_points
        graphic["schematic_only"] = True
    else:
        claim_id = str(graphic.get("claim_id", ""))
        phrase = re.sub(r"\s+", " ", str(graphic.get("target_text", "")).strip())
        source = claims.get(claim_id, "")
        if claim_id not in allowed_ids or len(phrase) < 3 or phrase.casefold() not in source.casefold():
            return None, "Document highlight phrase must be present in an assigned verified claim"
        graphic.update({"claim_id": claim_id, "target_text": phrase[:180]})
    return graphic, None


def write_script(topic: str, channel: dict, research: dict, niche_config: dict | None = None) -> dict:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    claims = research.get("verified_claims", [])
    if not claims:
        raise RuntimeError("Script writing stopped: no research claims passed the verification step")
    voice_name = channel.get("voice_name") or niche_config.get("voice_name", "en-US-Ethan:MAI-Voice-2.1")
    narration_direction = niche_config.get("narration_direction", "Natural, clear English.")
    prompt = f"""Write a retention-focused documentary script based ONLY on the supplied VERIFIED research claims. Do not add unsupported facts. Attach source claim IDs to every factual paragraph. Preserve qualifications and uncertainty. Cite claims inline as [C1].
Channel: {channel.get('name', channel.get('handle', ''))}; niche: {channel.get('niche')}; voice/tone: {channel.get('tone', 'cinematic, clear, curious')}.
Topic: {topic}. Target at least 20 minutes spoken narration (roughly 2,700-3,000 words at a measured documentary pace). {niche_config.get('system_prompt', '')}
Retention design: {niche_config.get('retention_guidance', '')}
Maintain attention from the first line through the ending: open on a specific verified moment or question; state a truthful story promise early; build in clear acts where each section adds evidence, changes the stakes, or corrects an earlier assumption; pay off every major question. Add meaningful re-hooks about every 2-3 minutes, but never use fake cliffhangers, repeat the same teaser, or withhold basic facts merely to inflate watch time. Remove repeated exposition and filler. The ending must resolve the central story, then offer a natural reason to watch another episode without an artificial cliffhanger.
Write for a natural human narrator using English. Keep sentences speakable and varied, use contractions where natural, and avoid stiff transitions, repeated rhetorical questions, forced slang, and overly polished essay language. Never put performance directions inside the words to be spoken. The narrator should follow the emotion and tone assigned to each scene; let intensity rise and fall with the evidence rather than reading every line dramatically.
Narration performance is a scene-by-scene arc, not just emotion labels. Keep each narration chunk to 1-3 speakable sentences (about 15-45 words) and split when the emotional beat changes so TTS can change delivery between chunks. Write times in words for spoken narration (for example, `eleven forty-seven`), never as clock digits such as `11:47`, which this local TTS may misread. Do not add all-caps emphasis or performance directions to spoken text. For SOMBER or sad beats, use restrained, intimate language, shorter clauses, reflective wording, `delivery_emotion` such as `somber` or `sad`, `tone` such as `quiet grief` or `grief-stricken reflection`, `delivery_pace` `slow`, `vocal_intensity` around 0.25-0.4, and `pause_after_seconds` around 0.5-0.9. Let the word choice carry grief; do not shout or over-dramatize. For SUSPENSE, create a real build across 3-5 consecutive chunks: start restrained (`vocal_intensity` around 0.25), add one verified clue per beat, shorten sentences as the reveal approaches, then raise intensity in controlled steps (for example 0.25, 0.4, 0.55, 0.7). Keep `delivery_pace` `slow` throughout the build, use tones such as `quiet tension` then `rising anticipation`, and set deliberate `pause_after_seconds` around 0.6-1.2 before the reveal. Do not make suspenseful lines sound like shouting; the final reveal should stay controlled. Use ellipses sparingly; rely on sentence structure and actual pauses. Chatterbox only has broad intensity and cadence controls, so the script's sentence shape and chunk boundaries must carry the intended performance. For every chunk also set `tone`, `delivery_emotion`, `delivery_pace`, `vocal_intensity` from 0.0 to 1.0, and `pause_after_seconds`. Overall narration direction: {narration_direction}
Act as the visual creative director too. For each chunk choose one `visual_mode` from `minimalist_text`, `split_screen`, `animated_chart`, `document_focus`, `animated_map`, `parallax_portrait`, `kinetic_fact`, or `cinematic_broll`. Choose it because it expresses the verified evidence in that scene, not to create random variation. Add `visual_evidence`, naming the exact verified detail or source visual that should appear, and `visual_rationale`, explaining why the visual mode suits the beat. For `asset_queries`, provide 2-3 concise, scene-specific search phrases for REAL MOVING VIDEO FOOTAGE (subjects, actions, places, era); do not put URLs, source citations, or image-only concepts in these queries. Put any source URLs in separate `reference_urls` if needed. The asset downloader treats these as video-only requests and will never replace missing footage with a still image. Still images are requested only through `parallax_assets` when that specific layered portrait treatment is essential. Use channel branding consistently: {json.dumps(channel.get('visual_brand', {}), ensure_ascii=False)}. Keep its font and brand colors stable; use topic palette and layout changes only when they serve this episode's story. Never invent a chart, map, document, or evidence image.
Add optional `graphic_spec` for evidence-backed graphics only. Charts use 2-6 items with label, exact numeric value phrase copied from a verified claim, and its claim_id. Counters use the exact numeric phrase and claim_id. Route schematics use 2-6 place names copied exactly from the same verified claim, in order, labelled schematic and not to scale. For `geographic_map`, use 2-8 exact coordinates only from that claim's independently checked geo_points; include name, exact decimal latitude/longitude, and claim_id. Never guess coordinates. For `document_highlight`, provide claim_id and a short exact phrase appearing in that verified claim; the renderer must find the phrase with OCR in the selected document image before highlighting it. Set graphic_spec to null if evidence or asset support is missing. The program rejects unsupported graphic data. For a true 2.5D `parallax_portrait`, optionally provide `parallax_assets` with distinct `portrait_query` and `background_query`; Python must use a separate background plate so the portrait is not duplicated. Set `generated_media_required` true only when a custom original background illustration materially improves the scene and public-source images cannot convey it; default false. Never use image generation to fabricate a recognizable real person, company logo, historical record, or evidentiary document.
For each narration chunk, decide whether a real news headline is creatively necessary. Normally set "news_article_screenshot": null. Request one only when an actual news headline materially strengthens the scene (for example, showing contemporaneous media coverage of a major event). Do not request generic headlines or decorative screenshots; request at most two total. When needed, set {{"search_query":"specific article/story query","highlight_keywords":["one or two exact headline terms"],"reason":"why the headline itself helps this scene"}}.
Research ledger (only these verified claims may support factual narration): {json.dumps(claims, ensure_ascii=False)}
Return strict JSON {{"title_working":"...","logline":"...","narration_direction":"...","voice_name":"{voice_name}","narration_chunks":[{{"section_title":"...","text":"...","claim_ids":["C1"],"tone":"...","delivery_emotion":"...","delivery_pace":"measured|brisk|slow","vocal_intensity":0.5,"pause_after_seconds":0.5,"audio_energy":0.5,"retention_function":"...","visual_mode":"one allowed mode","visual_evidence":"specific verified story detail to show","visual_rationale":"why this treatment fits the beat","visual_note":"...","asset_queries":["scene-specific real video footage search phrase"],"reference_urls":[],"parallax_assets":{{"portrait_query":"...","background_query":"...","generated_media_required":false}}|null,"graphic_spec":null,"news_article_screenshot":null,"music_mood":"...","sfx_cues":["..."]}}],"shorts_script":{{"text":"standalone complete 30-second short, about 60-75 words","hook":"...","payoff":"...","claim_ids":["C1"],"narration_direction":"..."}}}}."""
    script = parse_json_text(response_text(openai_response(prompt, stage="scripting")))
    graphic_issues = []
    for index, chunk in enumerate(script.get("narration_chunks", []), 1):
        validated, issue = validate_graphic_spec(chunk.get("graphic_spec"), chunk, claims)
        chunk["graphic_spec"] = validated
        if issue:
            graphic_issues.append({"chunk": index, "reason": issue})
    script["graphic_spec_issues"] = graphic_issues
    claim_ids = {c.get("claim_id") for c in claims}
    used = {cid for chunk in script.get("narration_chunks", []) for cid in chunk.get("claim_ids", [])}
    used.update(script.get("shorts_script", {}).get("claim_ids", []))
    script["unsupported_claim_ids"] = sorted(used - claim_ids)
    script["approx_word_count"] = sum(len(c.get("text", "").split()) for c in script.get("narration_chunks", []))
    script["target_met_estimate"] = script["approx_word_count"] >= 2500
    script.setdefault("narration_direction", narration_direction)
    script.setdefault("voice_name", voice_name)
    script["visual_brand"] = channel.get("visual_brand", {})
    script["narration_audio_generated"] = False
    return script


def asset_research(script: dict, out_dir: Path) -> dict:
    """Compile the AI creative brief into one per-video downloader handoff."""
    requests_list, seen = [], set()

    def footage_query(raw: Any, chunk: dict) -> tuple[str, list[str]]:
        text = str(raw or "").strip()
        refs = re.findall(r"https?://[^\s\]\[()<>\"']+", text)
        clean = re.sub(r"https?://[^\s\]\[()<>\"']+", " ", text).strip()
        if refs:
            # URLs are evidence references, not search terms. Give providers a
            # short visual query grounded in the scene text instead.
            parts = [str(chunk.get(key, "")).strip() for key in
                     ("visual_evidence", "section_title", "visual_rationale")]
            for ref in refs:
                parsed = urlparse(ref)
                domain = parsed.netloc.lower().removeprefix("www.").split(":")[0]
                domain = domain.rsplit(".", 1)[0].replace("-", " ") if domain else ""
                path_words = re.sub(r"[-_/]+", " ", unquote(parsed.path))
                path_words = re.sub(r"\.[a-z0-9]{2,5}$", " ", path_words, flags=re.I)
                parts.extend((domain, path_words))
            clean = " ".join(part for part in parts if part)
        clean = clean.replace("‘", " ").replace("’", " ").replace("“", " ").replace("”", " ")
        clean = re.sub(r"\s+", " ", clean).strip(" .,/;:")
        # Pixabay rejects oversized q values. Keep the downloader query below
        # its documented search-term limit while retaining the source URL.
        return clean[:100].rstrip(), refs

    for i, chunk in enumerate(script.get("narration_chunks", []), 1):
        parallax = chunk.get("parallax_assets")
        if isinstance(parallax, dict) and chunk.get("visual_mode") == "parallax_portrait":
            for role, field in (("portrait_foreground", "portrait_query"), ("parallax_background", "background_query")):
                query = str(parallax.get(field, "")).strip()
                if query:
                    requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": query,
                        "category": "picture", "segment": i, "parallax_role": role,
                        "generated_media_required": bool(parallax.get("generated_media_required") is True and role == "parallax_background"),
                        "purpose": "Source image for a layered 2.5D portrait scene; foreground and background must be distinct",
                        "suggested_sources": ["Pixabay API", "Wikimedia Commons (no key)", "Internet Archive (no key)", "official company newsroom", "OpenAI generated image"],
                        "source_url": None, "local_path": None, "rights_status": "unknown", "license_evidence": None, "download_status": "not_downloaded"})
        raw_queries = chunk.get("asset_queries", [])
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        for query in raw_queries:
            query, reference_urls = footage_query(query, chunk)
            category = "video"
            key = (query.lower(), category)
            if not key or key in seen:
                continue
            seen.add(key)
            requests_list.append({"asset_id": f"asset_{len(requests_list)+1:03d}", "query": query,
                                 "category": category, "segment": i,
                                 "purpose": chunk.get("visual_rationale") or chunk.get("visual_note", "Support the narration in this section"),
                                 "visual_mode": chunk.get("visual_mode"),
                                 "visual_evidence": chunk.get("visual_evidence"),
                                 "visual_rationale": chunk.get("visual_rationale"),
                        "suggested_sources": ["YouTube Data API search + yt-dlp candidate download", "TikTok web search + yt-dlp candidate download", "Pixabay video API", "Wikimedia Commons video (no key)", "Internet Archive movies (no key)", "NASA video library (no key)"],
                                 "reference_urls": reference_urls, "source_url": None, "local_path": None,
                                 "rights_status": "unknown", "max_duration_seconds": 5,
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
    social_limit = max(0, int(os.getenv("SOCIAL_VIDEO_SEARCHES_PER_RUN", "10")))
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
    screenshots = fulfill_news_headline_requests(topic, script, base_dir / "assets", job_id=job_id)
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
            for category in ("music", "sfx"):
                source_folder = args.youtube_audio_library_dir / category
                if source_folder.exists():
                    shared_assets.import_audio_library_folder(source_folder, category)
        requests_manifest = _json(job_dir / "asset_requests.json", {"assets": []})
        _progress(f"Asset acquisition: resolving {len(requests_manifest.get('assets', []))} scene requests; one update will appear per asset.")
        def asset_progress(index: int, total: int, message: str) -> None:
            if _RUN_PROGRESS is not None:
                pct = 83 + 15 * index / max(1, total)
                _RUN_PROGRESS.update(f"Asset {index}/{total}: {message}", pct)
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
