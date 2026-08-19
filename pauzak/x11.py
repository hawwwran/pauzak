"""X11/EWMH plumbing for the overlay windows.

pauzak forces the GTK X11 backend (XWayland under GNOME) because a Wayland
client can neither request always-on-top nor place itself on screen, and both
are core to this tool. Everything here therefore assumes an X11 GdkSurface.
"""

from __future__ import annotations

import gi

gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11  # noqa: E402

from Xlib import X, Xatom  # noqa: E402
from Xlib import display as _xdisplay  # noqa: E402
from Xlib.protocol import event as _xevent  # noqa: E402

_STATE_REMOVE, _STATE_ADD = 0, 1
_SOURCE_APPLICATION = 1 << 12
_MOVE_X, _MOVE_Y = 1 << 8, 1 << 9
_RESIZE_W, _RESIZE_H = 1 << 10, 1 << 11

_dpy = None
_atoms: dict[str, int] = {}


def _display():
    global _dpy
    if _dpy is None:
        _dpy = _xdisplay.Display()
    return _dpy


def _atom(name: str) -> int:
    if name not in _atoms:
        _atoms[name] = _display().intern_atom(name)
    return _atoms[name]


def _win(xid: int):
    return _display().create_resource_object("window", xid)


def surface_xid(surface) -> int:
    return GdkX11.X11Surface.get_xid(surface)


def _send_root(xid: int, message: str, data: list[int]) -> None:
    dpy = _display()
    ev = _xevent.ClientMessage(window=_win(xid), client_type=_atom(message), data=(32, data))
    dpy.screen().root.send_event(
        ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
    )
    dpy.flush()


def set_state(xid: int, atom_name: str, enabled: bool) -> None:
    """Toggle one _NET_WM_STATE_* hint on an already-mapped window."""
    _send_root(
        xid,
        "_NET_WM_STATE",
        [_STATE_ADD if enabled else _STATE_REMOVE, _atom(atom_name), 0, 1, 0],
    )


def set_initial_states(xid: int, atom_names: list[str]) -> None:
    """Seed _NET_WM_STATE before the window is mapped.

    A client message is only honoured once the WM manages the window, so the
    first always-on-top state has to arrive as a property the WM reads at
    MapRequest time.
    """
    dpy = _display()
    _win(xid).change_property(
        _atom("_NET_WM_STATE"), Xatom.ATOM, 32, [_atom(n) for n in atom_names]
    )
    dpy.flush()


def set_above(xid: int, enabled: bool) -> None:
    set_state(xid, "_NET_WM_STATE_ABOVE", enabled)


def set_skip_taskbar(xid: int, enabled: bool) -> None:
    set_state(xid, "_NET_WM_STATE_SKIP_TASKBAR", enabled)
    set_state(xid, "_NET_WM_STATE_SKIP_PAGER", enabled)


def move(xid: int, x: float, y: float) -> None:
    # StaticGravity makes x/y refer to the client area's top-left in root
    # coordinates, matching what position() reports back.
    _send_root(
        xid,
        "_NET_MOVERESIZE_WINDOW",
        [X.StaticGravity | _MOVE_X | _MOVE_Y | _SOURCE_APPLICATION, int(x), int(y), 0, 0],
    )


def move_resize(xid: int, x: float, y: float, width: float, height: float) -> None:
    """Place and size the window in one go.

    Edge resizing goes through this rather than gdk_toplevel_begin_resize:
    Mutter ignores _NET_WM_MOVERESIZE for XWayland clients, so GTK's own
    interactive resize is a no-op in a GNOME Wayland session.
    """
    _send_root(
        xid,
        "_NET_MOVERESIZE_WINDOW",
        [
            X.StaticGravity | _MOVE_X | _MOVE_Y | _RESIZE_W | _RESIZE_H | _SOURCE_APPLICATION,
            int(x),
            int(y),
            int(width),
            int(height),
        ],
    )


def position(xid: int) -> tuple[int, int]:
    """Root coordinates of the window's client-area top-left."""
    dpy = _display()
    geom = dpy.screen().root.translate_coords(_win(xid), 0, 0)
    return geom.x, geom.y


def pointer() -> tuple[int, int]:
    """Root coordinates of the pointer.

    Drags read the pointer from X rather than from GTK event coordinates:
    dragging the window moves it out from under the cursor, which would feed
    back into the next GTK-reported delta and make the motion self-amplifying.
    """
    q = _display().screen().root.query_pointer()
    return q.root_x, q.root_y
