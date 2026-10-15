"""First-run workspace and local API-secret setup for StoryPilot."""
from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable



OPTIONAL_API_KEYS = {
    "YOUTUBE_API_KEY": "YouTube Data API (topic discovery; optional when a topic is supplied)",
    "PIXABAY_API_KEY": "Pixabay API (picture/video search and downloads)",
    "FREESOUND_API_KEY": "Freesound API (music/SFX search)",
    "AZURE_SPEECH_KEY": "Azure Speech resource key (narration synthesis)",
    "AZURE_SPEECH_REGION": "Azure Speech resource region (for example australiaeast)",
}


def ensure_workspace(base_dir: Path) -> dict[str, Path]:
    """Create the standard production, media, cache, and seed directories."""
    base = Path(base_dir).expanduser().resolve()
    paths = {
        "root": base,
        "discovery": base / "discovery",
        "jobs": base / "jobs",
        "assets": base / "assets",
        "pictures": base / "assets" / "pictures",
        "video_clips": base / "assets" / "video_clips",
        "generated_media": base / "assets" / "generated_media",
        "background_music": base / "assets" / "background_music",
        "sfx": base / "assets" / "sfx",
        "pixabay_search_cache": base / "assets" / ".pixabay_search_cache",
        "public_search_cache": base / "assets" / ".public_search_cache",
        "rendered_outputs": base / "rendered_outputs",
        "temp_assets": base / "temp_assets",
        "logs": base / "logs",
        "audio_library_music_seed": base / "audio_library_seed" / "background_music",
        "audio_library_sfx_seed": base / "audio_library_seed" / "sfx",
    }
    for name, path in paths.items():
        if name != "root":
            path.mkdir(parents=True, exist_ok=True)
    return paths


def _read_secret_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read secrets file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Secrets file must contain a JSON object: {path}")
    return value


def _write_secret_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".secrets-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.replace(temp_path, path)
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def configure_api_keys(
    base_dir: Path,
    *,
    required_keys: tuple[str, ...] = ("OPENAI_API_KEY",),
    force_optional: bool = False,
    interactive: bool | None = None,
    prompt_fn: Callable[[str], str] | None = None,
) -> dict:
    """Load API keys from environment/local JSON, prompting once for missing keys.

    Environment variables take precedence over the local secrets file. Optional
    providers are prompted during first setup; use force_optional=True to add a
    provider later. Key values are never printed.
    """
    dirs = ensure_workspace(base_dir)
    secret_path = dirs["root"] / "secrets.json"
    # Load the workspace .env in the same way the dependency checker does.
    # Existing process variables and the JSON secrets file remain authoritative.
    env_path = dirs["root"] / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            # Keep secrets usable even in a partially installed environment;
            # dependency_check.py will report python-dotenv as a missing package.
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                value = value.strip().strip("\"'")
                if name.strip() and value:
                    os.environ.setdefault(name.strip(), value)
    secrets = _read_secret_file(secret_path)
    for name, value in secrets.items():
        if not name.startswith("_") and isinstance(value, str) and value.strip():
            os.environ.setdefault(name, value.strip())

    available = {key for key in set(required_keys) | set(OPTIONAL_API_KEYS)
                 if os.environ.get(key, "").strip()}
    missing_required = [key for key in required_keys if key not in available]
    first_optional_setup = force_optional or not secrets.get("_optional_setup_completed", False)
    missing_optional = [key for key in OPTIONAL_API_KEYS if key not in available] if first_optional_setup else []
    to_prompt = list(dict.fromkeys(missing_required + missing_optional))

    if interactive is None:
        interactive = bool(sys.stdin.isatty())
    prompt = prompt_fn or getpass.getpass
    entered = {}
    if to_prompt and interactive:
        print("StoryPilot API setup: enter keys now; values are hidden while typing.")
        for key in to_prompt:
            label = "Required" if key in required_keys else "Optional"
            value = prompt(f"{label} — {key} ({OPTIONAL_API_KEYS.get(key, 'OpenAI API')}): ").strip()
            if value:
                entered[key] = value
                os.environ[key] = value
        available.update(entered)
        if first_optional_setup:
            secrets["_optional_setup_completed"] = True
    elif missing_required:
        names = ", ".join(missing_required)
        raise RuntimeError(
            f"Missing required API key(s): {names}. Run this command in a terminal to enter them, "
            f"or add them to {secret_path} / environment variables."
        )

    for key, value in entered.items():
        secrets[key] = value
    if entered or (interactive and first_optional_setup):
        _write_secret_file(secret_path, secrets)

    missing_required = [key for key in required_keys if not os.environ.get(key, "").strip()]
    if missing_required:
        raise RuntimeError(
            "Required key(s) were left blank: " + ", ".join(missing_required) +
            f". Add them to {secret_path} or run setup again."
        )

    return {
        "secrets_file": secret_path,
        "configured": sorted(key for key in set(required_keys) | set(OPTIONAL_API_KEYS)
                             if os.environ.get(key, "").strip()),
        "missing_optional": sorted(key for key in OPTIONAL_API_KEYS
                                   if not os.environ.get(key, "").strip()),
    }
