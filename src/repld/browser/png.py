"""PNG resize — pure stdlib (struct + zlib), used by Tab.screenshot().

Ported from resize.rs via nanokvm client. Pre-sizes to the Anthropic
vision API's token grid so the model sees exactly what we send.
"""

_MAX_PX = 1440
_PX_PER_TOKEN = 28
_MAX_TOKENS = 1716  # ceil(1440/28) * ceil(900/28) = 52*33


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _resize_png(data: bytes, tgt_w: int, tgt_h: int) -> bytes:
    """Nearest-neighbor resize of an RGBA PNG. Pure stdlib (struct + zlib)."""
    import struct
    import zlib

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    ihdr = data[16:29]
    src_w, src_h, bit_depth, color_type = struct.unpack(">IIbB", ihdr[:10])
    if bit_depth != 8 or color_type not in (2, 6):
        return data
    bpp = 4 if color_type == 6 else 3
    stride = 1 + src_w * bpp  # filter byte + pixels

    idat = bytearray()
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        if ctype == b"IDAT":
            idat.extend(data[pos + 8 : pos + 8 + length])
        pos += 12 + length

    raw = zlib.decompress(bytes(idat))

    # Unfilter scanlines — PNG filters encode deltas, not raw pixels.
    row_len = src_w * bpp
    rows: list[bytearray] = []
    prev = bytearray(row_len)
    for y in range(src_h):
        off = y * stride
        filt = raw[off]
        cur = bytearray(raw[off + 1 : off + 1 + row_len])
        if filt == 1:  # Sub
            for i in range(bpp, row_len):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif filt == 2:  # Up
            for i in range(row_len):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(row_len):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + (a + prev[i]) // 2) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(row_len):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + _paeth(a, b, c)) & 0xFF
        rows.append(cur)
        prev = cur

    out_rows = bytearray()
    for ty in range(tgt_h):
        sy = ty * src_h // tgt_h
        src_row = rows[sy]
        out_rows.append(0)  # no filter
        for tx in range(tgt_w):
            sx = tx * src_w // tgt_w
            out_rows.extend(src_row[sx * bpp : sx * bpp + bpp])

    compressed = zlib.compress(bytes(out_rows))

    def _chunk(ctype: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)

    new_ihdr = struct.pack(">IIbBbbb", tgt_w, tgt_h, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", new_ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


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
