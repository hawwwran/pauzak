"""Running one alignment: capture the backdrop, match it, apply the result.

This lives on the application rather than on the panel so that an overlay can
ask for an alignment the same way it asks for undo, instead of reaching into a
sibling window for it. What it does need from the panel -- which overlays are
open, where the panel itself is, and somewhere to put a message -- it takes
through a host, so nothing here knows what a window is.

The matching runs on a worker thread. Downloading a 20 MP texture and
correlating a pair of profiles is not much, but it is enough to be seen as a
hitch while the overlay is sitting there see-through, and the capture it
follows was made asynchronous for the same reason.
"""

from __future__ import annotations

import threading

from gi.repository import GLib

from . import backdrop, snap
from .overlay import screen_bounds


class Aligner:
    """One scan at a time, for whichever overlay asked."""

    def __init__(self):
        self.host = None  # set once the panel exists
        self.target = None  # the overlay being aligned, while a scan is running
        self._rect = None
        self._blinked = []

    @property
    def busy(self) -> bool:
        return self.target is not None

    def align(self, overlay) -> None:
        if overlay is None or self.host is None:
            return
        if self.busy:
            self._report("Still reading the screen.")
            return
        if not snap.available():
            self._report("Aligning needs python3-numpy.")
            return
        if overlay.hidden:
            self._report("Show the overlay before lining it up.")
            return
        rect = overlay.geometry()
        if not _inside(rect, screen_bounds()):
            # A capture can only be cropped to what is on the screen, and a
            # crop that quietly stopped at the edge would be read as though it
            # lined up with the whole window.
            self._report("Move the overlay fully onto the screen to line it up.")
            return
        if _overlap(self.host.panel_rect(), rect):
            self._report("Move the panel off the overlay: it would be scanned too.")
            return

        self.target = overlay
        # What the capture will show is where the window is now, so a window
        # that moves while the shot is being taken invalidates it.
        self._rect = rect
        # Every overlay over this one goes see-through too. They are ours, not
        # what is underneath, and lining a design up with another overlay's
        # edges is never what was asked for.
        self._blinked = [
            other
            for other in self.host.overlays()
            if other is overlay or (not other.hidden and _overlap(other.geometry(), rect))
        ]
        for other in self._blinked:
            other.begin_scan()
        # The capture is of the composited screen, so the see-through frame has
        # to have reached it before the shot is taken.
        GLib.timeout_add(backdrop.BLINK_MS, self._capture)

    def _capture(self):
        backdrop.capture_async(self._captured, self._failed)
        return GLib.SOURCE_REMOVE

    def _captured(self, pixbuf):
        overlay = self._finish_scan()
        if overlay is None:
            return
        if overlay.geometry() != self._rect:
            self._report("The overlay moved while the screen was being read.")
            return
        # Both of these touch GDK, so they stay on the main loop; what follows
        # is arrays, and goes to a thread.
        under = backdrop.crop_to(pixbuf, self._rect, screen_bounds())
        image = snap.gray_from_texture(overlay.view.texture)
        if under is None or image is None:
            self._report("Could not read the pixels to line up.")
            return
        rect = overlay.view.image_geometry()
        size = (overlay.get_width(), overlay.get_height())
        threading.Thread(
            target=self._match, args=(overlay, image, rect, size, under), daemon=True
        ).start()

    def _match(self, overlay, image, rect, size, under):
        """Off the main loop. Nothing here touches a widget."""
        try:
            alignment = snap.align(image, rect, size, snap.gray_from_pixbuf(under))
        except Exception as error:  # noqa: BLE001  a worker's failure must not vanish
            GLib.idle_add(self._report, f"Could not line the image up: {error}")
            return
        GLib.idle_add(self._apply, overlay, alignment)

    def _apply(self, overlay, alignment):
        if not alignment.guides:
            self._report("Found no edges underneath to line up with.")
        elif alignment.moved():
            overlay.apply_alignment(alignment)
            self._report(f"Moved the image {alignment.dx:+.0f}, {alignment.dy:+.0f} px")
        else:
            overlay.show_guides(alignment.guides)
            self._report("Already lined up.")
        return GLib.SOURCE_REMOVE

    def _failed(self, message):
        self._finish_scan()
        self._report(f"Could not read the screen: {message}")

    def _finish_scan(self):
        """Put every overlay that went see-through back, and hand back the one
        being aligned."""
        overlay, self.target = self.target, None
        for other in self._blinked:
            other.end_scan()
        self._blinked = []
        return overlay

    def _report(self, message: str):
        if self.host is not None:
            self.host.toast(message)
        return GLib.SOURCE_REMOVE


def _overlap(first, second) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _inside(rect, bounds) -> bool:
    if bounds is None:
        return True
    left, top, right, bottom = bounds
    x, y, width, height = rect
    return x >= left and y >= top and x + width <= right and y + height <= bottom
