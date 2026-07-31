"""Agent-facing credits tool + slash formatter.

Loads the dashboard probe module (``dashboard/plugin_api.py``) via importlib
so we share one probe/cache implementation with the UI — no duplicated
provider clients.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from tools.registry import tool_error, tool_result

_PLUGIN_ROOT = Path(__file__).resolve().parent
_API_PATH = _PLUGIN_ROOT / "dashboard" / "plugin_api.py"
_api_mod = None

# Fields that commonly carry key labels / secrets in probe payloads.
_DROP_KEYS = frozenset(
    {
        "label",  # OpenRouter key label is often the key prefix itself
        "raw",
        "authorization",
        "api_key",
        "token",
        "access_token",
        "refresh_token",
    }
)
_SECRETISH = re.compile(
    r"(?i)\b(sk-[a-z0-9_-]{8,}|rpa_[a-z0-9_-]{8,}|r8_[a-z0-9_-]{8,}|"
    r"apikey-[a-z0-9_-]{8,}|tvly-[a-z0-9_-]{8,}|fc-[a-z0-9_-]{8,}|"
    r"Bearer\s+\S+|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.)"
)

CREDITS_STATUS_SCHEMA = {
    "name": "credits_status",
    "description": (
        "Fetch sanitized provider credit/balance status (OpenRouter, FAL, "
        "Atlas, RunPod, Replicate, Tavily, Firecrawl, Anthropic, OpenAI). "
        "Use when the user asks about remaining credits, API spend, billing, "
        "or token balance. Never DIY OpenRouter/BWS key fetches — call this. "
        "Does not modify config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": (
                    "Bypass the 5-minute probe cache and re-query providers. "
                    "Default false."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional provider filter (openrouter, fal, atlas, runpod, "
                    "replicate, tavily, firecrawl, anthropic, openai). "
                    "Omit for all configured providers."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _load_api():
    global _api_mod
    if _api_mod is not None:
        return _api_mod
    if not _API_PATH.is_file():
        raise FileNotFoundError(f"credits probe module missing: {_API_PATH}")
    spec = importlib.util.spec_from_file_location(
        "hermes_plugin_credits_dashboard_api",
        _API_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load credits probe from {_API_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _api_mod = mod
    return mod


def _looks_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if _SECRETISH.search(value):
        return True
    # Truncated key previews like "sk-or-v1-137...1fc"
    if "..." in value and any(
        value.lower().startswith(p)
        for p in ("sk-", "rpa_", "r8_", "apikey-", "tvly-", "fc-", "key-")
    ):
        return True
    return False


def sanitize_provider(row: dict[str, Any]) -> dict[str, Any]:
    """Drop key material / secret-looking strings from a provider row."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        lk = str(k).lower()
        if lk in _DROP_KEYS or "key" in lk or "secret" in lk or "token" in lk:
            continue
        if _looks_secret(v):
            continue
        out[k] = v
    return out


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    providers = payload.get("providers") or []
    clean = [sanitize_provider(p) for p in providers if isinstance(p, dict)]
    return {
        "providers": clean,
        "cached": bool(payload.get("cached")),
        "age_seconds": int(payload.get("age_seconds") or 0),
    }


def _money(v: Any) -> str | None:
    if v is None:
        return None
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def format_provider_line(p: dict[str, Any]) -> str:
    name = (p.get("provider") or "?").title()
    if p.get("provider") == "openrouter":
        name = "OpenRouter"
    elif p.get("provider") == "fal":
        name = "FAL"
    elif p.get("provider") == "runpod":
        name = "RunPod"

    if not p.get("ok", True) and p.get("error"):
        return f"**{name}** — error: {p.get('error')}"

    bits: list[str] = []
    rem = _money(p.get("remaining_usd"))
    lim = _money(p.get("limit_usd"))
    if rem and lim:
        bits.append(f"{rem} / {lim} remaining")
    elif rem:
        bits.append(f"{rem} remaining")
    elif lim:
        bits.append(f"limit {lim}")

    if p.get("usage_usd") is not None and rem is None:
        bits.append(f"used {_money(p.get('usage_usd'))}")

    if p.get("remaining") is not None and p.get("unit"):
        lim_u = p.get("limit")
        if lim_u is not None:
            bits.append(f"{p['remaining']} / {lim_u} {p['unit']} remaining")
        else:
            bits.append(f"{p['remaining']} {p['unit']} remaining")
    elif p.get("usage") is not None and p.get("limit") is not None and p.get("unit"):
        left = None
        try:
            left = int(p["limit"]) - int(p["usage"])
        except (TypeError, ValueError):
            left = None
        if left is not None:
            bits.append(f"{left} / {p['limit']} {p['unit']} remaining")

    if p.get("username"):
        bits.append(f"@{p['username']}")
    if p.get("note"):
        bits.append(str(p["note"]))
    if p.get("plan"):
        bits.append(f"plan={p['plan']}")

    if not bits:
        bits.append("ok" if p.get("ok") else "configured")
    return f"**{name}** — " + " · ".join(bits)


def format_credits_markdown(payload: dict[str, Any]) -> str:
    clean = sanitize_payload(payload)
    providers = clean["providers"]
    if not providers:
        return "No configured provider credentials found for credits probes."

    lines = ["### Provider Credits", ""]
    for p in providers:
        lines.append(format_provider_line(p))
    cache_note = (
        f"\n\n_Cached {clean['age_seconds']}s ago._"
        if clean.get("cached")
        else "\n\n_Fresh probe._"
    )
    lines.append(cache_note)
    lines.append("\nUse `/credits refresh` or `credits_status(force=true)` to bypass cache.")
    return "\n".join(lines)


async def fetch_status(*, force: bool = False, provider: str | None = None) -> dict[str, Any]:
    api = _load_api()
    payload = await api.status(force=force)
    clean = sanitize_payload(payload)
    if provider:
        want = provider.strip().lower()
        clean["providers"] = [
            p for p in clean["providers"] if str(p.get("provider", "")).lower() == want
        ]
        clean["filter"] = want
    return clean


def handle_credits_status(args: dict[str, Any] | None = None, **_kwargs) -> str:
    """Sync tool handler — bridges the async probe."""
    args = args or {}
    force = bool(args.get("force") or False)
    provider = args.get("provider")
    if provider is not None:
        provider = str(provider).strip() or None
    try:
        from model_tools import _run_async

        payload = _run_async(fetch_status(force=force, provider=provider))
        return tool_result(
            {
                "success": True,
                "summary_markdown": format_credits_markdown(payload),
                **payload,
            }
        )
    except Exception as e:
        return tool_error(f"credits_status failed: {type(e).__name__}: {e}")


def handle_credits_command(raw_args: str) -> str:
    """Slash handler: ``/credits`` or ``/credits refresh [provider]``."""
    parts = (raw_args or "").strip().split()
    force = False
    provider = None
    if parts:
        if parts[0].lower() in {"refresh", "force", "--force", "-f"}:
            force = True
            if len(parts) > 1:
                provider = parts[1]
        else:
            provider = parts[0]
            if len(parts) > 1 and parts[1].lower() in {"refresh", "force"}:
                force = True
    try:
        from model_tools import _run_async

        payload = _run_async(fetch_status(force=force, provider=provider))
        return format_credits_markdown(payload)
    except Exception as e:
        return f"credits command failed: {type(e).__name__}: {e}"


def credits_available() -> bool:
    """Tool gate — probe module must be present (keys may still be missing)."""
    return _API_PATH.is_file()
