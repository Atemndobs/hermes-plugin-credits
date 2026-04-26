"""Credits plugin — backend API.

Mounted at /api/plugins/credits/ by the dashboard plugin system.
Returns provider credit/quota status across known providers. Each provider is
queried independently; failures degrade gracefully so one bad key doesn't blank
the whole panel.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from fastapi import APIRouter

router = APIRouter()

TIMEOUT = httpx.Timeout(6.0, connect=3.0)


# Subscription monthly prices (USD). Sources: openai.com/chatgpt/pricing,
# anthropic.com/pricing as of 2026. Update when providers shift pricing.
CHATGPT_PLAN_PRICE_USD = {
    "free": 0,
    "plus": 20,
    "pro": 200,
    "team": 30,        # per user / month
    "business": 30,    # alias surfaced by some endpoints
    "enterprise": None,  # custom — show "custom"
    "edu": 0,
}
CLAUDE_PLAN_PRICE_USD = {
    "pro": 20,
    "max": 100,        # Max 5x baseline; Max 20x is 200
    "max_5x": 100,
    "max_20x": 200,
    "team": 30,
    "enterprise": None,
}


async def _openrouter(client: httpx.AsyncClient) -> dict[str, Any]:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return {"provider": "openrouter", "configured": False}
    try:
        r = await client.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        # OpenRouter returns: usage (USD spent), limit (USD cap or null=unlimited),
        # limit_remaining (USD or null), is_free_tier, rate_limit
        usage = float(data.get("usage") or 0)
        limit = data.get("limit")  # null means unlimited
        limit_remaining = data.get("limit_remaining")
        return {
            "provider": "openrouter",
            "configured": True,
            "ok": True,
            "usage_usd": usage,
            "limit_usd": float(limit) if limit is not None else None,
            "remaining_usd": float(limit_remaining) if limit_remaining is not None else None,
            "is_free_tier": bool(data.get("is_free_tier", False)),
            "label": data.get("label"),
        }
    except Exception as e:
        return {"provider": "openrouter", "configured": True, "ok": False, "error": str(e)[:200]}


def _hdr_int(headers, name: str):
    v = headers.get(name)
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _claude_subscription_label() -> str | None:
    """Return the Claude subscription tier from the local creds file."""
    import json
    from pathlib import Path
    p = Path.home() / ".claude" / ".credentials.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("claudeAiOauth", {}).get("subscriptionType")
    except Exception:
        return None


def _claude_oauth_from_file() -> tuple[str | None, str | None]:
    """Get a working Anthropic OAuth access token.

    Tries (in order):
      1. ~/.hermes/.anthropic_oauth.json — Hermes-managed; refreshes cleanly
         since Hermes writes here AND the dashboard runs from this same
         install. No keychain access needed.
      2. ~/.claude/.credentials.json — fallback. Refreshable only if Claude
         Code hasn't already burned the refresh token (single-use rotation).

    Returns (access_token, source_label) so we can surface why a refresh
    might fail.
    """
    import json
    import time
    from pathlib import Path

    now_ms = int(time.time() * 1000)

    # 1) Hermes auth pool (~/.hermes/auth.json) — populated by `hermes auth
    #    add anthropic`. Highest-priority oauth credential is used.
    pool_path = Path.home() / ".hermes" / "auth.json"
    if pool_path.exists():
        try:
            data = json.loads(pool_path.read_text())
            entries = (data.get("credential_pool") or {}).get("anthropic") or []
            # Prefer non-env credentials (manual OAuth from PKCE flow).
            entries = sorted(
                entries,
                key=lambda e: (
                    e.get("source", "").startswith("env"),
                    -(e.get("priority") or 0),
                ),
            )
            for e in entries:
                if e.get("auth_type") != "oauth":
                    continue
                access = e.get("access_token")
                refresh = e.get("refresh_token")
                exp = int(e.get("expires_at_ms") or 0)
                if access and (exp == 0 or exp > now_ms + 60_000):
                    return access, "hermes_pool"
                if refresh:
                    t = _refresh_anthropic_pool(refresh, pool_path, e["id"])
                    if t:
                        return t, "hermes_pool"
        except Exception:
            pass

    # 2) Legacy Hermes-only file (older versions).
    hermes_path = Path.home() / ".hermes" / ".anthropic_oauth.json"
    if hermes_path.exists():
        try:
            data = json.loads(hermes_path.read_text())
            access = data.get("accessToken") or data.get("access_token")
            refresh = data.get("refreshToken") or data.get("refresh_token")
            exp = int(data.get("expiresAt") or data.get("expires_at_ms") or 0)
            if access and exp > now_ms + 60_000:
                return access, "hermes_oauth"
            if refresh:
                t = _refresh_anthropic(refresh, hermes_path, hermes_format=True)
                if t:
                    return t, "hermes_oauth"
        except Exception:
            pass

    # 3) Claude Code's credentials file.
    p = Path.home() / ".claude" / ".credentials.json"
    if not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None, None

    creds = data.get("claudeAiOauth", {}) or {}
    access = creds.get("accessToken")
    refresh = creds.get("refreshToken")
    expires_at_ms = int(creds.get("expiresAt") or 0)

    if access and expires_at_ms > now_ms + 60_000:
        return access, "claude_code"

    if refresh:
        t = _refresh_anthropic(refresh, p, hermes_format=False, claude_data=data)
        if t:
            return t, "claude_code"
    return access, "claude_code"


def _refresh_anthropic_pool(refresh: str, pool_path, cred_id: str) -> str | None:
    """Refresh an Anthropic credential stored in ~/.hermes/auth.json's pool."""
    import json
    import time
    try:
        with httpx.Client(timeout=8) as c:
            for endpoint in (
                "https://platform.claude.com/v1/oauth/token",
                "https://console.anthropic.com/v1/oauth/token",
            ):
                r = c.post(
                    endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                    },
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
                new_access = payload.get("access_token")
                new_refresh = payload.get("refresh_token") or refresh
                expires_in = int(payload.get("expires_in") or 3600)
                if not new_access:
                    continue
                # Persist back into the pool.
                try:
                    data = json.loads(pool_path.read_text())
                    entries = (data.get("credential_pool") or {}).get("anthropic") or []
                    for e in entries:
                        if e.get("id") == cred_id:
                            e["access_token"] = new_access
                            e["refresh_token"] = new_refresh
                            e["expires_at_ms"] = int(time.time() * 1000) + expires_in * 1000
                            break
                    pool_path.write_text(json.dumps(data, indent=2))
                except Exception:
                    pass
                return new_access
    except Exception:
        pass
    return None


def _refresh_anthropic(
    refresh: str,
    file_path,
    hermes_format: bool,
    claude_data: dict | None = None,
) -> str | None:
    """Run the Anthropic OAuth refresh-token grant and persist the result.

    Single-use rotation: each refresh issues a new refresh_token; older ones
    are invalidated. Returns the new access token or None on failure.
    """
    import json
    import time
    try:
        with httpx.Client(timeout=8) as c:
            for endpoint in (
                "https://platform.claude.com/v1/oauth/token",
                "https://console.anthropic.com/v1/oauth/token",
            ):
                try:
                    r = c.post(
                        endpoint,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh,
                            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                        },
                    )
                    if r.status_code != 200:
                        continue
                    payload = r.json()
                    new_access = payload.get("access_token")
                    new_refresh = payload.get("refresh_token") or refresh
                    expires_in = int(payload.get("expires_in") or 3600)
                    if not new_access:
                        continue
                    new_exp = int(time.time() * 1000) + expires_in * 1000
                    try:
                        if hermes_format:
                            file_path.write_text(json.dumps({
                                "accessToken": new_access,
                                "refreshToken": new_refresh,
                                "expiresAt": new_exp,
                            }, indent=2))
                        elif claude_data is not None:
                            claude_data["claudeAiOauth"]["accessToken"] = new_access
                            claude_data["claudeAiOauth"]["refreshToken"] = new_refresh
                            claude_data["claudeAiOauth"]["expiresAt"] = new_exp
                            file_path.write_text(json.dumps(claude_data, indent=2))
                    except Exception:
                        pass
                    return new_access
                except Exception:
                    continue
    except Exception:
        pass
    return None


async def _anthropic(client: httpx.AsyncClient) -> dict[str, Any]:
    """Anthropic has no balance endpoint, but every /v1/messages response carries
    rate-limit headers. For Claude Pro/Max subscriptions, the *unified* headers
    show 5h + 7d window utilization. We probe with a 1-token request to read
    them. Costs ~$0.0001 per refresh (or counts a tiny sliver of subscription).

    Auth precedence: ANTHROPIC_API_KEY > CLAUDE_CODE_OAUTH_TOKEN > ~/.claude/.credentials.json.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    oauth_token: str | None = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    oauth_source: str | None = "env" if oauth_token else None
    if not oauth_token:
        oauth_token, oauth_source = _claude_oauth_from_file()
    if not api_key and not oauth_token:
        return {"provider": "anthropic", "configured": False}
    plan = _claude_subscription_label()  # e.g. "max", "pro"

    headers = {"anthropic-version": "2023-06-01"}
    auth_kind = "api_key"
    if api_key:
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {oauth_token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
        auth_kind = "oauth"

    body = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }
    try:
        r = await client.post(
            "https://api.anthropic.com/v1/messages", headers=headers, json=body,
        )
        h = r.headers

        def _flt(name):
            v = h.get(name)
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _ts(name):
            """Reset values are unix seconds for unified, ISO-8601 for legacy."""
            v = h.get(name)
            if v is None:
                return None
            try:
                return int(v)  # unix seconds (unified)
            except (ValueError, TypeError):
                return v       # ISO-8601 string (legacy)

        normalized_plan = (plan or "").lower().replace("-", "_")
        plan_price = CLAUDE_PLAN_PRICE_USD.get(normalized_plan)
        out = {
            "provider": "anthropic",
            "configured": True,
            "ok": r.is_success,
            "auth": auth_kind,
            "plan": plan,
            "plan_price_usd": plan_price,
            "oauth_source": oauth_source,
            # Claude Pro/Max subscription (unified headers, utilization 0..1)
            "unified_5h_utilization": _flt("anthropic-ratelimit-unified-5h-utilization"),
            "unified_5h_status": h.get("anthropic-ratelimit-unified-5h-status"),
            "unified_5h_reset": _ts("anthropic-ratelimit-unified-5h-reset"),
            "unified_7d_utilization": _flt("anthropic-ratelimit-unified-7d-utilization"),
            "unified_7d_status": h.get("anthropic-ratelimit-unified-7d-status"),
            "unified_7d_reset": _ts("anthropic-ratelimit-unified-7d-reset"),
            # Legacy / API-key headers
            "tokens_remaining": _hdr_int(h, "anthropic-ratelimit-tokens-remaining"),
            "tokens_limit": _hdr_int(h, "anthropic-ratelimit-tokens-limit"),
            "tokens_reset": _ts("anthropic-ratelimit-tokens-reset"),
            "requests_remaining": _hdr_int(h, "anthropic-ratelimit-requests-remaining"),
            "requests_limit": _hdr_int(h, "anthropic-ratelimit-requests-limit"),
        }
        if not r.is_success:
            if r.status_code == 401 and auth_kind == "oauth":
                out["error"] = "OAuth token rejected"
                out["hint"] = (
                    "Run `hermes auth add anthropic` once to mint a fresh "
                    "Hermes-managed OAuth token (writes to "
                    "~/.hermes/.anthropic_oauth.json)."
                )
            else:
                out["error"] = f"HTTP {r.status_code}"
        return out
    except Exception as e:
        return {
            "provider": "anthropic", "configured": True, "ok": False,
            "auth": auth_kind, "plan": plan, "error": str(e)[:200],
        }


def _codex_token_from_file() -> tuple[str | None, dict | None]:
    """Read Codex (ChatGPT subscription) OAuth from ~/.codex/auth.json.
    Returns (access_token, full_token_payload_for_metadata).
    """
    import json
    from pathlib import Path
    p = Path.home() / ".codex" / "auth.json"
    if not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text())
        # Codex stores: tokens.access_token + tokens.id_token (JWT with plan info)
        tokens = data.get("tokens") or {}
        return tokens.get("access_token"), tokens
    except Exception:
        return None, None


def _codex_plan_from_jwt(id_token: str) -> str | None:
    """Decode unsigned JWT body to read chatgpt_plan_type (plus/pro/etc)."""
    import base64
    import json
    try:
        body = id_token.split(".")[1]
        body += "=" * (-len(body) % 4)  # pad
        payload = json.loads(base64.urlsafe_b64decode(body))
        auth = payload.get("https://api.openai.com/auth", {})
        return auth.get("chatgpt_plan_type")
    except Exception:
        return None


async def _openai(client: httpx.AsyncClient) -> dict[str, Any]:
    """OpenAI / Codex: no balance endpoint; rate-limit headers come back on
    /chat/completions responses. For ChatGPT-subscription Codex auth, the
    standard OpenAI API rejects the token, so we just surface the plan info
    from the local auth.json (ChatGPT Plus/Pro shows here).

    Auth precedence: OPENAI_API_KEY (real API) > Codex OAuth (subscription only).
    """
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        # Fall back to Codex subscription metadata.
        access, tokens = _codex_token_from_file()
        if not access:
            return {"provider": "openai", "configured": False}
        plan = None
        id_token = (tokens or {}).get("id_token")
        if id_token:
            plan = _codex_plan_from_jwt(id_token)
        normalized_plan = (plan or "").lower().replace("-", "_")
        plan_price = CHATGPT_PLAN_PRICE_USD.get(normalized_plan)
        return {
            "provider": "openai",
            "configured": True,
            "ok": True,
            "auth": "codex_oauth",
            "plan": plan,  # "plus", "pro", "free", etc.
            "plan_price_usd": plan_price,
            "note": "ChatGPT subscription; usage limits not exposed via API.",
        }
    try:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
        )
        h = r.headers
        out = {
            "provider": "openai",
            "configured": True,
            "ok": r.is_success,
            "tokens_remaining": _hdr_int(h, "x-ratelimit-remaining-tokens"),
            "tokens_limit": _hdr_int(h, "x-ratelimit-limit-tokens"),
            "tokens_reset": h.get("x-ratelimit-reset-tokens"),
            "requests_remaining": _hdr_int(h, "x-ratelimit-remaining-requests"),
            "requests_limit": _hdr_int(h, "x-ratelimit-limit-requests"),
        }
        if not r.is_success:
            out["error"] = f"HTTP {r.status_code}"
        return out
    except Exception as e:
        return {"provider": "openai", "configured": True, "ok": False, "error": str(e)[:200]}


_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL = 300.0  # 5 minutes — Anthropic/OpenAI probes burn ~1 token each


@router.get("/status")
async def status(force: bool = False) -> dict[str, Any]:
    """Return per-provider credit status. Cached 5min to limit probe cost."""
    import time
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["at"]) < _CACHE_TTL:
        return {**_CACHE["data"], "cached": True, "age_seconds": int(now - _CACHE["at"])}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        results = await asyncio.gather(
            _openrouter(client),
            _anthropic(client),
            _openai(client),
            return_exceptions=False,
        )
    providers = [p for p in results if p.get("configured")]
    payload = {"providers": providers, "cached": False, "age_seconds": 0}
    _CACHE["at"] = now
    _CACHE["data"] = {"providers": providers}
    return payload
