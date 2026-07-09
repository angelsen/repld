"""PNG resize — Pillow-backed, used by Tab.screenshot().

Pre-sizes screenshots to the Anthropic vision API's token grid so the
model sees exactly what we send.
"""

import io

from PIL import Image

_MAX_PX = 1440
_PX_PER_TOKEN = 28
_MAX_TOKENS = 1716  # ceil(1440/28) * ceil(900/28) = 52*33


def _resize_png(data: bytes, tgt_w: int, tgt_h: int) -> bytes:
    """Resize a PNG to (tgt_w, tgt_h). Raises on unparseable image data."""
    img = Image.open(io.BytesIO(data))
    img = img.resize((tgt_w, tgt_h), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


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
