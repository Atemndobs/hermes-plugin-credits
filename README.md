# hermes-plugin-credits

A Hermes Agent dashboard plugin that adds a **Provider Credits** widget to the
Analytics page. At a glance you see how much you have left across your model
providers — no need to bounce between OpenRouter, Anthropic, and OpenAI dashboards.

## What it shows

| Provider | Auth | What's surfaced |
|---|---|---|
| **OpenRouter** | API key (`OPENROUTER_API_KEY`) | Real `$ remaining / $ limit`, progress bar, free-tier badge |
| **Anthropic** | `ANTHROPIC_API_KEY` _or_ `CLAUDE_CODE_OAUTH_TOKEN` | Rate-limit headers (5h / 7d window utilization for Claude Pro/Max; tokens-remaining for org keys) |
| **OpenAI / Codex** | `OPENAI_API_KEY` _or_ ChatGPT subscription via `~/.codex/auth.json` | Per-minute tokens-remaining (API keys); ChatGPT plan name (subscription) |

## Install

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/Atemndobs/hermes-plugin-credits ~/.hermes/plugins/credits
```

Then either restart your Hermes dashboard or hit `POST /api/dashboard/plugins/rescan`. The widget appears at the top of the Analytics tab.

## How it works

* **Backend** — `dashboard/plugin_api.py` exposes `GET /api/plugins/credits/status`. Each provider is queried in parallel; failures degrade gracefully so one bad key doesn't blank the panel. Results are cached for 5 minutes since the Anthropic/OpenAI probes burn ~1 token each.
* **Frontend** — `dashboard/dist/index.js` is a plain IIFE using the Hermes Plugin SDK (`window.__HERMES_PLUGIN_SDK__`). No build step needed.
* **Slot** — registers into the `analytics:top` slot via `window.__HERMES_PLUGINS__.registerSlot("credits", "analytics:top", Component)`.

## Caveats

- **Anthropic via Claude Code OAuth** needs a non-expired access token. The
  plugin attempts a refresh-token grant if the token in `~/.claude/.credentials.json`
  is stale — but Claude Code rotates its refresh token in the macOS keychain,
  which a launchd-spawned dashboard can't read. If you see
  `"OAuth token expired and refresh blocked"`, set a real `ANTHROPIC_API_KEY`
  in `~/.hermes/.env`, _or_ run a Claude Code session to mint a fresh token,
  then refresh the widget.
- **OpenAI / Codex via ChatGPT subscription**: there is no public usage API.
  The widget surfaces the plan tier ("plus", "pro", etc.) from the local OAuth
  JWT and stops there.
- Adding a new provider is ~10 lines in `plugin_api.py`.

## Requirements

- Hermes Agent ≥ 0.11
- The dashboard's Python env (already includes `httpx`)

## License

MIT
