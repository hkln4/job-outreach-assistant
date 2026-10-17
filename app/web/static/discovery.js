// Discovery page: the per-row ✓ / ✕ buttons decide in the background and the
// row disappears in place - with ~1,500 candidates, a full reload that jumps
// back to the top after every decision would make the list unusable.
// Without JS the same forms just post and redirect.
document.addEventListener("submit", async function (e) {
  const form = e.target;
  if (!form.classList.contains("row-decide")) return;
  e.preventDefault();
  const action = e.submitter ? e.submitter.value : "";
  const data = new FormData(form);
  data.set("action", action);
  form.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    // getAttribute, not form.action: the buttons are named "action", and a
    // control with that name shadows the form's own action property.
    const resp = await fetch(form.getAttribute("action"), {
      method: "POST",
      body: data,
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) throw new Error(String(resp.status));
    form.closest("tr").remove();
    const count = document.getElementById("shown-count");
    if (count) count.textContent = Math.max(0, Number(count.textContent) - 1);
  } catch (err) {
    // Fall back to a plain post + redirect, keeping which button was pressed
    // (form.submit() alone would drop it).
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "action";
    hidden.value = action;
    form.appendChild(hidden);
    HTMLFormElement.prototype.submit.call(form);
  }
});
