// Stops a draft edit from being lost at the last step: the edit fields and the
// Approve button are separate forms, so typing in the body and hitting Approve
// used to send the previously saved text with no warning at all.
//
// Watches the edit fields, shows a badge once they diverge from what's stored,
// and refuses the send until it's saved. Loaded only by application_detail.html.
(function () {
  const editForm = document.querySelector('form[id^="edit-form-"]');
  const approveForm = document.querySelector(".review-approve-form");
  const badge = document.getElementById("unsaved-badge");
  if (!editForm || !approveForm) return;

  const strings = JSON.parse(
    document.getElementById("draft-guard-i18n").textContent
  );
  // The fields live outside the form element and are attached to it with the
  // HTML form= attribute, so they can't be read off editForm.elements alone.
  const fields = Array.from(
    document.querySelectorAll('[form="' + editForm.id + '"]')
  ).filter(function (el) {
    return el.name;
  });

  const saved = {};
  fields.forEach(function (field) {
    saved[field.name] = field.value;
  });

  function isDirty() {
    return fields.some(function (field) {
      return field.value !== saved[field.name];
    });
  }

  function refresh() {
    if (badge) badge.hidden = !isDirty();
  }

  fields.forEach(function (field) {
    field.addEventListener("input", refresh);
  });

  // Saving navigates away, so the snapshot doesn't need updating - but clear
  // the badge so the button isn't left looking blocked while the POST runs.
  editForm.addEventListener("submit", function () {
    if (badge) badge.hidden = true;
  });

  approveForm.addEventListener("submit", function (e) {
    if (!isDirty()) return;
    e.preventDefault();
    refresh();
    window.alert(strings.unsavedBlocked);
  });

  refresh();
})();
