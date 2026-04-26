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
    // ISO-8601 timestamp from Anthropic / seconds-style from OpenAI.
    const d = new Date(s);
    if (isNaN(d.getTime())) return s; // OpenAI sends "1m23s" etc.
    const diffMs = d.getTime() - Date.now();
    if (diffMs <= 0) return "now";
    const m = Math.round(diffMs / 60000);
    if (m < 60) return "in " + m + "m";
    const h = Math.floor(m / 60);
    return "in " + h + "h " + (m % 60) + "m";
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
        main = React.createElement("div", { className: "flex items-baseline gap-2" },
          React.createElement("span", { className: "text-2xl font-semibold tabular-nums" },
            Math.round(remaining5h * 100) + "%"),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "left in 5h window"),
          React.createElement("span", { className: "text-xs text-muted-foreground ml-auto tabular-nums" },
            "7d: " + Math.round((1 - (used7d || 0)) * 100) + "%"),
        );
        barEl = bar(used5h * 100);
        const parts = [];
        if (p.unified_5h_reset) parts.push("5h resets " + fmtReset(p.unified_5h_reset * 1000));
        if (p.unified_7d_reset) parts.push("7d resets " + fmtReset(p.unified_7d_reset * 1000));
        footnote = parts.join(" · ");
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
      // Stamp plan + monthly price wherever Anthropic OAuth is in use,
      // even on probe failure — gives the user the cost context up front.
      if (p.auth === "oauth") {
        const planLabel = (p.plan || "").toString();
        const priceLabel =
          p.plan_price_usd === 0 ? "free"
            : p.plan_price_usd != null ? "$" + p.plan_price_usd + "/mo"
            : null;
        const sub = planLabel
          ? "Claude " + planLabel.charAt(0).toUpperCase() + planLabel.slice(1) +
            (priceLabel ? " · " + priceLabel : "")
          : "Claude Code subscription";
        footnote = footnote ? sub + " · " + footnote : sub;
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
      footnote && React.createElement("span", { className: "text-[10px] text-muted-foreground" }, footnote),
    );
  }

  function CreditsWidget() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState(null);

    const load = useCallback(function () {
      setLoading(true);
      SDK.fetchJSON("/api/plugins/credits/status")
        .then(function (d) { setData(d); setErr(null); })
        .catch(function (e) { setErr(String(e)); })
        .finally(function () { setLoading(false); });
    }, []);

    useEffect(function () {
      load();
      const t = setInterval(load, 60000);
      return function () { clearInterval(t); };
    }, [load]);

    const providers = (data && data.providers) || [];
    const empty = !loading && providers.length === 0;

    return React.createElement(Card, { className: "mb-4" },
      React.createElement(CardHeader, { className: "flex flex-row items-center justify-between space-y-0 pb-3" },
        React.createElement(CardTitle, { className: "text-sm font-medium" }, "Provider Credits"),
        React.createElement(Button, {
          size: "sm",
          variant: "ghost",
          onClick: load,
          disabled: loading,
        }, loading ? "..." : "Refresh"),
      ),
      React.createElement(CardContent, null,
        err && React.createElement("div", { className: "text-xs text-destructive mb-2" }, err),
        empty && React.createElement("div", { className: "text-xs text-muted-foreground" },
          "No supported providers configured (OPENROUTER_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY)."),
        !empty && React.createElement("div", { className: "grid gap-3 md:grid-cols-2 lg:grid-cols-3" },
          providers.map(function (p) {
            return React.createElement(ProviderRow, { key: p.provider, p: p });
          }),
        ),
      ),
    );
  }

  // Register into the analytics:top slot
  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGINS__.registerSlot) {
    window.__HERMES_PLUGINS__.registerSlot("credits", "analytics:top", CreditsWidget);
  }
})();
