# hermes-plugin-credits

A Hermes Agent dashboard plugin that adds a **Provider Credits** widget to the
Analytics page (and a `/credits` tab). At a glance you see how much you have
left across your model and tool providers — no need to bounce between billing tabs.

## What it shows

| Provider | Auth | What's surfaced |
|---|---|---|
| **OpenRouter** | `OPENROUTER_API_KEY` | Real `$ remaining / $ limit`, progress bar, free-tier badge |
| **Anthropic** | `ANTHROPIC_API_KEY` _or_ `CLAUDE_CODE_OAUTH_TOKEN` | Rate-limit headers (5h / 7d window utilization for Claude Pro/Max; tokens-remaining for org keys) |
| **OpenAI** | `OPENAI_API_KEY` | Per-minute tokens-remaining. Hidden when unset. Codex/ChatGPT OAuth only if `CREDITS_INCLUDE_CODEX=1` |
| **FAL** | `FAL_KEY` (or `FAL_API_KEY`) | `$` balance via platform billing API |
| **Atlas Cloud** | `ATLASCLOUD_API_KEY` (or `ATLAS_API_KEY`) | `$` available balance via `public/v1/balance` |
| **RunPod** | `RUNPOD_API_KEY` | `$` `clientBalance` via GraphQL `myself` |
| **Replicate** | `REPLICATE_API_KEY` (or `REPLICATE_API_TOKEN`) | Key validity + username (no public balance API) |
| **Tavily** | `TAVILY_API_KEY` | Credits used / plan limit for the billing cycle |
| **Firecrawl** | `FIRECRAWL_API_KEY` | Remaining team credits |

Providers with no key are omitted from the UI entirely (no error cards).

**Dashboard secret fallback:** if a key is missing from the process env (common
when only the Hermes gateway syncs Bitwarden SM), the backend also reads
`HERMES_HOME/profiles/forge/cache/bws_cache.json` / `HERMES_HOME/cache/bws_cache.json`.

**OpenAI gating:** a set `OPENAI_API_KEY` is ignored when `OPENAI_BASE_URL` points
somewhere other than `api.openai.com` (e.g. FreeLLM proxy leftovers).

## Install

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/Dirt-Nasty/hermes-plugin-credits ~/.hermes/plugins/credits
```

On Hermes installs that use a shared home (e.g. forge `HERMES_HOME=/opt/data`):

```bash
git clone https://github.com/Dirt-Nasty/hermes-plugin-credits /opt/data/plugins/hermes-plugin-credits
```

Enable the **manifest name** (`credits`) in config — folder name alone is not enough:

```yaml
plugins:
  enabled:
    - credits
    # optional aliases / other plugins…
```

Then restart the dashboard (or rescan **and** restart — plugin API routes mount at process start).

## How it works

* **Backend** — `dashboard/plugin_api.py` exposes `GET /api/plugins/credits/status`. Each provider is queried in parallel; failures degrade gracefully so one bad key doesn't blank the panel. Results are cached for 5 minutes since the Anthropic/OpenAI probes burn ~1 token each.
* **Frontend** — `dashboard/dist/index.js` is a plain IIFE using the Hermes Plugin SDK (`window.__HERMES_PLUGIN_SDK__`). No build step needed.
* **Slot + tab** — registers into `analytics:top` via `registerSlot`, and calls `register("credits", …)` so the `/credits` nav tab works.

## Caveats

- **Anthropic via Claude Code OAuth** needs a non-expired access token. The
  plugin attempts a refresh-token grant if the token in `~/.claude/.credentials.json`
  is stale — but Claude Code rotates its refresh token in the macOS keychain,
  which a launchd-spawned dashboard can't read. If you see
  `"OAuth token expired and refresh blocked"`, set a real `ANTHROPIC_API_KEY`
  in `~/.hermes/.env`, _or_ run a Claude Code session to mint a fresh token,
  then refresh the widget.
- **OpenAI / Codex via ChatGPT subscription**: opt-in only (`CREDITS_INCLUDE_CODEX=1`).
  There is no public usage API; the widget surfaces the plan tier and stops there.
- **FAL** billing expand may require an admin-capable key; model-only keys can 401.
- Adding a new provider is ~10–40 lines in `plugin_api.py` plus a small UI branch.

## Requirements

- Hermes Agent ≥ 0.11
- The dashboard's Python env (already includes `httpx`)

## License

MIT

## Upstream

Fork of [Atemndobs/hermes-plugin-credits](https://github.com/Atemndobs/hermes-plugin-credits)
with forge-oriented provider cards (FAL / Tavily / Firecrawl) and stricter OpenAI gating.
