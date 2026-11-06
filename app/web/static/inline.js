// Row actions happen in place: a form marked data-inline posts in the
// background and its row (the closest [data-row]) is removed or swapped for
// the same row (by id) from the page the server answers with. The page never
// reloads, so it never jumps to the top or closes an open section.
//   data-inline="remove"  -> remove the row on success
//   data-inline="swap"    -> replace the row with its fresh version
// A button can override the form's mode with data-inline on the button.
// Anything unexpected falls back to a normal form post.
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
    const response = await fetch(form.action, { method: "POST", body: body, credentials: "same-origin" });
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
    HTMLFormElement.prototype.submit.call(form);
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
      frame.src = base + (base.includes("?") ? "&" : "?") + "t=" + Date.now();
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
      const response = await fetch(form.action, { method: "POST", body: body, credentials: "same-origin" });
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
