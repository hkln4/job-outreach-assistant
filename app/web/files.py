"""Filename sanitization for anything derived from user input (a display name,
or a browser-supplied upload filename) before it's used to build a filesystem
path - prevents slashes/".."/etc. from writing outside the target directory or
into a non-existent subdirectory."""
import re
import uuid
from pathlib import Path


def safe_component(text: str) -> str:
    """Turn arbitrary user text (e.g. a CV's display name) into one safe path segment."""
    cleaned = re.sub(r"[^\w\-]+", "_", text.strip()).strip("_")
    return cleaned or "file"


def unique_stored_filename(display_name: str, suffix: str) -> str:
    """Disk name for an uploaded file: readable, but never shared with another
    record - two CVs with the same display name used to overwrite each other's
    file, and deleting one removed the other's attachment."""
    return f"{safe_component(display_name)}-{uuid.uuid4().hex[:8]}{suffix}"


def safe_uploaded_filename(filename: str) -> str:
    """Strip any directory components from a browser-supplied filename and
    sanitize what's left, keeping the original extension."""
    base = Path(filename).name  # drops any leading path/".." segments
    stem, suffix = Path(base).stem, Path(base).suffix
    return safe_component(stem) + suffix
