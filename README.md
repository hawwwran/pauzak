# pauzak

Pin an image on top of your other windows and work against it. Named after
*pauzovací papír*, tracing paper.

Drop an image on the control panel (or paste it with Ctrl+V) and it opens as a
borderless overlay window. The panel then keeps hold of that overlay: how
transparent it is, whether the mouse can touch it or falls through to whatever
is underneath, whether it stays above other windows, and how the image is
zoomed and panned inside it. Several images can be open at once, each with its
own settings.

## Why it runs on XWayland

A Wayland client cannot ask to be kept above other windows and cannot place
itself on screen. Both are the entire point of this tool, so `pauzak` forces
`GDK_BACKEND=x11` at startup and runs through XWayland in a GNOME session.
Everything it needs is then available: `_NET_WM_STATE_ABOVE` for stacking, an
empty X input region for click-through, and `_NET_MOVERESIZE_WINDOW` for
positioning.

Setting `PAUZAK_ALLOW_WAYLAND=1` skips that override. The window and the image
still work; always-on-top and dragging do not.

Two Mutter behaviours shaped the code. The first: it ignores `_NET_WM_MOVERESIZE` for XWayland
clients, so GTK's own `gdk_toplevel_begin_resize()` is a no-op here. Edge
resizing therefore drives the geometry directly, the same way the window drag
does. The second is ordinary EWMH rather than a quirk, but it has the same
effect: a window outside the above layer can never be stacked over one inside
it, which is why the panel toggles its own `_NET_WM_STATE_ABOVE` with focus.

## Install

```
./install.sh
```

User-local, no root. It checks the dependencies first, then writes:

| | |
|---|---|
| `~/.local/share/applications/com.hawwwran.pauzak.desktop` | the launcher entry |
| `~/.local/share/icons/hicolor/scalable/apps/com.hawwwran.pauzak.svg` | the icon |
| `~/.local/bin/pauzak` | symlink, so `pauzak` works from a terminal |

Each is written only when it differs from what is already installed, so
re-running is a no-op and the output says which items it touched. The desktop
entry points at this checkout: re-run the script after moving the directory.
It will not overwrite a `~/.local/bin/pauzak` that is not its own symlink.

```
./uninstall.sh
```

Removes all three, and strips `com.hawwwran.pauzak.desktop` out of your
`mimeapps.list` files, which is where a single "Open With" picks up an
association that outlives the deleted desktop entry. Other handlers on those
lines are left alone. It also deletes `~/.local/share/pauzak/history.json`, the
history strip's list of recently used images and the only state pauzak keeps.
Safe to run twice, or when nothing was installed. The checkout is the only
thing left afterwards; delete it to finish.

Needs `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1` and `python3-xlib`, all
present on a stock Zorin 18 install.

## Using it

Getting an image in, from the control panel:

| | |
|---|---|
| Drop a file on the window | opens it as an overlay |
| `Ctrl+V` | pastes an image, or a copied file, from the clipboard |
| `Ctrl+O` | file dialog |
| `Ctrl+H` | hides or shows the selected overlay |
| `Ctrl+W` | closes the selected overlay |
| click a thumbnail in the list | hides or shows that overlay |
| click a thumbnail in the bottom strip | opens a recently used image again |

On the overlay itself:

| | |
|---|---|
| drag | moves the window; it stops at the outer edge of the monitors and slides along it |
| `Shift`+drag | moves it at a fifth of the speed, for placing it exactly |
| `Ctrl`+drag | pans the image inside the window |
| `Ctrl+Shift`+drag | pans at a fifth of the speed |
| `Ctrl+Alt`+drag | pans, snapping the image flush to a window edge within 50 px |
| drag an edge or corner | resizes the window (`Shift` slows this too) |
| wheel | zooms around the pointer (`Shift` for finer steps) |
| `Esc` | closes the overlay |

**Touchable** off makes the overlay ignore the mouse completely. The click is
not merely ignored, it lands on whatever is behind: a button under an
untouchable overlay is pressed as if the overlay were not there. The overlay
gets an empty X input region, so the pointer never hits it in the first place.
That also means you cannot move or resize it again until you switch touchable
back on from the panel.

**Always on top** puts the overlay in the WM's above layer. Stacking is
per-layer, so the panel would be stuck underneath its own overlays; instead the
panel joins that layer while it is focused and leaves again when it is not.
Click the panel and it comes to the front, click anything else and the overlays
go back over it.

**Opacity** is a plain percentage: 100% hides what is behind, lower values let
it through. The slider is labelled opacity rather than transparency because a
number that goes *up* as the image fades reads backwards on a slider.

**Border** draws a 1 px outline at the window's edge in a colour you pick, with
alpha if you want it faint. It is deliberately *not* affected by the opacity
slider: the border marks where the window ends, which is exactly what you
cannot see once a pale image is faded down, and the edges are what you grab to
resize. Fading it with the image would remove it precisely when it earns its
keep.

**Hiding** an overlay takes it off the screen without closing it: its position,
size, zoom, pan and every switch come back untouched. The eye badge on each
thumbnail in the list toggles it, and the thumbnail dims while it is hidden.
Hiding keeps the X window but the WM discards its position, so pauzak stores
the position and puts it back on the way in.

**The history strip** along the bottom of the panel holds up to ten recently
used images, so getting back to one is a click rather than another trip through
the file dialog. Its first three slots go to the desktop's own recent-files
list, and only to entries used in the last fifteen minutes: an image that was
just open in an editor or a browser download is right there, while the
thousands of older entries in that list never show up. The remaining slots come
from pauzak's own history, in `~/.local/share/pauzak/history.json`, which is
the one thing pauzak stores. Pasted images are not in it (there is no file to
re-open), and anything that has since been deleted, moved or is no longer a
readable image is left out.

**Snapping** while panning is what `Alt` adds: bring an edge of the image
within 50 px of the matching edge of the window and it goes flush, which is how
you line a reference up with the frame rather than by eye. The pan pauzak is
actually tracking carries on underneath, so leaving an edge again costs exactly
the distance you drag and nothing extra. GNOME drags windows with `Super`,
which is why `Alt` is free for this; a session set back to `Alt` for window
dragging will take the drag before pauzak sees it.

**Zoom** scales the image inside the window and leaves the window size alone;
anything past the edges is clipped and can be panned into view. **1:1** puts it
at exactly 100%, one image pixel per screen pixel. **Fit** scales it to the
current window, **Center** undoes panning.

## Layout

| | |
|---|---|
| `pauzak/control.py` | the panel: image intake, overlay list, all the controls |
| `pauzak/overlay.py` | the overlay window and its pan/zoom/drag/resize input |
| `pauzak/x11.py` | EWMH and Xlib calls that GTK4 does not expose |
| `pauzak/imagesrc.py` | files, drops and clipboard contents to `Gdk.Texture` |
| `pauzak/history.py` | which recently used images the strip offers |
| `tests/behaviour.py` | end-to-end checks against a live X server |

## Tests

```
python3 tests/behaviour.py
```

It launches the app and verifies the results through X itself: window geometry,
`_NET_WM_STATE`, which window the pointer actually hits, and screenshot pixels
for the opacity blend. It drives the real pointer with `xdotool`, so the cursor
moves on its own for about a minute; run it when you are not using the machine.
Needs `xdotool`, `xclip`, `gnome-screenshot` and `python3-pil`.
