"""The overlay window: an undecorated image pane pinned over other windows."""

from __future__ import annotations

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GObject, Graphene, Gsk, Gtk  # noqa: E402

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


def default_border_color() -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.parse(BORDER_COLOR)
    return rgba


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
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot):  # noqa: N802  (GTK vfunc naming)
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        snapshot.push_opacity(self.image_opacity)
        snapshot.append_scaled_texture(self.texture, self._filter(), self._image_rect())
        snapshot.pop()
        snapshot.pop()
        if self.border_visible:
            self._snapshot_border(snapshot, width, height)

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

    def _filter(self):
        return Gsk.ScalingFilter.LINEAR if self.zoom >= 1.0 else Gsk.ScalingFilter.TRILINEAR

    def _image_rect(self) -> Graphene.Rect:
        x, y, w, h = self.image_geometry()
        return Graphene.Rect().init(x, y, w, h)

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

    def __init__(self, application, texture: Gdk.Texture, name: str):
        super().__init__(application=application, title=f"pauzak - {name}")
        self.image_name = name
        self.touchable = True
        self.always_on_top = True
        self.hidden = False
        self._hidden_at = None
        self._xid = 0
        self._drag_last = (0, 0)
        self._drag_edge = None
        self._drag_rect = [0.0, 0.0, 0.0, 0.0]
        self._drag_pan = [0.0, 0.0]

        self.set_decorated(False)
        self.add_css_class("pauzak-overlay")
        self.view = ImageView(texture)
        self.set_child(self.view)
        self.set_default_size(*self._initial_size(texture))

        self._install_controllers()
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
        if self._hidden_at is not None:
            # Hiding keeps the surface and its size but not its place on
            # screen: the WM re-places the window when it maps again.
            x11.move(self._xid, *self._hidden_at)
            self._hidden_at = None

    def _on_surface_resized(self, *_args):
        self._apply_input_region()

    # -- window properties -------------------------------------------------

    def _apply_input_region(self):
        """An empty input region is what makes the window click-through."""
        surface = self.get_surface()
        if surface is None:
            return
        if self.touchable:
            rect = cairo.RectangleInt(0, 0, surface.get_width(), surface.get_height())
            region = cairo.Region(rect)
        else:
            region = cairo.Region()
        surface.set_input_region(region)

    def _apply_above(self):
        if self._xid:
            x11.set_above(self._xid, self.always_on_top)

    def set_touchable(self, enabled: bool) -> None:
        self.touchable = enabled
        self._apply_input_region()
        self.emit("state-changed")

    def set_always_on_top(self, enabled: bool) -> None:
        self.always_on_top = enabled
        self._apply_above()
        self.emit("state-changed")

    def set_image_opacity(self, value: float) -> None:
        self.view.image_opacity = min(max(value, 0.0), 1.0)
        self.view.queue_draw()
        self.emit("state-changed")

    def set_hidden(self, hidden: bool) -> None:
        if hidden == self.hidden:
            return
        self.hidden = hidden
        if hidden:
            self._hidden_at = x11.position(self._xid)
            self.set_visible(False)
        else:
            self.present()
        self.emit("state-changed")

    def set_border_visible(self, enabled: bool) -> None:
        self.view.border_visible = enabled
        self.view.queue_draw()
        self.emit("state-changed")

    def set_border_color(self, color: Gdk.RGBA) -> None:
        self.view.border_color = color
        self.view.queue_draw()
        self.emit("state-changed")

    def set_zoom(self, zoom: float) -> None:
        self.view.set_zoom(zoom)
        self.emit("state-changed")

    def zoom_one_to_one(self) -> None:
        self.set_zoom(1.0)

    def zoom_to_fit(self) -> None:
        self.view.center()
        self.set_zoom(self.view.fit_zoom())

    def center_image(self) -> None:
        self.view.center()
        self.emit("state-changed")

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

    def _on_scroll(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        step = WHEEL_STEP_SLOW if state & Gdk.ModifierType.SHIFT_MASK else WHEEL_STEP
        x, y = self._scroll_anchor(controller)
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

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE
