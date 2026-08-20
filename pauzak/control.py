"""The control panel: takes images in, and drives every overlay's properties."""

from __future__ import annotations

import itertools
import math
import os
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import history, imagesrc, undo, x11
from .overlay import MAX_ZOOM, MIN_SIZE, MIN_ZOOM, OverlayWindow, State

# The zoom slider works in log2 so that 100% sits exactly on the middle mark
# and each mark is a doubling.
ZOOM_LOG_MIN = math.log2(MIN_ZOOM)
ZOOM_LOG_MAX = math.log2(MAX_ZOOM)

SPIN_LIMIT = 32000  # px bound on the geometry and offset spin buttons
CLOSED_LIMIT = 8  # closed overlays whose pixels are kept for a possible undo


class _Presence(NamedTuple):
    """Whether an overlay is open, as a value undo can put back."""

    key: int
    open: bool


class _Recipe(NamedTuple):
    """What it takes to put a closed overlay back where it was."""

    texture: Gdk.Texture
    name: str
    index: int
    state: State


class HistoryStrip(Gtk.ScrolledWindow):
    """A row of recently used images; clicking one opens it as an overlay.

    Thumbnails are loaded scaled down, which doubles as the readable-image
    test: a path that no longer holds a decodable image is cached as broken and
    left out of the strip rather than being retried on every refresh.
    """

    __gtype_name__ = "PauzakHistoryStrip"

    THUMB_WIDTH, THUMB_HEIGHT = 72, 48

    def __init__(self, on_open):
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
        )
        self._on_open = on_open
        self._entries: list[tuple[str, float | None]] = []
        self._thumbs: dict[tuple[str, float | None], Gdk.Paintable | None] = {}
        self._row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._row.set_margin_top(4)
        self._row.set_margin_bottom(4)
        self._row.set_margin_start(8)
        self._row.set_margin_end(8)
        self.set_child(self._row)
        # Nothing to show yet, and an empty bottom bar is still a visible bar.
        self.set_visible(False)

    def refresh(self):
        # Keyed on the modification time as well as the path, so an image that
        # has been edited since gets a fresh thumbnail rather than the cached one.
        entries = [(path, _mtime(path)) for path in history.strip_paths()]
        if entries == self._entries:
            return
        self._entries = entries
        while (child := self._row.get_first_child()) is not None:
            self._row.remove(child)
        for entry in entries:
            button = self._button(entry)
            if button is not None:
                self._row.append(button)
        self.set_visible(self._row.get_first_child() is not None)

    def _button(self, entry: tuple[str, float | None]):
        path = entry[0]
        if entry not in self._thumbs:
            self._thumbs[entry] = self._load_thumbnail(path)
        paintable = self._thumbs[entry]
        if paintable is None:
            return None
        picture = Gtk.Picture.new_for_paintable(paintable)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_size_request(self.THUMB_WIDTH, self.THUMB_HEIGHT)
        button = Gtk.Button(valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.add_css_class("pauzak-thumb-button")
        button.set_child(picture)
        button.set_tooltip_text(path)
        button.connect("clicked", lambda _b, path=path: self._on_open(path))
        return button

    def _load_thumbnail(self, path: str):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.THUMB_WIDTH * 2, self.THUMB_HEIGHT * 2, True
            )
        except GLib.Error:
            return None
        return Gdk.Texture.new_for_pixbuf(pixbuf)


class OverlayRow(Gtk.ListBoxRow):
    __gtype_name__ = "PauzakOverlayRow"

    def __init__(self, overlay: OverlayWindow, on_close, on_align, on_diff):
        super().__init__()
        self.overlay = overlay
        self._on_diff = on_diff
        self._shown = None  # the state the row is currently drawn for

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        self.thumb = Gtk.Picture.new_for_paintable(overlay.view.texture)
        self.thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.thumb.set_size_request(56, 36)
        self.thumb.add_css_class("card")

        # The eye sits on the thumbnail rather than beside it, so the preview
        # doubles as the show/hide switch instead of costing a second control.
        self.eye = Gtk.Image.new_from_icon_name("view-reveal-symbolic")
        self.eye.set_halign(Gtk.Align.END)
        self.eye.set_valign(Gtk.Align.END)
        self.eye.add_css_class("pauzak-eye")

        stack = Gtk.Overlay()
        stack.set_child(self.thumb)
        stack.add_overlay(self.eye)

        self.visibility_button = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
        self.visibility_button.add_css_class("flat")
        self.visibility_button.add_css_class("pauzak-thumb-button")
        self.visibility_button.set_child(stack)

        # The comparison badge sits over the thumbnail rather than inside its
        # button, so a click reaches one or the other and not both.
        self.diff_button = Gtk.ToggleButton(
            halign=Gtk.Align.START, valign=Gtk.Align.START, can_focus=False
        )
        self.diff_button.add_css_class("flat")
        self.diff_button.add_css_class("pauzak-diff-badge")
        self.diff_button.set_child(Gtk.Image.new_from_icon_name("view-dual-symbolic"))
        self.diff_button.connect("toggled", self._on_diff_toggled)

        badges = Gtk.Overlay(valign=Gtk.Align.CENTER)
        badges.set_child(self.visibility_button)
        badges.add_overlay(self.diff_button)
        self.visibility_button.connect("toggled", self._on_visibility_toggled)
        box.append(badges)

        label = Gtk.Label(label=overlay.image_name, xalign=0.0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(label)

        box.append(self._menu_button(on_align, on_diff))

        close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER)
        close.add_css_class("flat")
        close.set_tooltip_text("Close this overlay")
        close.connect("clicked", lambda _b: on_close(overlay))
        box.append(close)

        self.set_child(box)
        overlay.connect("state-changed", lambda _o: self.refresh())
        self.refresh()

    def _menu_button(self, on_align, on_diff):
        """The row's own menu: what applies to this overlay and nothing else."""
        actions = Gio.SimpleActionGroup()
        for name, run in (
            ("visibility", lambda: self.overlay.set_hidden(not self.overlay.hidden)),
            ("align", on_align),
            ("diff", on_diff),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, run=run: run())
            actions.add_action(action)
        self.insert_action_group("row", actions)

        self.menu = Gio.Menu()
        # Horizontal ellipsis: view-more-symbolic is the vertical one.
        self.more_button = Gtk.MenuButton(
            icon_name="view-more-horizontal-symbolic", valign=Gtk.Align.CENTER
        )
        self.more_button.add_css_class("flat")
        self.more_button.set_tooltip_text("More for this overlay")
        self.more_button.set_menu_model(self.menu)
        return self.more_button

    def _rebuild_menu(self, hidden: bool) -> None:
        self.menu.remove_all()
        self.menu.append("Show" if hidden else "Hide", "row.visibility")
        for label, action, accel in (
            ("Smart snap", "row.align", "<Control><Shift>a"),
            ("Visual diff", "row.diff", "<Control><Shift>d"),
        ):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("accel", GLib.Variant.new_string(accel))
            self.menu.append_item(item)

    def _on_visibility_toggled(self, button):
        if self._shown is None:  # mid-refresh, following the overlay
            return
        self.overlay.set_hidden(button.get_active())
        self.refresh()

    def _on_diff_toggled(self, button):
        """Off puts the marks away; on brings the same ones back, or reads the
        screen once if there are none yet."""
        if self._shown is None:  # mid-refresh, following the overlay
            return
        if button.get_active() == self.overlay.showing_diff():
            return
        if self.overlay.toggle_diff():
            return
        # Nothing to show yet, so the screen has to be read, which may take a
        # second or be refused outright. Either way the badge goes back to
        # saying what is actually on screen; it lights up when marks arrive.
        self._on_diff()
        self._shown = None
        self.refresh()

    def refresh(self):
        # Everything here follows from these two, and state-changed arrives
        # once per motion event while a drag is going on.
        hidden, comparing = self.overlay.hidden, self.overlay.showing_diff()
        if (hidden, comparing) == self._shown:
            return
        was_hidden = self._shown[0] if self._shown is not None else None
        self._shown = None  # the handlers stand down while the row catches up
        if hidden != was_hidden:
            # The menu's wording follows `hidden` and nothing else, and
            # rebuilding it under an open popover is worth avoiding.
            self._rebuild_menu(hidden)
        if self.visibility_button.get_active() != hidden:
            self.visibility_button.set_active(hidden)
        self.eye.set_from_icon_name(
            "view-conceal-symbolic" if hidden else "view-reveal-symbolic"
        )
        self.thumb.set_opacity(0.35 if hidden else 1.0)
        self.visibility_button.set_tooltip_text(
            "Show this overlay" if hidden else "Hide this overlay"
        )
        if self.diff_button.get_active() != comparing:
            self.diff_button.set_active(comparing)
        self.diff_button.set_tooltip_text(
            "Hide the differences" if comparing else "Show the differences (Ctrl+Shift+D)"
        )
        self._shown = (hidden, comparing)


class ControlWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PauzakControlWindow"

    def __init__(self, application):
        super().__init__(application=application, title="pauzak")
        self.set_default_size(460, 620)
        self._syncing = False
        self._above_holds = 0
        self._sync_pending = False
        self.undo = application.undo
        # Overlays are addressed by key rather than by object: one that undo
        # closes and opens again is a new window, and the key carries across.
        self._keys = itertools.count(1)
        self._by_key: dict[int, OverlayWindow] = {}
        self._recipes: dict[int, _Recipe] = {}

        self._build_ui()
        self._install_actions()
        self._install_drop_target()
        self._update_stack()
        self.history_strip.refresh()
        self.connect("notify::is-active", self._on_active_changed)
        self.undo.connect("changed", self._on_undo_changed)
        self._on_undo_changed()

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open…")
        open_button.set_action_name("win.open")
        header.pack_start(open_button)
        for icon, tooltip, action in (
            ("edit-undo-symbolic", "Undo (Ctrl+Z)", "win.undo"),
            ("edit-redo-symbolic", "Redo (Ctrl+Shift+Z)", "win.redo"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
            button.set_action_name(action)
            header.pack_start(button)
        paste_button = Gtk.Button(icon_name="edit-paste-symbolic")
        paste_button.set_tooltip_text("Paste image from clipboard (Ctrl+V)")
        paste_button.set_action_name("win.paste")
        header.pack_end(paste_button)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_ready_page(), "ready")

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.stack)

        self.history_strip = HistoryStrip(self._open_from_history)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toast_overlay)
        view.add_bottom_bar(self.history_strip)
        self.set_content(view)

    def _build_empty_page(self):
        page = Adw.StatusPage(
            icon_name="image-x-generic-symbolic",
            title="Drop an image here",
            description="Or press Ctrl+V to paste one from the clipboard.",
        )
        button = Gtk.Button(label="Open Image…", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.set_action_name("win.open")
        page.set_child(button)
        return page

    def _build_ready_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list_box.add_css_class("boxed-list")
        self.list_box.connect("row-selected", lambda _lb, _row: self._sync_controls())
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        scroller.set_max_content_height(180)
        scroller.set_child(self.list_box)
        clamp = Adw.Clamp(margin_top=12, margin_bottom=6, margin_start=12, margin_end=12)
        clamp.set_child(scroller)
        box.append(clamp)

        box.append(self._build_controls())
        return box

    def _build_controls(self):
        page = Adw.PreferencesPage(vexpand=True)

        window_group = Adw.PreferencesGroup(title="Overlay window")

        position_row, self.position_spins = self._spin_row(
            "Position",
            None,
            (("X", -SPIN_LIMIT, SPIN_LIMIT), ("Y", -SPIN_LIMIT, SPIN_LIMIT)),
            self._on_geometry_spun,
        )
        window_group.add(position_row)

        size_row, self.size_spins = self._spin_row(
            "Size",
            None,
            (("W", MIN_SIZE, SPIN_LIMIT), ("H", MIN_SIZE, SPIN_LIMIT)),
            self._on_geometry_spun,
        )
        window_group.add(size_row)

        self.touchable_row = Adw.SwitchRow(
            title="Touchable",
            subtitle="Off lets clicks pass straight through to what is underneath",
        )
        self.touchable_row.connect("notify::active", self._on_touchable_toggled)
        window_group.add(self.touchable_row)

        self.on_top_row = Adw.SwitchRow(title="Always on top")
        self.on_top_row.connect("notify::active", self._on_on_top_toggled)
        window_group.add(self.on_top_row)

        self.opacity_scale, opacity_row = self._scale_row(
            "Opacity", 5, 100, 1, self._on_opacity_changed
        )
        opacity_row.set_subtitle("Lower it to see through to the window underneath")
        window_group.add(opacity_row)

        self.border_row = Adw.SwitchRow(
            title="Border",
            subtitle="A 1 px outline marking where the overlay ends",
        )
        self.border_row.connect("notify::active", self._on_border_toggled)
        window_group.add(self.border_row)

        color_dialog = Gtk.ColorDialog(title="Border Colour", with_alpha=True)
        self.border_button = Gtk.ColorDialogButton(
            dialog=color_dialog, valign=Gtk.Align.CENTER
        )
        self.border_button.connect("notify::rgba", self._on_border_color_changed)
        self.border_color_row = Adw.ActionRow(title="Border colour")
        self.border_color_row.add_suffix(self.border_button)
        self.border_color_row.set_activatable_widget(self.border_button)
        window_group.add(self.border_color_row)
        page.add(window_group)

        image_group = Adw.PreferencesGroup(
            title="Image",
            description="Ctrl+drag pans the image, plain drag moves the window; "
            "hold Shift for fine control, or Alt to snap the image to the window's edges.",
        )
        self.zoom_scale, zoom_row = self._scale_row(
            "Zoom", ZOOM_LOG_MIN, ZOOM_LOG_MAX, 0.01, self._on_zoom_changed
        )
        for mark in range(-3, 4):
            self.zoom_scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        self.zoom_row = zoom_row
        image_group.add(zoom_row)

        offset_row, self.offset_spins = self._spin_row(
            "Offset",
            "0, 0 is centred",
            (("X", -SPIN_LIMIT, SPIN_LIMIT), ("Y", -SPIN_LIMIT, SPIN_LIMIT)),
            self._on_offset_spun,
        )
        image_group.add(offset_row)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        buttons.set_margin_top(6)
        buttons.set_margin_bottom(6)
        for label, tooltip, handler in (
            ("1:1", "Show the image at 100%, one image pixel per screen pixel", self._on_one_to_one),
            ("Fit", "Scale the image to fit the overlay window", self._on_fit),
            ("Center", "Undo panning and re-center the image", self._on_center),
            (
                "Align",
                "Line the image's edges up with the ones underneath it (Ctrl+Shift+A)",
                lambda _b: self.activate_action("win.align", None),
            ),
        ):
            button = Gtk.Button(label=label, tooltip_text=tooltip)
            button.connect("clicked", handler)
            buttons.append(button)
        button_row = Adw.PreferencesRow(activatable=False)
        button_row.set_child(_padded(buttons))
        image_group.add(button_row)
        page.add(image_group)

        close_group = Adw.PreferencesGroup()
        close = Gtk.Button(label="Close Overlay")
        close.add_css_class("destructive-action")
        close.connect("clicked", lambda _b: self._close_overlay(self.selected_overlay()))
        close_row = Adw.PreferencesRow(activatable=False)
        close_row.set_child(_padded(close))
        close_group.add(close_row)
        page.add(close_group)
        return page

    def _scale_row(self, title, lower, upper, step, handler):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lower, upper, step)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(200, -1)
        scale.connect("value-changed", handler)
        value_label = Gtk.Label(width_chars=6, xalign=1.0)
        value_label.add_css_class("dim-label")
        value_label.add_css_class("numeric")
        row = Adw.ActionRow(title=title)
        row.add_suffix(scale)
        row.add_suffix(value_label)
        scale.value_label = value_label
        return scale, row

    def _spin_row(self, title, subtitle, fields, handler):
        """A row carrying one editable number per field, captioned X/Y or W/H.

        The captions are what tell two bare spin buttons apart at a glance.
        """
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4, valign=Gtk.Align.CENTER
        )
        spins = []
        for caption, lower, upper in fields:
            label = Gtk.Label(label=caption)
            label.add_css_class("dim-label")
            box.append(label)
            spin = Gtk.SpinButton.new_with_range(lower, upper, 1)
            spin.set_digits(0)
            spin.set_width_chars(5)
            spin.set_max_width_chars(5)
            spin.add_css_class("numeric")
            spin.connect("value-changed", handler)
            box.append(spin)
            spins.append(spin)
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.add_suffix(box)
        return row, spins

    def _install_actions(self):
        for name, handler in (
            ("open", self._action_open),
            ("paste", self._action_paste),
            ("close-overlay", lambda *_: self._close_overlay(self.selected_overlay())),
            ("toggle-hidden", lambda *_: self._toggle_hidden()),
            ("align", self._action_align),
            ("diff", self._action_diff),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.undo_action = self._add_stack_action("undo", self.undo.undo)
        self.redo_action = self._add_stack_action("redo", self.undo.redo)

    def _add_stack_action(self, name, run):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: run())
        self.add_action(action)
        return action

    def _install_drop_target(self):
        target = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        target.set_gtypes(
            [
                Gdk.FileList.__gtype__,
                Gio.File.__gtype__,
                Gdk.Texture.__gtype__,
                GdkPixbuf.Pixbuf.__gtype__,
                GObject.TYPE_STRING,
            ]
        )
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _on_active_changed(self, *_args):
        """Join the WM's above layer while focused, and leave it again after.

        Overlays live in that layer, and stacking is per-layer: a normal window
        can never be raised over one, so clicking the panel would leave it
        buried. Staying there permanently is just as wrong -- the panel would
        then cover the windows the overlays are meant to sit on. A modal child
        such as the file dialog takes the focus with it, so _above_holds keeps
        the layer for as long as one is open.
        """
        surface = self.get_surface()
        if surface is not None:
            above = self.is_active() or self._above_holds > 0
            x11.set_above(x11.surface_xid(surface), above)
        if self.is_active():
            # Other applications will have been using images in the meantime.
            self.history_strip.refresh()

    # -- overlays ----------------------------------------------------------

    def overlays(self):
        return [row.overlay for row in self.list_box]

    def selected_overlay(self):
        row = self.list_box.get_selected_row()
        return row.overlay if row else None

    def add_overlay(self, loaded: imagesrc.Loaded) -> None:
        key = next(self._keys)
        self._install(loaded.texture, loaded.name, key)
        self.undo.push(
            undo.Step(
                apply=self._apply_presence,
                before=_Presence(key, False),
                after=_Presence(key, True),
                key=key,
            )
        )
        history.remember(loaded.path)
        self.history_strip.refresh()

    def _install(self, texture, name, key, index=None, state=None) -> OverlayWindow:
        overlay = OverlayWindow(self.get_application(), texture, name, key, state)
        overlay.recorder = self._record_state
        overlay.connect("state-changed", self._on_overlay_state_changed)
        overlay.connect("close-request", self._on_overlay_close_request)
        self._by_key[key] = overlay
        row = OverlayRow(
            overlay,
            self._close_overlay,
            lambda: self.get_application().scanner.align(overlay),
            lambda: self.get_application().scanner.diff(overlay, fresh=True),
        )
        if index is None:
            self.list_box.append(row)
        else:
            self.list_box.insert(row, index)
        self.list_box.select_row(row)
        overlay.present()
        self._update_stack()
        return overlay

    def _open_from_history(self, path: str) -> None:
        try:
            self.add_overlay(imagesrc.texture_from_file(Gio.File.new_for_path(path)))
        except GLib.Error as error:
            self.toast(f"Could not load that image: {error.message}")

    def _toggle_hidden(self):
        overlay = self.selected_overlay()
        if overlay is not None:
            overlay.set_hidden(not overlay.hidden)

    def _row_for(self, overlay):
        return next((row for row in self.list_box if row.overlay is overlay), None)

    def _close_overlay(self, overlay):
        if overlay is not None:
            overlay.close()

    def _on_overlay_close_request(self, overlay):
        row = self._row_for(overlay)
        self._remember_closed(overlay, row.get_index() if row is not None else 0)
        if row is not None:
            self.list_box.remove(row)
        self._by_key.pop(overlay.key, None)
        # A no-op while undo is the one doing the closing, which is what keeps
        # an undone open from coming back as a step of its own.
        self.undo.push(
            undo.Step(
                apply=self._apply_presence,
                before=_Presence(overlay.key, True),
                after=_Presence(overlay.key, False),
                key=overlay.key,
            )
        )
        GLib.idle_add(self._after_overlay_removed)
        return False

    def _remember_closed(self, overlay, index):
        # Popped before it is set so the dict stays in closing order, which is
        # the order _prune_recipes lets them go in.
        self._recipes.pop(overlay.key, None)
        self._recipes[overlay.key] = _Recipe(
            overlay.view.texture, overlay.image_name, index, overlay.snapshot()
        )

    def _after_overlay_removed(self):
        rows = list(self.list_box)
        if rows and self.list_box.get_selected_row() is None:
            self.list_box.select_row(rows[-1])
        self._update_stack()
        return GLib.SOURCE_REMOVE

    def _update_stack(self):
        has_any = self.list_box.get_row_at_index(0) is not None
        self.stack.set_visible_child_name("ready" if has_any else "empty")
        self._sync_controls()

    # -- undo / redo -------------------------------------------------------

    def _record_state(self, overlay, before, after, tag):
        """An overlay reporting that something about it changed."""
        self.undo.push(
            undo.Step(
                apply=self._apply_state,
                before=(overlay.key, before),
                after=(overlay.key, after),
                key=overlay.key,
                tag=tag,
            )
        )

    def _apply_state(self, payload):
        """The overlay is always there to be restored.

        Steps are dropped oldest first, and an overlay's close is newer than
        every state step of its own, so walking back always meets the close --
        which reopens it -- before any of them; forget() drops a key's steps as
        a set. The guard is for a corrupt stack, not for a reachable case.
        """
        key, state = payload
        overlay = self._by_key.get(key)
        if overlay is None:
            return
        overlay.restore(state)
        # Undo that changes an overlay you are not looking at would otherwise
        # go unnoticed, so the panel follows it.
        self._select(overlay)

    def _apply_presence(self, payload):
        overlay = self._by_key.get(payload.key)
        if payload.open and overlay is None:
            self._reopen(payload.key)
        elif not payload.open and overlay is not None:
            overlay.close()

    def _reopen(self, key):
        recipe = self._recipes.get(key)
        if recipe is not None:
            self._install(recipe.texture, recipe.name, key, recipe.index, recipe.state)

    def _select(self, overlay):
        row = self._row_for(overlay)
        if row is not None and row is not self.list_box.get_selected_row():
            self.list_box.select_row(row)

    def _on_undo_changed(self, *_args):
        self.undo_action.set_enabled(self.undo.can_undo())
        self.redo_action.set_enabled(self.undo.can_redo())
        self._prune_recipes()

    def _prune_recipes(self):
        """Let go of a closed overlay's pixels once undo can no longer reach it.

        An image kept for a possible reopen is an image kept in memory, so the
        history is bounded by that as well as by its step count: past
        CLOSED_LIMIT closed overlays the oldest go, and their steps go with them
        rather than staying on as steps that would silently do nothing.
        """
        live = self.undo.keys() | set(self._by_key)
        self._recipes = {key: r for key, r in self._recipes.items() if key in live}
        closed = [key for key in self._recipes if key not in self._by_key]
        excess = closed[: max(0, len(closed) - CLOSED_LIMIT)]
        if excess:
            for key in excess:
                del self._recipes[key]
            self.undo.forget(set(excess))

    # -- control <-> overlay synchronisation -------------------------------

    def _sync_controls(self):
        overlay = self.selected_overlay()
        if overlay is None:
            return
        self._syncing = True
        self.touchable_row.set_active(overlay.touchable)
        self.on_top_row.set_active(overlay.always_on_top)
        self.opacity_scale.set_value(overlay.view.image_opacity * 100)
        self.border_row.set_active(overlay.view.border_visible)
        self.border_button.set_rgba(overlay.view.border_color)
        self.border_color_row.set_sensitive(overlay.view.border_visible)
        self.zoom_scale.set_value(math.log2(overlay.view.zoom))
        self._sync_numbers(overlay)
        self._syncing = False
        self._update_value_labels(overlay)

    def _sync_numbers(self, overlay):
        x, y, width, height = overlay.geometry()
        for spin, value in (
            (self.position_spins[0], x),
            (self.position_spins[1], y),
            (self.size_spins[0], width),
            (self.size_spins[1], height),
            (self.offset_spins[0], overlay.view.pan_x),
            (self.offset_spins[1], overlay.view.pan_y),
        ):
            spin.set_value(value)

    def _update_value_labels(self, overlay):
        self.opacity_scale.value_label.set_text(f"{overlay.view.image_opacity * 100:.0f}%")
        zoom_percent = overlay.view.zoom * 100
        digits = 0 if zoom_percent >= 10 else 1
        self.zoom_scale.value_label.set_text(f"{zoom_percent:.{digits}f}%")
        self.zoom_row.set_subtitle(
            f"{overlay.view.texture.get_width()} × {overlay.view.texture.get_height()} px"
        )

    def _on_overlay_state_changed(self, overlay):
        """Coalesce: a drag reports every motion event, and restoring a state
        reports once per property, while the panel only has to end up right
        once per turn of the loop."""
        if overlay is not self.selected_overlay() or self._sync_pending:
            return
        self._sync_pending = True
        GLib.idle_add(self._sync_now)

    def _sync_now(self):
        self._sync_pending = False
        self._sync_controls()
        return GLib.SOURCE_REMOVE

    def _on_touchable_toggled(self, row, _param):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_touchable(row.get_active())

    def _on_on_top_toggled(self, row, _param):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_always_on_top(row.get_active())

    def _on_border_toggled(self, row, _param):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_border_visible(row.get_active())
        self.border_color_row.set_sensitive(row.get_active())

    def _on_border_color_changed(self, button, _param):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_border_color(button.get_rgba())

    def _on_opacity_changed(self, scale):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_image_opacity(scale.get_value() / 100)
        if overlay:
            self._update_value_labels(overlay)

    def _on_zoom_changed(self, scale):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_zoom(2 ** scale.get_value())
        if overlay:
            self._update_value_labels(overlay)

    def _on_geometry_spun(self, _spin):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_window_geometry(
                self.position_spins[0].get_value(),
                self.position_spins[1].get_value(),
                self.size_spins[0].get_value(),
                self.size_spins[1].get_value(),
            )

    def _on_offset_spun(self, _spin):
        overlay = self.selected_overlay()
        if overlay and not self._syncing:
            overlay.set_image_pan(
                self.offset_spins[0].get_value(), self.offset_spins[1].get_value()
            )

    def _on_one_to_one(self, _button):
        overlay = self.selected_overlay()
        if overlay:
            overlay.zoom_one_to_one()

    def _on_fit(self, _button):
        overlay = self.selected_overlay()
        if overlay:
            overlay.zoom_to_fit()

    def _on_center(self, _button):
        overlay = self.selected_overlay()
        if overlay:
            overlay.center_image()

    # -- lining up with what is underneath ---------------------------------

    def _action_align(self, *_args):
        self.get_application().scanner.align(self.selected_overlay())

    def _action_diff(self, *_args):
        self.get_application().scanner.diff(self.selected_overlay())

    def panel_rect(self):
        """Where the panel is, so a scan can refuse to photograph it."""
        surface = self.get_surface()
        if surface is None:
            return (0, 0, 0, 0)
        x, y = x11.position(x11.surface_xid(surface))
        return (x, y, self.get_width(), self.get_height())

    # -- image intake ------------------------------------------------------

    def _on_drop(self, _target, value, _x, _y):
        try:
            loaded = imagesrc.texture_from_value(value)
        except GLib.Error as error:
            self.toast(f"Could not load that image: {error.message}")
            return False
        if loaded is None:
            self.toast("That drop did not contain an image.")
            return False
        self.add_overlay(loaded)
        return True

    def _action_paste(self, *_args):
        clipboard = self.get_display().get_clipboard()
        imagesrc.paste(
            clipboard,
            self.add_overlay,
            lambda error: self.toast(
                f"Clipboard has no image: {error.message}" if error else "Clipboard has no image."
            ),
        )

    def _action_open(self, *_args):
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        for mime in (
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "image/bmp",
            "image/tiff",
            "image/svg+xml",
        ):
            image_filter.add_mime_type(mime)
        every_filter = Gtk.FileFilter()
        every_filter.set_name("All files")
        every_filter.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        filters.append(every_filter)

        dialog = Gtk.FileDialog(
            title="Open Image", filters=filters, default_filter=image_filter
        )
        self._above_holds += 1
        dialog.open(self, None, self._on_open_finished)

    def _on_open_finished(self, dialog, result):
        self._above_holds -= 1
        self._on_active_changed()
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed
        try:
            self.add_overlay(imagesrc.texture_from_file(gfile))
        except GLib.Error as error:
            self.toast(f"Could not load that image: {error.message}")

    def toast(self, message: str):
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=4))


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _padded(child):
    child.set_margin_top(8)
    child.set_margin_bottom(8)
    child.set_margin_start(12)
    child.set_margin_end(12)
    return child
