// Any form with data-loading-text swaps its submit button for a spinner +
// that text the instant it's submitted, so a slow LLM call never looks like
// a frozen page - CLAUDE.md has no JS anywhere else; keep this the only bit.
document.addEventListener("submit", function (e) {
  // A cancelled submit (draft_guard.js blocking an unsaved send, an onsubmit
  // confirm the owner declined) never navigates, so spinnering the button here
  // would leave it disabled and spinning forever on a page that isn't loading.
  if (e.defaultPrevented) return;
  const form = e.target;
  const loadingText = form.getAttribute("data-loading-text");
  if (!loadingText) return;
  const button = form.querySelector('button[type="submit"]');
  if (!button) return;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>' + loadingText;
});

// Every action in this app is a form post that redirects back to the page -
// which lands at the top again. Remember where the owner was and restore it
// when the redirect comes back to the same page (not when it goes elsewhere,
// and not when the URL points at an anchor of its own).
const SCROLL_KEY = "scroll-restore";
function rememberScroll(form) {
  if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
  try {
    sessionStorage.setItem(
      SCROLL_KEY,
      JSON.stringify({ path: location.pathname, y: window.scrollY, at: Date.now() })
    );
  } catch (err) {
    /* storage unavailable - just lose the position, as before */
  }
}
document.addEventListener("submit", function (e) {
  // Checked after every listener ran: a submit another script handles in the
  // background (preventDefault) never reloads, so there's nothing to restore.
  setTimeout(function () {
    if (!e.defaultPrevented) rememberScroll(e.target);
  }, 0);
});
// Dropdowns that save on change call form.submit(), which fires no submit
// event - cover that path too.
const nativeSubmit = HTMLFormElement.prototype.submit;
HTMLFormElement.prototype.submit = function () {
  rememberScroll(this);
  return nativeSubmit.call(this);
};
window.addEventListener("load", function () {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
    sessionStorage.removeItem(SCROLL_KEY);
  } catch (err) {
    return;
  }
  if (!saved || location.hash) return;
  if (saved.path === location.pathname && Date.now() - saved.at < 60000) {
    window.scrollTo(0, saved.y);
  }
});
