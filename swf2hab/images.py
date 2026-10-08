"""Pixel conversion and PNG encoding.

Pure standard library by default. When numpy and/or Pillow are installed they are
used to make the same operations faster (numpy) and the PNGs smaller (Pillow's
adaptive filtering, palette output); the decoded pixels are identical either way.
"""

from __future__ import annotations

import io
import math
import struct
import zlib

import os

try:  # optional accelerators
    import numpy as np
except Exception:  # pragma: no cover - depends on environment
    np = None
try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None
if os.environ.get("SWF2HAB_PURE"):  # force the standard-library-only code paths
    np = None
    PILImage = None


def accelerators() -> dict:
    return {"numpy": np is not None, "pillow": PILImage is not None}


# --------------------------------------------------------------------------- decode

def argb_premultiplied_to_rgba(data: bytes, w: int, h: int) -> bytes:
    """DefineBitsLossless2 format 5: premultiplied ARGB -> straight RGBA."""
    n = w * h
    if np is not None:
        arr = np.frombuffer(data, dtype=np.uint8, count=n * 4).reshape(n, 4)
        a = arr[:, 0].astype(np.uint32)
        rgb = arr[:, 1:4].astype(np.uint32)
        # Math.round(c * (255 / a)) in float64, matching Habbo's converter bit for bit
        scale = 255.0 / np.maximum(a, 1).astype(np.float64)
        un = np.minimum(np.floor(rgb * scale[:, None] + 0.5), 255).astype(np.uint32)
        un[a == 0] = 0
        out = np.empty((n, 4), dtype=np.uint8)
        out[:, 0:3] = un
        out[:, 3] = arr[:, 0]
        return out.tobytes()
    out = bytearray(n * 4)
    out[0::4] = data[1:n * 4:4]
    out[1::4] = data[2:n * 4:4]
    out[2::4] = data[3:n * 4:4]
    out[3::4] = data[0:n * 4:4]
    alphas = data[0:n * 4:4]
    for i, a in enumerate(alphas):
        if a == 255:
            continue
        j = i * 4
        if a == 0:
            out[j] = out[j + 1] = out[j + 2] = 0
        else:
            scale = 255.0 / a
            out[j] = min(255, math.floor(out[j] * scale + 0.5))
            out[j + 1] = min(255, math.floor(out[j + 1] * scale + 0.5))
            out[j + 2] = min(255, math.floor(out[j + 2] * scale + 0.5))
    return bytes(out)


def xrgb_to_rgba(data: bytes, w: int, h: int) -> bytes:
    """DefineBitsLossless format 5: pad byte + RGB, always opaque."""
    n = w * h
    out = bytearray(n * 4)
    out[0::4] = data[1:n * 4:4]
    out[1::4] = data[2:n * 4:4]
    out[2::4] = data[3:n * 4:4]
    out[3::4] = b"\xff" * n
    return bytes(out)


def pix15_to_rgba(data: bytes, w: int, h: int) -> bytes:
    stride = (w * 2 + 3) & ~3
    out = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            (v,) = struct.unpack_from(">H", data, y * stride + x * 2)
            j = (y * w + x) * 4
            out[j] = ((v >> 10) & 31) * 255 // 31
            out[j + 1] = ((v >> 5) & 31) * 255 // 31
            out[j + 2] = (v & 31) * 255 // 31
            out[j + 3] = 255
    return bytes(out)


def colormapped_to_rgba(table: bytes, entry: int, pixels: bytes, w: int, h: int, premultiplied: bool) -> bytes:
    colors = []
    for i in range(0, len(table), entry):
        if entry == 4:
            r, g, b, a = table[i:i + 4]
            if premultiplied and 0 < a < 255:
                r, g, b = (min(255, math.floor(c * (255.0 / a) + 0.5)) for c in (r, g, b))
            elif a == 0:
                r = g = b = 0
        else:
            r, g, b = table[i:i + 3]
            a = 255
        colors.append(bytes((r, g, b, a)))
    blank = b"\0\0\0\0"
    stride = (w + 3) & ~3
    out = bytearray()
    for y in range(h):
        row = pixels[y * stride:y * stride + w]
        out += b"".join(colors[p] if p < len(colors) else blank for p in row)
    return bytes(out)


def _image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def decode_embedded_image(image: bytes, alpha_zlib: bytes | None):
    """Decode a JPEG/PNG/GIF payload from a DefineBits* tag (needs Pillow for JPEG)."""
    from .swf import Bitmap

    if image[:4] == b"\xff\xd9\xff\xd8":  # erroneous header written by old Flash tools
        image = image[4:]
    mime = _image_mime(image)
    if mime == "image/jpeg":
        # Tables and image data are sometimes concatenated with an EOI/SOI pair between them.
        cut = image.find(b"\xff\xd9\xff\xd8", 2)
        if cut > 0:
            image = image[:cut] + image[cut + 4:]
    if PILImage is None:
        w = h = 0
        if mime == "image/png":
            w, h = struct.unpack(">II", image[16:24])
        return Bitmap(w, h, encoded=image, encoded_mime=mime)
    im = PILImage.open(io.BytesIO(image)).convert("RGBA")
    w, h = im.size
    rgba = bytearray(im.tobytes())
    if alpha_zlib:
        alpha = zlib.decompressobj().decompress(alpha_zlib)
        if len(alpha) >= w * h:
            rgba[3::4] = alpha[: w * h]
    return Bitmap(w, h, rgba=bytes(rgba))


# --------------------------------------------------------------------------- geometry

def trim_box(rgba: bytes, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Bounding box (x, y, w, h) of pixels with alpha > 0, or None if fully transparent."""
    if w == 0 or h == 0:
        return None
    if np is not None:
        a = np.frombuffer(rgba, dtype=np.uint8, count=w * h * 4)[3::4].reshape(h, w)
        rows = np.flatnonzero(a.any(axis=1))
        if rows.size == 0:
            return None
        cols = np.flatnonzero(a.any(axis=0))
        return int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)
    stride = w * 4
    top = bottom = None
    left, right = w, -1
    for y in range(h):
        alpha = rgba[y * stride + 3:(y + 1) * stride:4]
        if alpha.strip(b"\0"):
            if top is None:
                top = y
            bottom = y
            first = len(alpha) - len(alpha.lstrip(b"\0"))
            last = len(alpha.rstrip(b"\0")) - 1
            left = min(left, first)
            right = max(right, last)
    if top is None:
        return None
    return left, top, right - left + 1, bottom - top + 1


def crop(rgba: bytes, w: int, x: int, y: int, cw: int, ch: int) -> bytes:
    stride = w * 4
    return b"".join(rgba[(y + r) * stride + x * 4:(y + r) * stride + (x + cw) * 4] for r in range(ch))


def blit(atlas: bytearray, atlas_w: int, src: bytes, sw: int, sh: int, dx: int, dy: int) -> None:
    astride = atlas_w * 4
    sstride = sw * 4
    for r in range(sh):
        o = (dy + r) * astride + dx * 4
        atlas[o:o + sstride] = src[r * sstride:(r + 1) * sstride]


# --------------------------------------------------------------------------- encode

def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png(width: int, height: int, color_type: int, rows: bytes, extra: list[bytes]) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    out = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", ihdr)]
    out.extend(extra)
    out.append(_chunk(b"IDAT", zlib.compress(rows, 9)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def _palette_png(rgba: bytes, w: int, h: int) -> bytes | None:
    """Lossless indexed PNG when the image has at most 256 distinct RGBA values (numpy only)."""
    if np is None:
        return None
    px = np.frombuffer(rgba, dtype=np.uint32, count=w * h)
    colors, index = np.unique(px, return_inverse=True)
    if colors.size > 256:
        return None
    pal = colors.view(np.uint8).reshape(-1, 4)
    plte = pal[:, 0:3].tobytes()
    trns = pal[:, 3].tobytes()
    idx = index.astype(np.uint8).reshape(h, w)
    rows = np.empty((h, w + 1), dtype=np.uint8)
    rows[:, 0] = 0
    rows[:, 1:] = idx
    extra = [_chunk(b"PLTE", plte)]
    if trns.rstrip(b"\xff"):
        extra.append(_chunk(b"tRNS", trns.rstrip(b"\xff")))
    return _png(w, h, 3, rows.tobytes(), extra)


def encode_png(rgba: bytes, w: int, h: int) -> bytes:
    """Smallest of: indexed PNG (<=256 colours), Pillow RGBA PNG, plain RGBA PNG."""
    candidates = []
    pal = _palette_png(rgba, w, h)
    if pal is not None:
        candidates.append(pal)
    if PILImage is not None:
        buf = io.BytesIO()
        PILImage.frombytes("RGBA", (w, h), rgba).save(buf, "PNG", compress_level=9)
        candidates.append(buf.getvalue())
    if not candidates:
        stride = w * 4
        rows = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride] for y in range(h))
        candidates.append(_png(w, h, 6, rows, []))
    return min(candidates, key=len)


def decode_png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG to RGBA (Pillow required); used by the comparison tooling only."""
    if PILImage is None:
        raise RuntimeError("Pillow is required to decode PNG files")
    im = PILImage.open(io.BytesIO(data)).convert("RGBA")
    return im.size[0], im.size[1], im.tobytes()
