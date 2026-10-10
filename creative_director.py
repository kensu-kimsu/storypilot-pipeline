"""Create a creatively varied, editable documentary timeline package.

This module plans and exports an edit. It deliberately does not render or upload a
finished video: the FCPXML/JSON outputs are the assistant-editor handoff for a
human polish pass in Final Cut Pro or a compatible editor.
"""
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
        and assets[a].get("rights_status") in {"owned", "licensed", "public_domain", "permission"}
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


def build_edit_plan(job: dict) -> dict:
    required = {"job_id", "topic", "narration_file", "narration_duration", "segments", "assets"}
    missing = sorted(required - set(job))
    if missing:
        raise ValueError(f"Job is missing required fields: {', '.join(missing)}")
    if not job["segments"]:
        raise ValueError("At least one narration segment is required")

    rng = random.Random(_stable_seed(str(job["job_id"])))
    assets = job["assets"]
    palette = choose_palette(job["topic"], job.get("palette"))
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
        if previous_mode in choices and len(choices) > 1:
            choices.remove(previous_mode)
        mode = rng.choice(choices)
        previous_mode = mode
        low, high = _duration_range(tone, energy)
        usable_assets = _candidate_asset_ids(segment, assets, mode)
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
            if asset_id and asset.get("rights_status") not in {"owned", "licensed", "public_domain", "permission"}:
                # Rights must be known before media gets into the editor timeline.
                asset_id = None
                asset = {}
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
                "asset_id": asset_id,
                "asset_path": asset.get("path"),
                "asset_kind": asset.get("kind", "generated_placeholder" if asset_id is None else "video"),
                "overlay_text": segment.get("overlay_text", ""),
                "palette": palette,
                "camera": _camera_motion(rng, shot_mode),
                "transition_out": rng.choice(["hard_cut", "short_dissolve", "match_cut", "whip_pan"]),
                "sfx_cue": rng.choice(segment.get("sfx_candidates", ["paper_rustle", "camera_shutter", "low_impact", "none"])),
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
    return {
        "job_id": job["job_id"],
        "topic": job["topic"],
        "format": job.get("format", "long_form"),
        "canvas": job.get("canvas", {"width": 1920, "height": 1080, "fps": 30}),
        "duration": duration_total,
        "palette": palette,
        "random_seed": _stable_seed(str(job["job_id"])),
        "narration_file": str(Path(job["narration_file"]).resolve()),
        "shots": output,
        "human_polish_targets": [s["shot_id"] for s in polish],
        "workflow": "assistant_editor_handoff",
    }


def _editor_note(mode: str, segment: dict) -> str:
    notes = {
        "minimalist_text": "Drop B-roll. Dark field, single gold sentence, slow deliberate push; leave breathing room.",
        "split_screen": "Use 2–3 panels only when facts compare; stagger panel entrances and keep labels readable.",
        "animated_chart": "Use full-frame chart; animate only data supported by the research record.",
        "document_focus": "Use a verified document image; add paper depth, angled camera, and a restrained highlight sweep.",
        "animated_map": "Trace only verified routes/places; let the route reveal match narration.",
        "parallax_portrait": "Separate portrait, mid-ground props, and smoky background into depth layers; subtle drift.",
        "kinetic_fact": "Emphasise a short phrase or number; avoid repeating the same text animation in adjacent shots.",
        "cinematic_broll": "Use licensed/owned footage, motivated movement, and a corner statistic only when it adds information.",
    }
    note = notes.get(mode, "Choose a visual that directly supports this narration beat.")
    if segment.get("visual_note"):
        note += " " + str(segment["visual_note"])
    return note


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_editor_notes(path: Path, plan: dict) -> None:
    lines = [
        f"# Edit handoff: {plan['topic']}",
        "",
        "This is an editable assistant-editor timeline. It is not a final rendered MP4.",
        "Import the FCPXML into Final Cut Pro or a compatible editor, review the human-polish targets, make creative changes, then export the final master.",
        "",
        f"- Planned duration: {plan['duration']:.1f} seconds",
        f"- Canvas: {plan['canvas']['width']}×{plan['canvas']['height']} at {plan['canvas']['fps']} fps",
        f"- Palette: {plan['palette']}",
        f"- Stable variation seed: `{plan['random_seed']}`",
        "- Visual source rights status is required before an asset is accepted into the plan.",
        "",
        "## Priority human polish pass",
        "",
    ]
    for shot_id in plan["human_polish_targets"]:
        shot = next(item for item in plan["shots"] if item["shot_id"] == shot_id)
        lines.append(f"- `{shot_id}` at {shot['start']:.2f}s: {shot['tone']} / {shot['visual_mode']} — {shot['editor_note']}")
    lines += ["", "## Full shot list", "", "| In | Duration | Tone | Mode | Asset | Editor direction |", "|---:|---:|---|---|---|---|"]
    for s in plan["shots"]:
        lines.append(f"| {s['start']:.2f}s | {s['duration']:.2f}s | {s['tone']} | {s['visual_mode']} | {s['asset_id'] or 'placeholder'} | {s['editor_note']} |")
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


def build_package(job_path: Path, output_dir: Path) -> dict:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    # All referenced media must exist and have a positive duration before the edit
    # is handed to an editor. Unclear rights are rejected during plan generation.
    for asset_id, asset in job.get("assets", {}).items():
        asset_path = Path(asset.get("path", ""))
        if not asset_path.is_file():
            raise FileNotFoundError(f"Asset {asset_id} is missing: {asset_path}")
        if float(asset.get("duration", 0)) <= 0:
            raise ValueError(f"Asset {asset_id} must include its source duration")
    narration_path = Path(job["narration_file"])
    if not narration_path.is_file():
        raise FileNotFoundError(f"Narration file is missing: {narration_path}")

    plan = build_edit_plan(job)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "timeline.json", plan)
    write_editor_notes(output_dir / "EDIT_NOTES.md", plan)
    write_fcpxml(output_dir / "assistant_edit.fcpxml", plan, job["assets"])
    # Copy the input manifest into the package for provenance and repeatability.
    shutil.copy2(job_path, output_dir / "job_manifest.json")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a varied, editable video timeline package")
    parser.add_argument("job_json", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plan = build_package(args.job_json, args.out)
    print(f"Created assistant-editor package: {args.out.resolve()}")
    print(f"Shots: {len(plan['shots'])}; human polish targets: {len(plan['human_polish_targets'])}")
    print("No final MP4 was rendered or uploaded.")


if __name__ == "__main__":
    main()
