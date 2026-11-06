"""The shape of a ProfileItem record, for the owner's edit form and for
re-analysis (CLAUDE.md section 3: the owner reviews and edits per-item
records, and is the final authority on facts about himself).

Two schemas exist: the v1 four-field record and the phase 1 rich record
(app/profile_builder/repo_record.py). The edit form is driven by the field
list of whichever schema the record has, so one form serves both.

Owner edits are sticky: a field the owner changed is listed in
`owner_edited_fields`, and a later re-analysis never overwrites it - the
model's new value is parked in `pending_update` and shown as a diff for the
owner to accept or dismiss.
"""
import json

from app.models import ProfileItem

# (field, kind) - kinds: text, textarea, list (one per line), pairs
# (" | "-separated columns per line), mapping ("key: value" per line), json.
V1_FIELDS: list[tuple[str, str]] = [
    ("purpose", "textarea"),
    ("stack", "list"),
    ("role", "text"),
    ("highlights", "list"),
]
RICH_FIELDS: list[tuple[str, str]] = [
    ("one_liner", "text"),
    ("problem", "textarea"),
    ("approach", "textarea"),
    ("architecture", "list"),
    ("stack_used_for", "pairs:tech,for"),
    ("owner_contribution", "textarea"),
    ("results", "pairs:claim,quote,source"),
    ("domains", "list"),
    ("role_angles", "mapping"),
    ("talking_points", "list"),
    ("claims_to_verify", "list"),
    ("open_questions", "list"),
]
ENTRY_FIELDS: list[tuple[str, str]] = [
    ("entry_type", "text"),
    ("title", "text"),
    ("organization", "text"),
    ("start", "text"),
    ("end", "text"),
    ("location", "text"),
    ("bullets", "list"),
    ("stack", "list"),
    ("purpose", "textarea"),
]


def record_fields(extracted: dict) -> list[tuple[str, str]]:
    """The editable fields for this record's schema."""
    extracted = extracted or {}
    if "one_liner" in extracted:
        return RICH_FIELDS
    if "entry_type" in extracted:
        return ENTRY_FIELDS
    return V1_FIELDS


def _pair_keys(kind: str) -> list[str]:
    return kind.split(":", 1)[1].split(",")


def to_form_value(value: object, kind: str) -> str:
    """A field's value as the text the edit form shows."""
    if kind in ("text", "textarea"):
        return "" if value is None else str(value)
    if kind == "list":
        return "\n".join(str(v) for v in value or [])
    if kind.startswith("pairs:"):
        keys = _pair_keys(kind)
        return "\n".join(" | ".join(str((row or {}).get(k, "")) for k in keys) for row in value or [])
    if kind == "mapping":
        return "\n".join(f"{k}: {v}" for k, v in (value or {}).items())
    return json.dumps(value, ensure_ascii=False, indent=2)


def from_form_value(text: str, kind: str) -> object:
    """Parse the edit form's text back into the field's value."""
    text = (text or "").strip()
    if kind in ("text", "textarea"):
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if kind == "list":
        return lines
    if kind.startswith("pairs:"):
        keys = _pair_keys(kind)
        rows = []
        for line in lines:
            parts = [part.strip() for part in line.split("|", len(keys) - 1)]
            rows.append(dict(zip(keys, parts + [""] * (len(keys) - len(parts)))))
        return rows
    if kind == "mapping":
        mapping = {}
        for line in lines:
            key, _, value = line.partition(":")
            if key.strip():
                mapping[key.strip()] = value.strip()
        return mapping
    return json.loads(text) if text else None


def apply_owner_edit(item: ProfileItem, form: dict[str, str]) -> list[str]:
    """Save the owner's edits; returns the fields that changed. Changed fields
    join `owner_edited_fields` so re-analysis won't overwrite them."""
    extracted = dict(item.extracted or {})
    changed = []
    for field, kind in record_fields(extracted):
        if field not in form:
            continue
        # Compared as the form shows them, so re-saving an untouched form is
        # not an edit.
        if form[field].strip() != to_form_value(extracted.get(field), kind).strip():
            extracted[field] = from_form_value(form[field], kind)
            changed.append(field)
    if changed:
        extracted.pop("parse_error", None)
        item.extracted = _refresh_derived(item, extracted)
        item.owner_edited_fields = sorted(set(item.owner_edited_fields or []) | set(changed))
        item.pending_update = {k: v for k, v in (item.pending_update or {}).items() if k not in changed}
    return changed


def merge_reanalysis(item: ProfileItem, new_extracted: dict) -> dict:
    """The record to store after a re-analysis: the model's new values, except
    for owner-edited fields, which keep the owner's value; differing proposals
    for those go to `pending_update`."""
    owner_fields = set(item.owner_edited_fields or [])
    old = item.extracted or {}
    merged = dict(new_extracted)
    pending = dict(item.pending_update or {})
    for field in owner_fields:
        if field in old:
            if field in new_extracted and new_extracted[field] != old[field]:
                pending[field] = new_extracted[field]
            merged[field] = old[field]
    item.pending_update = pending
    return merged


def _refresh_derived(item: ProfileItem, extracted: dict) -> dict:
    if "one_liner" in extracted:
        from app.profile_builder.repo_record import with_v1_fields

        return with_v1_fields(extracted, item.facts or {})
    return extracted


def accept_pending(item: ProfileItem, field: str) -> None:
    pending = dict(item.pending_update or {})
    fields = list(pending) if field == "__all__" else [field]
    extracted = dict(item.extracted or {})
    for name in fields:
        if name in pending:
            extracted[name] = pending.pop(name)
    item.extracted = _refresh_derived(item, extracted)
    item.pending_update = pending


def dismiss_pending(item: ProfileItem, field: str) -> None:
    pending = dict(item.pending_update or {})
    if field == "__all__":
        pending = {}
    pending.pop(field, None)
    item.pending_update = pending
