"""Check or install StoryPilot's Python and external dependencies.

Examples:
    python dependency_check.py --base-dir ./production
    python dependency_check.py --stage edit --base-dir ./production
    python dependency_check.py --install --base-dir ./production
    python dependency_check.py --install --features maps,ocr --base-dir ./production

The default command only checks the current environment. Optional graphics and
browser dependencies are installed only when explicitly selected.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
CORE_REQUIREMENTS = ROOT / "requirements.txt"
FEATURE_REQUIREMENTS = {
    "maps": (ROOT / "requirements-maps.txt", ("matplotlib", "cartopy")),
    "ocr": (ROOT / "requirements-ocr.txt", ("pytesseract",)),
    "portraits": (ROOT / "requirements-portraits.txt", ("rembg",)),
    "news": (ROOT / "requirements-news.txt", ("playwright",)),
}
CORE_MODULES = ("requests", "dotenv", "PIL", "azure.cognitiveservices.speech", "ormsgpack")
STAGES = ("all", "preproduction", "narration", "edit")


def _read_secrets(base_dir: Path) -> dict[str, Any]:
    path = base_dir / "secrets.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _load_env_file(base_dir: Path) -> dict[str, str]:
    """Read simple KEY=value entries without printing or exporting values."""
    path = base_dir / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if key.strip() and value:
            values[key.strip()] = value
    return values


def _configured(name: str, secrets: dict[str, Any], env_file: dict[str, str]) -> bool:
    return bool(str(os.environ.get(name) or secrets.get(name) or env_file.get(name) or "").strip())


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _command_version(command: str, *version_args: str) -> str | None:
    try:
        result = subprocess.run(
            [command, *(version_args or ("-version",))], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).splitlines()[0].strip()


def _ffmpeg_component_available(group: str, component: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", f"-{group}"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and component in (result.stdout + result.stderr)


def _fish_server_ok(url: str) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/v1/health", timeout=4) as response:
            return 200 <= response.status < 300
    except (OSError, URLError, TimeoutError):
        return False


def _install(requirements: list[Path]) -> bool:
    ok = True
    for req in requirements:
        print(f"\nInstalling {req.name} with this Python environment...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], check=False)
        if result.returncode:
            print(f"  FAIL: pip could not install {req.name} (exit {result.returncode}).")
            ok = False
    return ok


def _check(args: argparse.Namespace) -> tuple[list[tuple[str, bool, str]], list[str]]:
    base_dir = args.base_dir.expanduser().resolve()
    findings: list[tuple[str, bool, str]] = []
    guidance: list[str] = []

    findings.append(("Python version", sys.version_info >= (3, 10), f"{platform.python_version()} (need 3.10 or newer)"))
    for module in CORE_MODULES:
        findings.append((f"Python package: {module}", _module_available(module), "installed" if _module_available(module) else "missing; run pip install -r requirements.txt"))

    for binary in ("ffmpeg", "ffprobe"):
        version = _command_version(binary)
        findings.append((binary, version is not None, version or "not found on PATH"))
    if _command_version("ffmpeg"):
        for encoder in ("libx264", "aac"):
            ok = _ffmpeg_component_available("encoders", encoder)
            findings.append((f"FFmpeg encoder: {encoder}", ok, "available" if ok else "missing from this FFmpeg build"))
        for video_filter in ("drawtext", "subtitles"):
            ok = _ffmpeg_component_available("filters", video_filter)
            findings.append((f"FFmpeg filter: {video_filter}", ok, "available" if ok else "missing from this FFmpeg build"))
    if not _command_version("ffmpeg") or not _command_version("ffprobe"):
        if os.name == "nt":
            guidance.append("Install FFmpeg on Windows (for example: winget install Gyan.FFmpeg), then open a new terminal so ffmpeg and ffprobe are on PATH.")
        elif platform.system() == "Darwin":
            guidance.append("Install FFmpeg with: brew install ffmpeg")
        else:
            guidance.append("Install FFmpeg with your system package manager (Debian/Ubuntu: sudo apt install ffmpeg).")

    for feature in sorted(args.features):
        req, modules = FEATURE_REQUIREMENTS[feature]
        for module in modules:
            ok = _module_available(module)
            findings.append((f"Optional feature {feature}: {module}", ok, "installed" if ok else f"missing; run --install --features {feature}"))
    if "ocr" in args.features:
        tesseract = _command_version("tesseract", "--version")
        findings.append(("Optional OCR executable: tesseract", tesseract is not None, tesseract or "not found on PATH; install Tesseract OCR with English language data"))
        if not tesseract:
            guidance.append("Tesseract is a separate system program and is not installed by pip. Install it and ensure the tesseract executable and English data are available on PATH.")
    if "news" in args.features:
        findings.append(("Playwright Chromium", _playwright_chromium_ok(), "launchable" if _playwright_chromium_ok() else "not installed; run python -m playwright install chromium"))

    try:
        secrets = _read_secrets(base_dir)
        env_file = _load_env_file(base_dir)
    except ValueError as exc:
        findings.append(("Local secrets/config", False, str(exc)))
        secrets, env_file = {}, {}

    if args.stage in {"all", "preproduction"}:
        openai_ok = _configured("OPENAI_API_KEY", secrets, env_file)
        findings.append(("OpenAI API key", openai_ok, "configured" if openai_ok else f"missing in environment, {base_dir / 'secrets.json'}, or {base_dir / '.env'}"))
        if not openai_ok:
            guidance.append(f"Set up the OpenAI key with: python main_pipeline.py setup-apis --base-dir \"{base_dir}\"")
        needs_youtube = not args.topic_provided and args.stage in {"all", "preproduction"}
        if needs_youtube:
            youtube_ok = _configured("YOUTUBE_API_KEY", secrets, env_file)
            findings.append(("YouTube Data API key", youtube_ok, "configured" if youtube_ok else "missing; required for topic discovery, not when a topic is supplied"))
            if not youtube_ok:
                guidance.append("Add YOUTUBE_API_KEY through setup-apis if using automatic topic discovery, or check preproduction with --topic-provided.")
        for key in ("PIXABAY_API_KEY", "FREESOUND_API_KEY"):
            configured = _configured(key, secrets, env_file)
            findings.append((f"Optional API key: {key}", True, "configured" if configured else "not configured; matching asset searches will be skipped"))

    if args.stage in {"all", "narration"}:
        provider = str(os.environ.get("NARRATION_PROVIDER") or secrets.get("NARRATION_PROVIDER") or env_file.get("NARRATION_PROVIDER") or "azure").strip().lower()
        findings.append(("Narration provider", provider in {"azure", "fish"}, provider or "unset (defaults to azure)"))
        if provider == "azure":
            for key in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"):
                ok = _configured(key, secrets, env_file)
                findings.append((f"Azure Speech: {key}", ok, "configured" if ok else "missing; required for Azure narration"))
        elif provider == "fish":
            url = str(os.environ.get("FISH_SPEECH_URL") or secrets.get("FISH_SPEECH_URL") or env_file.get("FISH_SPEECH_URL") or "http://127.0.0.1:8080").strip()
            ok = _fish_server_ok(url)
            findings.append(("Local Fish Speech server", ok, f"healthy at {url}" if ok else f"no healthy /v1/health at {url}"))
        else:
            guidance.append("Set NARRATION_PROVIDER to azure or fish.")

    # Directories are created by the normal setup command; check write access without creating files.
    if args.stage in {"all", "edit"}:
        findings.append(("Production workspace", base_dir.is_dir(), str(base_dir) if base_dir.is_dir() else "does not exist; setup-apis or preproduction will create it"))
    return findings, guidance


def _playwright_chromium_ok() -> bool:
    if not _module_available("playwright"):
        return False
    code = "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=20, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _configure_credentials(args: argparse.Namespace) -> None:
    """Prompt once for missing stage credentials and persist them in secrets.json."""
    if args.stage == "edit":
        return
    base_dir = args.base_dir.expanduser().resolve()
    try:
        secrets = _read_secrets(base_dir)
        env_file = _load_env_file(base_dir)
    except (OSError, ValueError) as exc:
        print(f"Credential setup could not read existing secrets: {exc}")
        return

    required: list[str] = []
    if args.stage in {"all", "preproduction"}:
        required.append("OPENAI_API_KEY")
        if not args.topic_provided:
            required.append("YOUTUBE_API_KEY")
    if args.stage in {"all", "narration"}:
        provider = str(
            os.environ.get("NARRATION_PROVIDER")
            or secrets.get("NARRATION_PROVIDER")
            or env_file.get("NARRATION_PROVIDER")
            or "azure"
        ).strip().lower()
        if provider == "azure":
            required.extend(("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"))

    try:
        from workspace_setup import configure_api_keys
        configure_api_keys(base_dir, required_keys=tuple(required))
        if required:
            print("Required API credentials are configured or were saved during setup.")
    except (RuntimeError, OSError) as exc:
        # Continue into the report so users get the full missing-dependency list
        # alongside actionable credential guidance.
        print(f"Credential setup needs attention: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or install StoryPilot pipeline dependencies")
    parser.add_argument("--base-dir", type=Path, default=Path("production"), help="Production workspace containing secrets.json")
    parser.add_argument("--stage", choices=STAGES, default="all", help="Limit credential checks to the requested pipeline stage")
    parser.add_argument("--topic-provided", action="store_true", help="Preproduction already has a topic, so YouTube discovery credentials are not required")
    parser.add_argument("--install", action="store_true", help="Install core Python requirements into the active Python environment")
    parser.add_argument("--features", default="", help="Comma-separated optional dependencies: maps,ocr,portraits,news (or all)")
    args = parser.parse_args()

    requested = {item.strip().lower() for item in args.features.split(",") if item.strip()}
    if "all" in requested:
        requested = set(FEATURE_REQUIREMENTS)
    unknown = requested - set(FEATURE_REQUIREMENTS)
    if unknown:
        parser.error("Unknown feature(s): " + ", ".join(sorted(unknown)))
    args.features = requested

    if args.install:
        reqs = [CORE_REQUIREMENTS]
        reqs.extend(FEATURE_REQUIREMENTS[name][0] for name in sorted(requested))
        if not _install(reqs):
            return 1
        if "news" in requested:
            print("\nInstalling Playwright Chromium browser runtime...")
            if subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False).returncode:
                print("  FAIL: Playwright could not install Chromium.")
                return 1

    _configure_credentials(args)
    findings, guidance = _check(args)
    print("StoryPilot dependency check")
    print(f"Python: {sys.executable}")
    print(f"Workspace: {args.base_dir.expanduser().resolve()}")
    for label, ok, detail in findings:
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")
    if guidance:
        print("\nNext steps:")
        for item in dict.fromkeys(guidance):
            print(f"- {item}")
    failed = [label for label, ok, _ in findings if not ok]
    if failed:
        print(f"\nResult: {len(failed)} check(s) need attention. No production run was started.")
        return 1
    print("\nResult: all requested checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
