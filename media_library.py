"""Reusable local media catalog with Pixabay, keyless archives, Freesound and OpenAI.

Media stays in per-type folders and is indexed by a SHA-256 hash. Search results
are downloaded only from an explicitly supported provider; generated images are
recorded separately as generated_media. Add new provider adapters as needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

try:
    import requests
except ImportError:
    requests = None


FOLDERS = {
    "picture": "pictures",
    "video": "video_clips",
    "generated": "generated_media",
    "music": "background_music",
    "sfx": "sfx",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "asset"


def _require_requests() -> Any:
    if requests is None:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt")
    return requests


def _is_cc0_license(value: str | None) -> bool:
    return (value or "").strip().lower() in {"creative commons 0", "cc0", "cc-zero"}


class MediaLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in FOLDERS.values():
            (root / folder).mkdir(parents=True, exist_ok=True)
        self.db = root / "catalog.sqlite3"
        self.pixabay_cache = root / ".pixabay_search_cache"
        self.pixabay_cache.mkdir(parents=True, exist_ok=True)
        self.public_cache = root / ".public_search_cache"
        self.public_cache.mkdir(parents=True, exist_ok=True)
        self.pixabay_searches_this_run = 0
        self.public_searches_this_run = 0
        with sqlite3.connect(self.db) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, category TEXT NOT NULL,
                path TEXT NOT NULL, title TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
                source_url TEXT NOT NULL, license TEXT, creator TEXT, duration REAL,
                created_at TEXT NOT NULL, metadata_json TEXT NOT NULL
            )""")

    def _register(self, data: bytes, *, category: str, title: str, tags: list[str], source: str,
                  source_url: str, license_name: str | None = None, creator: str | None = None,
                  duration: float | None = None, ext: str = ".bin", metadata: dict | None = None) -> dict:
        if category not in FOLDERS:
            raise ValueError(f"Unknown category: {category}")
        digest = hashlib.sha256(data).hexdigest()
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT asset_id,path,source,source_url,license,creator,category FROM assets WHERE sha256=?", (digest,)).fetchone()
            if row:
                return {"asset_id": row[0], "path": row[1], "source": row[2], "source_url": row[3],
                        "license": row[4], "creator": row[5], "category": row[6], "reused": True}
        title_slug = _slug(title)
        file_name = f"{title_slug}_{digest[:12]}{ext}"
        path = self.root / FOLDERS[category] / file_name
        path.write_bytes(data)
        asset_id = digest[:16]
        details = metadata or {}
        record = {
            "asset_id": asset_id, "sha256": digest, "category": category,
            "path": str(path.resolve()), "title": title, "tags": sorted(set(tags)),
            "source": source, "source_url": source_url, "license": license_name,
            "creator": creator, "duration": duration,
            "created_at": datetime.now(timezone.utc).isoformat(), "metadata": details,
        }
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                record["asset_id"], record["sha256"], category, record["path"], title,
                json.dumps(record["tags"]), source, source_url, license_name, creator, duration,
                record["created_at"], json.dumps(details, ensure_ascii=False)))
        sidecar = path.with_suffix(path.suffix + ".json")
        sidecar.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"asset_id": asset_id, "path": str(path.resolve()), "source": source,
                "source_url": source_url, "license": license_name, "creator": creator,
                "category": category, "metadata": details, "reused": False}

    def find_reusable(self, query: str, category: str | None = None, limit: int = 10) -> list[dict]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        if not terms:
            return []
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT asset_id,category,path,title,tags,source,source_url,license,creator,duration,metadata_json FROM assets").fetchall()
        scored = []
        for row in rows:
            asset_id, cat, path, title, tags, source, source_url, lic, creator, duration, metadata = row
            if category and cat != category:
                continue
            words = set(re.findall(r"[a-z0-9]+", f"{title} {tags}".lower()))
            score = len(terms & words) / max(len(terms), 1)
            if score:
                scored.append((score, {"asset_id": asset_id, "category": cat, "path": path, "title": title,
                                      "tags": json.loads(tags), "source": source, "source_url": source_url,
                                      "license": lic, "creator": creator, "duration": duration,
                                      "metadata": json.loads(metadata), "match_score": round(score, 3)}))
        return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]

    def download_pixabay(self, query: str, category: str, limit: int = 3) -> list[dict]:
        """Search and cache images/video from Pixabay. Music/SFX use Freesound."""
        if category not in {"picture", "video"}:
            raise ValueError("Pixabay adapter supports picture and video categories")
        key = os.getenv("PIXABAY_API_KEY")
        if not key:
            return []
        req = _require_requests()
        endpoint = "videos" if category == "video" else ""
        api_url = "https://pixabay.com/api/" if not endpoint else f"https://pixabay.com/api/{endpoint}/"
        cache_key = hashlib.sha256(f"{category}\0{query.strip().lower()}".encode()).hexdigest()
        cache_path = self.pixabay_cache / f"{cache_key}.json"
        search_results = None
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 24 * 60 * 60:
            try:
                search_results = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                search_results = None
        if search_results is None:
            max_searches = max(1, int(os.getenv("PIXABAY_MAX_SEARCHES_PER_RUN", "10")))
            if self.pixabay_searches_this_run >= max_searches:
                return []
            response = req.get(api_url, params={"key": key, "q": query, "per_page": min(max(limit, 1), 5), "safesearch": "true"}, timeout=30)
            response.raise_for_status()
            search_results = response.json()
            cache_path.write_text(json.dumps(search_results), encoding="utf-8")
            self.pixabay_searches_this_run += 1
        saved = []
        for hit in search_results.get("hits", [])[:limit]:
            if category == "video":
                variants = hit.get("videos", {})
                chosen = variants.get("medium") or variants.get("small") or next(iter(variants.values()), {})
                download_url = chosen.get("url")
                ext = ".mp4"
                duration = hit.get("duration")
            else:
                download_url = hit.get("largeImageURL") or hit.get("webformatURL")
                ext = ".jpg"
                duration = None
            if not download_url:
                continue
            media = req.get(download_url, timeout=60)
            media.raise_for_status()
            saved.append(self._register(media.content, category=category, title=hit.get("tags") or query,
                tags=(hit.get("tags") or query).split(","), source="pixabay", source_url=hit.get("pageURL", ""),
                license_name="Pixabay Content License (confirm current terms)", creator=hit.get("user"),
                duration=duration, ext=ext, metadata={"provider_id": hit.get("id"), "query": query,
                    "download_url": download_url, "width": chosen.get("width") if category == "video" else hit.get("imageWidth"),
                    "height": chosen.get("height") if category == "video" else hit.get("imageHeight")}))
        return saved

    def _cached_public_json(self, provider: str, query: str, category: str,
                            url: str, params: dict, *, headers: dict | None = None) -> dict:
        """Fetch and cache a keyless archive search response for 24 hours."""
        cache_key = hashlib.sha256(f"{provider}\0{category}\0{query.strip().lower()}".encode()).hexdigest()
        cache_path = self.public_cache / f"{cache_key}.json"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 24 * 60 * 60:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        max_searches = max(1, int(os.getenv("PUBLIC_ARCHIVE_MAX_SEARCHES_PER_RUN", "20")))
        if self.public_searches_this_run >= max_searches:
            return {}
        response = _require_requests().get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.public_searches_this_run += 1
        return payload

    @staticmethod
    def _commons_candidates(payload: dict, category: str, query: str) -> list[dict]:
        pages = payload.get("query", {}).get("pages", {})
        pages = pages if isinstance(pages, list) else list(pages.values())
        candidates = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            mime = info.get("mime", "")
            is_video = mime.startswith("video/") or Path(info.get("url", "")).suffix.lower() in {".webm", ".ogv", ".mp4"}
            if (category == "video") != is_video:
                continue
            extmeta = info.get("extmetadata", {})
            title = page.get("title", "Commons file").removeprefix("File:")
            candidates.append({
                "download_url": (info.get("thumburl") or info.get("url")) if mime == "image/svg+xml" else (info.get("url") or info.get("thumburl")),
                "source_url": "https://commons.wikimedia.org/wiki/" + page.get("title", "").replace(" ", "_"),
                "title": title, "creator": extmeta.get("Artist", {}).get("value"),
                "license": extmeta.get("LicenseShortName", {}).get("value"),
                "license_url": extmeta.get("LicenseUrl", {}).get("value"),
                "mime": mime, "query": query,
            })
        return candidates

    def _internet_archive_candidates(self, query: str, category: str, limit: int) -> list[dict]:
        mediatype = "movies" if category == "video" else "image"
        search = self._cached_public_json("internet_archive", query, category,
            "https://archive.org/advancedsearch.php", {
                "q": f"({query}) AND mediatype:{mediatype}",
                "fl[]": ["identifier", "title", "creator", "date", "mediatype", "licenseurl", "rights"],
                "rows": limit, "output": "json",
            })
        results = []
        for item in search.get("response", {}).get("docs", [])[:limit]:
            identifier = item.get("identifier")
            if not identifier:
                continue
            metadata = self._cached_public_json("internet_archive_item", identifier, "metadata",
                f"https://archive.org/metadata/{identifier}", {})
            files = metadata.get("files", [])
            allowed = {"jpg", "jpeg", "png", "gif", "tif", "tiff"} if category == "picture" else {"mp4", "mpeg4", "ogv", "webm", "mov"}
            for file in files:
                name = file.get("name", "")
                suffix = Path(name).suffix.lower().lstrip(".")
                fmt = str(file.get("format", "")).lower()
                if suffix not in allowed and not (category == "video" and "mpeg4" in fmt):
                    continue
                results.append({
                    "download_url": f"https://archive.org/download/{identifier}/{quote(name)}",
                    "source_url": f"https://archive.org/details/{identifier}",
                    "title": item.get("title") or identifier, "creator": item.get("creator"),
                    "license": item.get("licenseurl") or item.get("rights") or metadata.get("metadata", {}).get("rights"),
                    "license_url": item.get("licenseurl"), "query": query,
                    "format": file.get("format"), "provider_id": identifier,
                })
                break
        return results

    def _loc_candidates(self, query: str, category: str, limit: int) -> list[dict]:
        if category != "picture":
            return []
        payload = self._cached_public_json("library_of_congress", query, category,
            "https://www.loc.gov/pictures/search/", {"q": query, "fo": "json", "c": limit})
        results = []
        for item in payload.get("results", [])[:limit]:
            image_urls = item.get("image_url") or []
            if not image_urls:
                continue
            results.append({"download_url": image_urls[-1], "source_url": item.get("url", ""),
                "title": item.get("title") or query, "creator": item.get("contributor", [None])[0] if item.get("contributor") else None,
                "license": item.get("rights") or item.get("rights_advisory"), "query": query,
                "provider_id": item.get("id"), "date": item.get("date")})
        return results

    def _nasa_candidates(self, query: str, category: str, limit: int) -> list[dict]:
        media_type = "video" if category == "video" else "image"
        search = self._cached_public_json("nasa_images", query, category,
            "https://images-api.nasa.gov/search", {"q": query, "media_type": media_type, "page_size": limit})
        results = []
        for item in search.get("collection", {}).get("items", [])[:limit]:
            data = (item.get("data") or [{}])[0]
            nasa_id = data.get("nasa_id")
            if not nasa_id:
                continue
            asset_payload = self._cached_public_json("nasa_asset", nasa_id, "metadata",
                f"https://images-api.nasa.gov/asset/{nasa_id}", {})
            assets = asset_payload.get("collection", {}).get("items", [])
            accepted = {".mp4"} if category == "video" else {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
            direct = next((entry.get("href") for entry in assets if Path(urlparse(entry.get("href", "")).path).suffix.lower() in accepted), None)
            if direct:
                results.append({"download_url": direct, "source_url": f"https://images.nasa.gov/details/{nasa_id}",
                    "title": data.get("title") or nasa_id, "creator": data.get("center"),
                    "license": "NASA media; verify item-specific usage notes", "query": query,
                    "provider_id": nasa_id, "description": data.get("description"), "date": data.get("date_created")})
        return results

    def _public_archive_candidates(self, query: str, category: str, limit: int = 1) -> list[dict]:
        """Search no-key public collections for candidate image/video files.

        The adapters are deliberately sequential and bounded. Archive presence
        does not itself establish reuse rights; rights metadata is retained for QA.
        """
        if category not in {"picture", "video"}:
            return []
        headers = {"User-Agent": "StoryPilot/0.1 (https://github.com/kensu-kimsu/storypilot-pipeline)"}
        sources = [
            ("wikimedia_commons", lambda: self._commons_candidates(self._cached_public_json(
                "wikimedia_commons", query, category, "https://commons.wikimedia.org/w/api.php", {
                    "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
                    "gsrlimit": min(limit, 10), "prop": "imageinfo", "iiprop": "url|extmetadata",
                    "iiurlwidth": 1600, "format": "json", "formatversion": 2,
                }, headers=headers), category, query)),
            ("internet_archive", lambda: self._internet_archive_candidates(query, category, limit)),
            ("library_of_congress", lambda: self._loc_candidates(query, category, limit)),
            ("nasa_images", lambda: self._nasa_candidates(query, category, limit)),
        ]
        all_candidates = []
        for provider, search_candidates in sources:
            try:
                provider_candidates = search_candidates()
            except Exception:
                continue
            provider_added = 0
            for candidate in provider_candidates:
                candidate["source"] = provider
                all_candidates.append(candidate)
                provider_added += 1
                if provider_added >= limit:
                    break
        return all_candidates

    def search_public_archives(self, query: str, category: str, limit: int = 3) -> list[dict]:
        """Return keyless archive search candidates without downloading media."""
        return self._public_archive_candidates(query, category, limit)

    def download_public_archives(self, query: str, category: str, limit: int = 1) -> list[dict]:
        """Search keyless public collections and download relevant image/video files."""
        candidates = self._public_archive_candidates(query, category, limit)
        req = _require_requests()
        headers = {"User-Agent": "StoryPilot/0.1 (https://github.com/kensu-kimsu/storypilot-pipeline)"}
        saved = []
        for candidate in candidates:
            if len(saved) >= limit:
                break
            provider = candidate.get("source", "public_archive")
            download_url = candidate.get("download_url")
            if not download_url:
                continue
            try:
                media = req.get(download_url, headers=headers if provider == "wikimedia_commons" else None,
                                timeout=60, stream=True)
                media.raise_for_status()
                content_type = media.headers.get("Content-Type", "").split(";")[0].lower()
                if category == "picture" and content_type and not content_type.startswith("image/"):
                    continue
                if category == "video" and content_type and not content_type.startswith("video/") and not download_url.lower().endswith((".mp4", ".webm", ".ogv", ".mov")):
                    continue
                suffix = Path(urlparse(download_url).path).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".mp4", ".webm", ".ogv", ".mov"}:
                    suffix = ".jpg" if category == "picture" else ".mp4"
                max_bytes = max(1, int(float(os.getenv("MAX_PUBLIC_ASSET_MB", "250")) * 1024 * 1024))
                content_length = int(media.headers.get("Content-Length", "0") or 0)
                if content_length > max_bytes:
                    continue
                chunks, received = [], 0
                for chunk in media.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > max_bytes:
                        chunks = []
                        break
                    chunks.append(chunk)
                if not chunks:
                    continue
                saved.append(self._register(b"".join(chunks), category=category,
                    title=candidate.get("title") or query, tags=[query], source=provider,
                    source_url=candidate.get("source_url", ""), license_name=candidate.get("license"),
                    creator=candidate.get("creator"), duration=candidate.get("duration"), ext=suffix,
                    metadata={**candidate, "download_url": download_url}))
            except Exception:
                continue
        return saved

    def download_freesound_preview(self, query: str, category: str = "sfx", limit: int = 3) -> list[dict]:
        """Search Freesound and cache MP3 previews; original downloads need OAuth."""
        if category not in {"music", "sfx"}:
            raise ValueError("Freesound adapter supports music and sfx categories")
        token = os.getenv("FREESOUND_API_KEY")
        if not token:
            return []
        req = _require_requests()
        response = req.get("https://freesound.org/apiv2/search/", params={
            "query": query, "token": token, "page_size": min(max(limit, 1), 10),
            "filter": 'license:"Creative Commons 0"',
            "fields": "id,name,tags,username,license,duration,previews,url",
        }, timeout=30)
        response.raise_for_status()
        saved = []
        for hit in response.json().get("results", [])[:limit]:
            # Re-check the returned metadata; fail closed if the provider ignored
            # or changed the filter instead of storing a non-CC0 file.
            if not _is_cc0_license(hit.get("license")):
                continue
            preview = hit.get("previews", {}).get("preview-hq-mp3") or hit.get("previews", {}).get("preview-lq-mp3")
            if not preview:
                continue
            audio = req.get(preview, timeout=60)
            audio.raise_for_status()
            saved.append(self._register(audio.content, category=category, title=hit.get("name") or query,
                tags=hit.get("tags", []), source="freesound_preview", source_url=hit.get("url", ""),
                license_name=hit.get("license"), creator=hit.get("username"), duration=hit.get("duration"),
                ext=".mp3", metadata={"provider_id": hit.get("id"), "query": query, "preview_url": preview,
                    "original_download_requires_oauth": True, "license_filter": "Creative Commons 0"}))
        return saved

    def generate_image(self, prompt: str, title: str, tags: list[str] | None = None) -> dict | None:
        """Generate and cache a still visual through OpenAI Images API."""
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return None
        req = _require_requests()
        response = req.post("https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
                  "size": "1536x1024", "quality": "high", "n": 1}, timeout=300)
        response.raise_for_status()
        data = base64.b64decode(response.json()["data"][0]["b64_json"])
        return self._register(data, category="generated", title=title, tags=tags or [], source="openai_image",
                              source_url="", license_name="AI-generated; retain provider terms",
                              ext=".png", metadata={"prompt": prompt})

    def import_audio_library_folder(self, source: Path, category: str) -> list[dict]:
        """Index pre-downloaded YouTube Audio Library files for automatic reuse.

        Optional `<audio-file>.<suffix>.json` sidecars can provide `title`,
        `tags`, `license`, `creator`, and `source_url`; absent tags fall back to
        filename words. The YouTube Audio Library download itself is not
        automated here; files are copied into this managed catalog once.
        """
        if category not in {"music", "sfx"}:
            raise ValueError("Audio category must be music or sfx")
        accepted = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
        imported = []
        for path in source.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in accepted:
                continue
            sidecar_path = path.with_suffix(path.suffix + ".json")
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.exists() else {}
            title = sidecar.get("title", path.stem)
            tags = sidecar.get("tags", re.findall(r"[a-z0-9]+", path.stem.lower()))
            result = self._register(path.read_bytes(), category=category, title=title, tags=tags,
                source="youtube_audio_library_local", source_url=sidecar.get("source_url", ""),
                license_name=sidecar.get("license", "YouTube Audio Library; retain track-specific attribution notes"),
                creator=sidecar.get("creator"), ext=path.suffix.lower(), metadata={"imported_from": str(path.resolve())})
            imported.append(result)
        return imported


def acquire_script_assets(script: dict, library: MediaLibrary, *, per_query: int = 1) -> dict:
    """Fetch/reuse visuals and match local Audio Library tracks to script cues."""
    manifest = {"assets": [], "audio_assignments": [], "notes": []}
    visual_seen = set()
    for section_index, chunk in enumerate(script.get("narration_chunks", []), 1):
        queries = chunk.get("asset_queries", [])
        for query_index, query in enumerate(queries):
            key = (str(query).lower().strip(), query_index % 2)
            if key in visual_seen:
                continue
            visual_seen.add(key)
            category = "video" if query_index % 2 == 0 else "picture"
            found = library.find_reusable(query, category=category, limit=1)
            if found and found[0]["match_score"] >= 0.5:
                item = {"asset_id": found[0]["asset_id"], "path": found[0]["path"], "category": category,
                        "reused": True, "query": query, "section": section_index}
            else:
                downloaded = library.download_pixabay(query, category=category, limit=per_query)
                if not downloaded:
                    downloaded = library.download_public_archives(query, category=category, limit=per_query)
                item = ({**downloaded[0], "category": category, "reused": downloaded[0].get("reused", False),
                         "query": query, "section": section_index} if downloaded else None)
            if item is None and category == "picture":
                # Mark as illustration, never as real archival proof/document.
                prompt = (f"Create an editorial documentary illustration for: {query}. "
                          "Atmospheric visual metaphor only. Do not fabricate text, records, evidence, logos, or a real historical photograph.")
                generated = library.generate_image(prompt, title=query, tags=[query, "generated illustration"])
                if generated:
                    item = {**generated, "category": "generated", "reused": generated.get("reused", False),
                            "query": query, "section": section_index}
            if item:
                manifest["assets"].append(item)
            else:
                manifest["notes"].append(f"No asset found for visual query: {query}")

        music_query = str(chunk.get("music_mood", chunk.get("tone", "documentary background music")))
        for category, cue in (("music", music_query), ("sfx", " ".join(chunk.get("sfx_candidates", [])))):
            if not cue.strip():
                continue
            matches = library.find_reusable(cue, category=category, limit=1)
            manifest["audio_assignments"].append({"section": section_index, "category": category,
                "cue": cue, "asset": matches[0] if matches else None,
                "status": "matched" if matches else "needs_local_audio_library_asset"})
    return manifest


def acquire_asset_requests(request_manifest: dict, library: MediaLibrary, *, per_request: int = 1) -> dict:
    """Consume the AI-authored asset_requests.json handoff and resolve each row."""
    results = {"assets": [], "unresolved": [], "source_manifest": "asset_requests.json"}
    for request in request_manifest.get("assets", []):
        category = request.get("category")
        query = str(request.get("query", "")).strip()
        if not query or category not in FOLDERS:
            results["unresolved"].append({**request, "status": "invalid_request"})
            continue
        matches = library.find_reusable(query, category=category, limit=1)
        selected = None
        if matches and matches[0]["match_score"] >= 0.34:
            selected = {**matches[0], "reused": True}
        elif category in {"picture", "video"}:
            downloads = library.download_pixabay(query, category=category, limit=per_request)
            if not downloads:
                downloads = library.download_public_archives(query, category=category, limit=per_request)
            if downloads:
                selected = {**downloads[0], "category": category}
            elif category == "picture":
                generated = library.generate_image(
                    f"Editorial documentary illustration for {query}. {request.get('purpose','')} "
                    "Use an atmospheric visual metaphor. Do not invent historical photographs, records, evidence, logos or readable document text.",
                    title=query, tags=[query, "generated illustration"])
                if generated:
                    selected = {**generated, "category": "generated"}
        elif category in {"music", "sfx"} and os.getenv("FREESOUND_API_KEY"):
            audio = library.download_freesound_preview(query, category=category, limit=per_request)
            if audio:
                selected = {**audio[0], "category": category}
        # Music and SFX are selected from the local catalog first, then from
        # Freesound CC0 previews when an API key is configured.
        if selected:
            results["assets"].append({**request, "status": "reused" if selected.get("reused") else "downloaded_or_generated",
                                      "selected": selected})
        else:
            results["unresolved"].append({**request, "status": "no_match_in_configured_sources"})
    return results

