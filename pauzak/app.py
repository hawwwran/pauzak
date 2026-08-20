"""Application object: stylesheet, accelerators, window lifecycle."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402

from . import APP_ID, diff, imagesrc, scan, undo
from .control import ControlWindow

_MARK_COLOR = "#%02x%02x%02x" % diff.IMAGE_DARKER

# Overlay windows must be transparent wherever the image is not, otherwise the
# default window background shows up as a grey slab around a scaled-down image.
_CSS = """
window.pauzak-overlay,
window.pauzak-overlay > * {
    background: none;
    background-color: transparent;
    box-shadow: none;
    border: none;
}

/* The badges sit on top of an arbitrary image, so they carry their own
   contrast rather than relying on the theme's foreground colour. */
.pauzak-eye,
.pauzak-diff-badge {
    background-color: rgba(0, 0, 0, 0.7);
    color: #ffffff;
    border-radius: 5px;
    margin: 2px;
    padding: 1px;
    -gtk-icon-size: 12px;
}

.pauzak-diff-badge {
    min-width: 0;
    min-height: 0;
}

.pauzak-thumb-button {
    padding: 2px;
    min-width: 0;
    min-height: 0;
}
"""

# Lit, the comparison badge wears the colour of the marks it puts on screen, so
# retuning that magenta in diff.py does not leave the badge behind on the old one.
_CSS += ".pauzak-diff-badge:checked { background-color: %s; }\n" % _MARK_COLOR


class PauzakApplication(Adw.Application):
    __gtype_name__ = "PauzakApplication"

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.control = None
        # One stack for every overlay: undo walks back through what was changed
        # in the order it was changed, whichever overlay each step belongs to.
        self.undo = undo.UndoStack()
        # Same reason as the stack: an overlay asks for a scan of what is under
        # it without having to know that the panel is the thing that runs one.
        self.scanner = scan.Scanner()

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(_CSS)
        else:
            provider.load_from_data(_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.set_accels_for_action("win.paste", ["<Control>v"])
        self.set_accels_for_action("win.open", ["<Control>o"])
        self.set_accels_for_action("win.close-overlay", ["<Control>w"])
        self.set_accels_for_action("win.toggle-hidden", ["<Control>h"])
        self.set_accels_for_action("win.undo", ["<Control>z"])
        self.set_accels_for_action("win.redo", ["<Control><Shift>z", "<Control>y"])
        self.set_accels_for_action("win.align", ["<Control><Shift>a"])
        self.set_accels_for_action("win.diff", ["<Control><Shift>d"])

    def do_activate(self):
        self._control_window().present()

    def do_open(self, files, _n_files, _hint):
        window = self._control_window()
        window.present()
        for gfile in files:
            try:
                window.add_overlay(imagesrc.texture_from_file(gfile))
            except GLib.Error as error:
                window.toast(f"Could not load {gfile.get_basename()}: {error.message}")

    def _control_window(self) -> ControlWindow:
        if self.control is None:
            self.control = ControlWindow(self)
            self.control.connect("close-request", self._on_control_closed)
            self.scanner.host = self.control
        return self.control

    def _on_control_closed(self, window):
        # Overlays are application windows too, so they would keep the process
        # alive after the panel is gone.
        for overlay in window.overlays():
            overlay.close()
        self.quit()
        return False
