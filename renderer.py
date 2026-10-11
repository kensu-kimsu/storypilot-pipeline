"""Render a timed StoryPilot edit to an MP4 with FFmpeg; never uploads it."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from motion_graphics import highlight_document_text, prepare_parallax_cutout, render_overlay_sequence
from prepost_pipeline import validate_graphic_spec

TRANSITIONS = {
    "hard_cut": "fade",
    "short_dissolve": "fade",
    "match_cut": "smoothleft",
    "whip_pan": "wipeleft",
}


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=capture)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required program was not found: {command[0]}. Install FFmpeg and ensure ffmpeg/ffprobe are on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"Command failed ({command[0]}): {detail[-3500:]}") from exc


def _probe(path: Path) -> dict:
    result = _run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], capture=True)
    return json.loads(result.stdout)


def _filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")


def _ass_time(seconds: float) -> str:
    value = max(0.0, seconds)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    whole = int(value % 60)
    centiseconds = int(round((value - math.floor(value)) * 100))
    if centiseconds == 100:
        whole += 1
        centiseconds = 0
    if whole == 60:
        minutes += 1
        whole = 0
    return f"{hours}:{minutes:02d}:{whole:02d}.{centiseconds:02d}"


def _srt_cues(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues = []
    pattern = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    for block in blocks:
        lines = block.splitlines()
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        points = pattern.findall(timing)
        if len(points) != 2:
            continue
        values = [int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in points]
        body_index = lines.index(timing) + 1
        body = " ".join(line.strip() for line in lines[body_index:] if line.strip())
        if body:
            cues.append({"start": values[0], "end": values[1], "text": body})
    return cues


def _ass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _color_ass(value: str) -> str:
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value or "")
    if not match:
        return "&H0000D7FF"
    rgb = match.group(1)
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def _write_ass(captions_path: Path, output_path: Path, plan: dict) -> None:
    width = int(plan["canvas"]["width"])
    height = int(plan["canvas"]["height"])
    brand = plan.get("visual_brand", {})
    font = str(brand.get("primary_font", "Arial")).replace(",", "")
    accent = _color_ass(str(brand.get("brand_accent", "#FFD34E")))
    size = max(32, int(width * 0.047))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{size},&H00FFFFFF,{accent},&H00101010,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,48,48,50,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    shots = plan.get("shots", [])
    for cue in _srt_cues(captions_path):
        shot = next((row for row in shots if row["start"] <= cue["start"] < row["end"]), None)
        position = (shot or {}).get("caption_position", "lower_third")
        if position == "top_safe":
            alignment, y = 8, int(height * 0.12)
        elif position == "center_emphasis":
            alignment, y = 5, int(height * 0.73)
        else:
            alignment, y = 2, int(height * 0.88)
        override = f"{{\\an{alignment}\\pos({width // 2},{y})}}"
        lines.append(f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},Default,,0,0,0,,{override}{_ass_escape(cue['text'])}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _wrap_overlay(text: str, width: int, fontsize: int) -> str:
    max_chars = max(12, int(width / max(1, fontsize * 0.58)))
    words = text.split()
    rows: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            rows.append(current)
            current = word
        else:
            current = candidate
    if current:
        rows.append(current)
    return "\n".join(rows)


def _find_font(family: str, weight: str) -> Path | None:
    explicit = Path(family).expanduser()
    if explicit.is_file():
        return explicit
    name = re.sub(r"[^A-Za-z0-9]", "", family).lower()
    candidates = []
    if os.name == "nt":
        candidates.extend([Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / f"{family}.ttf", Path("C:/Windows/Fonts/arialbd.ttf" if weight == "bold" else "C:/Windows/Fonts/arial.ttf")])
    candidates.extend([
        Path(f"/usr/share/fonts/truetype/msttcorefonts/{family}{' Bold' if weight == 'bold' else ''}.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if weight == "bold" else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if weight == "bold" else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    font_roots = [Path("/usr/share/fonts"), Path.home() / ".fonts"]
    if os.name == "nt":
        font_roots.insert(0, Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
    for root in font_roots:
        if root.exists():
            matches = list(root.rglob("*.ttf"))
            match = next((item for item in matches if name and name in re.sub(r"[^A-Za-z0-9]", "", item.stem).lower()), None)
            if match:
                return match
    return None


def _write_overlay_text(path: Path, text: str, width: int, fontsize: int) -> None:
    path.write_text(_wrap_overlay(text, width, fontsize), encoding="utf-8")


def _encoder_args(encoder: str) -> list[str]:
    if encoder == "nvenc":
        available = _run(["ffmpeg", "-hide_banner", "-encoders"], capture=True).stdout
        if "h264_nvenc" not in available:
            raise RuntimeError("FFmpeg does not include h264_nvenc. Choose --encoder cpu or install an FFmpeg build with NVENC.")
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    if encoder not in {"auto", "cpu"}:
        raise ValueError("Encoder must be auto, cpu, or nvenc")
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def _audio_events(job: dict, plan: dict) -> tuple[list[dict], list[dict]]:
    assets = job.get("assets", {})
    music_events: list[dict] = []
    sfx_events: list[dict] = []
    segment_rows: dict[int, list[dict]] = {}
    for shot in plan.get("shots", []):
        segment_rows.setdefault(int(shot.get("segment_index", 0)), []).append(shot)
    for index, shots in segment_rows.items():
        start = min(float(shot["start"]) for shot in shots)
        end = max(float(shot["end"]) for shot in shots)
        music_ids = shots[0].get("music_asset_ids", [])
        music_id = next((asset_id for asset_id in music_ids if asset_id in assets and Path(assets[asset_id].get("path", "")).is_file()), None)
        if music_id:
            music_events.append({"asset_id": music_id, "start": start, "duration": end - start})
    last_sfx_time = -10.0
    for shot in plan.get("shots", []):
        asset_id = shot.get("sfx_asset_id")
        if not asset_id or asset_id not in assets or not Path(assets[asset_id].get("path", "")).is_file():
            continue
        if shot.get("sfx_cue") == "none" or float(shot["start"]) - last_sfx_time < 1.8:
            continue
        sfx_events.append({"asset_id": asset_id, "start": float(shot["start"]), "duration": min(4.0, float(shot["duration"]) + 1.0)})
        last_sfx_time = float(shot["start"])
    return music_events, sfx_events


def _build_audio_filters(job: dict, plan: dict, input_args: list[str], graph: list[str], narration_index: int, duration: float) -> str:
    assets = job.get("assets", {})
    music_events, sfx_events = _audio_events(job, plan)
    audio_inputs: dict[str, int] = {}
    next_input = narration_index + 1
    for event, kind in [(event, "music") for event in music_events] + [(event, "sfx") for event in sfx_events]:
        asset_id = event["asset_id"]
        if asset_id not in audio_inputs:
            path = str(Path(assets[asset_id]["path"]).resolve())
            if kind == "music":
                input_args.extend(["-stream_loop", "-1", "-i", path])
            else:
                input_args.extend(["-i", path])
            # Inputs are video sources first, then narration, then unique audio assets.
            audio_inputs[asset_id] = next_input
            next_input += 1

    graph.append(f"[{narration_index}:a]aresample=48000,volume=1.0,apad,atrim=duration={duration:.3f}[narration]")
    mix_labels = ["[narration]"]
    per_asset_events: dict[str, list[tuple[int, dict, str]]] = {}
    for kind, events in (("music", music_events), ("sfx", sfx_events)):
        for event in events:
            per_asset_events.setdefault(event["asset_id"], []).append((audio_inputs[event["asset_id"]], event, kind))
    event_number = 0
    for asset_id, event_rows in per_asset_events.items():
        source_index = audio_inputs[asset_id]
        if len(event_rows) > 1:
            split_labels = [f"asrc{event_number}_{i}" for i in range(len(event_rows))]
            graph.append(f"[{source_index}:a]asplit={len(split_labels)}" + "".join(f"[{label}]" for label in split_labels))
        else:
            split_labels = [f"{source_index}:a"]
        for branch, (_, event, kind) in enumerate(event_rows):
            input_label = f"[{split_labels[branch]}]" if not split_labels[branch].endswith(":a") else f"[{split_labels[branch]}]"
            label = f"audioevent{event_number}"
            start = float(event["start"])
            length = max(0.1, float(event["duration"]))
            if kind == "music":
                fade_out_at = max(0.0, length - 0.4)
                volume = 0.09
                chain = f"atrim=duration={length:.3f},asetpts=PTS-STARTPTS,volume={volume},afade=t=in:st=0:d=0.35,afade=t=out:st={fade_out_at:.3f}:d=0.4,adelay={int(start * 1000)}:all=1"
            else:
                chain = f"atrim=duration={length:.3f},asetpts=PTS-STARTPTS,volume=0.28,adelay={int(start * 1000)}:all=1"
            graph.append(f"{input_label}{chain}[{label}]")
            mix_labels.append(f"[{label}]")
            event_number += 1
    graph.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=1:normalize=0,atrim=duration={duration:.3f}[aout]")
    return "[aout]"


def render_final_video(job_path: Path, output_dir: Path, plan: dict, *, encoder: str = "auto") -> dict:
    """Render one timed edit to MP4, run technical QA, and publish no uploads."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required. Install FFmpeg and add both executables to PATH.")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    shots = plan.get("shots", [])
    if not shots:
        raise ValueError("Cannot render an edit with no shots")
    width = int(plan.get("canvas", {}).get("width", 1920))
    height = int(plan.get("canvas", {}).get("height", 1080))
    fps = int(plan.get("canvas", {}).get("fps", 30))
    format_name = str(plan.get("format", "long_form"))
    if format_name == "short_form" and width < height:
        raise ValueError("Short-form render must use a portrait canvas (height greater than width)")
    if format_name == "short_form" and abs((width / height) - (9 / 16)) > 0.01:
        raise ValueError("Short-form render must use a 9:16 canvas")
    duration = float(plan["duration"])
    narration_path = Path(plan["narration_file"]).expanduser().resolve()
    if not narration_path.is_file():
        raise FileNotFoundError(f"Narration file is missing: {narration_path}")
    captions_path = Path(job.get("captions_file", "")).expanduser() if job.get("captions_file") else None
    if job.get("captions_required", True) and (not captions_path or not captions_path.is_file()):
        raise FileNotFoundError("Required English captions file is missing")

    output_dir.mkdir(parents=True, exist_ok=True)
    job_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(plan.get("job_id", "storypilot"))).strip("_")
    label = "short" if format_name == "short_form" else "long" if format_name == "long_form" else "preview"
    final_path = output_dir / f"{job_id}_{label}.mp4"
    candidate_path = output_dir / f".{job_id}_{label}.rendering.mp4"
    report_path = output_dir / f"{job_id}_{label}_qa.json"
    brand = plan.get("visual_brand", {})
    font_path = _find_font(str(brand.get("primary_font", "Arial")), str(brand.get("caption_font_weight", "bold")).lower())
    palette = plan.get("palette", {})
    background = str(palette.get("background", "#080A0F")).lstrip("#")
    accent = str(brand.get("brand_accent") or palette.get("accent", "#FFD34E")).lstrip("#")
    graph: list[str] = []
    input_args: list[str] = ["-hide_banner", "-loglevel", "error", "-y"]
    video_labels: list[str] = []
    graphic_inputs: list[tuple[int, str, float]] = []
    parallax_inputs: list[tuple[int, str, float]] = []
    rendered_graphics: list[dict[str, str]] = []
    cutout_cache: dict[str, Path] = {}
    transition_durations = [min(0.18, max(0.04, float(shot["duration"]) * 0.12)) for shot in shots[:-1]]
    transition_durations.append(0.0)

    with tempfile.TemporaryDirectory(prefix="storypilot_render_") as scratch:
        temp_root = Path(scratch)
        for index, shot in enumerate(shots):
            planned_duration = float(shot["duration"])
            clip_duration = planned_duration + transition_durations[index]
            spec = shot.get("graphic_spec")
            validated = None
            if spec:
                validated, issue = validate_graphic_spec(spec, shot, job.get("verified_claims", []))
                if issue or not validated:
                    raise ValueError(f"Graphic spec for {shot.get('shot_id', index)} failed evidence validation: {issue or 'no valid spec'}")
            asset_id = shot.get("asset_id")
            asset = job.get("assets", {}).get(asset_id, {}) if asset_id else {}
            asset_path = Path(asset.get("path", "")).expanduser().resolve() if asset.get("path") else None
            if validated and validated["kind"] == "document_highlight":
                if not asset_path or not asset_path.is_file() or asset_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
                    raise ValueError("Document highlighting requires a local document image asset on the shot")
                highlighted = temp_root / f"highlighted_document_{index:05d}.png"
                highlight_document_text(asset_path, validated["target_text"], highlighted)
                asset_path = highlighted
                rendered_graphics.append({"shot_id": str(shot.get("shot_id", index)), "kind": "document_highlight"})
            is_video = bool(asset_path and asset_path.is_file() and (asset.get("kind") == "video" or asset_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}))
            if asset_path and asset_path.is_file() and is_video:
                input_args.extend(["-stream_loop", "-1", "-i", str(asset_path)])
            elif asset_path and asset_path.is_file():
                input_args.extend(["-loop", "1", "-framerate", str(fps), "-i", str(asset_path)])
            else:
                input_args.extend(["-f", "lavfi", "-i", f"color=c=0x{background}:s={width}x{height}:r={fps}"])
            source_index = index
            target = f"v{index}"
            chain = [
                f"trim=duration={clip_duration:.3f}", "setpts=PTS-STARTPTS",
                f"scale={width + 80}:{height + 80}:force_original_aspect_ratio=increase",
                f"crop={width + 80}:{height + 80}", "setsar=1", f"fps={fps}",
                "eq=contrast=1.04:saturation=0.90:brightness=-0.015",
            ]
            if not is_video:
                chain.append(f"zoompan=z='min(zoom+0.00035,1.08)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}")
            else:
                chain.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
                chain.append(f"crop={width}:{height}")
            mode = str(shot.get("visual_mode", "cinematic_broll"))
            if mode == "minimalist_text":
                chain.append(f"drawbox=c=0x{background}@0.72:t=fill")
            overlay_text = str(shot.get("overlay_text", "")).strip()
            if overlay_text:
                text_path = temp_root / f"overlay_{index:05d}.txt"
                fontsize = max(34, int(width * (0.055 if mode == "minimalist_text" else 0.042)))
                _write_overlay_text(text_path, overlay_text, int(width * 0.82), fontsize)
                text_args = ["drawtext="]
                if font_path:
                    text_args.append(f"fontfile='{_filter_path(font_path)}'")
                else:
                    text_args.append("font='sans-serif'")
                text_args.extend([
                    f"textfile='{_filter_path(text_path)}'", f"fontcolor=0x{accent}", f"fontsize={fontsize}",
                    "line_spacing=12", "borderw=2", "bordercolor=black@0.85",
                    "x=(w-text_w)/2", "y=(h-text_h)/2" if mode == "minimalist_text" else "y=h*0.72-text_h/2",
                    "box=1", "boxcolor=black@0.38", "boxborderw=18",
                ])
                chain.append("drawtext=" + ":".join(text_args[1:]))
            chain.extend(["format=yuv420p", "settb=AVTB"])
            graph.append(f"[{source_index}:v]{','.join(chain)}[{target}]")
            if validated and validated["kind"] not in {"document_highlight"}:
                shot_palette = shot.get("palette", palette)
                sequence_dir = temp_root / f"graphic_{index:05d}"
                sequence_pattern = render_overlay_sequence(
                    validated, sequence_dir, width=width, height=height, fps=fps,
                    duration=clip_duration,
                    accent=str(shot_palette.get("accent", brand.get("brand_accent", "#FFD34E"))),
                    background=str(shot_palette.get("background", "#080A0F")),
                )
                graphic_inputs.append((index, sequence_pattern, clip_duration))
                rendered_graphics.append({"shot_id": str(shot.get("shot_id", index)), "kind": str(validated["kind"])})
            if shot.get("parallax_foreground_asset_id"):
                if shot.get("parallax_foreground_asset_id") == asset_id:
                    raise ValueError("2.5D portraits need a distinct background plate asset to avoid a doubled subject")
                foreground = job.get("assets", {}).get(shot["parallax_foreground_asset_id"], {})
                foreground_path = Path(foreground.get("path", "")).expanduser().resolve() if foreground.get("path") else None
                if not foreground_path or not foreground_path.is_file() or foreground_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
                    raise ValueError("2.5D portrait requires a local portrait image asset")
                if asset_path and foreground_path == asset_path:
                    raise ValueError("2.5D portrait foreground and background must be different files")
                cutout_key = str(foreground_path)
                cutout_path = cutout_cache.get(cutout_key)
                if cutout_path is None:
                    cutout_path = prepare_parallax_cutout(foreground_path, temp_root / f"portrait_cutout_{len(cutout_cache):05d}.png")
                    cutout_cache[cutout_key] = cutout_path
                parallax_inputs.append((index, str(cutout_path), clip_duration))
                rendered_graphics.append({"shot_id": str(shot.get("shot_id", index)), "kind": "parallax_portrait"})
            video_labels.append(target)

        for shot_index, sequence_pattern, clip_duration in graphic_inputs:
            graphic_index = len(shots) + len([item for item in graphic_inputs if item[0] < shot_index])
            input_args.extend(["-framerate", str(fps), "-i", sequence_pattern])
            overlay_label = f"g{shot_index}"
            composed_label = f"vg{shot_index}"
            graph.append(f"[{graphic_index}:v]trim=duration={clip_duration:.3f},setpts=PTS-STARTPTS,format=rgba[{overlay_label}]")
            graph.append(f"[{video_labels[shot_index]}][{overlay_label}]overlay=0:0:shortest=1:format=auto[{composed_label}]")
            video_labels[shot_index] = composed_label

        for shot_index, cutout_path, clip_duration in parallax_inputs:
            foreground_index = len(shots) + len(graphic_inputs) + len([item for item in parallax_inputs if item[0] < shot_index])
            input_args.extend(["-loop", "1", "-framerate", str(fps), "-i", cutout_path])
            foreground_label = f"p{shot_index}"
            composed_label = f"vp{shot_index}"
            # Foreground has a gentle independent drift/scale over a separate selected background plate.
            graph.append(f"[{foreground_index}:v]trim=duration={clip_duration:.3f},setpts=PTS-STARTPTS,scale={int(width*0.58)}:{int(height*0.86)}:force_original_aspect_ratio=decrease,format=rgba,zoompan=z='min(zoom+0.00012,1.025)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},format=rgba[{foreground_label}]")
            graph.append(f"[{video_labels[shot_index]}][{foreground_label}]overlay=x=(W-w)/2:y=H-h:shortest=1:format=auto[{composed_label}]")
            video_labels[shot_index] = composed_label

        narration_index = len(shots) + len(graphic_inputs) + len(parallax_inputs)
        input_args.extend(["-i", str(narration_path)])
        audio_filter = _build_audio_filters(job, plan, input_args, graph, narration_index, duration)

        last_video = f"[{video_labels[0]}]"
        cumulative = 0.0
        for index in range(1, len(video_labels)):
            previous_shot = shots[index - 1]
            transition_name = str(previous_shot.get("transition_out", "short_dissolve"))
            transition = TRANSITIONS.get(transition_name, "fade")
            trans_dur = transition_durations[index - 1]
            xfade_label = f"x{index}"
            cumulative += float(previous_shot["duration"])
            graph.append(f"{last_video}[{video_labels[index]}]xfade=transition={transition}:duration={trans_dur:.3f}:offset={cumulative:.3f}[{xfade_label}]")
            last_video = f"[{xfade_label}]"

        final_video_label = "vfinal"
        video_chain = []
        if captions_path and captions_path.is_file():
            ass_path = temp_root / "captions.ass"
            _write_ass(captions_path, ass_path, plan)
            video_chain.append(f"subtitles=filename='{_filter_path(ass_path)}'")
        video_chain.append(f"trim=duration={duration:.3f}")
        video_chain.append("setpts=PTS-STARTPTS")
        graph.append(f"{last_video}{','.join(video_chain)}[{final_video_label}]")
        filter_path = temp_root / "render.filter"
        filter_path.write_text(";\n".join(graph), encoding="utf-8")
        encoder_args = _encoder_args(encoder)
        command = ["ffmpeg", *input_args, "-filter_complex_script", str(filter_path),
                   "-map", f"[{final_video_label}]", "-map", audio_filter,
                   "-t", f"{duration:.3f}", "-r", str(fps), *encoder_args,
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                   "-movflags", "+faststart", str(candidate_path)]
        _run(command, capture=True)

    qa = quality_assurance(candidate_path, plan)
    qa["motion_graphics"] = rendered_graphics
    report_path.write_text(json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8")
    if qa["status"] != "passed":
        failed_path = output_dir / f"{job_id}_{label}_qa_failed.mp4"
        candidate_path.replace(failed_path)
        raise RuntimeError(f"Rendered video failed QA. See {report_path} and {failed_path}.")
    candidate_path.replace(final_path)
    qa["video_path"] = str(final_path.resolve())
    report_path.write_text(json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8")
    return qa


def quality_assurance(path: Path, plan: dict) -> dict:
    """Check container, streams, duration, size, canvas, and basic edit diversity."""
    checks: list[dict] = []
    errors: list[str] = []
    expected_duration = float(plan.get("duration", 0))
    minimum_bytes = max(2_000, int(expected_duration * 500))
    if not path.is_file() or path.stat().st_size < minimum_bytes:
        errors.append("Output MP4 is missing or implausibly small")
        return {"status": "failed", "checks": checks, "errors": errors}
    info = _probe(path)
    streams = info.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        errors.append("MP4 has no video stream")
    if not audio:
        errors.append("MP4 has no narration/audio stream")
    target_width = int(plan.get("canvas", {}).get("width", 1920))
    target_height = int(plan.get("canvas", {}).get("height", 1080))
    format_name = str(plan.get("format", "long_form"))
    if video and (int(video.get("width", 0)) != target_width or int(video.get("height", 0)) != target_height):
        errors.append(f"Canvas mismatch: expected {target_width}x{target_height}, got {video.get('width')}x{video.get('height')}")
    if format_name == "short_form" and abs((target_width / target_height) - (9 / 16)) > 0.01:
        errors.append("Short-form canvas is not 9:16")
    actual_duration = float(info.get("format", {}).get("duration", 0) or 0)
    tolerance = 0.65
    if abs(actual_duration - expected_duration) > tolerance:
        errors.append(f"Duration mismatch: expected {expected_duration:.2f}s, got {actual_duration:.2f}s")
    if format_name == "long_form" and actual_duration < 1200:
        errors.append(f"Long-form video is under 20 minutes ({actual_duration / 60:.2f} minutes)")
    if format_name == "short_form" and (actual_duration < 29.0 or actual_duration > 31.0):
        errors.append(f"Short-form video must be about 30 seconds; got {actual_duration:.2f}s")
    shots = plan.get("shots", [])
    unique_assets = {asset_id for shot in shots for asset_id in (shot.get("asset_id"), shot.get("parallax_foreground_asset_id")) if asset_id}
    unique_modes = {shot.get("visual_mode") for shot in shots}
    minimum_assets = 3 if format_name == "long_form" else 2 if format_name == "short_form" else 0
    if len(unique_assets) < minimum_assets:
        errors.append(f"Edit-diversity check found only {len(unique_assets)} unique visual assets; expected at least {minimum_assets}")
    if len(unique_modes) < 2 and len(shots) > 4:
        errors.append("Edit-diversity check found only one visual mode across a multi-shot video")
    checks.extend([
        {"name": "mp4_container", "passed": True},
        {"name": "video_stream", "passed": video is not None},
        {"name": "audio_stream", "passed": audio is not None},
        {"name": "canvas", "passed": bool(video and int(video.get("width", 0)) == target_width and int(video.get("height", 0)) == target_height), "expected": [target_width, target_height]},
        {"name": "shorts_aspect_ratio", "passed": format_name != "short_form" or abs((target_width / target_height) - (9 / 16)) <= 0.01, "expected": "9:16" if format_name == "short_form" else None},
        {"name": "duration", "passed": abs(actual_duration - expected_duration) <= tolerance and not (format_name == "long_form" and actual_duration < 1200) and not (format_name == "short_form" and not 29.0 <= actual_duration <= 31.0), "expected_seconds": expected_duration, "actual_seconds": actual_duration},
        {"name": "edit_diversity_proxy", "passed": len(unique_assets) >= minimum_assets and (len(unique_modes) >= 2 or len(shots) <= 4), "unique_assets": len(unique_assets), "unique_modes": len(unique_modes)},
    ])
    return {
        "status": "passed" if not errors else "failed",
        "video_path": str(path.resolve()),
        "duration_seconds": actual_duration,
        "file_size_bytes": path.stat().st_size,
        "canvas": {"width": target_width, "height": target_height, "fps": int(plan.get("canvas", {}).get("fps", 30))},
        "format": format_name,
        "unique_visual_assets": len(unique_assets),
        "unique_visual_modes": sorted(str(mode) for mode in unique_modes),
        "checks": checks,
        "errors": errors,
        "upload_performed": False,
    }
