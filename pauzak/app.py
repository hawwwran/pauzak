"""Application object: stylesheet, accelerators, window lifecycle."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402

from . import APP_ID, imagesrc
from .control import ControlWindow

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

/* The eye badge sits on top of an arbitrary image, so it carries its own
   contrast rather than relying on the theme's foreground colour. */
.pauzak-eye {
    background-color: rgba(0, 0, 0, 0.7);
    color: #ffffff;
    border-radius: 5px;
    margin: 2px;
    padding: 1px;
    -gtk-icon-size: 12px;
}

.pauzak-thumb-button {
    padding: 2px;
    min-width: 0;
    min-height: 0;
}
"""


class PauzakApplication(Adw.Application):
    __gtype_name__ = "PauzakApplication"

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.control = None

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
        return self.control

    def _on_control_closed(self, window):
        # Overlays are application windows too, so they would keep the process
        # alive after the panel is gone.
        for overlay in window.overlays():
            overlay.close()
        self.quit()
        return False
