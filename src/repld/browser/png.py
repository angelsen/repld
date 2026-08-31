"""PNG resize/crop — Pillow-backed, used by Tab.screenshot() and the
coordinate-seeded crop (observe.seeded_tree).

Pre-sizes screenshots to the Anthropic vision API's token grid so the
model sees exactly what we send.
"""

import asyncio
import io
import os
import pathlib
import time

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from ..paths import RUNTIME_DIR, ensure_runtime_dir
from ..state import open_private

_MAX_PX = 1440
_PX_PER_TOKEN = 28
_MAX_TOKENS = 1716  # ceil(1440/28) * ceil(900/28) = 52*33

# PNG text-chunk keys a seeded_tree() crop embeds in itself, so a caller
# handing the path back (browser_tree(at=..., in_crop=path)) never has to
# do crop_origin/scale arithmetic — see read_crop_metadata.
_CROP_ORIGIN_X_KEY = "repld:crop_origin_x"
_CROP_ORIGIN_Y_KEY = "repld:crop_origin_y"
_CROP_SCALE_KEY = "repld:scale"


async def save_capture(prefix: str, target_id: str, data: bytes) -> pathlib.Path:
    """Write a capture to RUNTIME_DIR as {pid}-{prefix}-{tid}-{ns}.png.

    The `{pid}-` prefix is what lets a later kernel's boot sweep reclaim the
    file once this process is gone; 0600 (open_private) because a capture can
    show authenticated page content. time_ns(), not int(time.time()) —
    confirmed live: two captures of the same target within the same
    wall-clock second collided on an identical filename, silently
    overwriting the first before it was ever read.
    """
    ensure_runtime_dir()
    tid = target_id.replace(":", "-")
    out = RUNTIME_DIR / f"{os.getpid()}-{prefix}-{tid}-{time.time_ns()}.png"

    def write() -> None:
        with open_private(out, "wb") as f:
            f.write(data)

    await asyncio.get_running_loop().run_in_executor(None, write)
    return out


def _resize_png(data: bytes, tgt_w: int, tgt_h: int) -> bytes:
    """Resize a PNG to (tgt_w, tgt_h). Raises on unparseable image data."""
    img = Image.open(io.BytesIO(data))
    img = img.resize((tgt_w, tgt_h), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _crop_png(data: bytes, box: tuple[float, float, float, float]) -> bytes:
    """Crop a PNG to (left, top, right, bottom) in the image's own pixel
    space — the caller scales a CSS-pixel box by the device pixel ratio
    first (Page.captureScreenshot's bytes are device pixels; DOM/AX
    geometry is CSS pixels)."""
    img = Image.open(io.BytesIO(data))
    out = io.BytesIO()
    img.crop(box).save(out, format="PNG")
    return out.getvalue()


def _png_size(data: bytes) -> tuple[int, int]:
    """Real pixel dimensions of a PNG. Image.open() is lazy — reading
    .size decodes only the header, not the full pixel buffer — so this is
    cheap enough to call from the kernel loop."""
    return Image.open(io.BytesIO(data)).size


def embed_crop_metadata(
    data: bytes, *, origin_x: float, origin_y: float, scale: float
) -> bytes:
    """Write crop_origin/scale into the PNG's own text chunks.

    Makes the file self-describing: a caller that only has the path (the
    common case — an agent handing back a point it picked in the image)
    can recover the translation via read_crop_metadata without a separate
    registry that would go stale on kernel restart.
    """
    info = PngInfo()
    info.add_text(_CROP_ORIGIN_X_KEY, repr(origin_x))
    info.add_text(_CROP_ORIGIN_Y_KEY, repr(origin_y))
    info.add_text(_CROP_SCALE_KEY, repr(scale))
    img = Image.open(io.BytesIO(data))
    out = io.BytesIO()
    img.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def read_crop_metadata(path: str) -> dict | None:
    """crop_origin/scale back out of a PNG embed_crop_metadata() wrote —
    None if the file carries no repld crop metadata (not one of ours, or
    a plain screenshot rather than a seeded_tree() crop).

    Text chunks populate .text only after the pixel data loads, not on
    Image.open() alone.
    """
    try:
        img = Image.open(path)
        img.load()
        text = img.text  # type: ignore[attr-defined]
        return {
            "crop_origin": {
                "x": float(text[_CROP_ORIGIN_X_KEY]),
                "y": float(text[_CROP_ORIGIN_Y_KEY]),
            },
            "scale": float(text[_CROP_SCALE_KEY]),
        }
    except Exception:
        return None


def _model_dims(w: int, h: int) -> tuple[int, int]:
    def _tok(px: int) -> int:
        return (px - 1) // _PX_PER_TOKEN + 1

    if w <= _MAX_PX and h <= _MAX_PX and _tok(w) * _tok(h) <= _MAX_TOKENS:
        return (w, h)
    if h > w:
        rw, rh = _model_dims(h, w)
        return (rh, rw)
    aspect = w / h
    lo, hi = 1, w
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        mid_h = max(round(mid / aspect), 1)
        if mid <= _MAX_PX and _tok(mid) * _tok(mid_h) <= _MAX_TOKENS:
            lo = mid
        else:
            hi = mid
    return (lo, max(round(lo / aspect), 1))


def check_budget(w: int, h: int, *, force: bool) -> None:
    """Raise unless (w, h) fits the vision token grid or force=True.

    The budget is evaluated in real pixels, never Page.getLayoutMetrics'
    CSS-pixel viewport size — they diverge by the device pixel ratio, and
    the vision API tokenizes on real pixels a screenshot actually delivers.
    """
    if w <= 0 or h <= 0:
        return
    tgt_w, tgt_h = _model_dims(w, h)
    if (tgt_w, tgt_h) != (w, h) and not force:
        raise ValueError(
            f"screenshot would be {w}x{h}, over the vision token budget "
            f"(fits at {tgt_w}x{tgt_h}) — pass force=True to capture natively "
            "anyway, or crop to a smaller region"
        )
