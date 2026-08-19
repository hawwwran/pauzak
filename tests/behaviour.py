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
from gi.repository import Gdk, GLib, Gio, Gtk  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from pauzak import history, imagesrc, x11  # noqa: E402
from pauzak.app import PauzakApplication  # noqa: E402
from pauzak.overlay import SNAP_DISTANCE, screen_bounds  # noqa: E402

IMAGE = os.path.join(TMP, "grid.png")
SHOT = os.path.join(TMP, "screen.png")
RECENTS = [os.path.join(TMP, f"recent-{n}.png") for n in range(4)]
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


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
        return self._then(self.finish, 900)

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
    suite = Suite()
    suite.exit_code = 1
    suite.set_flags(suite.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    suite.run([])
    return suite.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
