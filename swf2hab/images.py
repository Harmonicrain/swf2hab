"""Pixel conversion and PNG encoding.

Pure standard library by default. When numpy and/or Pillow are installed they are
used to make the same operations faster (numpy) and the PNGs smaller (Pillow's
adaptive filtering, palette output); the decoded pixels are identical either way.
"""

from __future__ import annotations

import io
import math
import re
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


def cut_frame(atlas: bytes, atlas_w: int, frame: dict) -> tuple[int, int, bytes]:
    """A spritesheet frame at its source size: trimmed border restored, rotation undone.

    Follows the TexturePacker JSON layout the bundles use: `frame` gives the sprite's size before
    rotation, and a rotated sprite is stored turned 90 degrees clockwise.
    """
    fr = frame["frame"]
    fw, fh = fr["w"], fr["h"]
    if frame.get("rotated"):
        turned = crop(atlas, atlas_w, fr["x"], fr["y"], fh, fw)
        piece = bytearray(fw * fh * 4)
        for y in range(fh):
            for x in range(fw):
                o = (x * fh + (fh - 1 - y)) * 4
                piece[(y * fw + x) * 4:(y * fw + x) * 4 + 4] = turned[o:o + 4]
        piece = bytes(piece)
    else:
        piece = crop(atlas, atlas_w, fr["x"], fr["y"], fw, fh)
    sss, src = frame.get("spriteSourceSize"), frame.get("sourceSize")
    if not sss or not src or (sss["x"], sss["y"], src["w"], src["h"]) == (0, 0, fw, fh):
        return fw, fh, piece
    canvas = bytearray(src["w"] * src["h"] * 4)
    blit(canvas, src["w"], piece, fw, fh, sss["x"], sss["y"])
    return src["w"], src["h"], bytes(canvas)


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
    short = trns.rstrip(b"\xff")
    if short:
        # Keep the trailing opaque entries when the shortened table would hit Pillow's misreading
        # (see _pillow_misreads_trns).
        extra.append(_chunk(b"tRNS", trns if _pillow_misreads_trns(short) else short))
    return _png(w, h, 3, rows.tobytes(), extra)


def _pillow_misreads_trns(trns: bytes) -> bool:
    """Pillow reads a palette tRNS matching ^\\xff*\\x00\\xff*$ as "one transparent index". Python's
    `$` also matches before a final newline, so a table ending in alpha 10 (0x0A), e.g. 00 0A, is
    read that way too and every alpha-10 entry comes out opaque."""
    return bool(re.match(rb"^\xff*\x00\xff*$", trns)) and not re.fullmatch(rb"\xff*\x00\xff*", trns)


def _palette_trns(data: bytes) -> bytes | None:
    """The tRNS chunk of a palette PNG, else None."""
    if data[25:26] != b"\x03":
        return None
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        if kind == b"tRNS":
            return data[pos + 8:pos + 8 + length]
        if kind in (b"IDAT", b"IEND"):
            return None
        pos += 12 + length
    return None


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
    """Decode a PNG to straight RGBA: Pillow when installed, else the standard-library decoder."""
    if PILImage is not None:
        trns = _palette_trns(data)
        if trns is None or not _pillow_misreads_trns(trns):
            im = PILImage.open(io.BytesIO(data)).convert("RGBA")
            return im.size[0], im.size[1], im.tobytes()
    return _decode_png_pure(data)


def _decode_png_pure(data: bytes) -> tuple[int, int, bytes]:
    """Non-interlaced PNGs of every colour type, 1-16 bits per sample (16-bit keeps the high byte)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, palette, trns = 8, [], b"", b""
    w = h = depth = ctype = interlace = None
    while pos + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if w is None:
        raise ValueError("PNG has no IHDR")
    if interlace:
        raise ValueError("interlaced PNGs need Pillow (pip install Pillow)")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bits = channels * depth
    bpp = max(1, bits // 8)
    stride = (w * bits + 7) // 8
    raw = zlib.decompress(b"".join(idat))
    rows = []
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif f != 0:
            raise ValueError("bad PNG filter %d" % f)
        rows.append(bytes(line))
        prev = line
    out = bytearray(w * h * 4)
    if depth < 8:
        mask = (1 << depth) - 1
        scale = 255 // mask
        grey_key = struct.unpack(">H", trns[:2])[0] if ctype == 0 and len(trns) >= 2 else None
        for y, line in enumerate(rows):
            for x in range(w):
                bit = x * depth
                v = (line[bit >> 3] >> (8 - depth - (bit & 7))) & mask
                o = (y * w + x) * 4
                if ctype == 3:
                    out[o:o + 3] = palette[v * 3:v * 3 + 3]
                    out[o + 3] = trns[v] if v < len(trns) else 255
                else:
                    g = v * scale
                    out[o:o + 4] = bytes((g, g, g, 0 if v == grey_key else 255))
        return w, h, bytes(out)
    step = depth // 8
    key = None
    if trns and ctype in (0, 2):
        key = tuple(struct.unpack(">%dH" % (len(trns) // 2), trns))
    for y, line in enumerate(rows):
        samples = line[::step] if step > 1 else line      # high byte of 16-bit samples
        full = line if step > 1 else None
        for x in range(w):
            s = samples[x * channels:(x + 1) * channels]
            o = (y * w + x) * 4
            if ctype == 6:
                out[o:o + 4] = s
            elif ctype == 3:
                out[o:o + 3] = palette[s[0] * 3:s[0] * 3 + 3]
                out[o + 3] = trns[s[0]] if s[0] < len(trns) else 255
            elif ctype == 4:
                out[o:o + 4] = bytes((s[0], s[0], s[0], s[1]))
            else:
                if full is not None:
                    value = struct.unpack_from(">%dH" % channels, full, x * channels * 2)
                else:
                    value = tuple(s)
                out[o:o + 3] = s if ctype == 2 else bytes((s[0], s[0], s[0]))
                out[o + 3] = 0 if value == key else 255
    return w, h, bytes(out)


def rgba_to_argb_premultiplied(rgba: bytes, w: int, h: int) -> bytes:
    """Straight RGBA -> DefineBitsLossless2 format 5 (premultiplied ARGB).

    Rounds c * a / 255 to nearest, the exact inverse of argb_premultiplied_to_rgba: pixels read
    from a Habbo SWF come back bit-identical.
    """
    n = w * h
    if np is not None:
        arr = np.frombuffer(rgba, dtype=np.uint8, count=n * 4).reshape(n, 4).astype(np.uint32)
        out = np.empty((n, 4), dtype=np.uint8)
        out[:, 0] = arr[:, 3]
        out[:, 1:4] = (2 * arr[:, 0:3] * arr[:, 3:4] + 255) // 510
        return out.tobytes()
    out = bytearray(n * 4)
    for i in range(n):
        j = i * 4
        r, g, b, a = rgba[j:j + 4]
        if a == 255:
            out[j:j + 4] = bytes((255, r, g, b))
        elif a:
            out[j:j + 4] = bytes((a, (2 * r * a + 255) // 510, (2 * g * a + 255) // 510, (2 * b * a + 255) // 510))
    return bytes(out)


def downscale_half(rgba: bytes, w: int, h: int) -> tuple[int, int, bytes]:
    """Half-size copy (rounded up): each pixel averages its 2x2 block in premultiplied space."""
    w2, h2 = (w + 1) // 2, (h + 1) // 2
    out = bytearray(w2 * h2 * 4)
    for y in range(h2):
        for x in range(w2):
            r = g = b = a = 0
            for sy in (2 * y, 2 * y + 1):
                if sy >= h:
                    continue
                for sx in (2 * x, 2 * x + 1):
                    if sx >= w:
                        continue
                    o = (sy * w + sx) * 4
                    pa = rgba[o + 3]
                    r += rgba[o] * pa
                    g += rgba[o + 1] * pa
                    b += rgba[o + 2] * pa
                    a += pa
            if a:
                o = (y * w2 + x) * 4
                half = a // 2
                out[o:o + 4] = bytes((min(255, (r + half) // a), min(255, (g + half) // a),
                                      min(255, (b + half) // a), (a + 2) // 4))
    return w2, h2, bytes(out)
