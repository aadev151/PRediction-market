// Rule test fixture for prm-dashboard-no-inner-html (semgrep --test). Not shipped code.
function render(el, rationale) {
  // ruleid: prm-dashboard-no-inner-html
  el.innerHTML = rationale;
  // ruleid: prm-dashboard-no-inner-html
  el.innerHTML += "<li>" + rationale + "</li>";
  // ruleid: prm-dashboard-no-inner-html
  el.outerHTML = rationale;
  // ruleid: prm-dashboard-no-inner-html
  el.insertAdjacentHTML("beforeend", rationale);
  // ok: prm-dashboard-no-inner-html
  el.textContent = rationale;
  // ok: prm-dashboard-no-inner-html
  const html = el.innerHTML;
  return html;
}
