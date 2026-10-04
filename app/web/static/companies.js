// Companies page: fetch-driven add (single + bulk) with visible progress.
// The page never navigates away - each add shows a spinner while the backend
// fetches the site and runs research, then flips to a done/failed marker and
// the new table row is inserted in place. Bulk rows are sent strictly one at
// a time so research calls never overlap.
(function () {
  const I18N = JSON.parse(document.getElementById("companies-i18n").textContent);

  const tbody = document.getElementById("companies-tbody");

  function insertRow(rowHtml) {
    const emptyRow = document.getElementById("companies-empty-row");
    if (emptyRow) emptyRow.remove();
    tbody.insertAdjacentHTML("afterbegin", rowHtml);
  }

  function spinnerNode(text) {
    const span = document.createElement("span");
    span.innerHTML = '<span class="spinner"></span>';
    span.append(text);
    return span;
  }

  function companyLink(company, text) {
    const a = document.createElement("a");
    a.href = "/companies/" + company.id;
    a.textContent = text;
    return a;
  }

  async function postCompany(fields) {
    const body = new FormData();
    Object.entries(fields).forEach(([k, v]) => body.append(k, v || ""));
    const resp = await fetch("/companies", {
      method: "POST",
      headers: { Accept: "application/json" },
      body: body,
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      /* non-JSON error page */
    }
    if (data && (data.status === "created" || data.status === "duplicate")) return data;
    return { status: "failed", detail: resp.status };
  }

  // Renders the outcome of one add into `target` (a status <li> or a preview cell).
  function showOutcome(target, data, name) {
    target.replaceChildren();
    if (data.status === "created") {
      target.append("✓ ");
      target.append(companyLink(data.company, name));
      target.append(" — " + I18N.added);
      if (data.research_error) {
        const warn = document.createElement("span");
        warn.className = "badge badge-soft";
        warn.textContent = I18N.research_failed;
        warn.title = data.research_error;
        target.append(" ");
        target.append(warn);
      }
      if (data.row_html) insertRow(data.row_html);
      return true;
    }
    if (data.status === "duplicate") {
      target.append("⚠ ");
      target.append(companyLink(data.company, name));
      target.append(" — " + I18N.duplicate);
      return false;
    }
    target.textContent = "✗ " + name + " — " + I18N.failed;
    return false;
  }

  // --- Single add form ---
  const form = document.getElementById("add-company-form");
  const statusList = document.getElementById("add-status");

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const fields = {
      name: form.name.value.trim(),
      website_url: form.website_url.value.trim(),
      contact_email: form.contact_email.value.trim(),
      location: form.location.value.trim(),
      technopark: form.technopark.value.trim(),
    };

    const li = document.createElement("li");
    li.append(spinnerNode(fields.name + " — " + I18N.researching));
    statusList.hidden = false;
    statusList.prepend(li);

    button.disabled = true;
    const label = button.textContent;
    button.replaceChildren(spinnerNode(I18N.researching));

    const data = await postCompany(fields);
    const created = showOutcome(li, data, fields.name);
    if (created) form.reset();

    button.disabled = false;
    button.textContent = label;
  });

  // --- Bulk add ---
  const parseBtn = document.getElementById("bulk-parse-btn");
  const addBtn = document.getElementById("bulk-add-btn");
  const summary = document.getElementById("bulk-summary");
  const preview = document.getElementById("bulk-preview");
  const bulkText = document.getElementById("bulk-text");
  let parsedRows = [];

  const ERROR_LABELS = {
    missing_website: I18N.row_missing_website,
    missing_name: I18N.row_missing_name,
    duplicate_in_batch: I18N.row_duplicate_in_batch,
    already_exists: I18N.row_already_exists,
  };

  function fmt(template, values) {
    return template.replace(/\{(\w+)\}/g, (_, key) => values[key]);
  }

  function renderPreview(rows) {
    preview.replaceChildren();
    const table = document.createElement("table");
    table.className = "table";
    const head = table.createTHead().insertRow();
    [I18N.col_name, I18N.col_website, I18N.col_email, I18N.col_status].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      head.append(th);
    });
    const body = table.createTBody();
    rows.forEach((row, i) => {
      const tr = body.insertRow();
      tr.insertCell().textContent = row.name || row.raw;
      tr.insertCell().textContent = row.website_url;
      tr.insertCell().textContent = row.contact_email;
      const statusCell = tr.insertCell();
      statusCell.id = "bulk-row-status-" + i;
      if (row.error) {
        tr.className = "muted";
        const badge = document.createElement("span");
        badge.className = "badge badge-hard";
        badge.textContent = ERROR_LABELS[row.error] || row.error;
        statusCell.append(badge);
      } else {
        statusCell.textContent = I18N.row_ok;
      }
    });
    preview.append(table);
  }

  parseBtn.addEventListener("click", async function () {
    const body = new FormData();
    body.append("text", bulkText.value);
    const resp = await fetch("/companies/bulk/parse", {
      method: "POST",
      headers: { Accept: "application/json" },
      body: body,
    });
    parsedRows = (await resp.json()).rows;
    renderPreview(parsedRows);
    const ok = parsedRows.filter((r) => !r.error).length;
    summary.textContent = fmt(I18N.preview_summary, { ok: ok, flagged: parsedRows.length - ok });
    addBtn.hidden = ok === 0;
  });

  addBtn.addEventListener("click", async function () {
    addBtn.disabled = true;
    parseBtn.disabled = true;
    bulkText.disabled = true;
    let added = 0;
    let failed = 0;

    const pending = parsedRows.map((row, i) => ({ row: row, i: i })).filter((p) => !p.row.error);
    pending.forEach((p) => {
      document.getElementById("bulk-row-status-" + p.i).textContent = I18N.waiting;
    });

    for (const p of pending) {
      const cell = document.getElementById("bulk-row-status-" + p.i);
      cell.replaceChildren(spinnerNode(I18N.researching));
      const data = await postCompany({
        name: p.row.name,
        website_url: p.row.website_url,
        contact_email: p.row.contact_email,
      });
      showOutcome(cell, data, p.row.name) ? added++ : failed++;
      summary.textContent = fmt(I18N.done_summary, { added: added, failed: failed });
    }

    addBtn.hidden = true;
    addBtn.disabled = false;
    parseBtn.disabled = false;
    bulkText.disabled = false;
  });
})();
