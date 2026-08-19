"""Turning files, drag-and-drop payloads and clipboard contents into textures."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gio  # noqa: E402

PASTED_NAME = "Pasted image"


class Loaded(NamedTuple):
    """An image ready to open. `path` is None for clipboard pixels, which have
    no file behind them and so cannot go into the history strip."""

    texture: Gdk.Texture
    name: str
    path: str | None


def name_for_file(gfile: Gio.File) -> str:
    return gfile.get_basename() or gfile.get_uri()


def texture_from_file(gfile: Gio.File) -> Loaded:
    return Loaded(Gdk.Texture.new_from_file(gfile), name_for_file(gfile), gfile.get_path())


def _file_from_text(text: str) -> Gio.File | None:
    text = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not text:
        return None
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme != "file":
            return None
        return Gio.File.new_for_path(unquote(parsed.path))
    path = Path(text).expanduser()
    return Gio.File.new_for_path(str(path)) if path.exists() else None


def texture_from_value(value) -> Loaded | None:
    """Best-effort conversion of a drop / clipboard value into a texture."""
    if isinstance(value, Gdk.Texture):
        return Loaded(value, PASTED_NAME, None)
    if isinstance(value, GdkPixbuf.Pixbuf):
        return Loaded(Gdk.Texture.new_for_pixbuf(value), PASTED_NAME, None)
    if isinstance(value, Gdk.FileList):
        files = value.get_files()
        return texture_from_file(files[0]) if files else None
    if isinstance(value, Gio.File):
        return texture_from_file(value)
    if isinstance(value, str):
        gfile = _file_from_text(value)
        return texture_from_file(gfile) if gfile else None
    return None


def paste(clipboard: Gdk.Clipboard, on_image, on_error) -> None:
    """Read an image off the clipboard, trying each representation in turn.

    Calls on_image with a Loaded, or on_error with the failure. Order matters:
    apps that copy a file offer both raw pixels and a path, and the path keeps
    the original filename for the overlay's label.
    """

    def fail(exc):
        on_error(exc)

    def try_text():
        def done(_cb, res):
            try:
                text = clipboard.read_text_finish(res)
            except GLib.Error as exc:
                fail(exc)
                return
            gfile = _file_from_text(text or "")
            if gfile is None:
                fail(None)
                return
            try:
                on_image(texture_from_file(gfile))
            except GLib.Error as exc:
                fail(exc)

        clipboard.read_text_async(None, done)

    def try_file():
        def done(_cb, res):
            try:
                value = clipboard.read_value_finish(res)
            except GLib.Error:
                try_text()
                return
            if not isinstance(value, Gio.File):
                try_text()
                return
            try:
                on_image(texture_from_file(value))
            except GLib.Error:
                try_text()

        clipboard.read_value_async(Gio.File.__gtype__, GLib.PRIORITY_DEFAULT, None, done)

    def try_texture():
        def done(_cb, res):
            try:
                texture = clipboard.read_texture_finish(res)
            except GLib.Error:
                texture = None
            if texture is None:
                try_file()
                return
            on_image(Loaded(texture, PASTED_NAME, None))

        clipboard.read_texture_async(None, done)

    try_texture()
