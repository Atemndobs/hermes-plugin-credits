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
    key = _env_key("OPENROUTER_API_KEY")
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
    api_key = _env_key("ANTHROPIC_API_KEY")
    oauth_token: str | None = _env_key("CLAUDE_CODE_OAUTH_TOKEN")
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


def _bws_cache_secrets() -> dict[str, str]:
    """Best-effort load of Bitwarden SM cache (Hermes forge / shared home).

    Dashboard processes often lack the gateway's injected secrets; Hermes
    still writes `cache/bws_cache.json` under profile or HERMES_HOME.
    Returns {SECRET_NAME: value} — never logs values.
    """
    from pathlib import Path

    candidates: list[Path] = []
    hermes_home = Path(os.getenv("HERMES_HOME") or Path.home())
    # Prefer forge profile cache when present (common on OVH multi-profile).
    candidates.append(hermes_home / "profiles" / "forge" / "cache" / "bws_cache.json")
    candidates.append(hermes_home / "cache" / "bws_cache.json")
    # Default-profile style
    candidates.append(Path.home() / ".hermes" / "cache" / "bws_cache.json")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            import json

            data = json.loads(path.read_text())
            secrets = data.get("secrets")
            if isinstance(secrets, dict) and secrets:
                return {str(k): str(v) for k, v in secrets.items() if v}
        except Exception:
            continue
    return {}


_BWS_SECRETS: dict[str, str] | None = None


def _secret(name: str) -> str | None:
    """Env first, then BWS cache fallback for dashboard-without-gateway-env."""
    global _BWS_SECRETS
    v = (os.getenv(name) or "").strip()
    if v:
        return v
    if _BWS_SECRETS is None:
        _BWS_SECRETS = _bws_cache_secrets()
    v = (_BWS_SECRETS.get(name) or "").strip()
    return v or None


def _env_key(*names: str) -> str | None:
    """First non-empty secret among *names* (env, then BWS cache)."""
    for name in names:
        v = _secret(name)
        if v:
            return v
    return None


def _openai_base_is_official() -> bool:
    """True when OPENAI_BASE_URL is unset or points at api.openai.com.

    Forge may set OPENAI_API_KEY + OPENAI_BASE_URL to a FreeLLM / proxy
    endpoint — that must not light up the OpenAI provider card.
    """
    base = (os.getenv("OPENAI_BASE_URL") or "").strip().lower()
    if not base:
        return True
    return "api.openai.com" in base


async def _fal(client: httpx.AsyncClient) -> dict[str, Any]:
    """FAL: platform billing balance via Admin/API key.

    Tries several Authorization shapes — model keys are often
    ``key_id:key_secret`` while platform docs use ``Key <token>``.
    """
    key = _env_key("FAL_KEY", "FAL_API_KEY")
    if not key:
        return {"provider": "fal", "configured": False}
    raw = key[4:].strip() if key.lower().startswith("key ") else key
    auth_candidates = [
        f"Key {raw}",
        f"Key {key}",
        key if key.lower().startswith("key ") else None,
        f"Bearer {raw}",
    ]
    last_status = None
    try:
        for auth in auth_candidates:
            if not auth:
                continue
            r = await client.get(
                "https://api.fal.ai/v1/account/billing",
                params={"expand": "credits"},
                headers={"Authorization": auth, "Accept": "application/json"},
            )
            last_status = r.status_code
            if r.status_code in (401, 403):
                continue
            if not r.is_success:
                return {
                    "provider": "fal",
                    "configured": True,
                    "ok": False,
                    "error": f"HTTP {r.status_code}",
                }
            data = r.json()
            credits = data.get("credits") or {}
            balance = credits.get("current_balance")
            return {
                "provider": "fal",
                "configured": True,
                "ok": True,
                "remaining_usd": float(balance) if balance is not None else None,
                "currency": credits.get("currency") or "USD",
                "username": data.get("username"),
            }
        return {
            "provider": "fal",
            "configured": True,
            "ok": False,
            "error": f"HTTP {last_status or 'auth'}",
            "hint": (
                "FAL billing requires an Admin-scoped API key "
                "(create one at fal.ai/dashboard/keys with scope ADMIN). "
                "API-scoped keys can call models but cannot read credit balance."
            ),
        }
    except Exception as e:
        return {"provider": "fal", "configured": True, "ok": False, "error": str(e)[:200]}


async def _tavily(client: httpx.AsyncClient) -> dict[str, Any]:
    """Tavily: key + account plan usage for the current billing cycle."""
    key = _env_key("TAVILY_API_KEY")
    if not key:
        return {"provider": "tavily", "configured": False}
    try:
        r = await client.get(
            "https://api.tavily.com/usage",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return {
                "provider": "tavily",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        data = r.json()
        key_info = data.get("key") or {}
        account = data.get("account") or {}
        # Prefer account plan numbers when present; fall back to key-scoped.
        usage = account.get("plan_usage")
        limit = account.get("plan_limit")
        if usage is None:
            usage = key_info.get("usage")
        if limit is None:
            limit = key_info.get("limit")
        remaining = None
        if usage is not None and limit is not None:
            remaining = max(0, int(limit) - int(usage))
        return {
            "provider": "tavily",
            "configured": True,
            "ok": True,
            "usage": int(usage) if usage is not None else None,
            "limit": int(limit) if limit is not None else None,
            "remaining": remaining,
            "unit": "credits",
        }
    except Exception as e:
        return {"provider": "tavily", "configured": True, "ok": False, "error": str(e)[:200]}


async def _firecrawl(client: httpx.AsyncClient) -> dict[str, Any]:
    """Firecrawl: team credit remaining."""
    key = _env_key("FIRECRAWL_API_KEY")
    if not key:
        return {"provider": "firecrawl", "configured": False}
    try:
        r = await client.get(
            "https://api.firecrawl.dev/v1/team/credit-usage",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return {
                "provider": "firecrawl",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        data = r.json()
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        remaining = payload.get("remaining_credits")
        plan = payload.get("plan_credits")
        return {
            "provider": "firecrawl",
            "configured": True,
            "ok": bool(data.get("success", True)),
            "remaining": float(remaining) if remaining is not None else None,
            "limit": float(plan) if plan is not None else None,
            "unit": "credits",
            "billing_period_end": payload.get("billing_period_end"),
        }
    except Exception as e:
        return {"provider": "firecrawl", "configured": True, "ok": False, "error": str(e)[:200]}


def _money_value(payload: Any) -> float | None:
    """Parse Atlas MoneyValue ``{"value": "24.81", "currency": "usd"}``."""
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        try:
            return float(payload.get("value"))
        except (TypeError, ValueError):
            return None
    if isinstance(payload, str):
        try:
            return float(payload)
        except ValueError:
            return None
    return None


async def _atlas(client: httpx.AsyncClient) -> dict[str, Any]:
    """Atlas Cloud: public billing balance (forge image-gen / Seedream)."""
    key = _env_key("ATLASCLOUD_API_KEY", "ATLAS_API_KEY")
    if not key:
        return {"provider": "atlas", "configured": False}
    try:
        r = await client.get(
            "https://api.atlascloud.ai/public/v1/balance",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return {
                "provider": "atlas",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        data = r.json()
        available = _money_value(data.get("available"))
        cash = _money_value(data.get("cash"))
        bonus = _money_value(data.get("bonus"))
        frozen = _money_value(data.get("frozen"))
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        return {
            "provider": "atlas",
            "configured": True,
            "ok": True,
            "remaining_usd": available,
            "cash_usd": cash,
            "bonus_usd": bonus,
            "frozen_usd": frozen,
            "account_type": account.get("type"),
            "account_name": account.get("name") or None,
        }
    except Exception as e:
        return {"provider": "atlas", "configured": True, "ok": False, "error": str(e)[:200]}


async def _runpod(client: httpx.AsyncClient) -> dict[str, Any]:
    """RunPod: GraphQL ``myself.clientBalance`` (USD credits)."""
    key = _env_key("RUNPOD_API_KEY")
    if not key:
        return {"provider": "runpod", "configured": False}
    try:
        r = await client.post(
            "https://api.runpod.io/graphql",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "query": (
                    "query { myself { id email clientBalance "
                    "currentSpendPerHr spendLimit } }"
                )
            },
        )
        if not r.is_success:
            return {
                "provider": "runpod",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        body = r.json()
        if body.get("errors"):
            err0 = body["errors"][0] if isinstance(body["errors"], list) else body["errors"]
            msg = err0.get("message") if isinstance(err0, dict) else str(err0)
            return {
                "provider": "runpod",
                "configured": True,
                "ok": False,
                "error": str(msg)[:200],
            }
        me = (body.get("data") or {}).get("myself") or {}
        balance = me.get("clientBalance")
        spend = me.get("currentSpendPerHr")
        return {
            "provider": "runpod",
            "configured": True,
            "ok": True,
            "remaining_usd": float(balance) if balance is not None else None,
            "spend_per_hr_usd": float(spend) if spend is not None else None,
            "spend_limit_usd": (
                float(me["spendLimit"]) if me.get("spendLimit") is not None else None
            ),
            "email": me.get("email"),
        }
    except Exception as e:
        return {"provider": "runpod", "configured": True, "ok": False, "error": str(e)[:200]}


async def _replicate(client: httpx.AsyncClient) -> dict[str, Any]:
    """Replicate: account probe only — no public credit-balance API."""
    key = _env_key("REPLICATE_API_KEY", "REPLICATE_API_TOKEN")
    if not key:
        return {"provider": "replicate", "configured": False}
    try:
        r = await client.get(
            "https://api.replicate.com/v1/account",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return {
                "provider": "replicate",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        data = r.json()
        return {
            "provider": "replicate",
            "configured": True,
            "ok": True,
            "username": data.get("username"),
            "account_type": data.get("type"),
            "name": data.get("name"),
            "note": "Balance not exposed via API — check replicate.com/account/billing",
        }
    except Exception as e:
        return {"provider": "replicate", "configured": True, "ok": False, "error": str(e)[:200]}


async def _openai(client: httpx.AsyncClient) -> dict[str, Any]:
    """OpenAI / Codex: no balance endpoint; rate-limit headers come back on
    /chat/completions responses. For ChatGPT-subscription Codex auth, the
    standard OpenAI API rejects the token, so we just surface the plan info
    from the local auth.json (ChatGPT Plus/Pro shows here).

    Auth precedence: OPENAI_API_KEY (real API) > Codex OAuth (opt-in only).

    Codex OAuth is opt-in via CREDITS_INCLUDE_CODEX=1 so hosts without an
    OpenAI API key (e.g. OpenRouter-only forge) do not show a broken card.
    """
    key = _env_key("OPENAI_API_KEY")
    # Proxy / FreeLLM keys often reuse OPENAI_API_KEY with a custom base URL.
    if key and not _openai_base_is_official():
        return {"provider": "openai", "configured": False}
    if not key:
        include_codex = (os.getenv("CREDITS_INCLUDE_CODEX") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not include_codex:
            return {"provider": "openai", "configured": False}
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


def _xai_cents_to_usd(val: Any) -> float | None:
    """xAI prepaid amounts are USD cents as strings; credit remaining is negative."""
    if val is None:
        return None
    try:
        return abs(int(str(val))) / 100.0
    except (TypeError, ValueError):
        return None


async def _xai_resolve_team_id(client: httpx.AsyncClient, mgmt_key: str) -> str | None:
    """Prefer XAI_TEAM_ID env; else discover via management-key validation."""
    explicit = _env_key("XAI_TEAM_ID")
    if explicit:
        return explicit
    try:
        r = await client.get(
            "https://management-api.x.ai/auth/management-keys/validation",
            headers={"Authorization": f"Bearer {mgmt_key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return None
        data = r.json()
        return (
            (data.get("scopeId") or data.get("teamId") or "").strip() or None
        )
    except Exception:
        return None


async def _xai(client: httpx.AsyncClient) -> dict[str, Any]:
    """xAI prepaid balance via Management API; inference key validates as fallback.

    Balance requires ``XAI_MANAGEMENT_KEY`` (console → Management Keys) plus a
    team id (``XAI_TEAM_ID`` or auto from validation). Plain ``XAI_API_KEY`` is
    enough for inference / Hermes ``x_search`` but cannot read billing.
    """
    mgmt = _env_key("XAI_MANAGEMENT_KEY", "XAI_MANAGEMENT_API_KEY")
    api_key = _env_key("XAI_API_KEY")
    if not mgmt and not api_key:
        return {"provider": "xai", "configured": False}

    if mgmt:
        team_id = await _xai_resolve_team_id(client, mgmt)
        if not team_id:
            return {
                "provider": "xai",
                "configured": True,
                "ok": False,
                "error": "team id missing",
                "hint": (
                    "Set XAI_TEAM_ID from console.x.ai → Team settings, "
                    "or ensure the management key can call "
                    "/auth/management-keys/validation."
                ),
            }
        try:
            r = await client.get(
                f"https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance",
                headers={"Authorization": f"Bearer {mgmt}", "Accept": "application/json"},
            )
            if not r.is_success:
                return {
                    "provider": "xai",
                    "configured": True,
                    "ok": False,
                    "error": f"HTTP {r.status_code}",
                    "hint": (
                        "Management key needs billing read on this team. "
                        "Create one at console.x.ai → Settings → Management Keys."
                    ),
                }
            data = r.json()
            remaining = _xai_cents_to_usd((data.get("total") or {}).get("val"))
            # Newest-first ledger: sum SPEND until the most recent PURCHASE.
            changes = data.get("changes") or []
            spend_since_topup_usd = 0.0
            last_topup_usd = None
            saw_spend = False
            for ch in changes:
                origin = (ch.get("changeOrigin") or "").upper()
                amt = _xai_cents_to_usd((ch.get("amount") or {}).get("val"))
                if amt is None:
                    continue
                if origin == "SPEND" and last_topup_usd is None:
                    spend_since_topup_usd += amt
                    saw_spend = True
                elif origin == "PURCHASE":
                    last_topup_usd = amt
                    break
            if not saw_spend:
                spend_since_topup_usd = None

            out: dict[str, Any] = {
                "provider": "xai",
                "configured": True,
                "ok": True,
                "remaining_usd": remaining,
                "currency": "USD",
                "auth": "management",
            }
            if last_topup_usd is not None:
                out["last_topup_usd"] = last_topup_usd
            if spend_since_topup_usd is not None and last_topup_usd is not None:
                out["usage_usd"] = spend_since_topup_usd
                out["limit_usd"] = last_topup_usd
            return out
        except Exception as e:
            return {
                "provider": "xai",
                "configured": True,
                "ok": False,
                "error": str(e)[:200],
            }

    # Inference key only — prove it works; no balance.
    try:
        r = await client.get(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if not r.is_success:
            return {
                "provider": "xai",
                "configured": True,
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }
        return {
            "provider": "xai",
            "configured": True,
            "ok": True,
            "auth": "api_key",
            "note": "inference key ok",
            "hint": (
                "Add XAI_MANAGEMENT_KEY (console.x.ai → Management Keys) "
                "to show prepaid credit balance."
            ),
        }
    except Exception as e:
        return {"provider": "xai", "configured": True, "ok": False, "error": str(e)[:200]}


_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL = 300.0  # 5 minutes — Anthropic/OpenAI probes burn ~1 token each


@router.get("/status")
async def status(force: bool = False) -> dict[str, Any]:
    """Return per-provider credit status. Cached 5min to limit probe cost."""
    import time
    global _BWS_SECRETS
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["at"]) < _CACHE_TTL:
        return {**_CACHE["data"], "cached": True, "age_seconds": int(now - _CACHE["at"])}

    # Reload BWS cache each probe so newly synced forge secrets appear.
    _BWS_SECRETS = None

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        results = await asyncio.gather(
            _openrouter(client),
            _anthropic(client),
            _openai(client),
            _fal(client),
            _atlas(client),
            _runpod(client),
            _replicate(client),
            _tavily(client),
            _firecrawl(client),
            _xai(client),
            return_exceptions=False,
        )
    providers = [p for p in results if p.get("configured")]
    payload = {"providers": providers, "cached": False, "age_seconds": 0}
    _CACHE["at"] = now
    _CACHE["data"] = {"providers": providers}
    return payload
