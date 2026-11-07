// WP F. Shared dashboard helpers (PLAN §6.14). No side effects at import, so node can unit-test it.
// Untrusted text (diffs, rationales, PR bodies, repo names) only ever reaches the DOM as text nodes:
// h() appends strings with append(), never as markup.

export const OPEN_STATES = new Set(["CREATED", "CONTEXT_READY", "TRADING", "HUMAN_WINDOW", "CLOSED"]);
export const POLL_MS = 2000;

/** Create an element. Strings in `kids` become text nodes; `text` sets textContent. */
export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = String(v);
    else if (k === "href") el.setAttribute("href", safeHref(v));
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(typeof kid === "object" ? kid : String(kid));
  }
  return el;
}

/** Same-origin paths and https links only; anything else (javascript:, data:, //host) becomes "#". */
export function safeHref(value) {
  const s = String(value);
  if (s.startsWith("/") && !s.startsWith("//")) return s;
  if (/^https:\/\/[^\s"'<>]+$/.test(s)) return s;
  return "#";
}

export function fmtPrice(p) {
  return typeof p === "number" && Number.isFinite(p) ? p.toFixed(2) : "—";
}

export function fmtUsd(x) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "—";
  if (x === 0) return "$0";
  return x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`;
}

export function fmtMs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

export function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** "owner/repo#12", or "owner/repo · demo 3" for markets without a PR number. */
export function marketLabel(m) {
  return m.pr_number != null ? `${m.repo}#${m.pr_number}` : `${m.repo} · ${m.source} ${m.id}`;
}

/** The PR on GitHub, for github-sourced markets only. */
export function githubUrl(m) {
  if (m.source !== "github" || m.pr_number == null || !/^[\w.-]+\/[\w.-]+$/.test(m.repo || "")) return null;
  return `https://github.com/${m.repo}/pull/${Number(m.pr_number)}`;
}

/**
 * Andon status for a market summary: tone (open|green|yellow|red|void), a short gate label, and the
 * verdict once the outcome is known. Mirrors MarketEngine.status_description: a paid market is "right" when
 * (gate succeeded) == (outcome YES); a refunded (rejected) one is checked against NO.
 */
export function marketStatus(m) {
  const verdict = verdictOf(m);
  if (m.state === "VOID" && m.resolution !== "refunded") {
    return { tone: "void", label: "void · human review", verdict };
  }
  if (OPEN_STATES.has(m.state)) {
    let label = "opening";
    if (m.state === "TRADING") label = m.escalations > 0 ? `escalation ${m.escalations}` : "trading";
    else if (m.state === "HUMAN_WINDOW") label = "humans trading";
    else if (m.state === "CLOSED") label = "gating";
    return { tone: "open", label, verdict };
  }
  if (m.gate_state === "success") return { tone: "green", label: "auto-merge OK", verdict };
  if (/no consensus/i.test(m.gate_description || "")) return { tone: "yellow", label: "no consensus", verdict };
  return { tone: "red", label: "blocked", verdict };
}

function verdictOf(m) {
  if (m.resolution === "paid" && m.outcome) {
    const right = (m.gate_state === "success") === (m.outcome === "YES");
    return { right, text: `settled ${m.outcome} · ${right ? "✓ market right" : "✗ market wrong"}` };
  }
  if (m.resolution === "refunded" && m.outcome) {
    const right = m.outcome === "NO";
    return { right, text: `refunded · check ${right ? "✓ agrees" : "✗ disagrees"}` };
  }
  if (m.state === "GATED" || m.state === "SETTLING") return { right: null, text: "awaiting post-merge outcome" };
  return null;
}

export async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!res.ok) {
    const err = new Error(`${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/** Run `fn` now and every `ms` after it finishes; pause while the tab is hidden. Shows liveness in #live. */
export function poll(fn, ms = POLL_MS) {
  let timer = null;
  const live = document.getElementById("live");
  async function tick() {
    clearTimeout(timer);
    try {
      await fn();
      if (live) {
        live.className = "live ok";
        live.textContent = `live · ${new Date().toLocaleTimeString()}`;
      }
    } catch (err) {
      if (live) {
        live.className = "live down";
        live.textContent = `offline · retrying (${err.message})`;
      }
    }
    if (!document.hidden) timer = setTimeout(tick, ms);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });
  tick();
}

/** Replace all children of `el` with `kids`. */
export function render(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}
