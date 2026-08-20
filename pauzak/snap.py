"""Lining the image up with whatever is underneath the overlay.

Both pictures are reduced to one number per column and one per row: how much
the pixels change across it. That turns "does this design line up with that
page" into two one-dimensional problems, which is the right shape for the job,
because axis-aligned edges are what a layout is made of. The peaks in those
profiles are the box borders, text baselines and image boundaries worth
snapping to.

Two passes per axis. A normalised cross-correlation of the whole profile
decides the shift, so every edge has a say and one spurious line cannot carry
the answer on its own. The shift is then nudged onto the nearest strong pair of
peaks, which is what turns a correlation optimum into a whole number of pixels
with a line you can point at.

The pairs that were close but not taken are returned as well: on a page of
repeated rows several matches are equally defensible, and showing them beats
pretending there was only one.
"""

from __future__ import annotations

from typing import NamedTuple

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

try:
    import numpy as np
except ImportError:  # numpy is what makes this cheap; without it, no aligning
    np = None

MAX_SHIFT = 40  # px searched either way along each axis
MIN_SCORE = 0.12  # correlation below this counts as nothing to line up with
PEAK_LIMIT = 6  # strongest edges kept per axis
PEAK_FLOOR = 0.15  # of the strongest edge; weaker ones are not peaks
PEAK_MIN_GAP = 3  # px; two peaks closer than this are the same edge
LOCK_DISTANCE = 3  # px; a pair this close after the shift is pulled flush
GUIDE_LIMIT = 4  # pairs shown per axis
SAMPLE_CAP = 2000  # px; larger images are strided down before differencing
MIN_OVERLAP = 16  # px of shared profile needed for a correlation to mean much


class Guide(NamedTuple):
    """A pair of edges that could be matched, in window coordinates."""

    axis: str  # "x" for a vertical line, "y" for a horizontal one
    image: float  # where the image's own edge is
    target: float  # where the edge underneath it is
    chosen: bool


class Alignment(NamedTuple):
    dx: float
    dy: float
    guides: tuple[Guide, ...]

    def moved(self) -> bool:
        return self.dx != 0 or self.dy != 0


def available() -> bool:
    return np is not None


def gray_from_pixbuf(pixbuf) -> "np.ndarray":
    """Luminance as a 2-D float array, straight out of a GdkPixbuf."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels, stride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    raw = np.frombuffer(pixbuf.get_pixels(), dtype=np.uint8)
    # The last row is allowed to stop short of a full stride.
    needed = stride * height
    if raw.size < needed:
        raw = np.concatenate([raw, np.zeros(needed - raw.size, dtype=np.uint8)])
    rows = raw[:needed].reshape(height, stride)
    pixels = rows[:, : width * channels].reshape(height, width, channels)
    return _luminance(pixels)


def gray_from_texture(texture: Gdk.Texture) -> "np.ndarray | None":
    pixbuf = Gdk.pixbuf_get_from_texture(texture)
    return gray_from_pixbuf(pixbuf) if pixbuf is not None else None


def align(image_gray, image_rect, window_size, backdrop_gray) -> Alignment:
    """How far to pan the image so its edges land on the ones underneath.

    `image_rect` is where the image is drawn, and `window_size` how big the
    window is, both in the window's own coordinates; `backdrop_gray` is what
    was captured under that window, at whatever resolution the screen gave.
    Everything is resampled into window coordinates here, which is also how
    display scaling is dealt with.
    """
    left, top, drawn_w, drawn_h = (int(round(value)) for value in image_rect)
    window_w, window_h = window_size

    dx, pairs_x = _axis(
        _profile(image_gray, axis=1), drawn_w, left, window_w,
        _profile(backdrop_gray, axis=1),
    )
    dy, pairs_y = _axis(
        _profile(image_gray, axis=0), drawn_h, top, window_h,
        _profile(backdrop_gray, axis=0),
    )
    return Alignment(dx, dy, tuple(_guides("x", pairs_x) + _guides("y", pairs_y)))


def _axis(image_profile, drawn, offset, window, backdrop_profile):
    """One axis: the shift, and the pairs of edges behind it."""
    if image_profile is None or backdrop_profile is None or window < MIN_OVERLAP:
        return 0.0, []
    target = _resample(backdrop_profile, window)
    image = _placed(_resample(image_profile, drawn), offset, window)
    span = (max(0, offset), min(window, offset + drawn))
    if span[1] - span[0] < MIN_OVERLAP:
        return 0.0, []

    shift, score = _best_lag(image, target, span)
    if score < MIN_SCORE:
        return 0.0, []

    image_peaks = _peaks(image, span)
    target_peaks = _peaks(target, (0, window))
    pairs = _pairs(image_peaks, target_peaks, shift)
    if pairs:
        # Land on the strongest edge rather than near it: a correlation
        # optimum half a pixel off is not what "pixel perfect" means.
        residual = pairs[0][1] - pairs[0][0]
        if abs(residual) <= LOCK_DISTANCE:
            shift += round(residual)
            pairs = _pairs(image_peaks, target_peaks, shift)
    return float(shift), pairs


def _luminance(pixels):
    # Strided down before the arithmetic, not after: a 20 MP frame in float
    # would cost a couple of hundred megabytes for nothing.
    step = 1 + max(pixels.shape[:2]) // SAMPLE_CAP
    sample = pixels[::step, ::step, :3].astype(np.float32)
    return 0.299 * sample[:, :, 0] + 0.587 * sample[:, :, 1] + 0.114 * sample[:, :, 2]


def _profile(gray, axis: int):
    """Edge energy per column (axis=1) or per row (axis=0).

    Index i covers the boundary between sample i and i + 1, so a peak's
    position is i + 0.5 once it is back in pixel terms.
    """
    if gray is None or gray.ndim != 2 or min(gray.shape) < 2:
        return None
    return np.abs(np.diff(gray, axis=axis)).sum(axis=1 - axis)


def _resample(profile, length: int):
    """Rescale a profile by integrating it, not by sampling it.

    An edge is one sample wide, so picking every n-th sample loses most of them
    outright: at quarter scale a design's borders simply stop being in the
    profile. Differencing the running total at the new bin edges is a box
    filter instead, which keeps both an edge's weight and its position whether
    the profile is being squeezed or stretched.
    """
    if length <= 1 or profile is None or profile.size == 0:
        return np.zeros(max(length, 0), dtype=np.float32)
    if profile.size == length:
        return profile.astype(np.float32)
    running = np.concatenate([[0.0], np.cumsum(profile, dtype=np.float64)])
    edges = np.linspace(0.0, float(profile.size), length + 1)
    return np.diff(np.interp(edges, np.arange(profile.size + 1), running)).astype(np.float32)


def _placed(profile, offset: int, window: int):
    """The image's profile written into a window-sized array at its offset."""
    placed = np.zeros(window, dtype=np.float32)
    start, stop = max(0, offset), min(window, offset + profile.size)
    if stop > start:
        placed[start:stop] = profile[start - offset : stop - offset]
    return placed


def _best_lag(image, target, span):
    best, best_score = 0, -2.0
    lo, hi = span
    for lag in range(-MAX_SHIFT, MAX_SHIFT + 1):
        low, high = max(lo, -lag), min(hi, target.size - lag)
        if high - low < MIN_OVERLAP:
            continue
        score = _correlation(image[low:high], target[low + lag : high + lag])
        if score > best_score:
            best, best_score = lag, score
    return best, max(best_score, 0.0)


def _correlation(left, right) -> float:
    left = left - left.mean()
    right = right - right.mean()
    scale = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(left @ right / scale) if scale > 1e-6 else -2.0


def _peaks(profile, span):
    """Strongest local maxima inside `span`, subpixel-refined, strongest first."""
    low, high = span
    window = profile[low:high]
    if window.size < 3 or window.max() <= 0:
        return []
    floor = window.max() * PEAK_FLOOR
    chosen: list[tuple[float, float]] = []
    for index in np.argsort(window)[::-1]:
        value = float(window[index])
        if value < floor or len(chosen) >= PEAK_LIMIT:
            break
        position = low + _subpixel(window, int(index)) + 0.5
        if any(abs(position - taken) < PEAK_MIN_GAP for taken, _ in chosen):
            continue
        chosen.append((position, value))
    return chosen


def _subpixel(profile, index: int) -> float:
    """Where the parabola through a peak and its neighbours actually tops out."""
    if 0 < index < profile.size - 1:
        left, middle, right = (float(profile[index + step]) for step in (-1, 0, 1))
        denominator = left - 2 * middle + right
        if denominator != 0:
            delta = 0.5 * (left - right) / denominator
            if abs(delta) <= 1:
                return index + delta
    return float(index)


def _pairs(image_peaks, target_peaks, shift):
    """Each image edge with the nearest edge underneath it, strongest first."""
    paired = []
    for position, strength in image_peaks:
        moved = position + shift
        near = min(target_peaks, key=lambda peak: abs(peak[0] - moved), default=None)
        if near is None or abs(near[0] - moved) > MAX_SHIFT:
            continue
        paired.append((moved, near[0], strength * near[1]))
    paired.sort(key=lambda pair: -pair[2])
    return paired


def _guides(axis: str, pairs) -> list[Guide]:
    return [
        Guide(axis, image, target, abs(target - image) <= 1.0)
        for image, target, _strength in pairs[:GUIDE_LIMIT]
    ]
