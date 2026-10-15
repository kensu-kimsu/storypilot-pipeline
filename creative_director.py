"""Plan and export a story-led documentary timeline for the local renderer."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PALETTES = {
    "market_crash": {"background": "#0A0D14", "accent": "#FF3333", "secondary": "#D8DEE9"},
    "tech_innovation": {"background": "#05101A", "accent": "#00E676", "secondary": "#D5F5E3"},
    "historical_monopoly": {"background": "#14100C", "accent": "#FFD700", "secondary": "#E8D9B5"},
    "noir_gold": {"background": "#080A0F", "accent": "#FFD34E", "secondary": "#C7CEDB"},
}

# Modes are chosen from semantic tone and available assets, then varied with a
# deterministic per-job seed. They are not applied in a repeating rotation.
TONE_MODES = {
    "dramatic_realization": ["minimalist_text", "parallax_portrait", "document_focus"],
    "rapid_fire_facts": ["split_screen", "kinetic_fact", "cinematic_broll"],
    "deep_analysis": ["animated_chart", "document_focus", "kinetic_fact"],
    "historical_context": ["document_focus", "parallax_portrait", "animated_map"],
    "suspense": ["minimalist_text", "parallax_portrait", "cinematic_broll"],
    "transition": ["cinematic_broll", "animated_map", "kinetic_fact"],
    "standard": ["cinematic_broll", "parallax_portrait", "document_focus", "kinetic_fact"],
}

VALID_VISUAL_MODES = {mode for modes in TONE_MODES.values() for mode in modes}
TYPE_TREATMENTS = {
    "minimalist_text": "single_statement",
    "split_screen": "comparison_labels",
    "animated_chart": "data_callout",
    "document_focus": "document_annotation",
    "animated_map": "map_label",
    "parallax_portrait": "name_or_date",
    "kinetic_fact": "short_phrase_emphasis",
    "cinematic_broll": "restrained_lower_third",
}
DEFAULT_VISUAL_BRAND = {
    "primary_font": "Montserrat",
    "caption_font_weight": "bold",
    "caption_position": "lower_third",
    "brand_accent": "#FFD34E",
    "brand_secondary": "#C7CEDB",
    "signature_motif": "subtle documentary grain",
}


def _stable_seed(job_id: str) -> int:
    return int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16], 16)


def _seconds(value: float) -> str:
    # FCPXML time values are rational seconds; quantize to 30 fps.
    frames = max(1, round(value * 30))
    return f"{frames}/30s"


def _fraction_time(value: float) -> str:
    return _seconds(value)


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


def _clean_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "asset"


def choose_palette(topic: str, explicit: dict | None = None) -> dict:
    if explicit:
        required = {"background", "accent", "secondary"}
        if required.issubset(explicit):
            return {key: explicit[key] for key in required}
    text = topic.lower()
    if any(word in text for word in ("crash", "scandal", "collapse", "fraud")):
        return dict(PALETTES["market_crash"])
    if any(word in text for word in ("ai", "technology", "innovation", "robot", "cyber")):
        return dict(PALETTES["tech_innovation"])
    if any(word in text for word in ("monopoly", "empire", "historical", "history", "rockefeller")):
        return dict(PALETTES["historical_monopoly"])
    return dict(PALETTES["noir_gold"])


def _smooth_noise(rng: random.Random, count: int = 6) -> list[float]:
    """Small, smooth camera-drift points (a dependency-free value-noise curve)."""
    raw = [rng.uniform(-1.0, 1.0) for _ in range(count)]
    points = []
    for i in range(count):
        left = raw[max(0, i - 1)]
        right = raw[min(count - 1, i + 1)]
        points.append((left + 2 * raw[i] + right) / 4)
    return points


def _camera_motion(rng: random.Random, mode: str) -> dict:
    # Keep drift subtle and scale dependent; avoid artificial shake on typography.
    if mode in {"minimalist_text", "animated_chart", "kinetic_fact"}:
        amplitude = 0.15
    else:
        amplitude = rng.uniform(0.35, 0.9)
    return {
        "style": rng.choice(["slow_push", "slow_pull", "lateral_drift", "parallax_drift"]),
        "scale_start": round(rng.uniform(1.015, 1.06), 3),
        "scale_end": round(rng.uniform(1.07, 1.16), 3),
        "x_noise": [round(v * amplitude, 3) for v in _smooth_noise(rng)],
        "y_noise": [round(v * amplitude * 0.55, 3) for v in _smooth_noise(rng)],
        "rotation_degrees": round(rng.uniform(-0.22, 0.22), 3),
        "intentional_hold": mode == "minimalist_text",
    }


def _duration_range(tone: str, energy: float) -> tuple[float, float]:
    if tone == "dramatic_realization" or energy < 0.18:
        return 4.0, 6.0
    if tone == "rapid_fire_facts" or energy > 0.78:
        return 0.8, 1.5
    if tone in {"deep_analysis", "historical_context"}:
        return 2.6, 4.8
    return 1.8, 4.0


def _candidate_asset_ids(segment: dict, assets: dict[str, dict], mode: str) -> list[str]:
    requested = segment.get("asset_ids", [])
    candidates = [
        a for a in requested
        if a in assets
        and assets[a].get("path")
    ]
    # Charts, documents and maps can be generated locally from data; if no suitable
    # asset exists, preserve an explicit placeholder instead of silently reusing B-roll.
    suitable = {
        "document_focus": {"document", "image", "graphic", "video"},
        "animated_map": {"map", "graphic", "image", "video"},
        "animated_chart": {"chart", "graphic", "image", "video"},
        "split_screen": {"video", "image", "graphic"},
    }.get(mode)
    if suitable:
        filtered = [a for a in candidates if assets[a].get("kind", "video") in suitable]
        if filtered:
            candidates = filtered
    return candidates


def _channel_key(job: dict) -> str:
    channel = job.get("channel", {})
    return str(job.get("channel_id") or channel.get("channel_id") or channel.get("handle") or "default")


def _brand_identity(job: dict) -> dict:
    channel = job.get("channel", {})
    configured = job.get("visual_brand") or channel.get("visual_brand") or {}
    brand = dict(DEFAULT_VISUAL_BRAND)
    brand.update({key: value for key, value in configured.items() if value not in (None, "")})
    brand["caption_positions"] = configured.get(
        "caption_positions", [brand["caption_position"], "top_safe", "center_emphasis"]
    )
    return brand


def _recent_mode_counts(style_history: list[dict] | None, channel_key: str) -> dict[str, int]:
    rows = [row for row in (style_history or []) if row.get("channel_key") == channel_key]
    counts: dict[str, int] = {}
    for row in rows[-5:]:
        signature = row.get("signature", {})
        for mode in signature.get("dominant_modes", []):
            counts[mode] = counts.get(mode, 0) + 1
    return counts


def _select_mode(rng: random.Random, segment: dict, choices: list[str], recent_counts: dict[str, int]) -> tuple[str, str]:
    """Use the director's scene choice when supplied; otherwise vary within tone-fit options."""
    requested = str(segment.get("visual_mode", "")).strip()
    if requested in choices:
        return requested, "scene_directed"
    if requested in VALID_VISUAL_MODES and requested not in choices:
        # Respect an explicit creative choice when it is an established treatment.
        return requested, "scene_directed"
    lowest_use = min(recent_counts.get(mode, 0) for mode in choices)
    fresh_choices = [mode for mode in choices if recent_counts.get(mode, 0) == lowest_use]
    return rng.choice(fresh_choices), "tone_led_variation"


def build_edit_plan(job: dict, style_history: list[dict] | None = None) -> dict:
    required = {"job_id", "topic", "narration_file", "narration_duration", "segments", "assets"}
    missing = sorted(required - set(job))
    if missing:
        raise ValueError(f"Job is missing required fields: {', '.join(missing)}")
    if not job["segments"]:
        raise ValueError("At least one narration segment is required")

    rng = random.Random(_stable_seed(str(job["job_id"])))
    assets = job["assets"]
    palette = choose_palette(job["topic"], job.get("palette"))
    brand = _brand_identity(job)
    channel_key = _channel_key(job)
    recent_counts = _recent_mode_counts(style_history, channel_key)
    duration_total = float(job["narration_duration"])
    ordered_segments = sorted(job["segments"], key=lambda item: float(item["start"]))
    cursor = 0.0
    for index, segment in enumerate(ordered_segments):
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if abs(seg_start - cursor) > 0.05:
            raise ValueError(f"Narration segments must cover the full timeline without gaps/overlaps near {cursor:.2f}s")
        if seg_end <= seg_start:
            raise ValueError(f"Invalid narration segment at index {index}")
        cursor = seg_end
    if abs(cursor - duration_total) > 0.05:
        raise ValueError("Narration segments must end at narration_duration")
    previous_mode = None
    output = []

    for seg_index, segment in enumerate(job["segments"]):
        start = float(segment["start"])
        end = float(segment["end"])
        if start < 0 or end <= start or end > duration_total + 0.05:
            raise ValueError(f"Invalid segment time range at index {seg_index}")
        tone = str(segment.get("tone", "standard")).lower()
        if tone not in TONE_MODES:
            tone = "standard"
        energy = max(0.0, min(1.0, float(segment.get("audio_energy", 0.5))))
        choices = list(TONE_MODES[tone])
        if segment.get("available_modes"):
            allowed = set(segment["available_modes"])
            choices = [mode for mode in choices if mode in allowed] or ["cinematic_broll"]
        if previous_mode in choices and len(choices) > 1 and not segment.get("visual_mode"):
            choices.remove(previous_mode)
        mode, selection_reason = _select_mode(rng, segment, choices, recent_counts)
        previous_mode = mode
        low, high = _duration_range(tone, energy)
        usable_assets = _candidate_asset_ids(segment, assets, mode)
        foreground_asset_id = segment.get("parallax_foreground_asset_id") if mode == "parallax_portrait" else None
        if foreground_asset_id not in usable_assets or assets.get(foreground_asset_id, {}).get("kind") not in {"image", "picture"}:
            foreground_asset_id = None
        if foreground_asset_id:
            # The base asset is a separate background plate; do not leave the
            # portrait in it where it would appear as a doubled/ghosted subject.
            usable_assets = [asset_id for asset_id in usable_assets if asset_id != foreground_asset_id]
        if mode == "minimalist_text":
            # Still keep an optional portrait/document as an underlay, but the mode
            # itself directs the editor to use a dark field and one gold line.
            asset_id = rng.choice(usable_assets) if usable_assets and rng.random() < 0.25 else None
        elif usable_assets:
            asset_id = rng.choice(usable_assets)
        else:
            asset_id = None

        t = start
        shot_index = 0
        while t < end - 1e-4:
            remaining = end - t
            cut_dur = min(remaining, rng.uniform(low, high))
            if remaining > high and remaining - cut_dur < low * 0.65:
                cut_dur = remaining
            shot_mode = mode
            if shot_index > 0 and mode == "split_screen" and rng.random() < 0.35:
                shot_mode = "kinetic_fact"
            asset = assets.get(asset_id, {}) if asset_id else {}
            sfx_cue = rng.choice(segment.get("sfx_candidates", ["paper_rustle", "camera_shutter", "low_impact", "none"]))
            shot = {
                "shot_id": f"{job['job_id']}_s{seg_index + 1:03d}_{shot_index + 1:02d}",
                "start": round(t, 3),
                "duration": round(cut_dur, 3),
                "end": round(t + cut_dur, 3),
                "segment_index": seg_index,
                "narration_text": segment.get("text", ""),
                "tone": tone,
                "audio_energy": round(energy, 3),
                "visual_mode": shot_mode,
                "visual_evidence": segment.get("visual_evidence", ""),
                "visual_rationale": segment.get("visual_rationale", segment.get("visual_note", "")),
                "graphic_spec": segment.get("graphic_spec") if shot_index == 0 else None,
                "claim_ids": segment.get("claim_ids", []),
                "style_selection": selection_reason,
                "asset_id": asset_id,
                "asset_path": asset.get("path"),
                "asset_kind": asset.get("kind", "generated_placeholder" if asset_id is None else "video"),
                "parallax_foreground_asset_id": foreground_asset_id if shot_mode == "parallax_portrait" else None,
                "overlay_text": segment.get("overlay_text", ""),
                "palette": palette,
                "caption_position": segment.get("caption_position") or _caption_position(brand, shot_mode),
                "type_treatment": TYPE_TREATMENTS.get(shot_mode, "restrained_lower_third"),
                "camera": _camera_motion(rng, shot_mode),
                "transition_out": rng.choice(["hard_cut", "short_dissolve", "match_cut", "whip_pan"]),
                "sfx_cue": sfx_cue,
                "sfx_asset_id": (segment.get("sfx_by_cue") or {}).get(sfx_cue),
                "music_asset_ids": segment.get("music_asset_ids", []),
                "music_mood": segment.get("music_mood", "low_tension"),
                "editor_note": _editor_note(shot_mode, segment),
            }
            output.append(shot)
            t += cut_dur
            shot_index += 1
            # Use different candidate media within a segment when supplied.
            if usable_assets and shot_index < len(usable_assets):
                asset_id = usable_assets[shot_index]

    # Identify especially high-value review points rather than demanding that every
    # shot be edited manually.
    polish = sorted(
        output,
        key=lambda s: (
            s["tone"] in {"dramatic_realization", "suspense"},
            s["audio_energy"],
            s["duration"],
        ),
        reverse=True,
    )[: min(5, len(output))]
    mode_counts: dict[str, int] = {}
    for shot in output:
        mode_counts[shot["visual_mode"]] = mode_counts.get(shot["visual_mode"], 0) + 1
    signature = {
        "palette_family": _palette_family(palette),
        "dominant_modes": [mode for mode, _ in sorted(mode_counts.items(), key=lambda item: (-item[1], item[0]))[:3]],
        "caption_positions": sorted({shot["caption_position"] for shot in output}),
        "primary_font": brand["primary_font"],
        "signature_motif": brand["signature_motif"],
    }
    return {
        "job_id": job["job_id"],
        "topic": job["topic"],
        "format": job.get("format", "long_form"),
        "canvas": job.get("canvas", {"width": 1920, "height": 1080, "fps": 30}),
        "duration": duration_total,
        "palette": palette,
        "channel_key": channel_key,
        "visual_brand": brand,
        "episode_style": {
            "selection_principle": "story_and_tone_first",
            "topic_palette_family": signature["palette_family"],
            "signature": signature,
            "recent_mode_counts": recent_counts,
        },
        "random_seed": _stable_seed(str(job["job_id"])),
        "narration_file": str(Path(job["narration_file"]).resolve()),
        "shots": output,
        "optional_polish_targets": [s["shot_id"] for s in polish],
        "human_polish_targets": [s["shot_id"] for s in polish],
        "workflow": "assistant_editor_handoff",
    }


def _caption_position(brand: dict, mode: str) -> str:
    """Keep captions in brand-approved safe zones; reserve center for intentional emphasis."""
    if mode == "minimalist_text":
        return "center_emphasis"
    if mode in {"document_focus", "animated_chart", "split_screen"}:
        return "top_safe" if "top_safe" in brand.get("caption_positions", []) else brand["caption_position"]
    return brand["caption_position"]


def _palette_family(palette: dict) -> str:
    for name, known in PALETTES.items():
        if palette.get("accent", "").lower() == known["accent"].lower():
            return name
    return "custom"


def _editor_note(mode: str, segment: dict) -> str:
    notes = {
        "minimalist_text": "Drop B-roll. Dark field, single gold sentence, slow deliberate push; leave breathing room.",
        "split_screen": "Use 2–3 panels only when facts compare; stagger panel entrances and keep labels readable.",
        "animated_chart": "Use full-frame chart; animate only data supported by the research record.",
        "document_focus": "Use a verified document image; add paper depth, angled camera, and a restrained highlight sweep.",
        "animated_map": "Trace only verified routes/places; let the route reveal match narration.",
        "parallax_portrait": "Separate portrait, mid-ground props, and smoky background into depth layers; subtle drift.",
        "kinetic_fact": "Emphasise a short phrase or number; avoid repeating the same text animation in adjacent shots.",
        "cinematic_broll": "Use selected footage, motivated movement, and a corner statistic only when it adds information.",
    }
    note = notes.get(mode, "Choose a visual that directly supports this narration beat.")
    if segment.get("visual_note"):
        note += " " + str(segment["visual_note"])
    return note


def prepare_assets_before_edit(job: dict, job_path: Path, report_path: Path) -> list[dict[str, Any]]:
    """Resolve missing manifest items, then report every item required by edit."""
    video_suffixes = {".mp4", ".webm", ".ogv", ".mov", ".mkv", ".mpeg", ".mpg"}

    def usable(path: Path | None, category: str | None) -> bool:
        if not path or not path.is_file() or path.stat().st_size <= 0:
            return False
        return category != "video" or path.suffix.lower() in video_suffixes

    assets_root = Path(job.get("assets_root") or job_path.parent.parent.parent / "assets").expanduser().resolve()
    requests_to_retry = []
    rows = job.get("asset_requirements", [])
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        selected = row.get("selected") or {}
        path = Path(selected.get("path", "")) if selected.get("path") else None
        if usable(path, row.get("category")):
            continue
        category = row.get("category")
        query = str(row.get("query", "")).strip()
        if category in {"picture", "video", "music", "sfx"} and query:
            requests_to_retry.append({key: row[key] for key in ("asset_id", "query", "category", "purpose", "segment", "suggested_sources", "max_duration_seconds", "parallax_role") if key in row})

    if requests_to_retry:
        # Load credentials without prompting for unrelated providers at edit time.
        try:
            from workspace_setup import configure_api_keys
            configure_api_keys(assets_root.parent, required_keys=(), interactive=False)
            from media_library import MediaLibrary, acquire_asset_requests
            library = MediaLibrary(assets_root)
            seed_root = assets_root.parent / "audio_library_seed"
            for category, folder in (("music", "background_music"), ("sfx", "sfx")):
                seed_folder = seed_root / folder
                if seed_folder.is_dir():
                    library.import_audio_library_folder(seed_folder, category)
            acquired = acquire_asset_requests({"assets": requests_to_retry}, library)
            by_query = {str(item.get("query", "")).strip().lower(): item for item in acquired.get("assets", [])}
            for row in rows:
                if row.get("selected", {}).get("path") and Path(row["selected"]["path"]).is_file():
                    continue
                result = by_query.get(str(row.get("query", "")).strip().lower())
                if result and result.get("selected", {}).get("path"):
                    row["selected"] = result["selected"]
                    row["status"] = result.get("status", "downloaded_or_generated")
            job["assets"] = job.get("assets", {})
            by_query_existing = {str(row.get("query", "")).strip().lower(): row for row in rows}
            for segment_index, segment in enumerate(job.get("segments", []), 1):
                ids = list(segment.get("asset_ids", []))
                for query in segment.get("asset_queries", []):
                    row = by_query_existing.get(str(query).strip().lower())
                    picked = (row or {}).get("selected") or {}
                    media_id = picked.get("asset_id")
                    if media_id and Path(picked.get("path", "")).is_file() and media_id not in ids:
                        ids.append(media_id)
                for row in rows:
                    if int(row.get("segment", 0) or 0) != segment_index or row.get("parallax_role") not in {"portrait_foreground", "parallax_background"}:
                        continue
                    picked = row.get("selected") or {}
                    media_id = picked.get("asset_id")
                    if media_id and Path(picked.get("path", "")).is_file():
                        media_id = str(media_id)
                        if media_id not in ids:
                            ids.append(media_id)
                        if row.get("parallax_role") == "portrait_foreground":
                            segment["parallax_foreground_asset_id"] = media_id
                segment["asset_ids"] = ids
            for row in rows:
                picked = row.get("selected") or {}
                media_id, path = picked.get("asset_id"), picked.get("path")
                if not media_id or not path or not Path(path).is_file():
                    continue
                category = picked.get("category") or row.get("category")
                kind = {"picture": "image", "generated": "image", "video": "video", "music": "audio", "sfx": "audio"}.get(category, category)
                job["assets"][str(media_id)] = {
                    "path": path, "kind": kind, "category": category,
                    "duration": float(picked.get("duration") or 0), "rights_status": "licensed" if picked.get("license") else "unknown",
                    "license": picked.get("license"), "creator": picked.get("creator"),
                    "source": picked.get("source"), "source_url": picked.get("source_url"),
                }
        except Exception as exc:
            job["asset_resolution_error"] = str(exc)

    readiness = []
    covered = set()
    for row in rows:
        picked = row.get("selected") or {}
        path_value = picked.get("path")
        path_obj = Path(path_value) if path_value else None
        exists = usable(path_obj, row.get("category"))
        covered.add(str(row.get("query", "")).strip().lower())
        readiness.append({
            "asset_id": row.get("asset_id"), "query": row.get("query"),
            "category": row.get("category"), "purpose": row.get("purpose"),
            "path": path_value, "source": picked.get("source"), "license": picked.get("license"),
            "status": "ready" if exists else ("wrong_media_type" if path_obj and path_obj.is_file() and row.get("category") == "video" else "missing"),
        })
    for asset_id in sorted({str(value) for segment in job.get("segments", []) for value in segment.get("asset_ids", [])}):
        asset = job.get("assets", {}).get(asset_id)
        path_value = (asset or {}).get("path")
        exists = bool(path_value and Path(path_value).is_file() and Path(path_value).stat().st_size > 0)
        if not asset:
            status = "missing_manifest_entry"
        elif not exists:
            status = "wrong_media_type" if path_value and Path(path_value).is_file() and asset.get("kind") == "video" else "missing_file"
        elif asset.get("kind") == "video" and Path(path_value).suffix.lower() not in video_suffixes:
            status = "wrong_media_type"
        elif asset.get("kind") == "video" and float(asset.get("duration", 0) or 0) <= 0:
            status = "missing_duration"
        else:
            status = "ready"
        readiness.append({"asset_id": asset_id, "query": asset.get("title") if asset else None,
                          "category": asset.get("kind") if asset else "visual", "path": path_value, "status": status})
    for index, segment in enumerate(job.get("segments", []), 1):
        for query in segment.get("asset_queries", []):
            if str(query).strip().lower() not in covered:
                readiness.append({"asset_id": None, "query": query, "category": "visual", "purpose": f"segment {index}", "path": None, "status": "missing_request"})
    if job.get("captions_required"):
        caption_path = Path(job.get("captions_file", "")) if job.get("captions_file") else None
        readiness.append({"asset_id": "captions", "category": "subtitle", "path": str(caption_path) if caption_path else None,
                          "status": "ready" if caption_path and caption_path.is_file() else "missing_file"})
    report = {
        "job_id": job.get("job_id"), "status": "ready" if all(row["status"] == "ready" for row in readiness) else "blocked",
        "assets_root": str(assets_root), "items": readiness,
        "resolution_error": job.get("asset_resolution_error"),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    missing = [row for row in readiness if row["status"] != "ready"]
    if missing:
        summary = ", ".join(f"{item.get('category')}:{item.get('query')}" for item in missing[:8])
        raise RuntimeError(f"Pre-edit asset check blocked the edit; see {report_path} ({summary})")
    return readiness


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_editor_notes(path: Path, plan: dict) -> None:
    lines = [
        f"# Edit handoff: {plan['topic']}",
        "",
        "This is an editable assistant-editor timeline. It is not a final rendered MP4.",
        "Import the FCPXML into Final Cut Pro or a compatible editor. The listed polish targets are optional editorial focus points, not a required review gate.",
        "",
        f"- Planned duration: {plan['duration']:.1f} seconds",
        f"- Canvas: {plan['canvas']['width']}×{plan['canvas']['height']} at {plan['canvas']['fps']} fps",
        f"- Palette: {plan['palette']}",
        f"- Story-led style selection: `{plan['episode_style']['selection_principle']}`",
        f"- Channel identity: {plan['visual_brand']}",
        f"- Recent style history considered: `{plan['episode_style']['recent_mode_counts']}`",
        "- Source and license metadata are preserved in the asset manifest.",
        "",
        "## Optional polish focus points",
        "",
    ]
    for shot_id in plan["human_polish_targets"]:
        shot = next(item for item in plan["shots"] if item["shot_id"] == shot_id)
        lines.append(f"- `{shot_id}` at {shot['start']:.2f}s: {shot['tone']} / {shot['visual_mode']} — {shot['editor_note']}")
    lines += ["", "## Full shot list", "", "| In | Duration | Tone | Mode | Evidence / rationale | Captions | Asset | Editor direction |", "|---:|---:|---|---|---|---|---|---|"]
    for s in plan["shots"]:
        evidence = (s.get("visual_evidence") or s.get("visual_rationale") or "").replace("|", "\\|")
        lines.append(f"| {s['start']:.2f}s | {s['duration']:.2f}s | {s['tone']} | {s['visual_mode']} | {evidence} | {s['caption_position']} / {s['type_treatment']} | {s['asset_id'] or 'placeholder'} | {s['editor_note']} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_fcpxml(path: Path, plan: dict, assets: dict[str, dict]) -> None:
    """Write an FCPXML rough cut with narration as the primary storyline.

    Visual modes, camera values, transitions, and SFX cues remain in the adjacent
    JSON/Markdown handoff because the editor should be able to adjust them freely.
    """
    root = ET.Element("fcpxml", {"version": "1.10"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", {
        "id": "r1", "name": "FFVideoFormat1080p30", "frameDuration": "1/30s",
        "width": str(plan["canvas"]["width"]), "height": str(plan["canvas"]["height"]),
    })
    all_paths: dict[str, tuple[Path, dict]] = {}
    narr_path = Path(plan["narration_file"])
    all_paths["__narration__"] = (narr_path, {"duration": plan["duration"], "hasVideo": "0", "hasAudio": "1"})
    for shot in plan["shots"]:
        if shot.get("asset_id") and shot.get("asset_path"):
            source = assets[shot["asset_id"]]
            p = Path(source["path"]).resolve()
            all_paths[shot["asset_id"]] = (p, {
                "duration": float(source.get("duration", shot["duration"])),
                "hasVideo": "1", "hasAudio": "0",
            })

    refs = {}
    rid = 2
    for asset_id, (asset_path, info) in all_paths.items():
        ref = f"r{rid}"
        rid += 1
        refs[asset_id] = ref
        asset_el = ET.SubElement(resources, "asset", {
            "id": ref,
            "name": asset_path.name,
            "start": "0s",
            "duration": _seconds(float(info["duration"])),
            "hasVideo": info["hasVideo"],
            "hasAudio": info["hasAudio"],
            "audioSources": "1" if info["hasAudio"] == "1" else "0",
            "audioChannels": "2" if info["hasAudio"] == "1" else "0",
            "audioRate": "48000",
        })
        media_rep = ET.SubElement(asset_el, "media-rep", {"kind": "original-media", "src": _file_url(asset_path)})
        del media_rep

    project = ET.SubElement(root, "project", {"name": plan["topic"][:80]})
    seq = ET.SubElement(project, "sequence", {
        "format": "r1", "duration": _seconds(plan["duration"]), "tcStart": "0s",
        "tcFormat": "NDF", "audioLayout": "stereo", "audioRate": "48k",
    })
    spine = ET.SubElement(seq, "spine")
    narration = ET.SubElement(spine, "asset-clip", {
        "name": "Narration — primary storyline", "ref": refs["__narration__"],
        "start": "0s", "duration": _seconds(plan["duration"]), "audioRole": "dialogue",
    })
    for shot in plan["shots"]:
        asset_id = shot.get("asset_id")
        if not asset_id or asset_id not in refs:
            continue
        clip = ET.SubElement(narration, "asset-clip", {
            "name": f"{shot['shot_id']} — {shot['visual_mode']}",
            "ref": refs[asset_id], "offset": _seconds(shot["start"]),
            "start": "0s", "duration": _seconds(shot["duration"]),
        })
        camera = shot["camera"]
        transform = ET.SubElement(clip, "adjust-transform", {
            "position": "0 0",
            "scale": f"{camera['scale_start']} {camera['scale_start']}",
            "rotation": str(camera["rotation_degrees"]),
        })
        pos_param = ET.SubElement(transform, "param", {"name": "position"})
        pos_anim = ET.SubElement(pos_param, "keyframeAnimation")
        point_count = min(len(camera["x_noise"]), len(camera["y_noise"]))
        for point in range(point_count):
            local_time = shot["duration"] * point / max(1, point_count - 1)
            ET.SubElement(pos_anim, "keyframe", {
                "time": _fraction_time(local_time),
                "value": f"{camera['x_noise'][point]} {camera['y_noise'][point]}",
                "interp": "linear",
                "curve": "smooth",
            })
        scale_param = ET.SubElement(transform, "param", {"name": "scale"})
        scale_anim = ET.SubElement(scale_param, "keyframeAnimation")
        ET.SubElement(scale_anim, "keyframe", {
            "time": "0s", "value": f"{camera['scale_start']} {camera['scale_start']}",
            "interp": "easeIn", "curve": "smooth",
        })
        ET.SubElement(scale_anim, "keyframe", {
            "time": _fraction_time(shot["duration"]),
            "value": f"{camera['scale_end']} {camera['scale_end']}",
            "interp": "easeOut", "curve": "smooth",
        })
        ET.SubElement(clip, "marker", {
            "start": "0s", "duration": "1/30s",
            "value": f"{shot['visual_mode']} | {shot['tone']} | {shot['editor_note'][:180]}",
        })
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _load_style_history(path: Path | None) -> list[dict]:
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return payload
    return payload.get("videos", []) if isinstance(payload, dict) else []


def _save_style_history(path: Path, history: list[dict], plan: dict) -> None:
    from datetime import datetime, timezone

    history = [row for row in history if row.get("job_id") != plan["job_id"]]
    history.append({
        "job_id": plan["job_id"],
        "channel_key": plan["channel_key"],
        "topic": plan["topic"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": plan["episode_style"]["signature"],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps({"version": 1, "videos": history[-100:]}, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def build_package(job_path: Path, output_dir: Path, style_history_path: Path | None = None) -> dict:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    prepare_assets_before_edit(job, job_path, output_dir / "edit_readiness.json")
    # Stop on unresolved ids rather than silently creating visual placeholders.
    referenced_ids = {str(asset_id) for segment in job.get("segments", []) for asset_id in segment.get("asset_ids", [])}
    missing_ids = referenced_ids - set(job.get("assets", {}))
    if missing_ids:
        raise ValueError("Pre-edit asset check found unresolved asset IDs: " + ", ".join(sorted(missing_ids)))
    # All present file paths are verified before handing off the timeline.
    for asset_id, asset in job.get("assets", {}).items():
        asset_path = Path(asset.get("path", ""))
        if not asset_path.is_file():
            raise FileNotFoundError(f"Asset {asset_id} is missing: {asset_path}")
        if asset.get("kind") == "video" and float(asset.get("duration", 0)) <= 0:
            raise ValueError(f"Asset {asset_id} must include its source duration")
    narration_path = Path(job["narration_file"])
    if not narration_path.is_file():
        raise FileNotFoundError(f"Narration file is missing: {narration_path}")

    if style_history_path is None:
        parents = job_path.resolve().parents
        style_history_path = parents[2] / "style_history.json" if len(parents) > 2 else None
    history = _load_style_history(style_history_path)
    plan = build_edit_plan(job, history)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "timeline.json", plan)
    write_editor_notes(output_dir / "EDIT_NOTES.md", plan)
    write_fcpxml(output_dir / "assistant_edit.fcpxml", plan, job["assets"])
    # Copy the input manifest into the package for provenance and repeatability.
    write_json(output_dir / "job_manifest.json", job)
    if style_history_path is not None:
        _save_style_history(style_history_path, history, plan)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a varied, editable video timeline package")
    parser.add_argument("job_json", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--style-history", type=Path, help="Cross-video style history JSON (defaults to workspace/style_history.json)")
    args = parser.parse_args()
    plan = build_package(args.job_json, args.out, args.style_history)
    print(f"Created assistant-editor package: {args.out.resolve()}")
    print(f"Shots: {len(plan['shots'])}; optional polish focus points: {len(plan['optional_polish_targets'])}")
    print("No final MP4 was rendered or uploaded.")


if __name__ == "__main__":
    main()
