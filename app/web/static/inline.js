// Row actions happen in place: a form marked data-inline posts in the
// background and its row (the closest [data-row]) is removed or swapped for
// the same row (by id) from the page the server answers with. The page never
// reloads, so it never jumps to the top or closes an open section.
//   data-inline="remove"  -> remove the row on success
//   data-inline="swap"    -> replace the row with its fresh version
// A button can override the form's mode with data-inline on the button.
// Anything unexpected falls back to a normal form post.
//
// Read the target with getAttribute: forms here have buttons named "action",
// and form.action then returns those buttons instead of the URL.
function formUrl(form) {
  return form.getAttribute("action") || location.pathname;
}

// The plain-post fallback keeps the clicked button's name/value (a native
// form.submit() drops it, which the server rejects).
function fallbackSubmit(form, button) {
  if (button && button.name) {
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = button.name;
    hidden.value = button.value;
    form.appendChild(hidden);
  }
  HTMLFormElement.prototype.submit.call(form);
}

document.addEventListener("submit", async function (e) {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-inline") || e.defaultPrevented) return;
  const row = form.closest("[data-row]");
  if (!row) return;
  e.preventDefault();
  const button = e.submitter;
  const mode = (button && button.getAttribute("data-inline")) || form.getAttribute("data-inline");
  const body = new FormData(form);
  if (button && button.name) body.append(button.name, button.value);
  row.classList.add("is-busy");
  try {
    const response = await fetch(formUrl(form), { method: "POST", body: body, credentials: "same-origin" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    if (mode === "remove") {
      row.remove();
      document.dispatchEvent(new CustomEvent("inline:done"));
      return;
    }
    const html = await response.text();
    const fresh = row.id && new DOMParser().parseFromString(html, "text/html").getElementById(row.id);
    if (fresh) {
      row.replaceWith(fresh);
    } else {
      row.remove(); // the row is gone from the page now (e.g. moved to another list)
    }
    document.dispatchEvent(new CustomEvent("inline:done"));
  } catch (err) {
    row.classList.remove("is-busy");
    fallbackSubmit(form, button);
  }
});

// Live previews: an <iframe data-live-preview> reloads (debounced) after any
// in-place change on the page.
let previewTimer = null;
document.addEventListener("inline:done", function () {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(function () {
    document.querySelectorAll("iframe[data-live-preview]").forEach(function (frame) {
      const base = frame.getAttribute("data-live-preview");
      frame.src = base + (base.includes("?") ? "&" : "?") + "t=" + Date.now() + "#view=FitH&toolbar=0";
    });
  }, 400);
});

// "Do all": click every matching inline form one after another, waiting for
// each (e.g. write bullets for every selected project without any).
document.addEventListener("click", async function (e) {
  const trigger = e.target.closest("[data-run-all]");
  if (!trigger) return;
  e.preventDefault();
  trigger.disabled = true;
  const forms = Array.from(document.querySelectorAll(trigger.getAttribute("data-run-all")));
  for (const form of forms) {
    const row = form.closest("[data-row]");
    const id = row && row.id;
    await new Promise(function (resolve) {
      const done = function () { document.removeEventListener("inline:done", done); resolve(); };
      document.addEventListener("inline:done", done);
      form.requestSubmit();
      setTimeout(done, 180000);
    });
    if (id && !document.getElementById(id)) continue;
  }
  trigger.disabled = false;
});

// Autosave forms: any change posts the form in the background (debounced);
// every [data-refresh] element with an id is swapped for its version in the
// answer, and live previews reload.
let autosaveTimer = null;
document.addEventListener("change", function (e) {
  const form = e.target.closest && e.target.closest("form[data-autosave]");
  if (!form) return;
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(async function () {
    const body = new FormData(form);
    body.append("action", form.getAttribute("data-autosave"));
    document.querySelectorAll("[data-refresh]").forEach(function (el) { el.classList.add("is-busy"); });
    try {
      const response = await fetch(formUrl(form), { method: "POST", body: body, credentials: "same-origin" });
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      document.querySelectorAll("[data-refresh]").forEach(function (el) {
        const fresh = el.id && doc.getElementById(el.id);
        if (fresh) el.replaceWith(fresh); else el.classList.remove("is-busy");
      });
    } finally {
      document.dispatchEvent(new CustomEvent("inline:done"));
    }
  }, 600);
});

// Bullet-style forms: editing the text saves on its own when the field loses
// focus (no save button) - posted in place with the form's data-save-on-change
// action.
document.addEventListener("change", function (e) {
  const field = e.target;
  const form = field.form;
  if (!form || !form.hasAttribute("data-save-on-change") || field.tagName !== "TEXTAREA") return;
  const hidden = document.createElement("button");
  hidden.type = "submit";
  hidden.name = "action";
  hidden.value = form.getAttribute("data-save-on-change");
  hidden.hidden = true;
  form.appendChild(hidden);
  form.requestSubmit(hidden);
  hidden.remove();
});

// Tabs: [data-tabs] holds buttons [data-tab=x] and panels [data-panel=x];
// one panel shows at a time, the first by default.
function initTabs(root) {
  root.querySelectorAll("[data-tabs]").forEach(function (box) {
    const buttons = Array.from(box.querySelectorAll("[data-tab]"));
    const show = function (key) {
      buttons.forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-tab") === key); });
      box.querySelectorAll("[data-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== key; });
    };
    buttons.forEach(function (b) { b.addEventListener("click", function () { show(b.getAttribute("data-tab")); }); });
    if (buttons.length) show(buttons[0].getAttribute("data-tab"));
    else box.hidden = true;
  });
}
document.addEventListener("DOMContentLoaded", function () { initTabs(document); });

// Clicking the letter opens its editor (same as the Edit toggle).
document.addEventListener("click", function (e) {
  const body = e.target.closest(".email-body");
  if (!body || window.getSelection().toString()) return;
  const toggle = body.closest(".email-card") && body.closest(".email-card").querySelector("details.edit-toggle");
  if (toggle) toggle.open = true;
});
