"""Small, factual motion-graphic overlays for the FFmpeg renderer.

The creative director supplies structured specs. This module renders only those
specs; it never invents chart values or geographic coordinates.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_overlay_sequence(
    spec: dict[str, Any],
    output_dir: Path,
    *,
    width: int,
    height: int,
    fps: int,
    duration: float,
    accent: str = "#FFD34E",
    background: str = "#080A0F",
) -> str:
    """Write transparent RGBA overlay frames and return an FFmpeg image pattern."""
    kind = spec.get("kind")
    if kind not in {"chart", "counter", "route_schematic", "geographic_map"}:
        raise ValueError(f"Unsupported motion graphic kind: {kind}")
    output_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, math.ceil(duration * fps))
    pattern = output_dir / "graphic_%05d.png"
    accent_rgb = _hex_rgb(accent, (255, 211, 78))
    bg_rgb = _hex_rgb(background, (8, 10, 15))
    title_font = _font(max(25, int(width * 0.036)), bold=True)
    label_font = _font(max(18, int(width * 0.020)))
    value_font = _font(max(19, int(width * 0.022)), bold=True)
    pad_x = int(width * 0.08)
    panel = (pad_x, int(height * 0.20), width - pad_x, int(height * 0.80))

    for frame_index in range(count):
        progress = min(1.0, frame_index / max(1, count - 1))
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rounded_rectangle(panel, radius=max(20, int(width * 0.018)), fill=(*bg_rgb, 228), outline=(*accent_rgb, 225), width=max(2, width // 500))
        title = str(spec.get("title", ""))[:90]
        draw.text((panel[0] + int(width * 0.035), panel[1] + int(height * 0.035)), title, font=title_font, fill=(255, 255, 255, 255))
        if kind == "chart":
            _draw_chart(draw, spec, progress, panel, width, height, accent_rgb, label_font, value_font)
        elif kind == "counter":
            _draw_counter(draw, spec, progress, panel, width, height, accent_rgb, value_font)
        elif kind == "route_schematic":
            _draw_route(draw, spec, progress, panel, width, height, accent_rgb, label_font)
        else:
            _draw_geographic_map_frame(spec, image, frame_index, count, width, height, accent_rgb, label_font, title_font, temp_root=output_dir)
        image.save(pattern.with_name(f"graphic_{frame_index:05d}.png"), optimize=True)
    return str(pattern)


def _draw_chart(draw, spec, progress, panel, width, height, accent, label_font, value_font):
    items = spec.get("items", [])
    if not items:
        return
    left = panel[0] + int(width * 0.05)
    right = panel[2] - int(width * 0.05)
    top = panel[1] + int(height * 0.16)
    bottom = panel[3] - int(height * 0.08)
    chart_height = bottom - top
    row_height = chart_height / len(items)
    max_value = max(float(item["numeric_value"]) for item in items) or 1.0
    label_width = int(width * 0.23)
    value_width = int(width * 0.15)
    bar_left = left + label_width
    bar_right = right - value_width
    bar_max = max(1, bar_right - bar_left)
    for index, item in enumerate(items):
        y = top + row_height * index
        local_progress = max(0.0, min(1.0, (progress - index * 0.12) / 0.58))
        label = str(item.get("label", ""))[:28]
        value_text = str(item.get("value", ""))[:24]
        draw.text((left, y + row_height * 0.23), label, font=label_font, fill=(245, 246, 248, 255))
        draw.rounded_rectangle((bar_left, y + row_height * 0.27, bar_right, y + row_height * 0.73), radius=8, fill=(255, 255, 255, 32))
        bar_width = int(bar_max * float(item["numeric_value"]) / max_value * local_progress)
        if bar_width:
            draw.rounded_rectangle((bar_left, y + row_height * 0.27, bar_left + bar_width, y + row_height * 0.73), radius=8, fill=(*accent, 255))
        draw.text((bar_right + int(width * 0.015), y + row_height * 0.22), value_text, font=value_font, fill=(255, 255, 255, 255))


def _draw_counter(draw, spec, progress, panel, width, height, accent, value_font):
    raw = str(spec.get("value", ""))
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", raw)
    if not match:
        return
    target = float(match.group(0).replace(",", ""))
    digits = f"{target * progress:,.0f}" if target.is_integer() else f"{target * progress:,.1f}"
    shown = raw[:match.start()] + digits + raw[match.end():]
    box = draw.textbbox((0, 0), shown, font=value_font)
    x = (width - (box[2] - box[0])) // 2
    y = panel[1] + int((panel[3] - panel[1]) * 0.43)
    draw.text((x, y), shown, font=value_font, fill=(*accent, 255), stroke_width=max(1, width // 800), stroke_fill=(0, 0, 0, 180))


def _draw_route(draw, spec, progress, panel, width, height, accent, label_font):
    places = spec.get("places", [])
    if len(places) < 2:
        return
    left = panel[0] + int(width * 0.10)
    right = panel[2] - int(width * 0.10)
    y = panel[1] + int((panel[3] - panel[1]) * 0.55)
    points = []
    for index in range(len(places)):
        x = left + (right - left) * index / (len(places) - 1)
        offset = int(height * 0.035) * (1 if index % 2 else -1)
        points.append((int(x), y + offset))
    draw.text((left, panel[1] + int(height * 0.16)), "SCHEMATIC ROUTE · NOT TO SCALE", font=label_font, fill=(215, 222, 233, 230))
    completed_edges = progress * (len(points) - 1)
    for index in range(len(points) - 1):
        edge_progress = max(0.0, min(1.0, completed_edges - index))
        if edge_progress <= 0:
            continue
        start, end = points[index], points[index + 1]
        current = (int(start[0] + (end[0] - start[0]) * edge_progress), int(start[1] + (end[1] - start[1]) * edge_progress))
        draw.line([start, current], fill=(*accent, 245), width=max(4, width // 200))
    visible_nodes = min(len(points), int(progress * (len(points) - 1)) + 1)
    for index, point in enumerate(points[:visible_nodes]):
        radius = max(8, width // 120)
        draw.ellipse((point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius), fill=(*accent, 255), outline=(255, 255, 255, 245), width=max(2, width // 600))
        name = str(places[index].get("name", ""))[:22]
        box = draw.textbbox((0, 0), name, font=label_font)
        draw.text((point[0] - (box[2] - box[0]) // 2, point[1] + radius + 8), name, font=label_font, fill=(255, 255, 255, 255))


def _draw_geographic_map_frame(spec, image, frame_index, count, width, height, accent, label_font, title_font, *, temp_root):
    """Render a Cartopy basemap once, then progressively draw verified locations."""
    cache = temp_root / "geographic_map_cache.json"
    if cache.exists():
        import json
        data = json.loads(cache.read_text(encoding="utf-8"))
        background_path = temp_root / data["background"]
        pixels = data["pixels"]
    else:
        try:
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature
            import os
            os.environ.setdefault("MPLCONFIGDIR", str(temp_root / ".mplconfig"))
            (temp_root / ".mplconfig").mkdir(exist_ok=True)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise RuntimeError("Geographic maps require optional dependencies in requirements-maps.txt (Cartopy and Matplotlib)") from exc
        points = spec["points"]
        longitudes = [float(point["longitude"]) for point in points]
        latitudes = [float(point["latitude"]) for point in points]
        lon_span = max(max(longitudes) - min(longitudes), 8.0)
        lat_span = max(max(latitudes) - min(latitudes), 6.0)
        lon_pad = min(35.0, lon_span * 0.24)
        lat_pad = min(25.0, lat_span * 0.28)
        extent = [max(-180, min(longitudes) - lon_pad), min(180, max(longitudes) + lon_pad), max(-85, min(latitudes) - lat_pad), min(85, max(latitudes) + lat_pad)]
        dpi = 100
        fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="#080A0F")
        ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree())
        ax.set_facecolor("#101720")
        ax.set_extent(extent, crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.OCEAN, facecolor="#101720", zorder=0)
        ax.add_feature(cfeature.LAND, facecolor="#29333B", edgecolor="#56616A", linewidth=0.35, zorder=1)
        ax.add_feature(cfeature.BORDERS, edgecolor="#6D7880", linewidth=0.35, zorder=2)
        ax.gridlines(crs=ccrs.PlateCarree(), color="#65727D", alpha=0.25, linewidth=0.45, linestyle=":", draw_labels=False, zorder=3)
        pixel_locations = []
        for point in points:
            px, py = ax.transData.transform((float(point["longitude"]), float(point["latitude"])))
            pixel_locations.append([round(px), round(height - py)])
        background_path = temp_root / "geographic_map_base.png"
        fig.savefig(background_path, dpi=dpi, facecolor=fig.get_facecolor())
        plt.close(fig)
        import json
        pixels = pixel_locations
        cache.write_text(json.dumps({"background": background_path.name, "pixels": pixels}), encoding="utf-8")
    from PIL import ImageDraw
    with Image.open(background_path) as base:
        map_image = base.convert("RGBA").resize((width, height))
    draw = ImageDraw.Draw(map_image, "RGBA")
    title = str(spec.get("title", "Verified locations"))[:90]
    draw.rounded_rectangle((int(width * .035), int(height * .035), int(width * .63), int(height * .14)), radius=max(8, width // 120), fill=(5, 8, 12, 210))
    draw.text((int(width * .055), int(height * .06)), title, font=title_font, fill=(255, 255, 255, 255))
    draw.text((int(width * .055), int(height * .155)), "SCHEMATIC ROUTE · PROJECTION DISTORTS SCALE", font=label_font, fill=(220, 226, 232, 235))
    locations = spec["points"]
    reveal = max(1, math.ceil((frame_index + 1) / max(1, count) * len(locations)))
    points_px = [tuple(point) for point in pixels]
    for index in range(1, reveal):
        draw.line([points_px[index - 1], points_px[index]], fill=(*accent, 235), width=max(3, width // 300))
    radius = max(6, width // 180)
    for index, point in enumerate(locations[:reveal]):
        x, y = points_px[index]
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(*accent, 255), outline=(255,255,255,255), width=max(1,width//700))
        draw.text((x + radius + 5, y - radius - 2), str(point["name"])[:24], font=label_font, fill=(255,255,255,255), stroke_width=2, stroke_fill=(0,0,0,210))
    image.alpha_composite(map_image)


def highlight_document_text(image_path: Path, target_text: str, output_path: Path) -> Path:
    """OCR an exact phrase, highlight its word boxes, and fail if OCR cannot verify it."""
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as exc:
        raise RuntimeError("Document highlighting requires pytesseract and the Tesseract OCR executable") from exc
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    data = pytesseract.image_to_data(image, lang="eng", output_type=Output.DICT)
    words = [str(value).strip() for value in data.get("text", [])]
    wanted = re.findall(r"[\w$%.-]+", target_text.casefold())
    if not wanted:
        raise ValueError("Document highlight text is empty")
    normalized = [re.findall(r"[\w$%.-]+", word.casefold()) for word in words]
    match_indices = None
    for start in range(len(words)):
        collected = []
        indices = []
        for index in range(start, len(words)):
            if normalized[index]:
                collected.extend(normalized[index])
                indices.append(index)
            if collected == wanted:
                match_indices = indices
                break
            if len(collected) > len(wanted) or collected != wanted[:len(collected)]:
                break
        if match_indices:
            break
    if not match_indices:
        raise ValueError(f"OCR could not locate the requested document phrase: {target_text!r}")
    boxes = []
    for index in match_indices:
        left, top = int(data["left"][index]), int(data["top"][index])
        right = left + int(data["width"][index])
        bottom = top + int(data["height"][index])
        boxes.append((left, top, right, bottom))
    left = max(0, min(box[0] for box in boxes) - 4)
    top = max(0, min(box[1] for box in boxes) - 3)
    right = min(image.width, max(box[2] for box in boxes) + 4)
    bottom = min(image.height, max(box[3] for box in boxes) + 3)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle((left, top, right, bottom), radius=4, fill=(255, 231, 0, 105))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image.convert("RGBA"), overlay).save(output_path, "PNG")
    return output_path


def prepare_parallax_cutout(image_path: Path, output_path: Path) -> Path:
    """Create/cache an RGBA portrait cutout with rembg; caller supplies a distinct backdrop."""
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError("2.5D portrait cutouts require the optional rembg package; install requirements-portraits.txt") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as source:
        cutout = remove(source.convert("RGBA"))
        if not isinstance(cutout, Image.Image):
            raise RuntimeError("rembg did not return an image cutout")
        if cutout.mode != "RGBA":
            cutout = cutout.convert("RGBA")
        cutout.save(output_path, "PNG", optimize=True)
    return output_path


def _hex_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value))
    if not match:
        return fallback
    rgb = match.group(1)
    return tuple(int(rgb[index:index + 2], 16) for index in (0, 2, 4))
