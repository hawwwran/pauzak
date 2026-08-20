"""End-to-end behaviour checks against a live X server.

These are not unit tests: they start the real application, then verify the
results through X itself (window geometry, _NET_WM_STATE, the pointer's hit
target, screen pixels). That is the only way to tell whether always-on-top,
click-through and edge resizing actually took effect, as opposed to whether we
merely asked for them.

It drives the real pointer with xdotool, so the cursor moves on its own for
about a minute. Run it when you have the desktop to yourself:

    python3 tests/behaviour.py

Requires xdotool, xclip, gnome-screenshot and python3-pil.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

os.environ["GDK_BACKEND"] = "x11"
TMP = tempfile.mkdtemp(prefix="pauzak-tests-")
# Both the history file and the recent-files list live under XDG_DATA_HOME, so
# redirecting it keeps these checks off the real desktop's data.
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gio, Gtk  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from pauzak import diff, history, imagesrc, scan, snap, x11  # noqa: E402
from pauzak.app import PauzakApplication  # noqa: E402
from pauzak.overlay import SNAP_DISTANCE, screen_bounds  # noqa: E402

IMAGE = os.path.join(TMP, "grid.png")
PATTERN = os.path.join(TMP, "pattern.png")
PATTERN_SIZE = (420, 300)
PATTERN_COLS, PATTERN_ROWS = (40, 150, 330), (30, 120, 250)
SHOT = os.path.join(TMP, "screen.png")
RECENTS = [os.path.join(TMP, f"recent-{n}.png") for n in range(4)]
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def make_pattern() -> None:
    """A grid of hard edges, the thing an alignment scan is supposed to find."""
    image = Image.new("RGB", PATTERN_SIZE, (250, 250, 252))
    draw = ImageDraw.Draw(image)
    for x in PATTERN_COLS:
        draw.rectangle([x, 0, x + 1, PATTERN_SIZE[1]], fill=(90, 90, 100))
    for y in PATTERN_ROWS:
        draw.rectangle([0, y, PATTERN_SIZE[0], y + 1], fill=(90, 90, 100))
    image.save(PATTERN)


def make_image() -> None:
    image = Image.new("RGBA", (640, 400), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(0, 640, 40):
        draw.line([(x, 0), (x, 400)], fill=(200, 210, 225, 255))
    for y in range(0, 400, 40):
        draw.line([(0, y), (640, y)], fill=(200, 210, 225, 255))
    draw.ellipse([260, 150, 380, 270], outline=(40, 90, 180, 255), width=6)
    image.save(IMAGE)
    for copy in RECENTS:
        image.save(copy)


def geom(xid: int) -> tuple[int, int, int, int]:
    out = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", str(xid)], capture_output=True, text=True
    ).stdout
    values = dict(line.split("=", 1) for line in out.strip().splitlines())
    return int(values["X"]), int(values["Y"]), int(values["WIDTH"]), int(values["HEIGHT"])


def window_under_pointer() -> int:
    out = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True
    ).stdout
    return int(dict(line.split("=", 1) for line in out.strip().splitlines())["WINDOW"])


def net_wm_state(xid: int) -> str:
    return subprocess.run(
        ["xprop", "-id", str(xid), "_NET_WM_STATE"], capture_output=True, text=True
    ).stdout


def xdo(*args) -> None:
    subprocess.run(["xdotool", *map(str, args)], check=False)


def stacking_order() -> list[int]:
    """Client windows bottom-to-top, as the WM sees them."""
    out = subprocess.run(
        ["xprop", "-root", "_NET_CLIENT_LIST_STACKING"], capture_output=True, text=True
    ).stdout
    return [int(v.strip(), 16) for v in out.split("=")[-1].split(",") if v.strip().startswith("0x")]


def stack_index(order: list[int], xid: int) -> int:
    return order.index(xid) if xid in order else -1


def settle_until(predicate, timeout: float = 3.0, interval: float = 0.15) -> bool:
    """Poll a cheap X-level predicate until it holds. Off the main loop only."""
    remaining = timeout
    while remaining > 0 and not predicate():
        time.sleep(interval)
        remaining -= interval
    return predicate()


def rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def screen() -> Image.Image:
    if os.path.exists(SHOT):
        os.remove(SHOT)
    subprocess.run(["gnome-screenshot", "-f", SHOT], check=False)
    return Image.open(SHOT).convert("RGB")


class Suite(PauzakApplication):
    def do_activate(self):
        PauzakApplication.do_activate(self)
        self.win = self.control
        self.win.add_overlay(imagesrc.texture_from_file(Gio.File.new_for_path(IMAGE)))
        self.overlay = self.win.overlays()[0]
        GLib.timeout_add(1500, self.conversions)

    def _then(self, step, delay=300):
        GLib.timeout_add(delay, step)
        return False

    # -- image intake ------------------------------------------------------

    def conversions(self):
        gfile = Gio.File.new_for_path(IMAGE)
        loaded = imagesrc.texture_from_value(gfile)
        check("a dropped Gio.File yields a texture", loaded.texture.get_width() == 640)
        check("the file name becomes the overlay label", loaded.name == "grid.png", loaded.name)
        check("a file keeps its path, for the history strip", loaded.path == IMAGE, loaded.path)
        check("clipboard pixels have no path",
              imagesrc.texture_from_value(loaded.texture).path is None)
        check("a dropped path string works", imagesrc.texture_from_value(IMAGE)[0].get_width() == 640)
        check("a dropped file:// URI works",
              imagesrc.texture_from_value(gfile.get_uri())[0].get_width() == 640)
        check("non-image text is rejected", imagesrc.texture_from_value("not a file") is None)
        file_list = Gdk.FileList.new_from_list([gfile])
        check("a dropped file list takes the first file",
              imagesrc.texture_from_value(file_list)[0].get_width() == 640)
        return self._then(self.history_strip)

    # -- history strip -----------------------------------------------------

    def history_strip(self):
        """Plant four just-used images in the desktop's recent files.

        GtkRecentManager only publishes an addition once it has written the
        list, on an idle callback, so the checks have to wait for a later turn
        of the main loop. The name and prgname are what add_item records as the
        registering application; without them it refuses, or crashes.
        """
        GLib.set_prgname("pauzak-tests")
        GLib.set_application_name("pauzak-tests")
        manager = Gtk.RecentManager.get_default()
        for path in RECENTS:
            manager.add_item(Gio.File.new_for_path(path).get_uri())
        return self._then(self.history_strip_check, 700)

    def history_strip_check(self):
        """Two sources, in order: a few just-used entries from the recent
        files, then pauzak's own history. The overlay opened in do_activate has
        already put IMAGE into the latter."""
        now = time.time()
        paths = history.strip_paths(now)
        check("an opened image goes into pauzak's own history", IMAGE in paths, f"{paths}")
        fresh = [path for path in paths if path in RECENTS]
        check("images just used elsewhere reach the strip", bool(fresh), f"{paths}")
        check("but they claim no more than their share of the slots",
              len(fresh) == history.RECENT_SLOTS, f"{len(fresh)} of {len(RECENTS)} offered")
        check("recent files come before pauzak's own history",
              paths.index(IMAGE) >= len(fresh), f"{paths}")

        stale = history.strip_paths(now + history.RECENT_MAX_AGE + 60)
        check("recent files stop appearing once past the cutoff",
              not [path for path in stale if path in RECENTS], f"{stale}")
        check("pauzak's own history has no such cutoff", IMAGE in stale, f"{stale}")

        history.remember(os.path.join(TMP, "deleted.png"))
        paths = history.strip_paths(now)
        check("an image that is no longer readable is left out",
              os.path.join(TMP, "deleted.png") not in paths, f"{paths}")
        check("and the rest of the strip survives it", IMAGE in paths, f"{paths}")
        return self._then(self.above_default)

    # -- always on top -----------------------------------------------------

    def above_default(self):
        check("always-on-top defaults to on",
              "_NET_WM_STATE_ABOVE" in net_wm_state(self.overlay._xid))
        self.overlay.set_always_on_top(False)
        return self._then(self.above_off, 500)

    def above_off(self):
        check("turning it off drops _NET_WM_STATE_ABOVE",
              "_NET_WM_STATE_ABOVE" not in net_wm_state(self.overlay._xid))
        self.overlay.set_always_on_top(True)
        return self._then(self.above_on, 500)

    def above_on(self):
        check("turning it back on restores _NET_WM_STATE_ABOVE",
              "_NET_WM_STATE_ABOVE" in net_wm_state(self.overlay._xid))
        return self._then(self.move)

    # -- positioning -------------------------------------------------------

    def move(self):
        x11.move(self.overlay._xid, 1100, 620)
        return self._then(self.move_check, 600)

    def move_check(self):
        gx, gy, _, _ = geom(self.overlay._xid)
        px, py = x11.position(self.overlay._xid)
        check("x11.move lands where asked", abs(gx - 1100) <= 2 and abs(gy - 620) <= 2, f"{gx},{gy}")
        check("x11.position agrees with the WM", abs(px - gx) <= 2 and abs(py - gy) <= 2, f"{px},{py}")
        return self._then(self.clickthrough_a)

    # -- click-through -----------------------------------------------------

    def clickthrough_a(self):
        gx, gy, gw, gh = geom(self.overlay._xid)
        self.centre = (gx + gw // 2, gy + gh // 2)
        xdo("mousemove", *self.centre)
        return self._then(self.clickthrough_b, 400)

    def clickthrough_b(self):
        self.touchable_target = window_under_pointer()
        check("while touchable the pointer lands on the overlay",
              self.touchable_target == self.overlay._xid,
              f"hit {self.touchable_target}, overlay is {self.overlay._xid}")
        self.overlay.set_touchable(False)
        xdo("mousemove", self.centre[0] + 1, self.centre[1])
        xdo("mousemove", *self.centre)
        return self._then(self.clickthrough_c, 500)

    def clickthrough_c(self):
        under = window_under_pointer()
        check("with touchable off the pointer passes to the window behind",
              under != self.touchable_target, f"was {self.touchable_target}, now {under}")
        self.overlay.set_touchable(True)
        xdo("mousemove", self.centre[0] + 1, self.centre[1])
        xdo("mousemove", *self.centre)
        return self._then(self.clickthrough_d, 500)

    def clickthrough_d(self):
        check("switching touchable back on takes the pointer again",
              window_under_pointer() == self.touchable_target)
        return self._then(self.zoom)

    # -- zoom --------------------------------------------------------------

    def zoom(self):
        view = self.overlay.view
        view.set_zoom(2.5)
        check("the zoom slider applies", abs(view.zoom - 2.5) < 1e-9, f"{view.zoom}")
        self.overlay.zoom_one_to_one()
        check("1:1 gives exactly 100%", view.zoom == 1.0, f"{view.zoom}")
        view.set_pan(37, -19)
        check("panning offsets the image", (view.pan_x, view.pan_y) == (37, -19))
        self.overlay.center_image()
        check("Center resets the pan", (view.pan_x, view.pan_y) == (0.0, 0.0))
        self.overlay.zoom_to_fit()
        expected = min(
            view.get_width() / view.texture.get_width(),
            view.get_height() / view.texture.get_height(),
        )
        check("Fit uses the smaller of the two ratios", abs(view.zoom - expected) < 1e-6,
              f"{view.zoom:.4f} vs {expected:.4f}")

        view.set_zoom(1.0)
        view.center()
        anchor = (120.0, 90.0)
        origin_x, origin_y, _, _ = view.image_geometry()
        before = ((anchor[0] - origin_x) / view.zoom, (anchor[1] - origin_y) / view.zoom)
        view.zoom_at(*anchor, 3.0)
        origin_x, origin_y, _, _ = view.image_geometry()
        after = ((anchor[0] - origin_x) / view.zoom, (anchor[1] - origin_y) / view.zoom)
        check("wheel zoom pins the image point under the cursor",
              abs(before[0] - after[0]) < 0.01 and abs(before[1] - after[1]) < 0.01,
              f"{before} -> {after}")
        view.set_zoom(1.0)
        view.center()

        # Alt-snapping is pure geometry: the pans that put the image's leading
        # and trailing edge flush with the window's are +/- half the gap.
        view.set_zoom(0.5)
        edge = (view.get_width() - view.texture.get_width() * view.zoom) / 2
        check("a pan near an edge snaps flush to it",
              view.snap_pan(edge - 10, 0.0, SNAP_DISTANCE)[0] == edge, f"edge pan {edge}")
        check("the opposite edge snaps too",
              view.snap_pan(-edge + 10, 0.0, SNAP_DISTANCE)[0] == -edge)
        check("a pan further out than the snap distance is left alone",
              view.snap_pan(edge - SNAP_DISTANCE - 1, 0.0, SNAP_DISTANCE)[0]
              == edge - SNAP_DISTANCE - 1)
        edge_y = (view.get_height() - view.texture.get_height() * view.zoom) / 2
        far = edge_y - SNAP_DISTANCE - 1
        check("each axis snaps on its own",
              view.snap_pan(edge - 10, far, SNAP_DISTANCE) == (edge, far),
              f"edges {edge},{edge_y}")
        view.set_zoom(1.0)
        view.center()
        return self._then(self.drag_move)

    # -- drag --------------------------------------------------------------

    def _drag(self, dx, dy, modifier, done):
        """Runs off-thread: sleeping on the main loop would stop the app from
        ever seeing the motion events this generates."""
        gx, gy, gw, gh = geom(self.overlay._xid)
        start = (gx + gw // 2, gy + gh // 2)

        def work():
            xdo("mousemove", *start)
            time.sleep(0.3)
            if modifier:
                xdo("keydown", modifier)
                time.sleep(0.1)
            xdo("mousedown", "1")
            time.sleep(0.3)
            for i in range(1, 16):
                xdo("mousemove", start[0] + dx * i // 15, start[1] + dy * i // 15)
                time.sleep(0.03)
            time.sleep(0.4)
            xdo("mouseup", "1")
            if modifier:
                xdo("keyup", modifier)
            time.sleep(0.4)
            GLib.idle_add(done, gx, gy)

        threading.Thread(target=work, daemon=True).start()

    def drag_move(self):
        self._drag(150, 80, None, self.drag_move_check)
        return False

    def drag_move_check(self, x0, y0):
        x1, y1, _, _ = geom(self.overlay._xid)
        check("a plain drag moves the window 1:1",
              abs((x1 - x0) - 150) <= 8 and abs((y1 - y0) - 80) <= 8,
              f"moved {x1 - x0},{y1 - y0}, wanted 150,80")
        return self._then(self.drag_move_slow)

    def drag_move_slow(self):
        self._drag(150, 80, "shift", self.drag_move_slow_check)
        return False

    def drag_move_slow_check(self, x0, y0):
        x1, y1, _, _ = geom(self.overlay._xid)
        check("Shift+drag moves the window at 0.2x",
              abs((x1 - x0) - 30) <= 8 and abs((y1 - y0) - 16) <= 8,
              f"moved {x1 - x0},{y1 - y0}, wanted 30,16")
        return self._then(self.drag_pan)

    def drag_pan(self):
        self.overlay.view.center()
        self._drag(150, 80, "ctrl", self.drag_pan_check)
        return False

    def drag_pan_check(self, x0, y0):
        view = self.overlay.view
        x1, y1, _, _ = geom(self.overlay._xid)
        check("Ctrl+drag pans the image 1:1",
              abs(view.pan_x - 150) <= 8 and abs(view.pan_y - 80) <= 8,
              f"pan {view.pan_x:.0f},{view.pan_y:.0f}, wanted 150,80")
        check("Ctrl+drag leaves the window where it was",
              abs(x1 - x0) <= 2 and abs(y1 - y0) <= 2, f"window moved {x1 - x0},{y1 - y0}")
        return self._then(self.drag_pan_slow)

    def drag_pan_slow(self):
        self.overlay.view.center()
        xdo("keydown", "ctrl")
        self._drag(150, 80, "shift", self.drag_pan_slow_check)
        return False

    def drag_pan_slow_check(self, x0, y0):
        xdo("keyup", "ctrl")
        view = self.overlay.view
        check("Ctrl+Shift+drag pans the image at 0.2x",
              abs(view.pan_x - 30) <= 8 and abs(view.pan_y - 16) <= 8,
              f"pan {view.pan_x:.0f},{view.pan_y:.0f}, wanted 30,16")
        return self._then(self.drag_pan_snap)

    def drag_pan_snap(self):
        """Half the gap, less 25 px: the drag has to end inside the snap
        distance of an edge for the snap to have anything to do."""
        view = self.overlay.view
        view.set_zoom(0.5)
        view.center()
        self.snap_edge = (view.get_width() - view.texture.get_width() * view.zoom) / 2
        xdo("keydown", "ctrl")
        self._drag(int(self.snap_edge) - 25, 0, "alt", self.drag_pan_snap_check)
        return False

    def drag_pan_snap_check(self, _x0, _y0):
        xdo("keyup", "ctrl")
        view = self.overlay.view
        check("Ctrl+Alt+drag snaps the image flush to the window edge",
              abs(view.pan_x - self.snap_edge) <= 2,
              f"pan {view.pan_x:.0f}, edge is at {self.snap_edge:.0f}")
        view.set_zoom(1.0)
        view.center()
        return self._then(self.drag_edge)

    def drag_edge(self):
        x11.move(self.overlay._xid, 40, 300)
        return self._then(self.drag_edge_drag, 700)

    def drag_edge_drag(self):
        """Drag left past the screen edge and down at the same time: the point
        is that hitting the edge must not cost the other axis its motion."""
        gx, gy, gw, gh = geom(self.overlay._xid)
        start = (gx + gw // 2, gy + gh // 2)

        def work():
            xdo("mousemove", *start)
            time.sleep(0.3)
            xdo("mousedown", "1")
            time.sleep(0.3)
            for i in range(1, 21):
                xdo("mousemove", start[0] - 10 * i, start[1] + 5 * i)
                time.sleep(0.03)
            time.sleep(0.4)
            xdo("mouseup", "1")
            time.sleep(0.4)
            GLib.idle_add(self.drag_edge_check, gy)

        threading.Thread(target=work, daemon=True).start()
        return False

    def drag_edge_check(self, y0):
        left = screen_bounds()[0]
        x1, y1, _, _ = geom(self.overlay._xid)
        check("a drag past the left edge stops at it",
              abs(x1 - left) <= 2, f"x {x1}, edge is {left}")
        check("and the window still slides along that edge",
              abs((y1 - y0) - 100) <= 8, f"moved {y1 - y0} down, wanted 100")
        return self._then(self.resize)

    # -- edge resize -------------------------------------------------------

    def resize(self):
        x11.move(self.overlay._xid, 900, 500)
        return self._then(self.resize_drag, 700)

    def resize_drag(self):
        gx, gy, gw, gh = geom(self.overlay._xid)
        self.before_size = (gw, gh)
        edge = (gx + gw - 3, gy + gh // 2)

        def work():
            xdo("mousemove", *edge)
            time.sleep(0.4)
            xdo("mousedown", "1")
            time.sleep(0.3)
            for i in range(1, 13):
                xdo("mousemove", edge[0] + 10 * i, edge[1])
                time.sleep(0.04)
            time.sleep(0.4)
            xdo("mouseup", "1")
            time.sleep(0.5)
            GLib.idle_add(self.resize_check)

        threading.Thread(target=work, daemon=True).start()
        return False

    def resize_check(self):
        _, _, width, height = geom(self.overlay._xid)
        check("dragging the right edge widens the window only",
              abs(width - self.before_size[0] - 120) <= 15
              and abs(height - self.before_size[1]) <= 4,
              f"{self.before_size[0]}x{self.before_size[1]} -> {width}x{height}")
        return self._then(self.alpha_setup)

    # -- opacity -----------------------------------------------------------

    def alpha_setup(self):
        """Read the real composited pixels: a solid red window behind the
        overlay makes the blend measurable rather than merely plausible."""
        self.backdrop = Gtk.Window(application=self, title="pauzak-test-backdrop")
        self.backdrop.set_decorated(False)
        area = Gtk.DrawingArea()
        area.set_draw_func(lambda _a, cr, _w, _h: (cr.set_source_rgb(1, 0, 0), cr.paint()))
        self.backdrop.set_child(area)
        self.backdrop.set_default_size(700, 460)
        self.backdrop.present()
        return self._then(self.alpha_place, 700)

    def alpha_place(self):
        # The panel joins the WM's above layer whenever it holds focus, so park
        # it well clear of anything these checks sample off the screen.
        self.panel_xid = x11.surface_xid(self.win.get_surface())
        x11.move(self.panel_xid, 3300, 60)
        x11.move(x11.surface_xid(self.backdrop.get_surface()), 900, 480)
        x11.move(self.overlay._xid, 940, 520)
        self.overlay.view.set_zoom(1.0)
        self.overlay.view.center()
        self.overlay.set_image_opacity(1.0)
        return self._then(self.alpha_opaque, 900)

    def _sample(self):
        gx, gy, _, _ = geom(self.overlay._xid)
        return screen().getpixel((gx + 150, gy + 300))

    def alpha_opaque(self):
        self.opaque = self._sample()
        check("at 100% the image hides the window behind it",
              min(self.opaque) > 200, f"rgb{self.opaque}")
        self.overlay.set_image_opacity(0.35)
        return self._then(self.alpha_blended, 900)

    def alpha_blended(self):
        red, green, blue = self._sample()
        check("at 35% the window behind shows through",
              (red, green, blue) != self.opaque and green < 160 and blue < 160 and red > 180,
              f"opaque rgb{self.opaque} -> blended rgb{(red, green, blue)}")
        check("the blend matches the requested opacity",
              abs(green - 0.35 * 255) < 40, f"green {green}, wanted ~{0.35 * 255:.0f}")
        self.overlay.set_image_opacity(1.0)
        return self._then(self.border_off, 700)

    # -- border ------------------------------------------------------------

    def _edge_pixels(self):
        """The leftmost two columns down the overlay's left edge."""
        gx, gy, _, gh = geom(self.overlay._xid)
        shot = screen()
        return [
            shot.getpixel((gx + dx, gy + gh * n // 4))
            for n in (1, 2, 3)
            for dx in (0, 1)
        ]

    @staticmethod
    def _matches(pixels, target, tolerance=40):
        return any(
            all(abs(channel - want) <= tolerance for channel, want in zip(pixel, target))
            for pixel in pixels
        )

    def border_off(self):
        self.overlay.view.border_visible = False
        self.overlay.view.queue_draw()
        return self._then(self.border_off_check, 700)

    def border_off_check(self):
        check("with the border off the edge is just image",
              not self._matches(self._edge_pixels(), (0, 255, 0)),
              f"edge {self._edge_pixels()[:2]}")
        self.overlay.set_border_color(rgba("#00ff00"))
        self.overlay.set_border_visible(True)
        return self._then(self.border_on_check, 800)

    def border_on_check(self):
        check("switching the border on draws it at the window edge",
              self._matches(self._edge_pixels(), (0, 255, 0)),
              f"edge {self._edge_pixels()[:2]}")
        self.overlay.set_border_color(rgba("#0000ff"))
        return self._then(self.border_color_check, 800)

    def border_color_check(self):
        pixels = self._edge_pixels()
        check("picking a colour repaints the border",
              self._matches(pixels, (0, 0, 255)) and not self._matches(pixels, (0, 255, 0)),
              f"edge {pixels[:2]}")
        check("the border is 1 px wide",
              not self._matches([self._inset_pixel()], (0, 0, 255)),
              f"3 px in: {self._inset_pixel()}")
        self.overlay.set_border_visible(False)
        self.backdrop.close()
        return self._then(self.paste, 400)

    def _inset_pixel(self):
        gx, gy, _, gh = geom(self.overlay._xid)
        return screen().getpixel((gx + 3, gy + gh // 2))

    # -- clipboard ---------------------------------------------------------

    def paste(self):
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", IMAGE], check=False
        )
        return self._then(self.paste_fire, 800)

    def paste_fire(self):
        self.before_count = len(self.win.overlays())
        self.win.activate_action("win.paste", None)
        return self._then(self.paste_check, 1500)

    def paste_check(self):
        overlays = self.win.overlays()
        check("Ctrl+V adds an overlay from the clipboard",
              len(overlays) == self.before_count + 1,
              f"{self.before_count} -> {len(overlays)}")
        if len(overlays) > self.before_count:
            check("the pasted overlay carries the clipboard image",
                  overlays[-1].view.texture.get_width() == 640)
        for extra in overlays[1:]:
            extra.close()
        return self._then(self.target_setup, 600)

    # -- click delivery ----------------------------------------------------

    def target_setup(self):
        """A real button under the overlay: the only proof that a click gets
        through is a widget underneath reacting to it."""
        self.clicks = 0
        self.target = Gtk.Window(application=self, title="pauzak-test-target")
        self.target.set_decorated(False)
        button = Gtk.Button(label="target", hexpand=True, vexpand=True)
        button.connect("clicked", self._count_click)
        self.target.set_child(button)
        self.target.set_default_size(500, 360)
        self.target.present()
        return self._then(self.target_place, 900)

    def _count_click(self, _button):
        self.clicks += 1

    def target_place(self):
        self.target_xid = x11.surface_xid(self.target.get_surface())
        x11.move(self.target_xid, 2200, 300)
        x11.move_resize(self.overlay._xid, 2200, 300, 500, 360)
        x11.move(self.panel_xid, 2900, 700)
        self.spot = (2450, 480)
        self.overlay.set_touchable(True)
        return self._then(self.click_touchable, 900)

    def _click_spot(self, done, overlay_under, settle=0.6):
        """Click the spot once the pointer really is where the check assumes.

        Geometry and input-region changes reach the X server asynchronously,
        so clicking on a fixed timer races them. Waiting on the actual hit
        target keeps the assertion honest: if it never settles the way the
        caller expects, the check still fails.
        """
        def work():
            time.sleep(settle)
            xdo("mousemove", self.spot[0] + 3, self.spot[1])
            time.sleep(0.2)
            xdo("mousemove", *self.spot)
            settle_until(lambda: (window_under_pointer() == self.overlay._xid) == overlay_under)
            xdo("click", "1")
            time.sleep(0.8)
            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def click_touchable(self):
        self._click_spot(self.click_touchable_check, overlay_under=True)
        return False

    def click_touchable_check(self):
        check("a touchable overlay swallows the click", self.clicks == 0,
              f"the button underneath saw {self.clicks}")
        self.baseline = self.clicks
        self.overlay.set_touchable(False)
        self._click_spot(self.click_through_check, overlay_under=False)
        return False

    def click_through_check(self):
        check("an untouchable overlay lets the click reach the button underneath",
              self.clicks == self.baseline + 1,
              f"the button underneath saw {self.clicks - self.baseline} new clicks")
        def work():
            time.sleep(0.3)
            xdo("windowactivate", self.panel_xid)
            time.sleep(0.6)
            xdo("windowactivate", self.target_xid)
            time.sleep(0.6)
            GLib.idle_add(
                lambda: self._click_spot(
                    self.click_persist_check, overlay_under=False, settle=0.2
                )
                or False
            )

        threading.Thread(target=work, daemon=True).start()
        return False

    def click_persist_check(self):
        check("click-through survives focus changes elsewhere",
              self.clicks == self.baseline + 2,
              f"{self.clicks - self.baseline} clicks got through in total")
        self.overlay.set_touchable(True)
        return self._then(self.stack_overlay, 600)

    # -- stacking ----------------------------------------------------------

    def _activate(self, xid, done, until=None, settle=0.8):
        """Focus a window, then wait for the restack it should cause."""
        def work():
            xdo("windowactivate", xid)
            time.sleep(settle)
            if until is not None:
                settle_until(until)
            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _above(self, upper: int, lower: int):
        def ordered():
            order = stacking_order()
            return stack_index(order, upper) > stack_index(order, lower)

        return ordered

    def stack_overlay(self):
        self._activate(self.overlay._xid, self.stack_overlay_check,
                       until=self._above(self.overlay._xid, self.panel_xid))
        return False

    def stack_overlay_check(self):
        order = stacking_order()
        overlay_at, panel_at = stack_index(order, self.overlay._xid), stack_index(order, self.panel_xid)
        check("a focused overlay sits above the panel", overlay_at > panel_at,
              f"overlay@{overlay_at} panel@{panel_at}")
        self._activate(self.panel_xid, self.stack_panel_check,
                       until=self._above(self.panel_xid, self.overlay._xid))
        return False

    def stack_panel_check(self):
        order = stacking_order()
        overlay_at, panel_at = stack_index(order, self.overlay._xid), stack_index(order, self.panel_xid)
        check("focusing the panel raises it over the always-on-top overlay",
              panel_at > overlay_at, f"panel@{panel_at} overlay@{overlay_at}")
        self._activate(self.target_xid, self.stack_drop_check,
                       until=self._above(self.overlay._xid, self.panel_xid))
        return False

    def stack_drop_check(self):
        order = stacking_order()
        overlay_at, panel_at = stack_index(order, self.overlay._xid), stack_index(order, self.panel_xid)
        check("focusing something else puts the overlay back on top",
              overlay_at > panel_at, f"overlay@{overlay_at} panel@{panel_at}")
        self.target.close()
        return self._then(self.hide)

    # -- show / hide -------------------------------------------------------

    def hide(self):
        x11.move(self.overlay._xid, 1400, 500)
        return self._then(self.hide_toggle, 700)

    def hide_toggle(self):
        self.hidden_from = x11.position(self.overlay._xid)
        # Drive the eye button itself, not set_hidden() behind it.
        self.win._row_for(self.overlay).visibility_button.set_active(True)
        return self._then(self.hide_check, 900)

    def hide_check(self):
        check("the eye button takes the overlay off the screen",
              self.overlay._xid not in stacking_order())
        check("the overlay knows it is hidden", self.overlay.hidden)
        self.win._row_for(self.overlay).visibility_button.set_active(False)
        return self._then(self.show_check, 1100)

    def show_check(self):
        check("the eye button brings it back", self.overlay._xid in stacking_order())
        now = x11.position(self.overlay._xid)
        check("it comes back where it was, not where the WM would put it",
              abs(now[0] - self.hidden_from[0]) <= 2 and abs(now[1] - self.hidden_from[1]) <= 2,
              f"was {self.hidden_from}, came back at {now}")
        return self._then(self.hide_action)

    def hide_action(self):
        self.win.activate_action("win.toggle-hidden", None)
        return self._then(self.hide_action_check, 900)

    def hide_action_check(self):
        row = self.win._row_for(self.overlay)
        check("Ctrl+H hides the selected overlay too",
              self.overlay.hidden and self.overlay._xid not in stacking_order())
        check("the eye button follows a change it did not make",
              row is not None and row.visibility_button.get_active())
        self.win.activate_action("win.toggle-hidden", None)
        return self._then(self.numbers, 900)

    # -- the panel's numbers -----------------------------------------------

    def numbers(self):
        """The position, size and offset spins, read and written."""
        self.win._sync_controls()
        shown = tuple(
            int(spin.get_value())
            for spin in self.win.position_spins + self.win.size_spins
        )
        check("the spins show the window's real geometry", shown == geom(self.overlay._xid),
              f"{shown} vs {geom(self.overlay._xid)}")
        self.placed = (420, 260, 380, 240)
        self.was = geom(self.overlay._xid)
        self.win.position_spins[0].set_value(self.placed[0])
        self.win.position_spins[1].set_value(self.placed[1])
        return self._then(self.numbers_size, 900)

    def numbers_size(self):
        check("editing the position spins moves the window",
              geom(self.overlay._xid)[:2] == self.placed[:2], geom(self.overlay._xid))
        self.win.size_spins[0].set_value(self.placed[2])
        self.win.size_spins[1].set_value(self.placed[3])
        return self._then(self.numbers_check, 900)

    def numbers_check(self):
        check("editing the size spins resizes it", geom(self.overlay._xid) == self.placed,
              geom(self.overlay._xid))
        self.pan_was = (self.overlay.view.pan_x, self.overlay.view.pan_y)
        self.win.offset_spins[0].set_value(64)
        self.win.offset_spins[1].set_value(-40)
        check("editing the offset spins pans the image",
              (self.overlay.view.pan_x, self.overlay.view.pan_y) == (64, -40),
              (self.overlay.view.pan_x, self.overlay.view.pan_y))
        return self._then(self.undo_offset, 900)

    # -- undo / redo -------------------------------------------------------

    def undo_offset(self):
        """Each of the edits above went in far enough apart to be its own step,
        so they come back off the stack one at a time and in order."""
        self.undo.undo()
        check("undo puts the pan back",
              (self.overlay.view.pan_x, self.overlay.view.pan_y) == self.pan_was,
              f"{(self.overlay.view.pan_x, self.overlay.view.pan_y)} vs {self.pan_was}")
        self.undo.undo()
        return self._then(self.undo_size_check, 600)

    def undo_size_check(self):
        check("undo puts the size back, and leaves the position alone",
              geom(self.overlay._xid) == (*self.placed[:2], *self.was[2:]),
              f"{geom(self.overlay._xid)} vs {(*self.placed[:2], *self.was[2:])}")
        self.undo.undo()
        return self._then(self.undo_move_check, 600)

    def undo_move_check(self):
        check("the step before that is the move", geom(self.overlay._xid) == self.was,
              f"{geom(self.overlay._xid)} vs {self.was}")
        self.undo.redo()
        return self._then(self.redo_check, 600)

    def redo_check(self):
        check("redo moves it again", geom(self.overlay._xid)[:2] == self.placed[:2],
              geom(self.overlay._xid))
        self.undo.undo()
        return self._then(self.undo_keys, 600)

    def undo_keys(self):
        """Ctrl+Z with the overlay focused, rather than the panel: the overlay
        is a plain window with no action map, so it handles the keys itself."""
        self.overlay.set_image_opacity(0.42)
        self._activate(self.overlay._xid, self.undo_key_press)
        return False

    def undo_key_press(self):
        def work():
            xdo("key", "--clearmodifiers", "ctrl+z")
            time.sleep(0.4)
            GLib.idle_add(self.undo_key_check)

        threading.Thread(target=work, daemon=True).start()
        return False

    def undo_key_check(self):
        check("Ctrl+Z on the overlay itself undoes",
              abs(self.overlay.view.image_opacity - 0.42) > 1e-6,
              self.overlay.view.image_opacity)
        restored = self.overlay.view.image_opacity

        def work():
            xdo("key", "--clearmodifiers", "ctrl+shift+z")
            time.sleep(0.4)
            GLib.idle_add(lambda: self.redo_key_check(restored))

        threading.Thread(target=work, daemon=True).start()
        return False

    def redo_key_check(self, restored):
        check("and Ctrl+Shift+Z redoes",
              abs(self.overlay.view.image_opacity - 0.42) < 1e-6,
              f"{restored} -> {self.overlay.view.image_opacity}")
        return self._then(self.undo_close, 400)

    def undo_close(self):
        self.closed_key = self.overlay.key
        self.closed_state = self.overlay.snapshot()
        self.win.activate_action("win.close-overlay", None)
        return self._then(self.undo_close_check, 700)

    def undo_close_check(self):
        check("Ctrl+W closes the overlay", self.win.overlays() == [])
        self.undo.undo()
        return self._then(self.undo_reopen_check, 1200)

    def undo_reopen_check(self):
        back = self.win.overlays()
        check("undo brings a closed overlay back", len(back) == 1, len(back))
        if not back:
            return self._then(self.snap_math, 300)
        self.overlay = back[0]
        check("it keeps the identity it had, so its earlier steps still apply",
              self.overlay.key == self.closed_key, self.overlay.key)
        state = self.overlay.snapshot()
        check("it comes back with its zoom, pan and opacity",
              (state.zoom, state.pan_x, state.pan_y, state.opacity)
              == (self.closed_state.zoom, self.closed_state.pan_x,
                  self.closed_state.pan_y, self.closed_state.opacity),
              f"{state} vs {self.closed_state}")
        check("and where it was on screen",
              abs(state.x - self.closed_state.x) <= 2 and abs(state.y - self.closed_state.y) <= 2,
              f"{(state.x, state.y)} vs {(self.closed_state.x, self.closed_state.y)}")
        return self._then(self.hidden_reopen, 300)

    def hidden_reopen(self):
        """An unmapped window reports no size at all, so hiding an overlay and
        then closing it has to fall back on the rectangle remembered from when
        it went away -- or it comes back at the image's natural size."""
        self.hidden_size = self.overlay.geometry()[2:]
        self.hidden_key = self.overlay.key
        self.overlay.set_hidden(True)
        return self._then(self.hidden_close, 800)

    def hidden_close(self):
        check("a hidden overlay still knows how big it is",
              self.overlay.geometry()[2:] == self.hidden_size,
              f"{self.overlay.geometry()[2:]} vs {self.hidden_size}")
        self.overlay.close()
        return self._then(self.hidden_undo, 800)

    def hidden_undo(self):
        self.undo.undo()
        return self._then(self.hidden_reopen_check, 1400)

    def hidden_reopen_check(self):
        back = [o for o in self.win.overlays() if o.key == self.hidden_key]
        check("undo brings a hidden overlay back too", bool(back))
        if not back:
            return self._then(self.snap_math, 300)
        self.overlay = back[0]
        check("at the size it had, not the image's own",
              self.overlay.geometry()[2:] == self.hidden_size,
              f"{self.overlay.geometry()[2:]} vs {self.hidden_size}")
        self.overlay.set_hidden(False)
        return self._then(self.hidden_shown_check, 1000)

    def hidden_shown_check(self):
        size = (self.overlay.get_width(), self.overlay.get_height())
        check("and showing it puts that size on the screen", size == self.hidden_size,
              f"{size} vs {self.hidden_size}")
        return self._then(self.snap_math, 300)

    # -- lining up with what is underneath ---------------------------------

    def snap_math(self):
        """The matching itself, on arrays rather than on the screen: a shift
        this recovers from synthetic edges is one the scan cannot get wrong for
        reasons of its own."""
        if not snap.available():
            check("numpy is installed, so aligning is available", False, "no numpy")
            return self._then(self.finish, 300)

        import numpy as np

        def picture(width, height, cols, rows, thickness=2):
            image = np.full((height, width), 240.0, dtype=np.float32)
            for x in cols:
                image[:, x : x + thickness] = 20.0
            for y in rows:
                image[y : y + thickness, :] = 20.0
            return image

        design = picture(200, 100, (50, 120, 180), (25, 75))
        shifted = picture(200, 100, (57, 127, 187), (32, 82))
        result = snap.align(design, (0, 0, 200, 100), (200, 100), shifted)
        check("a known shift comes back exactly", (result.dx, result.dy) == (7.0, 7.0),
              f"{(result.dx, result.dy)}")
        check("and the pairs behind it are marked as taken",
              any(guide.chosen for guide in result.guides), f"{len(result.guides)} guides")

        aligned = snap.align(design, (0, 0, 200, 100), (200, 100),
                             picture(200, 100, (50, 120, 180), (25, 75)))
        check("something already lined up is left alone",
              (aligned.dx, aligned.dy) == (0.0, 0.0), f"{(aligned.dx, aligned.dy)}")

        # A design drawn at a quarter of its size, and a backdrop captured at
        # twice the screen's own resolution: both go through the resampler, and
        # point-sampling it would drop the one-pixel edges altogether.
        big = picture(800, 400, (200, 480, 720), (100, 300), thickness=8)
        quarter = snap.align(big, (0, 0, 200, 100), (200, 100), shifted)
        check("a design drawn at a quarter size still matches",
              (quarter.dx, quarter.dy) == (7.0, 7.0), f"{(quarter.dx, quarter.dy)}")
        hidpi = picture(400, 200, (114, 254, 374), (64, 164), thickness=4)
        scaled = snap.align(design, (0, 0, 200, 100), (200, 100), hidpi)
        check("a backdrop captured at twice the scale still matches",
              (scaled.dx, scaled.dy) == (7.0, 7.0), f"{(scaled.dx, scaled.dy)}")

        flat = np.full((100, 200), 128.0, dtype=np.float32)
        nothing = snap.align(design, (0, 0, 200, 100), (200, 100), flat)
        check("a blank backdrop moves nothing and offers no guides",
              (nothing.dx, nothing.dy, len(nothing.guides)) == (0.0, 0.0, 0),
              f"{(nothing.dx, nothing.dy)}, {len(nothing.guides)} guides")
        return self._then(self.diff_math)

    def diff_math(self):
        """What counts as a difference, on arrays rather than on the screen."""
        import numpy as np

        def page(cols, rows, ink=40, bg=250):
            image = np.full((120, 200, 3), float(bg), dtype=np.float32)
            for x in cols:
                image[:, x : x + 2] = ink
            for y in rows:
                image[y : y + 2, :] = ink
            return image

        design = page((30, 100, 160), (20, 80))
        same = diff.compare(design, page((30, 100, 160), (20, 80)))
        check("two pictures that agree are not marked at all", same.coverage == 0.0,
              f"{same.coverage:.4f}")

        moved = diff.compare(design, page((30, 106, 160), (20, 80)))
        check("an edge in the wrong place is marked", moved.coverage > 0.005,
              f"{moved.coverage * 100:.2f}%")
        marked = np.where(moved.rgba[:, :, 3].sum(axis=0) > 0)[0]
        # Guarded: check() records a failure and carries on, so reducing an
        # empty array here would take every later stage down with it.
        check("and the marks cover where it is and where it should be",
              marked.size > 0 and marked.min() <= 101 and marked.max() >= 105,
              f"columns {list(marked[:1]) + list(marked[-1:])}")

        # Both sides of a moved edge, so which way it is out can be read off.
        one_sided = diff.compare(design, page((106,), (20, 80)))
        magenta = (one_sided.rgba[:, :, 0] > one_sided.rgba[:, :, 2]) & (
            one_sided.rgba[:, :, 3] > 0
        )
        yellow = (one_sided.rgba[:, :, 1] > 150) & (one_sided.rgba[:, :, 2] < 60) & (
            one_sided.rgba[:, :, 3] > 0
        )
        check("the image having the ink and the screen having it read differently",
              magenta.any() and yellow.any(), f"magenta={magenta.sum()} yellow={yellow.sum()}")

        # An edge in the same place, merely softened: this is what the page's
        # own text does to every glyph, and it must not be marked.
        softened = page((30, 100, 160), (20, 80))
        for column in (29, 32, 99, 102, 159, 162):
            softened[:, column] = 150
        soft = diff.compare(design, softened)
        check("an edge softened in place is not a difference",
              soft.coverage == 0.0, f"{soft.coverage * 100:.2f}%")

        missing = diff.compare(design, page((30, 160), (20, 80)))
        check("a stroke the screen is missing is marked, thin as it is",
              missing.coverage > 0.002, f"{missing.coverage * 100:.2f}%")

        # The pale boxes interfaces are made of, at the levels a real one has:
        # 244 of fill and 233 of border against 255 of white. A single
        # threshold either cannot see these move at all or repaints the screen
        # when its white is a shade off, which is why there are two.
        def boxed(left, fill=244, border=233, bg=255):
            image = np.full((120, 200, 3), float(bg), dtype=np.float32)
            image[30:90, left : left + 90] = fill
            image[30:32, left : left + 90] = border
            image[88:90, left : left + 90] = border
            image[30:90, left : left + 2] = border
            image[30:90, left + 88 : left + 90] = border
            return image

        pale = boxed(40)
        for shift in (1, 2, 4):
            moved = diff.compare(pale, boxed(40 + shift))
            check(f"a pale box {shift} px out of place is marked",
                  moved.coverage > 0.001, f"{moved.coverage * 100:.2f}% at {shift} px")
        tinted = diff.compare(pale, boxed(40, fill=236, border=225, bg=247))
        check("but a page whose every shade is off by eight is not",
              tinted.coverage == 0.0, f"{tinted.coverage * 100:.2f}%")
        wrong_fill = diff.compare(pale, boxed(40, fill=200))
        check("while a box that is simply the wrong shade still is",
              wrong_fill.coverage > 0.001, f"{wrong_fill.coverage * 100:.2f}%")

        # A design exported with a transparent background is the ordinary case.
        # Where it is see-through what you see is the page, so it agrees there;
        # comparing the colour stored behind the transparency -- black, here --
        # would report the whole overlay as one enormous difference.
        drawn = np.zeros((100, 200, 4), dtype=np.uint8)
        drawn[40:60, 50:150] = (30, 30, 40, 255)
        see_through = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(drawn.tobytes()), GdkPixbuf.Colorspace.RGB, True, 8,
            200, 100, 200 * 4,
        )
        white = np.full((100, 200, 3), 255.0, dtype=np.float32)
        composited = diff.compare(diff.over(see_through, white), white)
        check("only the drawn part of a transparent image is compared",
              0.05 < composited.coverage < 0.15, f"{composited.coverage * 100:.2f}%")

        # Only the piece of the image the window shows is ever compared, and
        # only that piece is scaled: a 3 MP image at sixteen times its size is
        # gigabytes of pixbuf to read a few hundred thousand of them.
        def flat_pixbuf(width, height, color):
            pixels = np.zeros((height, width, 4), dtype=np.uint8)
            pixels[:, :] = color
            return GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(pixels.tobytes()), GdkPixbuf.Colorspace.RGB, True, 8,
                width, height, width * 4,
            )

        zoomed = scan._difference(
            flat_pixbuf(2000, 1500, (200, 60, 60, 255)),
            (-12000.0, -9000.0, 32000.0, 24000.0),
            (800, 600),
            flat_pixbuf(800, 600, (255, 255, 255, 255)),
        )
        check("a hugely zoomed image is compared over the window, not in full",
              zoomed is not None and zoomed[1] == (0, 0, 800, 600),
              f"{zoomed[1] if zoomed else None}")

        # Panned so its top-left corner is off the window, which is the case
        # that tells whether the piece being read is the piece being shown: a
        # flat image agrees with itself however far the sampling slides.
        square = np.full((200, 200, 4), 255, dtype=np.uint8)
        square[100:140, 100:140] = (40, 40, 48, 255)
        under = np.full((100, 120, 4), 255, dtype=np.uint8)
        under[70:100, 60:100] = (40, 40, 48, 255)  # the same square, as drawn
        panned = scan._difference(
            GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(square.tobytes()), GdkPixbuf.Colorspace.RGB, True, 8,
                200, 200, 200 * 4,
            ),
            (-40.0, -30.0, 200.0, 200.0),
            (120, 100),
            GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(under.tobytes()), GdkPixbuf.Colorspace.RGB, True, 8,
                120, 100, 120 * 4,
            ),
        )
        check("an image panned off the corner is read from where it is drawn",
              panned is not None and panned[0].coverage == 0.0,
              f"{panned[0].coverage * 100:.1f}% differs" if panned else "nothing compared")
        return self._then(self.align_setup)

    def align_setup(self):
        """A white window with the same grid painted on it, for the overlay to
        find its way onto."""
        self.pattern_at = (960, 360)
        self.backdrop_at, self.backdrop_size = (900, 300), (700, 500)
        inset = (self.pattern_at[0] - self.backdrop_at[0],
                 self.pattern_at[1] - self.backdrop_at[1])

        def paint(_area, cr, _width, _height):
            cr.set_source_rgb(1, 1, 1)
            cr.paint()
            cr.set_source_rgb(0.2, 0.2, 0.24)
            scale = self.pattern_scale
            origin = (inset[0] + PATTERN_SIZE[0] * (1 - scale) / 2,
                      inset[1] + PATTERN_SIZE[1] * (1 - scale) / 2)
            for x in PATTERN_COLS:
                cr.rectangle(origin[0] + x * scale, 0, 2, self.backdrop_size[1])
                cr.fill()
            if self.pattern_extra:
                # A bar the design does not have: the page having drawn
                # something of its own is exactly what a diff is for.
                cr.rectangle(origin[0] + (PATTERN_COLS[0] + 60) * scale, 0, 6,
                             self.backdrop_size[1])
                cr.fill()
            for y in PATTERN_ROWS:
                cr.rectangle(0, origin[1] + y * scale, self.backdrop_size[0], 2)
                cr.fill()

        self.pattern_scale = 1.0
        self.pattern_extra = False
        self.pattern_window = Gtk.Window(application=self, title="pauzak-test-pattern")
        self.pattern_window.set_decorated(False)
        area = Gtk.DrawingArea()
        area.set_draw_func(paint)
        self.pattern_area = area
        self.pattern_window.set_child(area)
        self.pattern_window.set_default_size(*self.backdrop_size)
        self.pattern_window.present()
        self.win.add_overlay(imagesrc.texture_from_file(Gio.File.new_for_path(PATTERN)))
        self.aligned = self.win.overlays()[-1]
        return self._then(self.align_place, 900)

    def align_place(self):
        x11.move(self.panel_xid, 10, 10)
        x11.move(x11.surface_xid(self.pattern_window.get_surface()), *self.backdrop_at)
        self.aligned.set_window_geometry(*self.pattern_at, *PATTERN_SIZE)
        self.aligned.view.set_pan(9, -6)
        return self._then(self.align_guard, 900)

    def align_guard(self):
        """With the panel over the overlay the scan would photograph the panel,
        so it is refused rather than run."""
        x11.move(self.panel_xid, self.pattern_at[0] + 20, self.pattern_at[1] + 20)
        return self._then(self.align_guard_check, 800)

    def align_guard_check(self):
        self.win.activate_action("win.align", None)
        check("aligning is refused while the panel covers the overlay",
              not self.scanner.busy
              and (self.aligned.view.pan_x, self.aligned.view.pan_y) == (9, -6),
              f"busy={self.scanner.busy}, "
              f"pan={(self.aligned.view.pan_x, self.aligned.view.pan_y)}")
        x11.move(self.panel_xid, 10, 10)
        return self._then(self.align_fire, 800)

    def align_fire(self):
        self.win.activate_action("win.align", None)
        return self._then(self.align_check, 3000)

    def align_check(self):
        pan = (self.aligned.view.pan_x, self.aligned.view.pan_y)
        check("aligning pans the image onto the edges underneath it",
              abs(pan[0]) <= 1 and abs(pan[1]) <= 1, f"pan {pan}, started at (9, -6)")
        check("the overlay is drawing itself again afterwards",
              not self.aligned.view.scanning)
        check("the matches it took are left on screen as guides",
              any(guide.chosen for guide in self.aligned.view.guides),
              f"{len(self.aligned.view.guides)} guides")
        check("and the whole alignment is one step to undo", self.undo.can_undo())
        self.undo.undo()
        check("undoing it puts the image back out of line",
              (self.aligned.view.pan_x, self.aligned.view.pan_y) == (9, -6),
              f"{(self.aligned.view.pan_x, self.aligned.view.pan_y)}")
        self.undo.redo()
        return self._then(self.align_zoom, 600)

    def align_zoom(self):
        """The same match with the image scaled down, which is the case that
        goes through the profile resampler on the way in."""
        self.pattern_scale = 0.75
        self.pattern_area.queue_draw()
        self.aligned.set_zoom(0.75)
        self.aligned.view.set_pan(-7, 11)
        return self._then(self.align_zoom_fire, 1000)

    def align_zoom_fire(self):
        self.win.activate_action("win.align", None)
        return self._then(self.align_zoom_check, 3000)

    def align_zoom_check(self):
        """Two pixels, not one: at a fractional zoom the image's own edges are
        rendered soft while the ones on screen are crisp, and the profiles'
        peaks sit a pixel or so apart because of it."""
        pan = (self.aligned.view.pan_x, self.aligned.view.pan_y)
        check("at 75% zoom it still lands within two pixels",
              abs(pan[0]) <= 2 and abs(pan[1]) <= 2, f"pan {pan}, started at (-7, 11)")
        self.aligned.set_zoom(1.0)
        self.pattern_scale = 1.0
        self.pattern_area.queue_draw()
        self.row = self.win._row_for(self.aligned)
        self.row.activate_action("row.visibility", None)
        return self._then(self.menu_hidden_check, 800)

    # -- the row's own menu ------------------------------------------------

    def menu_hidden_check(self):
        check("the row menu takes the overlay off the screen",
              self.aligned.hidden and self.aligned._xid not in stacking_order())
        self.row.activate_action("row.visibility", None)
        return self._then(self.menu_shown_check, 900)

    def menu_shown_check(self):
        check("and puts it back", not self.aligned.hidden)
        self.aligned.view.set_pan(13, -9)
        return self._then(self.menu_align, 900)

    def menu_align(self):
        self.row.activate_action("row.align", None)
        return self._then(self.menu_align_check, 3000)

    def menu_align_check(self):
        pan = (self.aligned.view.pan_x, self.aligned.view.pan_y)
        check("Smart snap in the row menu lines the image up",
              abs(pan[0]) <= 1 and abs(pan[1]) <= 1, f"pan {pan}, started at (13, -9)")
        self.aligned.view.set_pan(-14, 8)
        return self._then(self.key_align, 600)

    def key_align(self):
        """Ctrl+Shift+A with the overlay focused rather than the panel: the
        overlay has no action map, so it cannot come through an accelerator."""
        def work():
            xdo("windowactivate", self.aligned._xid)
            time.sleep(0.8)
            xdo("key", "--clearmodifiers", "ctrl+shift+a")
            time.sleep(3.0)
            GLib.idle_add(self.key_align_check)

        threading.Thread(target=work, daemon=True).start()
        return False

    def key_align_check(self):
        pan = (self.aligned.view.pan_x, self.aligned.view.pan_y)
        check("Ctrl+Shift+A reaches a focused overlay window",
              abs(pan[0]) <= 1 and abs(pan[1]) <= 1, f"pan {pan}, started at (-14, 8)")
        return self._then(self.diff_setup, 400)

    # -- the visual diff ---------------------------------------------------

    def diff_setup(self):
        check("nothing is being compared to begin with", not self.aligned.showing_diff())
        self.undo_depth = len(self.undo._done)
        self.pattern_extra = True
        self.pattern_area.queue_draw()
        return self._then(self.diff_fire, 1000)

    def diff_fire(self):
        self.win.activate_action("win.diff", None)
        return self._then(self.diff_check, 3500)

    def diff_check(self):
        check("Ctrl+Shift+D marks where the screen and the image disagree",
              self.aligned.showing_diff())
        marks = self.aligned.view.diff
        if marks is not None:
            check("the marks are laid over where the image is drawn",
                  marks[1][2] > 100 and marks[1][3] > 100, marks[1])
        check("the overlay is drawing itself again afterwards",
              not self.aligned.view.scanning)
        # The marks belong to one arrangement of the image, and saying so after
        # it has moved would be a lie.
        self.aligned.view.set_pan(20, 0)
        check("moving the image drops the marks", not self.aligned.showing_diff())
        self.aligned.view.set_pan(0, 0)
        return self._then(self.diff_again, 700)

    def diff_again(self):
        self.win.activate_action("win.diff", None)
        return self._then(self.diff_toggle_check, 3500)

    def diff_toggle_check(self):
        check("asking again puts them back", self.aligned.showing_diff())
        check("comparing adds nothing to the history",
              len(self.undo._done) == self.undo_depth,
              f"{len(self.undo._done)} steps, was {self.undo_depth}")
        return self._then(self.diff_badge, 300)

    def diff_badge(self):
        """The badge on the thumbnail, and the point of it: the marks are put
        away and brought back without the screen being read again, which takes
        about a second."""
        row = self.win._row_for(self.aligned)
        self.diff_row = row
        self.diff_texture = self.aligned.view.diff[0]
        check("the badge shows the comparison is on", row.diff_button.get_active())
        row.diff_button.set_active(False)
        check("turning the badge off takes the marks off the screen",
              not self.aligned.showing_diff())
        check("but keeps them, ready to come back", self.aligned.has_diff())
        row.diff_button.set_active(True)
        check("turning it back on shows them again", self.aligned.showing_diff())
        check("and they are the very same marks, not a second reading",
              self.aligned.view.diff[0] is self.diff_texture)
        return self._then(self.diff_fresh, 300)

    def diff_fresh(self):
        """Visual diff in the menu asks for the screen to be read again."""
        self.win._row_for(self.aligned).activate_action("row.diff", None)
        return self._then(self.diff_fresh_check, 3500)

    def diff_fresh_check(self):
        check("the menu takes a fresh reading", self.aligned.showing_diff())
        check("which is a new set of marks",
              self.aligned.view.diff[0] is not self.diff_texture)
        self.aligned.view.set_pan(30, 0)
        check("and moving the image still drops them", not self.aligned.has_diff())
        self.pattern_window.close()
        self.aligned.close()
        return self._then(self.finish, 600)

    def finish(self):
        failed = [r for r in results if not r[0]]
        print(f"\n=== {len(results) - len(failed)}/{len(results)} passed ===", flush=True)
        for _, name, detail in failed:
            print(f"  FAILED: {name}  {detail}", flush=True)
        self.exit_code = 1 if failed else 0
        self.quit()
        return False


def main() -> int:
    make_image()
    make_pattern()
    suite = Suite()
    suite.exit_code = 1
    suite.set_flags(suite.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    suite.run([])
    return suite.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
