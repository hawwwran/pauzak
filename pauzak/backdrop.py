"""Reading the pixels underneath an overlay.

Every cheap way in is shut on a GNOME Wayland session; measured on GNOME Shell
46. XGetImage on the Xwayland root fails with BadMatch, that root having no
backing pixmap, so forcing the x11 backend buys nothing here. And
org.gnome.Shell.Screenshot answers AccessDenied to anything that is not the
shell's own screenshot UI. What is left is gnome-screenshot at about 0.8 s for
the whole screen: fine for an alignment the user asks for, hopeless for
anything continuous. Snapping while dragging wants the ScreenCast portal and a
PipeWire stream, which is a session, a consent dialog and a sharing indicator
to go with it.

A capture is of the composited screen, overlay included, which is why the
overlay stops drawing itself while one is taken (OverlayWindow.begin_scan).
"""

from __future__ import annotations

import os

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

SCREENSHOT = "gnome-screenshot"
BLINK_MS = 90  # time for the overlay's transparent frame to reach the screen


def capture_async(on_ready, on_error) -> None:
    """Take a shot of the whole screen and hand it back as a GdkPixbuf."""
    target = os.path.join(
        GLib.get_user_runtime_dir() or GLib.get_tmp_dir(), f"pauzak-scan-{os.getpid()}.png"
    )
    try:
        process = Gio.Subprocess.new(
            [SCREENSHOT, "-f", target],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
        )
    except GLib.Error as error:
        on_error(error.message)
        return
    process.wait_check_async(
        None, lambda proc, result: _finished(proc, result, target, on_ready, on_error)
    )


def _finished(process, result, target: str, on_ready, on_error) -> None:
    pixbuf = None
    try:
        process.wait_check_finish(result)
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(target)
    except GLib.Error as error:
        on_error(error.message)
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass
    if pixbuf is not None:
        on_ready(pixbuf)


def crop_to(pixbuf, rect, bounds):
    """The part of a full-screen capture that a window covers.

    The capture is in device pixels and the window's rectangle in logical ones.
    The ratio between the capture and the screen's own extent is what converts
    between the two, which is the display's scale without having to ask for it.
    """
    if pixbuf is None or bounds is None:
        return None
    left, top, right, bottom = bounds
    scale_x = pixbuf.get_width() / max(1.0, right - left)
    scale_y = pixbuf.get_height() / max(1.0, bottom - top)
    x = max(0, int(round((rect[0] - left) * scale_x)))
    y = max(0, int(round((rect[1] - top) * scale_y)))
    width = min(int(round(rect[2] * scale_x)), pixbuf.get_width() - x)
    height = min(int(round(rect[3] * scale_y)), pixbuf.get_height() - y)
    if width < 2 or height < 2:
        return None
    return pixbuf.new_subpixbuf(x, y, width, height)
