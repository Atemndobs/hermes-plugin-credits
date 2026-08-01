/**
 * Credits Plugin — Provider quota / credit overview.
 *
 * Mounts a card into the `analytics:top` slot showing usage, limit, and
 * remaining credits per configured provider. Polls every 60s.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;

  function fmtUSD(n) {
    if (n == null || isNaN(n)) return "—";
    if (n >= 1000) return "$" + n.toFixed(0);
    return "$" + n.toFixed(2);
  }

  function fmtNum(n) {
    if (n == null) return "—";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
    return String(n);
  }

  function fmtReset(s) {
    if (!s) return null;
    const d = new Date(s);
    if (isNaN(d.getTime())) return s; // OpenAI sends "1m23s" etc.
    const diffMs = d.getTime() - Date.now();
    if (diffMs <= 0) return "now";
    const m = Math.round(diffMs / 60000);
    if (m < 60) return "in " + m + "m";
    const h = Math.floor(m / 60);
    return "in " + h + "h " + (m % 60) + "m";
  }

  function diffMs(s) {
    if (!s) return null;
    const d = new Date(s);
    if (isNaN(d.getTime())) return null;
    return d.getTime() - Date.now();
  }

  /** Compact "2h 33m" / "47m" / "12s" countdown. */
  function fmtCountdown(ms) {
    if (ms == null || ms <= 0) return "now";
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    const h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  }

  /** Pill that shows time until a reset. Color shifts as reset approaches.
   *  windowMs = total length of the rate-limit window (e.g. 5h = 18e6 ms). */
  function ResetPill({ label, ms, windowMs }) {
    let tone = "border-zinc-700 bg-zinc-900/60 text-zinc-400";
    if (ms != null && ms <= 30 * 60_000) {
      tone = "border-emerald-700 bg-emerald-950/40 text-emerald-300"; // <30min: relief soon
    } else if (ms != null && ms <= 2 * 60 * 60_000) {
      tone = "border-amber-700 bg-amber-950/40 text-amber-300";       // 30m–2h: getting closer
    } else if (ms != null) {
      tone = "border-rose-900 bg-rose-950/30 text-rose-300/80";        // >2h: long wait
    }
    const pct = ms != null && windowMs ? Math.max(0, Math.min(100, ((windowMs - ms) / windowMs) * 100)) : 0;
    return React.createElement("div",
      { className: "flex flex-col gap-1 rounded-md border px-2 py-1.5 " + tone },
      React.createElement("div", { className: "flex items-baseline justify-between gap-2 text-[10px] uppercase tracking-wide" },
        React.createElement("span", { className: "opacity-70" }, label),
        React.createElement("span", { className: "font-medium tabular-nums" }, fmtCountdown(ms)),
      ),
      windowMs && React.createElement("div",
        { className: "h-0.5 w-full rounded-full bg-black/40 overflow-hidden" },
        React.createElement("div", { className: "h-full bg-current opacity-60", style: { width: pct + "%" } }),
      ),
    );
  }

  function bar(pct) {
    return React.createElement("div",
      { className: "h-1.5 w-full rounded-full bg-muted overflow-hidden" },
      React.createElement("div", {
        className: "h-full " + (pct > 90 ? "bg-destructive" : pct > 70 ? "bg-amber-500" : "bg-primary"),
        style: { width: pct + "%" },
      }),
    );
  }

  function ProviderRow(props) {
    const { p } = props;
    const name = p.provider;

    let main = null;
    let barEl = null;
    let extraEl = null;
    let footnote = null;

    if (!p.ok && !p.tokens_remaining) {
      main = React.createElement("span", { className: "text-xs text-destructive" },
        "error: " + (p.error || "unknown"));
    } else if (name === "openrouter") {
      const usage = p.usage_usd || 0;
      const limit = p.limit_usd;
      const remaining = p.remaining_usd;
      if (limit != null) {
        const pct = Math.min(100, Math.max(0, (usage / limit) * 100));
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtUSD(remaining)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "remaining"),
          React.createElement("span", { className: "text-xs text-muted-foreground ml-auto tabular-nums" },
            fmtUSD(usage) + " / " + fmtUSD(limit)),
        );
        barEl = bar(pct);
      } else {
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtUSD(usage)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "spent"),
          React.createElement(Badge, { variant: "outline", className: "ml-auto" }, "no cap"),
        );
      }
    } else if (name === "anthropic") {
      // Claude Pro/Max — unified headers expose 5h + 7d window utilization.
      if (p.unified_5h_utilization != null) {
        const used5h = p.unified_5h_utilization;
        const used7d = p.unified_7d_utilization;
        const remaining5h = Math.max(0, 1 - used5h);
        const reset5hMs = p.unified_5h_reset ? diffMs(p.unified_5h_reset * 1000) : null;
        const reset7dMs = p.unified_7d_reset ? diffMs(p.unified_7d_reset * 1000) : null;

        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" },
            Math.round(remaining5h * 100) + "%"),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "left in 5h window"),
          React.createElement("span", { className: "text-xs text-muted-foreground ml-auto tabular-nums" },
            "7d: " + Math.round((1 - (used7d || 0)) * 100) + "%"),
        );
        barEl = bar(used5h * 100);
        // Inline reset pills, side-by-side, with countdown + tone shift.
        extraEl = React.createElement("div",
          { className: "mt-2 grid grid-cols-2 gap-2" },
          React.createElement(ResetPill, { label: "5h reset", ms: reset5hMs, windowMs: 5 * 60 * 60_000 }),
          React.createElement(ResetPill, { label: "7d reset", ms: reset7dMs, windowMs: 7 * 24 * 60 * 60_000 }),
        );
      } else if (p.tokens_remaining != null && p.tokens_limit) {
        const pct = ((p.tokens_limit - p.tokens_remaining) / p.tokens_limit) * 100;
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtNum(p.tokens_remaining)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "tokens left"),
        );
        barEl = bar(pct);
        const r = fmtReset(p.tokens_reset);
        if (r) footnote = "resets " + r;
      } else if (p.ok) {
        main = React.createElement("span", { className: "text-sm text-muted-foreground" },
          "key valid; no rate-limit headers returned");
      } else {
        main = React.createElement("div", { className: "flex flex-col gap-1" },
          React.createElement("span", { className: "text-xs text-destructive" },
            p.error || "probe failed"),
          p.hint && React.createElement("span", { className: "text-[10px] text-muted-foreground" }, p.hint),
        );
      }
      // Plan + price label below — concise; reset countdowns are in the pills.
      if (p.auth === "oauth") {
        const planLabel = (p.plan || "").toString();
        const priceLabel =
          p.plan_price_usd === 0 ? "free"
            : p.plan_price_usd != null ? "$" + p.plan_price_usd + "/mo"
            : null;
        footnote = planLabel
          ? "Claude " + planLabel.charAt(0).toUpperCase() + planLabel.slice(1) +
            (priceLabel ? " · " + priceLabel : "")
          : "Claude Code subscription";
      }
    } else if (name === "openai") {
      if (p.auth === "codex_oauth") {
        // ChatGPT subscription — no usage API; show plan + monthly price.
        const priceLabel =
          p.plan_price_usd === 0 ? "free"
            : p.plan_price_usd != null ? "$" + p.plan_price_usd + "/mo"
            : "custom";
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold capitalize" }, p.plan || "active"),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "ChatGPT plan"),
          React.createElement("span", { className: "ml-auto text-xs tabular-nums text-muted-foreground" }, priceLabel),
        );
        footnote = "subscription quota not exposed via API";
      } else if (p.tokens_remaining != null && p.tokens_limit) {
        const pct = ((p.tokens_limit - p.tokens_remaining) / p.tokens_limit) * 100;
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtNum(p.tokens_remaining)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "tokens / min"),
        );
        barEl = bar(pct);
        const r = fmtReset(p.tokens_reset);
        if (r) footnote = "resets " + r;
      } else if (p.ok) {
        main = React.createElement("span", { className: "text-sm text-muted-foreground" }, "key valid");
      } else {
        main = React.createElement("span", { className: "text-xs text-destructive" }, p.error || "probe failed");
      }
    } else if (name === "fal" || name === "atlas" || name === "runpod" || name === "xai") {
      if (p.ok && p.remaining_usd != null) {
        const limit = p.limit_usd;
        const usage = p.usage_usd;
        if (name === "xai" && limit != null && usage != null) {
          const pct = Math.min(100, Math.max(0, (usage / limit) * 100));
          main = React.createElement("div", { className: "flex items-baseline gap-2" },
            React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtUSD(p.remaining_usd)),
            React.createElement("span", { className: "text-xs text-muted-foreground" }, "remaining"),
            React.createElement("span", { className: "text-xs text-muted-foreground ml-auto tabular-nums" },
              fmtUSD(usage) + " / " + fmtUSD(limit) + " since top-up"),
          );
          barEl = bar(pct);
        } else {
          main = React.createElement("div", { className: "flex items-baseline gap-2" },
            React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtUSD(p.remaining_usd)),
            React.createElement("span", { className: "text-xs text-muted-foreground" }, "balance"),
          );
        }
        if (name === "runpod" && p.spend_per_hr_usd != null && p.spend_per_hr_usd > 0) {
          footnote = "spend " + fmtUSD(p.spend_per_hr_usd) + "/hr";
        } else if (name === "atlas" && p.bonus_usd != null && p.bonus_usd > 0) {
          footnote = "incl. " + fmtUSD(p.bonus_usd) + " bonus";
        } else if (name === "xai" && p.auth === "management") {
          footnote = "xAI prepaid";
        } else if (p.username) {
          footnote = "@" + p.username;
        } else if (p.email) {
          footnote = p.email;
        }
      } else if (p.ok) {
        main = React.createElement("div", { className: "flex flex-col gap-1" },
          React.createElement("span", { className: "text-sm text-muted-foreground" },
            p.note || "key valid; no balance returned"),
          p.hint && React.createElement("span", { className: "text-[10px] text-muted-foreground" }, p.hint),
        );
      } else {
        main = React.createElement("div", { className: "flex flex-col gap-1" },
          React.createElement("span", { className: "text-xs text-destructive" }, p.error || "probe failed"),
          p.hint && React.createElement("span", { className: "text-[10px] text-muted-foreground" }, p.hint),
        );
      }
    } else if (name === "replicate") {
      if (p.ok) {
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold" }, "key ok"),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "no balance API"),
        );
        footnote = p.username
          ? "@" + p.username + (p.note ? " · " + p.note : "")
          : (p.note || null);
      } else {
        main = React.createElement("span", { className: "text-xs text-destructive" }, p.error || "probe failed");
      }
    } else if (name === "tavily" || name === "firecrawl") {
      if (p.ok && p.remaining != null && p.limit != null && p.remaining <= p.limit) {
        const used = Math.max(0, p.limit - p.remaining);
        const pct = p.limit > 0 ? Math.min(100, (used / p.limit) * 100) : 0;
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtNum(p.remaining)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "credits left"),
          React.createElement("span", { className: "text-xs text-muted-foreground ml-auto tabular-nums" },
            fmtNum(used) + " / " + fmtNum(p.limit)),
        );
        barEl = bar(pct);
      } else if (p.ok && p.remaining != null) {
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtNum(p.remaining)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "credits left"),
        );
      } else if (p.ok && p.usage != null) {
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" }, fmtNum(p.usage)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "credits used"),
          p.limit == null && React.createElement(Badge, { variant: "outline", className: "ml-auto" }, "no cap"),
        );
      } else if (p.ok) {
        main = React.createElement("span", { className: "text-sm text-muted-foreground" }, "key valid");
      } else {
        main = React.createElement("span", { className: "text-xs text-destructive" }, p.error || "probe failed");
      }
    } else {
      main = React.createElement("span", { className: "text-sm" }, "key valid");
    }

    return React.createElement("div", { className: "flex flex-col gap-2 p-3 rounded-lg border bg-card/40" },
      React.createElement("div", { className: "flex items-center gap-2" },
        React.createElement("span", { className: "text-xs font-medium uppercase tracking-wide text-muted-foreground" },
          name),
        p.is_free_tier && React.createElement(Badge, { variant: "outline", className: "text-[10px]" }, "free tier"),
      ),
      main,
      barEl,
      extraEl,
      footnote && React.createElement("span", { className: "text-[10px] text-muted-foreground" }, footnote),
    );
  }

  function CreditsWidget() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState(null);

    const load = useCallback(function (force) {
      setLoading(true);
      const url = force
        ? "/api/plugins/credits/status?force=true"
        : "/api/plugins/credits/status";
      SDK.fetchJSON(url)
        .then(function (d) {
          setData(d);
          setErr(null);
          window.__HERMES_CREDITS_AT__ = window.__HERMES_CREDITS_AT__ || { v: null };
          window.__HERMES_CREDITS_AT__.v = Date.now();
        })
        .catch(function (e) { setErr(String(e)); })
        .finally(function () { setLoading(false); });
    }, []);

    useEffect(function () {
      load(false);
      const t = setInterval(function () { load(false); }, 60000);
      return function () { clearInterval(t); };
    }, [load]);

    const providers = (data && data.providers) || [];
    const empty = !loading && providers.length === 0;

    // Live "X seconds ago" — re-renders every 10s so users see the freshness clock tick.
    const [tick, setTick] = useState(0);
    useEffect(function () {
      const t = setInterval(function () { setTick(function (x) { return x + 1; }); }, 10_000);
      return function () { clearInterval(t); };
    }, []);

    // Server returns age_seconds; combine with our own client-side clock.
    const fetchedAtRef = (window.__HERMES_CREDITS_AT__ = window.__HERMES_CREDITS_AT__ || { v: null });
    if (data && !fetchedAtRef.v) fetchedAtRef.v = Date.now();
    const dataAgeMs = data ? (data.age_seconds || 0) * 1000 + (Date.now() - (fetchedAtRef.v || Date.now())) : 0;
    void tick; // keep React hook closure alive

    function fmtAge(ms) {
      if (ms < 60_000) return Math.max(0, Math.floor(ms / 1000)) + "s ago";
      if (ms < 3_600_000) return Math.floor(ms / 60_000) + "m ago";
      return Math.floor(ms / 3_600_000) + "h ago";
    }
    const REFRESH_INTERVAL_S = 300; // server-side cache TTL
    const stale = dataAgeMs / 1000 > REFRESH_INTERVAL_S;

    return React.createElement(Card, { className: "mb-4" },
      React.createElement(CardHeader, { className: "flex flex-row items-center justify-between space-y-0 pb-3" },
        React.createElement(CardTitle, { className: "text-sm font-medium" }, "Provider Credits"),
        React.createElement("div", { className: "flex items-center gap-2 text-[10px] text-muted-foreground" },
          data && React.createElement("span", { className: "tabular-nums" },
            "Updated " + fmtAge(dataAgeMs)),
          data && React.createElement("span", { className: "opacity-60" },
            "· auto " + Math.floor(REFRESH_INTERVAL_S / 60) + "m"),
          React.createElement(Button, {
            size: "sm",
            variant: "ghost",
            onClick: function () { load(true); },
            disabled: loading,
            className: stale ? "text-amber-400" : "",
          }, loading ? "..." : "Refresh"),
        ),
      ),
      React.createElement(CardContent, null,
        err && React.createElement("div", { className: "text-xs text-destructive mb-2" }, err),
        empty && React.createElement("div", { className: "text-xs text-muted-foreground" },
          "No supported providers configured (OPENROUTER / ANTHROPIC / OPENAI / FAL / ATLAS / RUNPOD / REPLICATE / TAVILY / FIRECRAWL keys)."),
        !empty && React.createElement("div", { className: "grid gap-3 md:grid-cols-2 lg:grid-cols-3" },
          providers.map(function (p) {
            return React.createElement(ProviderRow, { key: p.provider, p: p });
          }),
        ),
      ),
    );
  }

  // Full /credits page (Hermes requires register() when the tab is visible).
  function CreditsPage() {
    return React.createElement("div", { className: "p-4 md:p-6 max-w-6xl mx-auto" },
      React.createElement("h1", { className: "text-lg font-semibold mb-4" }, "Provider Credits"),
      React.createElement(CreditsWidget, null),
    );
  }

  // Register into the analytics:top slot
  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGINS__.registerSlot) {
    window.__HERMES_PLUGINS__.registerSlot("credits", "analytics:top", CreditsWidget);
  }
  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGINS__.register) {
    window.__HERMES_PLUGINS__.register("credits", CreditsPage);
  }
})();
