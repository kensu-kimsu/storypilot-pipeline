"""Cost-aware model routing, usage reports, and reproducible stage checkpoints.

All model calls keep the configured OPENAI_MODEL unless a stage-specific override
is set. Checkpoints are keyed by the exact stage inputs; changing the topic,
script, channel, or prompt invalidates only downstream stages.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGE_MODEL_ENV = {
    "ideation": "OPENAI_MODEL_IDEATION",
    "research": "OPENAI_MODEL_RESEARCH",
    "verification": "OPENAI_MODEL_VERIFICATION",
    "scripting": "OPENAI_MODEL_SCRIPT",
    "metadata": "OPENAI_MODEL_METADATA",
    "news_search": "OPENAI_MODEL_NEWS_SEARCH",
    "social_search": "OPENAI_MODEL_SOCIAL_SEARCH",
    "social_ranking": "OPENAI_MODEL_SOCIAL_RANKING",
    "clip_analysis": "OPENAI_MODEL_CLIP_ANALYSIS",
}

# USD per million tokens. Update this small reference table when provider prices
# change; unknown/custom model names remain unpriced instead of showing a false
# cost. The report always preserves raw token usage.
RATE_CARD_USD_PER_MILLION = {
    "gpt-5": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    "gpt-6-luna": {"input": 0.05, "output": 0.25, "cached_input": 0.005},
    "gpt-6.1-sol": {"input": 1.0, "output": 5.0, "cached_input": 0.05},
}
WEB_SEARCH_USD_PER_CALL = 0.01


def resolve_model(stage: str, explicit_model: str | None = None) -> str:
    """Resolve explicit model, stage override, then the existing global default."""
    if explicit_model:
        return explicit_model
    env_name = STAGE_MODEL_ENV.get(stage, f"OPENAI_MODEL_{stage.upper()}")
    return os.getenv(env_name, "").strip() or os.getenv("OPENAI_MODEL", "").strip() or "gpt-5"


def fallback_model_env(stage: str) -> str:
    """Return the matching fallback setting, honoring legacy stage aliases."""
    env_name = STAGE_MODEL_ENV.get(stage, f"OPENAI_MODEL_{stage.upper()}")
    return f"{env_name}_FALLBACK"


def signature(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_checkpoint(job_dir: Path, stage: str, input_signature: str, filename: str) -> Any | None:
    """Return a prior stage result only when its exact input signature matches."""
    refresh = {item.strip().lower() for item in os.getenv("STORYPILOT_REFRESH_STAGES", "").split(",") if item.strip()}
    if stage.lower() in refresh or "*" in refresh:
        return None
    index_path = job_dir / "stage_cache.json"
    result_path = job_dir / filename
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        row = index.get("stages", {}).get(stage, {})
        if row.get("signature") != input_signature or row.get("file") != filename or not result_path.is_file():
            return None
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def save_checkpoint(job_dir: Path, stage: str, input_signature: str, filename: str, result: Any) -> None:
    """Write result first, then atomically record the completed checkpoint."""
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path = job_dir / filename
    temp_result = result_path.with_suffix(result_path.suffix + ".tmp")
    temp_result.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_result.replace(result_path)
    index_path = job_dir / "stage_cache.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"version": 1, "stages": {}}
    except (OSError, json.JSONDecodeError):
        index = {"version": 1, "stages": {}}
    index.setdefault("version", 1)
    index.setdefault("stages", {})[stage] = {
        "signature": input_signature, "file": filename,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    temp_index = index_path.with_suffix(".json.tmp")
    temp_index.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_index.replace(index_path)


def _usage_event(stage: str, model: str, response: dict, web_search: bool) -> dict:
    usage = response.get("usage") or {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens", 0) or 0)
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    tool_calls = sum(1 for item in response.get("output", []) if isinstance(item, dict) and item.get("type") == "web_search_call")
    if web_search and tool_calls == 0:
        tool_calls = 1
    rates = RATE_CARD_USD_PER_MILLION.get(model.lower())
    estimated_cost = None
    if rates:
        ordinary_input = max(0, input_tokens - cached)
        estimated_cost = (ordinary_input * rates["input"] + cached * rates.get("cached_input", rates["input"]) + output_tokens * rates["output"]) / 1_000_000
        estimated_cost += tool_calls * WEB_SEARCH_USD_PER_CALL
    return {
        "stage": stage, "model": model, "response_id": response.get("id"),
        "input_tokens": input_tokens, "cached_input_tokens": cached,
        "output_tokens": output_tokens, "web_search_calls": tool_calls,
        "estimated_cost_usd": round(estimated_cost, 6) if estimated_cost is not None else None,
    }


def record_openai_usage(response: dict, *, stage: str, model: str, web_search: bool = False,
                        job_dir: Path | str | None = None) -> None:
    """Append one API usage event and refresh a compact per-job aggregate."""
    target_value = job_dir or os.getenv("STORYPILOT_ACTIVE_JOB_DIR")
    if not target_value:
        return
    target = Path(target_value)
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "cost_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"version": 1, "events": []}
    except (OSError, json.JSONDecodeError):
        report = {"version": 1, "events": []}
    event = _usage_event(stage, model, response, web_search)
    event["recorded_at"] = datetime.now(timezone.utc).isoformat()
    report.setdefault("events", []).append(event)
    report["currency"] = "USD"
    report["pricing_note"] = "Estimates use the reference rates embedded in cost_controls.py; check provider pricing before relying on totals."
    report["totals"] = {
        "api_calls": len(report["events"]),
        "input_tokens": sum(int(row.get("input_tokens", 0)) for row in report["events"]),
        "cached_input_tokens": sum(int(row.get("cached_input_tokens", 0)) for row in report["events"]),
        "output_tokens": sum(int(row.get("output_tokens", 0)) for row in report["events"]),
        "web_search_calls": sum(int(row.get("web_search_calls", 0)) for row in report["events"]),
        "estimated_cost_usd": round(sum(float(row["estimated_cost_usd"]) for row in report["events"] if row.get("estimated_cost_usd") is not None), 6),
        "unpriced_calls": sum(1 for row in report["events"] if row.get("estimated_cost_usd") is None),
    }
    temp_path = report_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(report_path)


def record_local_event(event_type: str, *, stage: str, detail: dict | None = None,
                       job_dir: Path | str | None = None) -> None:
    """Record paid/non-token events (such as image generation) without guessing price."""
    target_value = job_dir or os.getenv("STORYPILOT_ACTIVE_JOB_DIR")
    if not target_value:
        return
    target = Path(target_value)
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "cost_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"version": 1, "events": []}
    except (OSError, json.JSONDecodeError):
        report = {"version": 1, "events": []}
    event = {"stage": stage, "event_type": event_type, "estimated_cost_usd": None,
             "detail": detail or {}, "recorded_at": datetime.now(timezone.utc).isoformat()}
    report.setdefault("events", []).append(event)
    report["currency"] = "USD"
    report["pricing_note"] = "Tokenized OpenAI usage is estimated from the reference rates in cost_controls.py; generated media and unpriced models are counted but not guessed."
    token_rows = [row for row in report["events"] if "input_tokens" in row]
    report["totals"] = {
        "api_calls": len(token_rows),
        "input_tokens": sum(int(row.get("input_tokens", 0)) for row in token_rows),
        "cached_input_tokens": sum(int(row.get("cached_input_tokens", 0)) for row in token_rows),
        "output_tokens": sum(int(row.get("output_tokens", 0)) for row in token_rows),
        "web_search_calls": sum(int(row.get("web_search_calls", 0)) for row in token_rows),
        "estimated_cost_usd": round(sum(float(row["estimated_cost_usd"]) for row in token_rows if row.get("estimated_cost_usd") is not None), 6),
        "unpriced_calls": sum(1 for row in report["events"] if row.get("event_type") or row.get("estimated_cost_usd") is None),
        "local_events": sum(1 for row in report["events"] if row.get("event_type")),
    }
    temp_path = report_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(report_path)
