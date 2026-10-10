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
        "voice_name": "en-AU-Isla:MAI-Voice-2.1",
        "narration_direction": "Natural, clear Australian English. Match the scene's emotional tone without forced slang or exaggerated delivery.",
        "default_search_fallback": "documentary topic",
        "research_prompt": "Prioritise primary documents, reputable reporting, and independent corroboration. Flag uncertainty and conflicting accounts.",
        "system_prompt": "Tell a clear, evidence-led story with a strong beginning, a logical build, and a satisfying factual conclusion.",
        "retention_guidance": "Use meaningful discoveries and payoffs to maintain curiosity throughout; do not invent suspense.",
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
    response = openai_response(prompt, web_search=True)
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


def research_story(topic: str, channel: dict, niche_config: dict | None = None) -> dict:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    prompt = f"""Research a factual documentary story for a {channel.get('niche')} YouTube channel.
Topic: {topic}
Use web search. Build a source-backed evidence ledger for a 20-minute documentary. Aim for at least eight credible, distinct sources when the topic supports it, including primary records where available. Each factual claim must have a direct source URL and a short note about what that source establishes. Separate facts, estimates, allegations, interpretations, and unresolved questions. Never fill gaps by guessing.
Niche research guidance: {niche_config.get('research_prompt', '')}
Return strict JSON: {{"research_brief":"...","claims":[{{"claim_id":"C1","claim":"one checkable factual claim","source_title":"...","url":"https://...","publisher":"...","published_or_accessed":"...","source_type":"primary|official|academic|reporting|archive|other","support":"short paraphrase of the relevant evidence","confidence":"high|medium|low","caveat":"qualification or empty string"}}],"open_questions":["..."]}}.
The brief may contain only claims represented in the ledger. Prioritise original records over summaries and flag conflicting evidence explicitly."""
    response = openai_response(prompt, web_search=True)
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
Use web search to inspect the cited source and seek independent corroboration. Check dates, names, amounts, definitions, attribution, causation, and qualifications. A claim is verified only when the cited material supports it. Mark qualified if accurate only with an explicit caveat. Mark unverified when evidence is missing or weak; disputed when reliable sources conflict. Do not infer certainty from a source's reputation alone.
Claims: {json.dumps(claims, ensure_ascii=False)}
Return strict JSON: {{"results":[{{"claim_id":"C1","status":"verified|qualified|unverified|disputed","verification_note":"what the sources support or fail to support","checked_sources":[{{"title":"...","url":"...","support":"short evidence summary"}}]}}]}}.
Provide at least one checked source for each verified or qualified claim. Do not invent source URLs."""
    response = openai_response(prompt, web_search=True)
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


def write_script(topic: str, channel: dict, research: dict, niche_config: dict | None = None) -> dict:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    claims = research.get("verified_claims", [])
    if not claims:
        raise RuntimeError("Script writing stopped: no research claims passed the verification step")
    voice_name = channel.get("voice_name") or niche_config.get("voice_name", "en-AU-Isla:MAI-Voice-2.1")
    narration_direction = niche_config.get("narration_direction", "Natural, clear Australian English.")
    prompt = f"""Write a retention-focused documentary script based ONLY on the supplied VERIFIED research claims. Do not add unsupported facts. Attach source claim IDs to every factual paragraph. Preserve qualifications and uncertainty. Cite claims inline as [C1].
Channel: {channel.get('name', channel.get('handle', ''))}; niche: {channel.get('niche')}; voice/tone: {channel.get('tone', 'cinematic, clear, curious')}.
Topic: {topic}. Target at least 20 minutes spoken narration (roughly 2,700-3,000 words at a measured documentary pace). {niche_config.get('system_prompt', '')}
Retention design: {niche_config.get('retention_guidance', '')}
Maintain attention from the first line through the ending: open on a specific verified moment or question; state a truthful story promise early; build in clear acts where each section adds evidence, changes the stakes, or corrects an earlier assumption; pay off every major question. Add meaningful re-hooks about every 2-3 minutes, but never use fake cliffhangers, repeat the same teaser, or withhold basic facts merely to inflate watch time. Remove repeated exposition and filler. The ending must resolve the central story, then offer a natural reason to watch another episode without an artificial cliffhanger.
Write for a natural human narrator using Australian English. Keep sentences speakable and varied, use contractions where natural, and avoid stiff transitions, repeated rhetorical questions, forced slang, and overly polished essay language. Never put performance directions inside the words to be spoken. The narrator should follow the emotion and tone assigned to each scene; let intensity rise and fall with the evidence rather than reading every line dramatically.
For each narration chunk, give a `tone` that the creative director can use, plus `delivery_emotion`, `delivery_pace`, `vocal_intensity` from 0.0 to 1.0, and `pause_after_seconds`. These fields guide a future TTS/narrator pass; do not write the audio itself. Overall narration direction: {narration_direction}
For each narration chunk, decide whether a real news headline is creatively necessary. Normally set "news_article_screenshot": null. Request one only when an actual news headline materially strengthens the scene (for example, showing contemporaneous media coverage of a major event). Do not request generic headlines or decorative screenshots; request at most two total. When needed, set {{"search_query":"specific article/story query","highlight_keywords":["one or two exact headline terms"],"reason":"why the headline itself helps this scene"}}.
Research ledger (only these verified claims may support factual narration): {json.dumps(claims, ensure_ascii=False)}
Return strict JSON {{"title_working":"...","logline":"...","narration_direction":"...","voice_name":"{voice_name}","narration_chunks":[{{"section_title":"...","text":"...","claim_ids":["C1"],"tone":"...","delivery_emotion":"...","delivery_pace":"measured|brisk|slow","vocal_intensity":0.5,"pause_after_seconds":0.5,"audio_energy":0.5,"retention_function":"...","visual_note":"...","asset_queries":["..."],"news_article_screenshot":null,"music_mood":"...","sfx_cues":["..."]}}],"shorts_script":{{"text":"standalone complete 30-second short, about 60-75 words","hook":"...","payoff":"...","claim_ids":["C1"],"narration_direction":"..."}}}}."""
    script = parse_json_text(response_text(openai_response(prompt)))
    claim_ids = {c.get("claim_id") for c in claims}
    used = {cid for chunk in script.get("narration_chunks", []) for cid in chunk.get("claim_ids", [])}
    used.update(script.get("shorts_script", {}).get("claim_ids", []))
    script["unsupported_claim_ids"] = sorted(used - claim_ids)
    script["approx_word_count"] = sum(len(c.get("text", "").split()) for c in script.get("narration_chunks", []))
    script["target_met_estimate"] = script["approx_word_count"] >= 2500
    script.setdefault("narration_direction", narration_direction)
    script.setdefault("voice_name", voice_name)
    script["narration_audio_generated"] = False
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
    requests_list.extend(_article_screenshot_requests(script))
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


def prepare_job(channel: dict, topic: str, base_dir: Path, *, generate_thumbnails: bool = False,
                niche_config: dict | None = None) -> Path:
    niche_config = niche_config or get_niche_config(channel.get("niche", ""))
    job_id = f"{_slug(channel.get('handle', 'channel'))}_{_slug(topic)}"
    out = base_dir / "jobs" / job_id
    out.mkdir(parents=True, exist_ok=True)
    research = research_story(topic, channel, niche_config)
    research = verify_research_claims(topic, research, niche_config)
    script = write_script(topic, channel, research, niche_config)
    asset_manifest = asset_research(script, out)
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
    parser.add_argument("--niche-prompts", type=Path, default=Path(__file__).with_name("niche_prompts.json"), help="Niche-specific research, script and narrator instructions")
    parser.add_argument("--make-thumbnails", action="store_true", help="Call the configured OpenAI image model")
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
    niche_config = get_niche_config(channel.get("niche", ""), args.niche_prompts)
    job_dir = prepare_job(channel, topic, args.base_dir, generate_thumbnails=args.make_thumbnails,
                          niche_config=niche_config)
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
