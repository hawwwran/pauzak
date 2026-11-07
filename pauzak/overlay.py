"""The overlay window: an undecorated image pane pinned over other windows."""

from __future__ import annotations

from contextlib import contextmanager
from typing import NamedTuple

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from . import x11

RESIZE_MARGIN = 10  # px hot zone along the window edges
MIN_SIZE = 80  # px floor for an interactive resize
SLOW_FACTOR = 0.2  # pointer-to-motion ratio while Shift is held
SNAP_DISTANCE = 50  # px pull towards the window's edges while Alt is held
WHEEL_STEP = 1.15
WHEEL_STEP_SLOW = 1.04
MIN_ZOOM, MAX_ZOOM = 0.05, 32.0
BORDER_WIDTH = 1.0
BORDER_COLOR = "#e01b24"  # Adwaita red; reads against most image content
GEOMETRY_SETTLE_MS = 120  # how long to give the WM before reading a move back
# Snap guides: the image's own edge, the edge underneath it, and the pair that
# was taken. Blue and green are dashed and come in twos, so a near miss reads
# as two lines a few pixels apart rather than as something in the image.
GUIDE_IMAGE_COLOR = "#3584e4"
GUIDE_TARGET_COLOR = "#26a269"
GUIDE_LOCKED_COLOR = "#f6d32d"
GUIDE_DASH_ON, GUIDE_DASH_OFF = 7.0, 5.0
GUIDE_LINGER_MS = 5000  # how long guides stay up after a scan


def parse_color(spec: str) -> Gdk.RGBA:
    """The colour a string names, or the border's own colour if it names none."""
    rgba = Gdk.RGBA()
    if not rgba.parse(spec):
        rgba.parse(BORDER_COLOR)
    return rgba


def default_border_color() -> Gdk.RGBA:
    return parse_color(BORDER_COLOR)


_TRANSPARENT = Gdk.RGBA()  # a fresh RGBA is already all zeroes
_GUIDE_COLORS = {
    "image": parse_color(GUIDE_IMAGE_COLOR),
    "target": parse_color(GUIDE_TARGET_COLOR),
    "locked": parse_color(GUIDE_LOCKED_COLOR),
}


class State(NamedTuple):
    """Everything about an overlay that undo has to be able to put back.

    A plain value, compared field by field: two equal states mean nothing
    happened and there is nothing to record. The border colour is kept as its
    string form because Gdk.RGBA does not compare by value.
    """

    x: int
    y: int
    width: int
    height: int
    pan_x: float
    pan_y: float
    zoom: float
    opacity: float
    touchable: bool
    always_on_top: bool
    border_visible: bool
    border_color: str
    hidden: bool


def screen_bounds() -> tuple[float, float, float, float] | None:
    """left, top, right, bottom of every monitor taken together.

    The union rather than one monitor's geometry: an overlay dragged towards a
    neighbouring screen has to be allowed to cross onto it. Monitor geometry
    rather than the work area, since an overlay is meant to be able to sit over
    a panel or a dock.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return None
    monitors = display.get_monitors()
    areas = [
        monitors.get_item(index).get_geometry() for index in range(monitors.get_n_items())
    ]
    if not areas:
        return None
    return (
        min(area.x for area in areas),
        min(area.y for area in areas),
        max(area.x + area.width for area in areas),
        max(area.y + area.height for area in areas),
    )


def _snapped(value: float, edge_pan: float, distance: float) -> float:
    """Pull `value` onto +/-edge_pan when it comes within `distance` of one.

    The two pan values that put the image's leading and trailing edge flush
    with the window's are symmetrical around no pan at all, which is why one
    offset describes both.
    """
    nearest = min((-edge_pan, edge_pan), key=lambda candidate: abs(value - candidate))
    return nearest if abs(value - nearest) <= distance else value


def _clamp_span(value: float, extent: float, lower: float, upper: float) -> float:
    """Keep the span [value, value + extent] inside [lower, upper].

    A window larger than the span inverts the limits, which turns the clamp
    into the opposite rule -- it may hang off both ends but not pull a gap in
    at either -- so an oversized image can still be dragged across.
    """
    low, high = lower, upper - extent
    if low > high:
        low, high = high, low
    return min(max(value, low), high)

# Edges are named by compass letters, so "ne" simply contains both "n" and "e".
_RESIZE_CURSORS = {
    "n": "n-resize",
    "s": "s-resize",
    "w": "w-resize",
    "e": "e-resize",
    "nw": "nw-resize",
    "ne": "ne-resize",
    "sw": "sw-resize",
    "se": "se-resize",
}


class ImageView(Gtk.Widget):
    """Draws the texture scaled and panned inside the widget's own bounds.

    Rendering goes through the snapshot API rather than cairo so the scale is a
    GPU operation: panning a 20 MP photo at 400% stays smooth.
    """

    __gtype_name__ = "PauzakImageView"

    def __init__(self, texture: Gdk.Texture):
        super().__init__()
        self.texture = texture
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.image_opacity = 1.0
        self.border_visible = False
        self.border_color = default_border_color()
        # Guides are snap.Guide values; the view only reads their fields.
        self.guides = ()
        self.scanning = False
        self.diff = None  # (texture, rect, arrangement, base) once one has been made
        self.diff_visible = True
        self.diff_style = "full"  # "diff" shows the greyed screen, marks in git colours
        self.position_source = None  # set by the window; see arrangement()
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot):  # noqa: N802  (GTK vfunc naming)
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        if self.scanning:
            # See-through for a capture, border and guides included. It has to
            # be an explicit transparent fill rather than no nodes at all: an
            # empty snapshot leaves the last frame standing on screen, and the
            # capture then finds the overlay's own image looking back at it.
            snapshot.append_color(
                _TRANSPARENT, Graphene.Rect().init(0, 0, width, height)
            )
            return
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        marks = self._valid_diff() if self.diff_visible else None
        if marks is not None:
            # The image itself is not drawn under a comparison: what the marks
            # are about is the screen, and it has to stay visible. Nor is the
            # opacity slider applied to them -- they are a reading, like the
            # border and the guides, not tracing paper. The "diff" style shows
            # the other rendering: the greyed screen with the marks baked in,
            # where colour means difference.
            texture, rect, _key, base = marks
            area = Graphene.Rect().init(*rect)
            snapshot.append_texture(base if self.diff_style == "diff" else texture, area)
        else:
            snapshot.push_opacity(self.image_opacity)
            snapshot.append_scaled_texture(
                self.texture, self._filter(), self._image_rect()
            )
            snapshot.pop()
        snapshot.pop()
        if self.border_visible:
            self._snapshot_border(snapshot, width, height)
        if self.guides:
            self._snapshot_guides(snapshot, width, height)

    def _snapshot_border(self, snapshot, width: int, height: int) -> None:
        """Outside the opacity push on purpose.

        The border marks where the window ends, which is exactly what you
        cannot see once a pale image is faded down -- and the edges are what
        you grab to resize. Fading it along with the image would remove it
        precisely when it earns its keep. Pick a translucent colour if you
        want a fainter one.
        """
        outline = Gsk.RoundedRect()
        outline.init_from_rect(Graphene.Rect().init(0, 0, width, height), 0)
        snapshot.append_border(outline, [BORDER_WIDTH] * 4, [self.border_color] * 4)

    def set_diff(self, texture, rect, arrangement, base) -> None:
        """`arrangement` is what the marks were measured against, which is not
        necessarily what is on screen by the time they arrive."""
        self.diff = (texture, rect, arrangement, base)
        self.diff_visible = True
        self.queue_draw()

    def _valid_diff(self):
        """The marks, if the image has not moved since they were worked out.

        Keeping them after a pan, a zoom or a resize would be a measurement of
        one arrangement shown over another, so they are dropped the moment the
        arrangement they belong to is gone -- whether they were on show at the
        time or merely put away.
        """
        if self.diff is not None and self.diff[2] != self.arrangement():
            self.diff = None
        return self.diff

    def arrangement(self):
        """Everything that decides what the window is showing and where.

        The window's own place on screen belongs in here: the marks describe
        what was underneath it, and dragging the window puts something else
        there just as surely as panning the image does.
        """
        return (
            self.zoom,
            self.pan_x,
            self.pan_y,
            self.get_width(),
            self.get_height(),
            self.window_position(),
        )

    def _snapshot_guides(self, snapshot, width: int, height: int) -> None:
        """Solid where a match was taken, dashed pairs where one was possible."""
        for guide in self.guides:
            if guide.chosen:
                self._guide_line(snapshot, guide, guide.image, "locked", width, height, False)
                continue
            self._guide_line(snapshot, guide, guide.target, "target", width, height, True)
            self._guide_line(snapshot, guide, guide.image, "image", width, height, True)

    def _guide_line(self, snapshot, guide, position, role, width, height, dashed) -> None:
        color = _GUIDE_COLORS[role]
        vertical = guide.axis == "x"
        length = height if vertical else width
        offset = round(position)
        run = GUIDE_DASH_ON if dashed else length
        start = 0.0
        while start < length:
            extent = min(run, length - start)
            rect = (
                Graphene.Rect().init(offset, start, 1, extent)
                if vertical
                else Graphene.Rect().init(start, offset, extent, 1)
            )
            snapshot.append_color(color, rect)
            start += run + (GUIDE_DASH_OFF if dashed else 0.0)

    def _filter(self):
        return Gsk.ScalingFilter.LINEAR if self.zoom >= 1.0 else Gsk.ScalingFilter.TRILINEAR

    def _image_rect(self) -> Graphene.Rect:
        x, y, w, h = self.image_geometry()
        return Graphene.Rect().init(x, y, w, h)

    def window_position(self):
        """Overridden from outside: a view does not know where its window is."""
        return self.position_source() if self.position_source is not None else (0, 0)

    def image_geometry(self) -> tuple[float, float, float, float]:
        scaled_w = self.texture.get_width() * self.zoom
        scaled_h = self.texture.get_height() * self.zoom
        x = (self.get_width() - scaled_w) / 2 + self.pan_x
        y = (self.get_height() - scaled_h) / 2 + self.pan_y
        if abs(self.zoom - 1.0) < 1e-9:
            # 1:1 only looks 1:1 if the texture lands on whole device pixels.
            x, y = round(x), round(y)
        return x, y, scaled_w, scaled_h

    def set_zoom(self, zoom: float) -> None:
        self.zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        self.queue_draw()

    def zoom_at(self, cx: float, cy: float, zoom: float) -> None:
        """Zoom while keeping the image point under (cx, cy) in place."""
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        origin_x, origin_y, _, _ = self.image_geometry()
        image_x = (cx - origin_x) / self.zoom
        image_y = (cy - origin_y) / self.zoom
        self.zoom = zoom
        tex_w = self.texture.get_width() * zoom
        tex_h = self.texture.get_height() * zoom
        self.pan_x = cx - image_x * zoom - (self.get_width() - tex_w) / 2
        self.pan_y = cy - image_y * zoom - (self.get_height() - tex_h) / 2
        self.queue_draw()

    def set_pan(self, pan_x: float, pan_y: float) -> None:
        self.pan_x, self.pan_y = pan_x, pan_y
        self.queue_draw()

    def snap_pan(self, pan_x: float, pan_y: float, distance: float) -> tuple[float, float]:
        """The given pan with each axis pulled flush to a window edge if close."""
        edge_x = (self.get_width() - self.texture.get_width() * self.zoom) / 2
        edge_y = (self.get_height() - self.texture.get_height() * self.zoom) / 2
        return _snapped(pan_x, edge_x, distance), _snapped(pan_y, edge_y, distance)

    def center(self) -> None:
        self.pan_x = self.pan_y = 0.0
        self.queue_draw()

    def fit_zoom(self) -> float:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return 1.0
        return min(width / self.texture.get_width(), height / self.texture.get_height())


class OverlayWindow(Gtk.Window):
    """One pinned image. All of its knobs are driven from the control panel."""

    __gtype_name__ = "PauzakOverlay"

    __gsignals__ = {"state-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(
        self,
        application,
        texture: Gdk.Texture,
        name: str,
        key: int = 0,
        state: State | None = None,
    ):
        super().__init__(application=application, title=f"pauzak - {name}")
        self.image_name = name
        self.key = key  # identity that survives being closed and reopened
        self.recorder = None  # set by the panel; see _record()
        self.touchable = True
        self.always_on_top = True
        self.hidden = False
        # Where and how big it was when it went away. An unmapped window
        # reports a size of zero, so this is the only record of it.
        self._hidden_rect = None
        self._xid = 0
        self._drag_last = (0, 0)
        self._drag_edge = None
        self._drag_rect = [0.0, 0.0, 0.0, 0.0]
        self._drag_pan = [0.0, 0.0]
        self._restoring = False
        self._edit_depth = 0
        self._edit_before = None
        self._requested = None  # geometry asked for but not yet confirmed
        self._settle_timer = 0
        self._guide_timer = 0
        # An overlay brought back by undo has to be placed and sized once the
        # WM has a window to place, which is not before the first map.
        self._pending_state = state

        self.set_decorated(False)
        self.add_css_class("pauzak-overlay")
        self.view = ImageView(texture)
        self.view.position_source = self.window_position
        self.set_child(self.view)
        size = (state.width, state.height) if state else self._initial_size(texture)
        self.set_default_size(*size)

        self._install_controllers()
        self._install_actions()
        self.connect("realize", self._on_realize)
        self.connect("map", self._on_map)

    # -- lifecycle ---------------------------------------------------------

    def _initial_size(self, texture: Gdk.Texture) -> tuple[int, int]:
        width, height = texture.get_width(), texture.get_height()
        display = Gdk.Display.get_default()
        monitor = display.get_monitors().get_item(0) if display else None
        if monitor is not None:
            area = monitor.get_geometry()
            limit = min(area.width * 0.8 / width, area.height * 0.8 / height, 1.0)
            width, height = int(width * limit), int(height * limit)
        return max(width, 120), max(height, 80)

    def _on_realize(self, *_args):
        surface = self.get_surface()
        self._xid = x11.surface_xid(surface)
        # The WM only reads this at MapRequest; toggles later go via client
        # messages in _apply_above().
        if self.always_on_top:
            x11.set_initial_states(self._xid, ["_NET_WM_STATE_ABOVE"])
        surface.connect("notify::width", self._on_surface_resized)
        surface.connect("notify::height", self._on_surface_resized)

    def _on_map(self, *_args):
        x11.set_skip_taskbar(self._xid, True)
        self._apply_above()
        self._apply_input_region()
        if self._hidden_rect is not None:
            # Hiding keeps the surface and its size but not its place on
            # screen: the WM re-places the window when it maps again.
            x11.move(self._xid, *self._hidden_rect[:2])
            self._hidden_rect = None
        if self._pending_state is not None:
            state, self._pending_state = self._pending_state, None
            self.restore(state)
        # Nothing notifies on a surface's first size, so the panel would still
        # be showing the nothing it had before the window was placed and sized.
        self._settle_later()

    def _on_surface_resized(self, *_args):
        self._apply_input_region()
        self.emit("state-changed")

    # -- window properties -------------------------------------------------

    def _apply_input_region(self):
        """An empty input region is what makes the window click-through."""
        surface = self.get_surface()
        if surface is None:
            return
        if self.touchable and not self.view.scanning:
            rect = cairo.RectangleInt(0, 0, surface.get_width(), surface.get_height())
            region = cairo.Region(rect)
        else:
            # Nothing to hit while a capture is being taken: the window is
            # see-through for it, and an invisible window that still swallows
            # clicks is worse than one that is merely in the way.
            region = cairo.Region()
        surface.set_input_region(region)

    def _apply_above(self):
        if self._xid:
            x11.set_above(self._xid, self.always_on_top)

    def set_touchable(self, enabled: bool) -> None:
        with self.edit("touchable"):
            self.touchable = enabled
            self._apply_input_region()
        self.emit("state-changed")

    def set_always_on_top(self, enabled: bool) -> None:
        with self.edit("on-top"):
            self.always_on_top = enabled
            self._apply_above()
        self.emit("state-changed")

    def set_image_opacity(self, value: float) -> None:
        with self.edit("opacity"):
            self.view.image_opacity = min(max(value, 0.0), 1.0)
            self.view.queue_draw()
        self.emit("state-changed")

    def set_hidden(self, hidden: bool) -> None:
        if hidden == self.hidden:
            return
        with self.edit("hidden"):
            if hidden:
                # Read before hiding: afterwards there is nothing to read.
                self._hidden_rect = self.geometry()
            self.hidden = hidden
            if hidden:
                self.set_visible(False)
            else:
                self.present()
        self.emit("state-changed")

    def set_border_visible(self, enabled: bool) -> None:
        with self.edit("border"):
            self.view.border_visible = enabled
            self.view.queue_draw()
        self.emit("state-changed")

    def set_border_color(self, color: Gdk.RGBA) -> None:
        with self.edit("border-color"):
            self.view.border_color = color
            self.view.queue_draw()
        self.emit("state-changed")

    def set_zoom(self, zoom: float) -> None:
        with self.edit("zoom"):
            self.view.set_zoom(zoom)
        self.emit("state-changed")

    def set_image_pan(self, pan_x: float, pan_y: float) -> None:
        with self.edit("pan"):
            self.view.set_pan(pan_x, pan_y)
        self.emit("state-changed")

    def zoom_one_to_one(self) -> None:
        with self.edit():
            self.set_zoom(1.0)

    def zoom_to_fit(self) -> None:
        with self.edit():
            self.view.center()
            self.set_zoom(self.view.fit_zoom())

    def center_image(self) -> None:
        with self.edit():
            self.view.center()
        self.emit("state-changed")

    # -- lining up with what is underneath ---------------------------------

    def begin_scan(self) -> None:
        """Stop drawing, so a screen capture sees through to the backdrop.

        Deliberately not the opacity setting: this is not a change to the
        overlay's state, so neither the panel nor undo should hear about it.
        """
        self.view.scanning = True
        self.view.queue_draw()
        self._apply_input_region()

    def end_scan(self) -> None:
        self.view.scanning = False
        self.view.queue_draw()
        self._apply_input_region()

    def apply_alignment(self, alignment) -> None:
        """Pan by what a scan found, and leave its guides up for a moment."""
        with self.edit("align"):
            self.view.set_pan(
                self.view.pan_x + alignment.dx, self.view.pan_y + alignment.dy
            )
        self.show_guides(alignment.guides)
        self.emit("state-changed")

    def show_diff(self, texture, rect, arrangement, base) -> None:
        self.view.set_diff(texture, rect, arrangement, base)
        self.emit("state-changed")

    def set_diff_style(self, style: str) -> None:
        """"full" lays the marks on the live page; "diff" greys the page first.

        Like the marks themselves this is a way of reading, not a change to the
        overlay, so undo hears nothing about it.
        """
        if style == self.view.diff_style:
            return
        self.view.diff_style = style
        self.view.queue_draw()
        self.emit("state-changed")

    def clear_diff(self) -> None:
        if self.view.diff is None:
            return
        self.view.diff = None
        self.view.queue_draw()
        self.emit("state-changed")

    def toggle_diff(self) -> bool:
        """Put the marks away, or bring the same ones back. False if there are
        none to show, in which case something has to go and read the screen."""
        if not self.has_diff():
            return False
        self.view.diff_visible = not self.view.diff_visible
        self.view.queue_draw()
        self.emit("state-changed")
        return True

    def has_diff(self) -> bool:
        """Whether a comparison has been made and still fits the image."""
        return self.view._valid_diff() is not None

    def arrangement(self):
        return self.view.arrangement()

    def showing_diff(self) -> bool:
        return self.has_diff() and self.view.diff_visible

    def show_guides(self, guides) -> None:
        self.view.guides = tuple(guides)
        self.view.queue_draw()
        if self._guide_timer:
            GLib.source_remove(self._guide_timer)
        self._guide_timer = GLib.timeout_add(GUIDE_LINGER_MS, self._on_guides_expired)

    def _on_guides_expired(self):
        self._guide_timer = 0
        self.view.guides = ()
        self.view.queue_draw()
        return GLib.SOURCE_REMOVE

    def window_position(self) -> tuple[int, int]:
        """Root coordinates of the window's top-left corner.

        While the overlay is hidden there is no window on screen to ask, and
        _hidden_rect is the rectangle it will come back to.
        """
        if self._hidden_rect is not None:
            return self._hidden_rect[:2]
        return x11.position(self._xid) if self._xid else (0, 0)

    def geometry(self) -> tuple[int, int, int, int]:
        """Position and size, preferring one that has been asked for.

        Moving and resizing are requests to the WM, so between the request and
        the WM acting on it X still reports the old rectangle. Reading that back
        into the panel would put the number the user just typed back to what it
        was, which is worse than a reading that is briefly optimistic.
        """
        if self._requested is not None:
            return self._requested
        if self._hidden_rect is not None:
            return self._hidden_rect
        return (*self.window_position(), self.get_width(), self.get_height())

    def set_window_geometry(self, x: float, y: float, width: float, height: float) -> None:
        """Place and size the window from outside, e.g. from the panel's spins.

        The move is a request to the WM, so what goes into the history is the
        geometry asked for rather than one read back before the WM has acted on
        it, and the panel is told to look again once it has.
        """
        width, height = max(MIN_SIZE, round(width)), max(MIN_SIZE, round(height))
        x, y = self._on_screen(round(x), round(y), width, height)
        before = self.snapshot()
        after = before._replace(x=x, y=y, width=width, height=height)
        self._record(before, after, "geometry")
        self._apply_window_geometry(after)
        self.emit("state-changed")

    def _on_screen(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        """Keep a typed rectangle reachable, the way a drag is kept reachable.

        A number typed into the panel can otherwise put the window somewhere
        there is no screen, and an overlay that cannot be seen cannot be
        dragged back either.
        """
        bounds = screen_bounds()
        if bounds is None:
            return x, y
        left, top, right, bottom = bounds
        return (
            round(_clamp_span(x, width, left, right)),
            round(_clamp_span(y, height, top, bottom)),
        )

    def _apply_window_geometry(self, state: State) -> None:
        if self.hidden:
            # An unmapped window is not the WM's to place; the map path reads
            # the position back out of _hidden_rect instead.
            self._hidden_rect = (
                state.x, state.y, max(MIN_SIZE, state.width), max(MIN_SIZE, state.height)
            )
            return
        width, height = max(MIN_SIZE, state.width), max(MIN_SIZE, state.height)
        if self.geometry() == (state.x, state.y, width, height):
            return
        self._requested = (state.x, state.y, width, height)
        x11.move_resize(self._xid, state.x, state.y, width, height)
        self._settle_later()

    def _settle_later(self) -> None:
        """Arm the read-back, replacing any still pending.

        Without replacing it, a timer armed for an earlier move fires in the
        middle of a later one and hands the reading back to X too early, which
        puts the number the user has just typed back to what it was.
        """
        if self._settle_timer:
            GLib.source_remove(self._settle_timer)
        self._settle_timer = GLib.timeout_add(GEOMETRY_SETTLE_MS, self._on_geometry_settled)

    def _on_geometry_settled(self):
        """Hand the reading back to X once the WM has had time to act.

        A window's position is not a property anything notifies on, so a move
        pauzak asked for has to be read back rather than waited for -- and if
        the WM turned the request down, this is when that shows.
        """
        self._settle_timer = 0
        self._requested = None
        self.emit("state-changed")
        return GLib.SOURCE_REMOVE

    # -- undo bookkeeping --------------------------------------------------

    def snapshot(self) -> State:
        x, y, width, height = self.geometry()
        return State(
            x=x,
            y=y,
            width=width,
            height=height,
            pan_x=self.view.pan_x,
            pan_y=self.view.pan_y,
            zoom=self.view.zoom,
            opacity=self.view.image_opacity,
            touchable=self.touchable,
            always_on_top=self.always_on_top,
            border_visible=self.view.border_visible,
            border_color=self.view.border_color.to_string(),
            hidden=self.hidden,
        )

    def restore(self, state: State) -> None:
        """Put the overlay back into `state`, recording nothing on the way."""
        self._restoring = True
        try:
            self.set_touchable(state.touchable)
            self.set_always_on_top(state.always_on_top)
            self.set_image_opacity(state.opacity)
            self.set_border_visible(state.border_visible)
            self.set_border_color(parse_color(state.border_color))
            self.set_zoom(state.zoom)
            self.set_image_pan(state.pan_x, state.pan_y)
            if self.hidden and not state.hidden:
                # Mapping again re-places the window from _hidden_rect, so aim
                # that at where the state wants it, not where it went away from.
                self._hidden_rect = (
                    state.x, state.y, max(MIN_SIZE, state.width), max(MIN_SIZE, state.height)
                )
            self.set_hidden(state.hidden)
            self._apply_window_geometry(state)
        finally:
            self._restoring = False
        self.emit("state-changed")

    def begin_edit(self) -> None:
        """Open a step. Nestable, and the outermost one decides its tag."""
        if self._edit_depth == 0:
            self._edit_before = self.snapshot()
        self._edit_depth += 1

    def end_edit(self, tag: str | None = None) -> None:
        self._edit_depth = max(0, self._edit_depth - 1)
        if self._edit_depth or self._edit_before is None:
            return
        before, self._edit_before = self._edit_before, None
        self._record(before, self.snapshot(), tag)

    @contextmanager
    def edit(self, tag: str | None = None):
        self.begin_edit()
        try:
            yield
        finally:
            self.end_edit(tag)

    def _record(self, before: State, after: State, tag: str | None) -> None:
        if self.recorder is not None and not self._restoring and not self._edit_depth:
            self.recorder(self, before, after, tag)

    # -- input -------------------------------------------------------------

    def _install_controllers(self):
        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def _edge_at(self, x: float, y: float) -> str | None:
        width, height = self.get_width(), self.get_height()
        margin = min(RESIZE_MARGIN, width // 4, height // 4)
        if margin <= 0:
            return None
        horizontal = "w" if x < margin else "e" if x > width - margin else ""
        vertical = "n" if y < margin else "s" if y > height - margin else ""
        return (vertical + horizontal) or None

    def _on_motion(self, _controller, x, y):
        if self._drag_edge is not None:
            return
        edge = self._edge_at(x, y)
        self.set_cursor_from_name(_RESIZE_CURSORS[edge] if edge else "default")

    def _on_drag_begin(self, gesture, start_x, start_y):
        state = gesture.get_current_event_state()
        panning = bool(state & Gdk.ModifierType.CONTROL_MASK)
        self._drag_edge = None if panning else self._edge_at(start_x, start_y)
        # A drag is one thing to undo however many motion events it takes, and
        # Ctrl may go down or up mid-drag, so the step stays untagged rather
        # than claiming to be about the window or about the image. The depth is
        # reset first: a gesture cancelled without its drag-end would otherwise
        # leave an edit open, and an open edit records nothing ever again.
        self._edit_depth = 0
        self.begin_edit()

        self._drag_pan = [self.view.pan_x, self.view.pan_y]

        window_x, window_y = x11.position(self._xid)
        self._drag_rect = [
            float(window_x),
            float(window_y),
            float(self.get_width()),
            float(self.get_height()),
        ]
        # Derive the press point in root coordinates from the event rather than
        # asking X where the pointer is now: by the time GTK dispatches this the
        # pointer has usually already moved, and that gap would be dropped.
        self._drag_last = (window_x + start_x, window_y + start_y)

    def _on_drag_update(self, gesture, _offset_x, _offset_y):
        pointer_x, pointer_y = x11.pointer()
        dx = pointer_x - self._drag_last[0]
        dy = pointer_y - self._drag_last[1]
        self._drag_last = (pointer_x, pointer_y)
        if dx == 0 and dy == 0:
            return

        state = gesture.get_current_event_state()
        factor = SLOW_FACTOR if state & Gdk.ModifierType.SHIFT_MASK else 1.0
        dx, dy = dx * factor, dy * factor

        if self._drag_edge is not None:
            self._resize_by(self._drag_edge, dx, dy)
        elif state & Gdk.ModifierType.CONTROL_MASK:
            self._pan_by(dx, dy, snap=bool(state & Gdk.ModifierType.ALT_MASK))
        else:
            self._move_by(dx, dy)

    def _on_drag_end(self, *_args):
        self._drag_edge = None
        self.end_edit()
        self._settle_later()

    def _pan_by(self, dx: float, dy: float, snap: bool) -> None:
        """Pan the image, keeping the un-snapped pan in the drag state.

        The snap is applied to what gets drawn and never folded back into the
        accumulator, so leaving an edge costs exactly the distance the pointer
        has travelled rather than that plus whatever the snap took up. Alt is
        free to grab here because GNOME drags windows with Super; a session
        that has been set back to Alt for that will take the drag instead.
        """
        self._drag_pan[0] += dx
        self._drag_pan[1] += dy
        pan = (self._drag_pan[0], self._drag_pan[1])
        if snap:
            pan = self.view.snap_pan(*pan, SNAP_DISTANCE)
        self.view.set_pan(*pan)
        self.emit("state-changed")

    def _move_by(self, dx: float, dy: float) -> None:
        """Move the window, clamping each axis on its own.

        Without the clamp the accumulated rectangle keeps travelling past the
        screen while the WM pins the real window at the edge, so the axis goes
        dead and coming back needs a drag as long as the drift. Clamping per
        axis is also what makes a window pushed against one edge still slide
        along it.
        """
        x = self._drag_rect[0] + dx
        y = self._drag_rect[1] + dy
        bounds = screen_bounds()
        if bounds is not None:
            left, top, right, bottom = bounds
            x = _clamp_span(x, self._drag_rect[2], left, right)
            y = _clamp_span(y, self._drag_rect[3], top, bottom)
        self._drag_rect[0], self._drag_rect[1] = x, y
        x11.move(self._xid, x, y)
        self._requested = tuple(int(value) for value in self._drag_rect)
        self.emit("state-changed")

    def _resize_by(self, edge: str, dx: float, dy: float) -> None:
        x, y, width, height = self._drag_rect
        if "w" in edge:
            dx = min(dx, width - MIN_SIZE)
            x, width = x + dx, width - dx
        elif "e" in edge:
            width = max(MIN_SIZE, width + dx)
        if "n" in edge:
            dy = min(dy, height - MIN_SIZE)
            y, height = y + dy, height - dy
        elif "s" in edge:
            height = max(MIN_SIZE, height + dy)
        self._drag_rect = [x, y, width, height]
        x11.move_resize(self._xid, x, y, width, height)
        self._requested = tuple(int(value) for value in self._drag_rect)
        self.emit("state-changed")

    def _on_scroll(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        step = WHEEL_STEP_SLOW if state & Gdk.ModifierType.SHIFT_MASK else WHEEL_STEP
        x, y = self._scroll_anchor(controller)
        with self.edit("zoom"):
            self.view.zoom_at(x, y, self.view.zoom * (step**-dy))
        self.emit("state-changed")
        return Gdk.EVENT_STOP

    def _scroll_anchor(self, controller) -> tuple[float, float]:
        """Where to pin the image while the wheel zooms, in surface coords."""
        event = controller.get_current_event()
        if event is not None:
            ok, x, y = event.get_position()
            if ok:
                return x, y
        return self.get_width() / 2, self.get_height() / 2

    def _install_actions(self):
        """The application's accelerators drive these.

        An overlay is a plain window rather than a GtkApplicationWindow, so it
        has no action map of its own -- but an inserted action group is enough
        for the accels declared in app.py to find their way here, which keeps
        one table of shortcuts instead of a second copy spelled out in keyvals.
        """
        actions = Gio.SimpleActionGroup()
        for name, run in (
            ("undo", lambda: self.get_application().undo.undo()),
            ("redo", lambda: self.get_application().undo.redo()),
            ("align", lambda: self.get_application().scanner.align(self)),
            ("diff", lambda: self.get_application().scanner.diff(self)),
            ("close-overlay", self.close),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, run=run: run())
            actions.add_action(action)
        self.insert_action_group("win", actions)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        # Escape stays a key rather than an accelerator: bound application-wide
        # it would also close the selected overlay from the panel.
        if keyval == Gdk.KEY_Escape:
            self.close()
            return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE
