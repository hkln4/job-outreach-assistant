// Any form with data-loading-text swaps its submit button for a spinner +
// that text the instant it's submitted, so a slow LLM call never looks like
// a frozen page - CLAUDE.md has no JS anywhere else; keep this the only bit.
document.addEventListener("submit", function (e) {
  const form = e.target;
  const loadingText = form.getAttribute("data-loading-text");
  if (!loadingText) return;
  const button = form.querySelector('button[type="submit"]');
  if (!button) return;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>' + loadingText;
});
