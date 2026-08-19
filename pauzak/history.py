"""Recently used images, for the control panel's history strip.

Two sources, deliberately unequal. The first few slots go to the desktop's
recent-files list, but only to entries used within the last quarter of an hour:
an image that was just being worked on in another application is then one click
away, while the thousand-odd older entries in that list never reach the strip.
Everything after those slots comes from pauzak's own history, which is the only
record of what has actually been used as tracing paper.

Timestamps come from the recent-files entry rather than the file's mtime. The
question the strip answers is "what was I just working with", and an entry's
time is when it was last opened; a file's mtime says when its bytes last
changed, which for a photo opened this minute can be years ago.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

STRIP_LIMIT = 10  # thumbnails the strip shows at most
RECENT_SLOTS = 3  # of those, how many the desktop's recent files may claim
RECENT_MAX_AGE = 15 * 60  # seconds; older recent-files entries are ignored
STORE_LIMIT = 32  # paths kept on disk, enough to refill the strip as files go


def history_path() -> Path:
    return Path(GLib.get_user_data_dir()) / "pauzak" / "history.json"


def remember(path: str | None) -> None:
    """Put a file at the head of pauzak's own history. Silent on failure.

    A pasted image has no path and simply is not recorded: there would be
    nothing for the strip to re-open.
    """
    if not path:
        return
    resolved = str(Path(path).expanduser())
    kept = [resolved] + [item for item in _load() if item != resolved]
    _save(kept[:STORE_LIMIT])


def strip_paths(now: float | None = None) -> list[str]:
    """The strip's contents, newest first, unreadable entries dropped."""
    now = GLib.get_real_time() / 1e6 if now is None else now
    chosen: list[str] = []
    for path in _fresh_recent_images(now) + _load():
        if len(chosen) >= STRIP_LIMIT:
            break
        if path not in chosen and _readable(path):
            chosen.append(path)
    return chosen


def _readable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _fresh_recent_images(now: float) -> list[str]:
    fresh = []
    for info in Gtk.RecentManager.get_default().get_items():
        mime = info.get_mime_type() or ""
        if not mime.startswith("image/") or not info.is_local():
            continue
        age = now - _used_at(info)
        if age > RECENT_MAX_AGE:
            continue
        path = _local_path(info.get_uri())
        if path is not None:
            fresh.append((age, path))
    fresh.sort()
    return [path for _age, path in fresh[:RECENT_SLOTS]]


def _used_at(info: Gtk.RecentInfo) -> float:
    stamps = [
        stamp.to_unix()
        for stamp in (info.get_visited(), info.get_modified(), info.get_added())
        if stamp is not None
    ]
    return max(stamps, default=0.0)


def _local_path(uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        path, _hostname = GLib.filename_from_uri(uri)
    except GLib.Error:
        return None
    return path


def _load() -> list[str]:
    try:
        with history_path().open(encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return []
    return [item for item in stored if isinstance(item, str)] if isinstance(stored, list) else []


def _save(paths: list[str]) -> None:
    """Best effort: a history that cannot be written is not worth an error."""
    target = history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(paths, handle, indent=1)
    except OSError:
        pass
