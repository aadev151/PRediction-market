// The market page's "Fast-forward 14 days" card (DECISIONS #73). Shown for a merged, gated GitHub market; the
// button asks the server to run the post-merge oracle as of the window's end. Admin only: the token is kept in
// this tab's sessionStorage (same key as /live) and sent as a Bearer header.
import { fmtTime, h, render } from "./prm.js";
import { errorText, postJSON } from "./judge-core.js";

const TOKEN_KEY = "prm_admin_token";
let busy = false;
let message = "";

function token() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function saveToken(value) {
  try {
    sessionStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* storage blocked: the request will answer 401 */
  }
}

export function canFastForward(m) {
  return m?.source === "github" && m.state === "GATED" && Boolean(m.merge_sha);
}

let lastKey = null;

export function renderFastForward(m, onDone) {
  const card = document.getElementById("ff-card");
  if (!card) return;
  const show = canFastForward(m) || Boolean(message);
  card.hidden = !show;
  // Re-render only on a change, so the 2 s poll doesn't wipe a token being typed.
  const key = JSON.stringify([m.state, m.merge_sha, busy, message, Boolean(token())]);
  if (!show || key === lastKey) return;
  lastKey = key;
  const input = token() ? null : h("input", { type: "password", id: "ff-token", autocomplete: "off", placeholder: "admin token" });
  const button = h("button", {
    type: "button", class: "primary", disabled: busy || !canFastForward(m) ? "" : null,
    onclick: async () => {
      if (input) saveToken(input.value.trim());
      busy = true;
      message = "running the post-merge check…";
      renderFastForward(m, onDone);
      const r = await postJSON(`/api/markets/${Number(m.id)}/fast-forward`, undefined, { Authorization: `Bearer ${token()}` });
      busy = false;
      message = r.ok ? `settled: ${r.data.status_description}` : errorText(r);
      if (r.status === 401) saveToken("");
      await onDone(); // re-fetches the market and re-renders this card with the new state
    },
  }, busy ? "settling…" : "Fast-forward 14 days");
  render(document.getElementById("ff"),
    m.merged_at ? h("p", { class: "muted small" }, `merged ${fmtTime(m.merged_at)} · settles on a revert or bug fix in the window`) : null,
    input, button, message ? h("p", { class: "msg" }, message) : null);
}
