"""Entry point. Selects the X11 backend before GTK touches a display."""

from __future__ import annotations

import os
import sys

# A Wayland client can neither request always-on-top nor place itself on
# screen, and pauzak needs both; under GNOME that means going through XWayland.
# Set PAUZAK_ALLOW_WAYLAND=1 to try the native backend anyway.
if os.environ.get("PAUZAK_ALLOW_WAYLAND") != "1":
    os.environ["GDK_BACKEND"] = "x11"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")

from gi.repository import GLib  # noqa: E402

from . import APP_ID  # noqa: E402
from .app import PauzakApplication  # noqa: E402


def main() -> int:
    # Without this the WM sees WM_CLASS "__main__.py", so the desktop file's
    # StartupWMClass never matches and the window gets no icon.
    GLib.set_prgname(APP_ID)
    return PauzakApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
