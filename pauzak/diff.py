"""Where the image and what is under it disagree, drawn so you can act on it.

The comparison is perceptual rather than arithmetic. A raw RGB subtraction
lights up every difference in font rendering, every subpixel fringe and every
half-shade of anti-aliasing, which buries the differences that matter; the
weighted YIQ distance and the constants below are pixelmatch's, which is the
measure the visual-regression tools settled on. Their anti-aliasing test comes
with it, and it earns its keep: a pixel is forgiven only when it is a
transition between two flat regions that both pictures have, which is what an
anti-aliased or differently hinted edge in the same place looks like. A stroke
the page is missing is not that, however thin it is. Without the test every
glyph on the page lights up, since no two text renderings agree pixel for
pixel; with it, only disagreements about where things are survive.

One threshold is not enough, though, and the reason is worth spelling out. The
pale boxes interfaces are made of sit a dozen levels away from the white behind
them -- a real measurement: 244 against 255. Any threshold loose enough to
ignore a page whose white is a shade off is far too loose to notice such a box
moving, so with a single bar the text shifts and the box silently does not.
Lowering the bar instead trades one silence for a worse noise: eight levels of
overall tint then paints the whole overlay.

So a difference is measured twice. Once as it is, which catches anything loud
enough to matter on its own, including a fill that is simply the wrong colour.
And once with the average of the pixel's neighbourhood taken out of it, which
is nothing at all when a whole region is a shade off and very nearly the whole
difference when an edge has moved. The second bar can then be low, because what
it measures is local by construction: a page tinted differently everywhere
stands out from its own neighbourhood nowhere.

What is drawn is a highlight layer, not a picture to compare by eye. Everything
that matches is fully transparent, so the page underneath stays visible and in
place, and only the disagreements are painted. They are painted in two colours
rather than one: knowing an edge is out is half the answer, and knowing which
side has the ink is the other half.
"""

from __future__ import annotations

from typing import NamedTuple

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

try:
    import numpy as np
except ImportError:  # same as snap: numpy is what makes this cheap
    np = None

THRESHOLD = 0.10  # pixelmatch's default, as a fraction of the largest distance
# The second, lower bar, for differences that are local rather than overall.
# A pale box moved a few pixels differs by ten levels or so, far under the
# threshold above, while a page whose white is a shade off differs by as much
# everywhere -- so the one that matters is the one with an edge to it.
STRUCTURAL = 0.032
RADIUS = 6  # px; the neighbourhood a difference has to stand out from
MAX_DISTANCE = 35215.0  # the weighted distance between black and white
SIBLINGS = 3  # identical neighbours that make a pixel part of a flat region
FLAT_LIMIT = 2  # identical neighbours a transition pixel may have and no more
MIN_ALPHA, MAX_ALPHA = 0.45, 0.92  # how solid a highlight gets, by magnitude
# Magenta and yellow because interfaces so rarely use them: a mark has to be
# obviously a mark and not something the page might have drawn itself, which
# ruled out the blue this started as -- it was a near match for a button.
IMAGE_DARKER = (230, 0, 126)
SCREEN_DARKER = (255, 212, 0)

_OFFSETS = tuple(
    (dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)
)

# Y is what the eye weighs most; I and Q carry the rest. pixelmatch's matrix.
_YIQ = (
    (0.29889531, 0.58662247, 0.11448223),
    (0.59597799, -0.27417610, -0.32180189),
    (0.21147017, -0.52261711, 0.31114694),
)


class Result(NamedTuple):
    rgba: object  # (h, w, 4) uint8, ready to become a texture
    coverage: float  # fraction of the compared pixels that disagree


def available() -> bool:
    return np is not None


def rgb_from_pixbuf(pixbuf) -> "np.ndarray":
    """The pixels as a float32 (h, w, 3) array, alpha thrown away."""
    return _pixels(pixbuf)[:, :, :3].astype(np.float32)


def over(pixbuf, screen_rgb) -> "np.ndarray":
    """What the image actually shows, given what is behind it.

    A design exported with a transparent background is the ordinary case, and
    where it is see-through what you see is the page. Comparing its stored
    colour there -- black, as often as not -- against the page would report the
    whole overlay as one enormous difference.
    """
    pixels = _pixels(pixbuf)
    rgb = pixels[:, :, :3].astype(np.float32)
    if pixels.shape[2] < 4:
        return rgb
    alpha = pixels[:, :, 3:4].astype(np.float32) / 255.0
    return rgb * alpha + screen_rgb * (1.0 - alpha)


def _pixels(pixbuf):
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels, stride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    raw = np.frombuffer(pixbuf.get_pixels(), dtype=np.uint8)
    needed = stride * height
    if raw.size < needed:  # the last row may stop short of a full stride
        raw = np.concatenate([raw, np.zeros(needed - raw.size, dtype=np.uint8)])
    rows = raw[:needed].reshape(height, stride)
    return rows[:, : width * channels].reshape(height, width, channels)


def texture_from_rgba(rgba) -> Gdk.Texture:
    height, width = rgba.shape[:2]
    data = GLib.Bytes.new(rgba.tobytes())
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, True, 8, width, height, width * 4
    )
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def compare(image_rgb, screen_rgb, threshold: float = THRESHOLD) -> Result | None:
    """Both arrays are (h, w, 3) in the same coordinate space."""
    if np is None or image_rgb.shape != screen_rgb.shape or image_rgb.size == 0:
        return None
    image_yiq = _yiq(image_rgb)
    screen_yiq = _yiq(screen_rgb)
    delta = image_yiq - screen_yiq
    distance = _distance(delta)
    # Two bars, because one number cannot separate a shifted pale edge from a
    # page tinted a shade differently: they differ by the same tiny amount. The
    # first bar catches any disagreement large enough to matter on its own. The
    # second removes each pixel's neighbourhood average from the difference
    # first, which leaves an overall shift at nothing and a local one intact.
    local = np.stack([_box_mean(plane, RADIUS) for plane in delta])
    standout = _distance(delta - local)
    # Both parts have to hold for the low bar: the pixel itself has to differ,
    # and that difference has to stand out from its surroundings. Asking only
    # the second paints a halo around every mark, since taking a neighbourhood
    # average out of nothing leaves the average.
    faint = (distance > STRUCTURAL**2) & (standout > STRUCTURAL**2)
    differing = (distance > threshold**2) | faint
    distance = np.maximum(distance, standout)
    if differing.any():
        image_around = _Neighbourhood(image_yiq[0])
        screen_around = _Neighbourhood(screen_yiq[0])
        differing &= ~(
            image_around.antialiased(screen_around)
            | screen_around.antialiased(image_around)
        )

    # Alpha rises with the distance, so a faint disagreement reads as faint.
    strength = np.sqrt(np.clip(distance, 0.0, 1.0))
    alpha = MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * np.clip(
        (strength - threshold) / max(1e-6, 1.0 - threshold), 0.0, 1.0
    )

    height, width = differing.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    darker_in_image = delta[0] < 0  # Y is luminance; less of it is darker
    for mask, color in (
        (differing & darker_in_image, IMAGE_DARKER),
        (differing & ~darker_in_image, SCREEN_DARKER),
    ):
        for channel, value in enumerate(color):
            rgba[:, :, channel][mask] = value
    rgba[:, :, 3][differing] = (alpha[differing] * 255).astype(np.uint8)
    return Result(rgba, float(differing.mean()))


def _yiq(rgb):
    """The three channels stacked, so the difference is one subtraction."""
    return np.stack(
        [sum(weight * rgb[:, :, band] for band, weight in enumerate(row)) for row in _YIQ]
    )


def _box_mean(plane, radius: int):
    """The average over a square neighbourhood, by summed-area table."""
    padded = np.pad(plane, radius, mode="edge")
    integral = np.zeros(
        (padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64
    )
    integral[1:, 1:] = padded.cumsum(axis=0).cumsum(axis=1)
    side = 2 * radius + 1
    height, width = plane.shape
    total = (
        integral[side : side + height, side : side + width]
        - integral[0:height, side : side + width]
        - integral[side : side + height, 0:width]
        + integral[0:height, 0:width]
    )
    return (total / (side * side)).astype(np.float32)


def _distance(delta):
    """The weighted YIQ distance, as a fraction of the largest there is."""
    return (
        0.5053 * delta[0] ** 2 + 0.299 * delta[1] ** 2 + 0.1957 * delta[2] ** 2
    ) / MAX_DISTANCE


class _Neighbourhood:
    """One picture's luminance, and what surrounds each of its pixels.

    The eight shifted copies are built once and then asked from. The
    anti-aliasing test wants them four times over, and copying a whole frame
    per question was most of the work a comparison did.
    """

    def __init__(self, luma):
        self.luma = luma
        self.around = np.stack([_shift(luma, dy, dx) for dy, dx in _OFFSETS])
        # Same luminance rather than the same three channels: cheaper by a
        # third, and two colours that match in brightness to the level are a
        # flat region for the purpose this serves.
        self.flat = (self.around == luma).sum(axis=0) >= SIBLINGS
        self.flat_around = np.stack([_shift(self.flat, dy, dx) for dy, dx in _OFFSETS])

    def antialiased(self, other: "_Neighbourhood"):
        """pixelmatch's test, per pixel and all at once.

        A pixel counts as rendering rather than difference when it sits between
        a brighter and a darker neighbour, is not itself part of a flat region,
        and the region on one side of it is flat in both pictures. That is an
        edge whose softness differs; one in the wrong place has no such
        agreement to point to.
        """
        deltas = self.luma - self.around
        identical = (deltas == 0).sum(axis=0)
        brightest, darkest = deltas.argmin(axis=0), deltas.argmax(axis=0)
        transition = (
            (identical <= FLAT_LIMIT)
            & (_pick(deltas, brightest) < 0)
            & (_pick(deltas, darkest) > 0)
        )
        agreed = np.zeros(self.luma.shape, dtype=bool)
        for side in (brightest, darkest):
            agreed |= _pick(self.flat_around, side) & _pick(other.flat_around, side)
        return transition & agreed


def _pick(stacked, chosen):
    return np.take_along_axis(stacked, chosen[None], axis=0)[0]


def _shift(array, dy: int, dx: int):
    """The array moved by (dy, dx), the edges holding their own value."""
    pad = [(1, 1), (1, 1)] + [(0, 0)] * (array.ndim - 2)
    padded = np.pad(array, pad, mode="edge")
    height, width = array.shape[:2]
    return padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
