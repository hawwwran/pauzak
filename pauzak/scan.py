"""Reading the screen under an overlay, and what the two things do with it.

Lining the image up and marking where it disagrees are the same errand as far
as the screen is concerned: refuse for the same reasons, go see-through, take
one capture, crop it to the window. Only the arithmetic at the end differs, so
that is the only part either operation supplies.

This lives on the application rather than on the panel so that an overlay can
ask for either the same way it asks for undo, instead of reaching into a
sibling window. What it does need from the panel -- which overlays are open,
where the panel itself is, and somewhere to put a message -- it takes through a
host, so nothing here knows what a window is.

The arithmetic runs on a worker thread. Downloading a 20 MP texture and
comparing it against the screen is not much, but it is enough to be seen as a
hitch while the overlay is sitting there see-through, and the capture it
follows was made asynchronous for the same reason.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

from . import backdrop, diff, snap
from .overlay import screen_bounds


class Scanner:
    """One scan at a time, for whichever overlay asked."""

    def __init__(self):
        self.host = None  # set once the panel exists
        self.target = None  # the overlay being scanned, while one is running
        self._rect = None
        self._blinked = []
        self._work = None  # what to do with the capture, once it arrives
        self._errand = "do that"

    @property
    def busy(self) -> bool:
        return self.target is not None

    def align(self, overlay) -> None:
        """Pan the image onto the edges underneath it."""
        self._start(overlay, self._match, "line it up")

    def diff(self, overlay, fresh: bool = False) -> None:
        """Mark where the image and what is under it disagree.

        Marks already worked out are simply shown or put away again: reading the
        screen takes about a second, and having to wait it out to glance from
        one to the other and back would make the comparison useless. `fresh`
        asks for the screen to be read again, for when what is under the overlay
        has moved on.
        """
        if overlay is not None and not fresh and overlay.toggle_diff():
            return
        if not diff.available():
            self._report("The visual diff needs python3-numpy.")
            return
        self._start(overlay, self._compare, "compare it")

    def _start(self, overlay, work, errand: str) -> None:
        """`errand` is what the caller is here to do, for when it cannot."""
        if overlay is None or self.host is None:
            return
        if self.busy:
            self._report("Still reading the screen.")
            return
        if not snap.available():
            self._report("This needs python3-numpy.")
            return
        if overlay.hidden:
            self._report(f"Show the overlay before you {errand}.")
            return
        rect = overlay.geometry()
        if not _inside(rect, screen_bounds()):
            # A capture can only be cropped to what is on the screen, and a
            # crop that quietly stopped at the edge would be read as though it
            # covered the whole window.
            self._report(f"Move the overlay fully onto the screen to {errand}.")
            return
        if _overlap(self.host.panel_rect(), rect):
            self._report("Move the panel off the overlay: it would be scanned too.")
            return

        self.target = overlay
        self._work = work
        self._errand = errand
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
        # Reading a texture and cropping a pixbuf are GDK's business and stay
        # on the main loop; what follows is arrays, and goes to a thread.
        under = backdrop.crop_to(pixbuf, self._rect, screen_bounds())
        image = Gdk.pixbuf_get_from_texture(overlay.view.texture)
        if under is None or image is None:
            self._report(f"Could not read the pixels to {self._errand}.")
            return
        rect = overlay.view.image_geometry()
        size = (overlay.get_width(), overlay.get_height())
        # The arrangement the comparison is about, taken now rather than when
        # the answer comes back: the image may be panned while it is being
        # worked out, and marks stamped with where it ended up would describe
        # one arrangement while claiming to belong to another.
        threading.Thread(
            target=self._work,
            args=(overlay, image, rect, size, under, overlay.arrangement()),
            daemon=True,
        ).start()

    def _match(self, overlay, image, rect, size, under, _arrangement):
        """Off the main loop. Nothing here touches a widget."""
        try:
            alignment = snap.align(
                snap.gray_from_pixbuf(image), rect, size, snap.gray_from_pixbuf(under)
            )
        except Exception as error:  # noqa: BLE001  a worker's failure must not vanish
            GLib.idle_add(self._report, f"Could not line the image up: {error}")
            return
        GLib.idle_add(self._applied, overlay, alignment)

    def _compare(self, overlay, image, rect, size, under, arrangement):
        """Off the main loop, as _match is."""
        try:
            result = _difference(image, rect, size, under)
        except Exception as error:  # noqa: BLE001  a worker's failure must not vanish
            GLib.idle_add(self._failed_comparison, overlay, f"Could not compare: {error}")
            return
        GLib.idle_add(self._compared, overlay, result, arrangement)

    def _compared(self, overlay, found, arrangement):
        if found is None:
            return self._failed_comparison(
                overlay, "Nothing of the image is over the screen to compare."
            )
        result, rect = found
        if result.coverage <= 0:
            # Any marks still up are the answer to an older question, and
            # leaving them would pass them off as this one's.
            overlay.clear_diff()
            self._report("No differences worth marking.")
            return GLib.SOURCE_REMOVE
        # The textures are made here rather than on the worker: they are GDK's.
        overlay.show_diff(
            diff.texture_from_rgba(result.rgba),
            rect,
            arrangement,
            diff.texture_from_rgba(result.base),
        )
        palette = (
            "Green: the image is darker there; red: the screen is."
            if overlay.view.diff_style == "diff"
            else "Magenta: the image is darker there; yellow: the screen is."
        )
        self._report(f"{result.coverage * 100:.1f}% differs. {palette}")
        return GLib.SOURCE_REMOVE

    def _applied(self, overlay, alignment):
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
        overlay = self._finish_scan()
        if overlay is not None:
            overlay.clear_diff()
        self._report(f"Could not read the screen: {message}")

    def _failed_comparison(self, overlay, message):
        """Nothing to show, so nothing is shown: whatever was up was an answer
        to a question that has since been asked again."""
        overlay.clear_diff()
        self._report(message)
        return GLib.SOURCE_REMOVE

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


def _difference(image, rect, size, under):
    """Compare the drawn image against the screen, in window pixels.

    The two arrive at different scales -- the image at its own resolution, the
    capture at the screen's -- so both are resampled into the window's
    coordinates and cut down to the part where they actually overlap. Outside
    that there is nothing to compare: the image does not reach.
    """
    left, top, drawn_w, drawn_h = (int(round(value)) for value in rect)
    window_w, window_h = size
    x = max(0, left)
    y = max(0, top)
    width = min(window_w, left + drawn_w) - x
    height = min(window_h, top + drawn_h) - y
    if width < 2 or height < 2 or drawn_w < 1 or drawn_h < 1:
        return None

    screen = under.scale_simple(window_w, window_h, GdkPixbuf.InterpType.BILINEAR)
    image_part = _scaled_part(image, drawn_w, drawn_h, x - left, y - top, width, height)
    if screen is None or image_part is None:
        return None
    screen_part = screen.new_subpixbuf(x, y, width, height)
    screen_rgb = diff.rgb_from_pixbuf(screen_part)
    result = diff.compare(diff.over(image_part, screen_rgb), screen_rgb)
    return None if result is None else (result, (x, y, width, height))


def _scaled_part(image, drawn_w, drawn_h, skip_x, skip_y, width, height):
    """The visible piece of the image, scaled as drawn and no further.

    Scaling the whole of it first would be a picture the size of the zoom: a
    12 MP photo at 32x is a hundred thousand pixels across, tens of gigabytes
    of it, to read a few hundred thousand pixels out of the middle. This
    renders only the piece the window shows.

    `skip_x`, `skip_y` are how far into the scaled image the visible piece
    starts. gdk_pixbuf_scale's offsets say where the scaled image is placed in
    the destination, which is the negative of that: passing them unnegated
    slides the piece the wrong way by twice the skip, and the comparison then
    reads the image from the wrong rows -- marks for content that is not
    there, in a place where nothing differs.
    """
    part = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    if part is None:
        return None
    part.fill(0x00000000)
    image.scale(
        part,
        0,
        0,
        width,
        height,
        -skip_x,
        -skip_y,
        drawn_w / max(1, image.get_width()),
        drawn_h / max(1, image.get_height()),
        GdkPixbuf.InterpType.BILINEAR,
    )
    return part


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
