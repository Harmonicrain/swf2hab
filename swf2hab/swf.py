"""Minimal SWF reader for Habbo asset libraries.

Only the tags that Habbo asset SWFs actually use are decoded: SymbolClass,
DefineBinaryData, the DefineBits* bitmap family and DefineSound. Everything else
is counted so the caller can report content that a .hab bundle cannot carry
(vector shapes, timelines, fonts, text).
"""

from __future__ import annotations

import lzma
import struct
import zlib
from collections import Counter
from dataclasses import dataclass, field

from . import images


class SwfError(Exception):
    pass


TAG_NAMES = {
    0: "End", 1: "ShowFrame", 2: "DefineShape", 6: "DefineBits", 8: "JPEGTables", 9: "SetBackgroundColor",
    10: "DefineFont", 11: "DefineText", 12: "DoAction", 14: "DefineSound", 20: "DefineBitsLossless",
    21: "DefineBitsJPEG2", 22: "DefineShape2", 26: "PlaceObject2", 28: "RemoveObject2", 32: "DefineShape3",
    33: "DefineText2", 35: "DefineBitsJPEG3", 36: "DefineBitsLossless2", 37: "DefineEditText", 39: "DefineSprite",
    41: "ProductInfo", 43: "FrameLabel", 48: "DefineFont2", 56: "ExportAssets", 64: "EnableDebugger2",
    65: "ScriptLimits", 69: "FileAttributes", 72: "DoABC", 73: "DefineFontAlignZones", 75: "DefineFont3",
    76: "SymbolClass", 77: "Metadata", 82: "DoABC2", 83: "DefineShape4", 86: "DefineSceneAndFrameLabelData",
    87: "DefineBinaryData", 88: "DefineFontName", 90: "DefineBitsJPEG4", 91: "DefineFont4",
}

# Content a .hab bundle cannot represent. Shapes inside an asset SWF are normally
# Flex preloader leftovers, so they are reported rather than treated as fatal.
VECTOR_TAGS = {2, 22, 32, 83, 39, 10, 48, 75, 91, 11, 33, 37}
CODE_TAGS = {72, 82, 12}


@dataclass
class Bitmap:
    width: int
    height: int
    rgba: bytes | None = None          # straight (non-premultiplied) RGBA, row-major
    encoded: bytes | None = None       # original encoded image when it could not be decoded
    encoded_mime: str | None = None


@dataclass
class Sound:
    mime: str
    data: bytes
    params: dict | None = None   # DefineSound header fields, so a reader can rebuild the tag


@dataclass
class SwfAssets:
    version: int
    frame_rate: float
    document_class: str
    symbols: list[tuple[int, str]]                    # SymbolClass order, (character id, class name)
    binaries: dict[int, bytes] = field(default_factory=dict)
    bitmaps: dict[int, Bitmap] = field(default_factory=dict)
    sounds: dict[int, Sound] = field(default_factory=dict)
    tag_counts: Counter = field(default_factory=Counter)
    abc_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def names_for(self, char_id: int) -> list[str]:
        return [n for cid, n in self.symbols if cid == char_id]

    def binary_by_name(self, name: str) -> bytes | None:
        for cid, n in self.symbols:
            if n == name and cid in self.binaries:
                return self.binaries[cid]
        return None

    @property
    def vector_tag_count(self) -> int:
        return sum(c for t, c in self.tag_counts.items() if t in VECTOR_TAGS)


def decompress(data: bytes) -> bytes:
    """Return the SWF as an uncompressed FWS byte string."""
    if len(data) < 8:
        raise SwfError("file shorter than a SWF header")
    sig = data[:3]
    if sig == b"FWS":
        return data
    if sig == b"CWS":
        d = zlib.decompressobj()
        body = d.decompress(data[8:])
        return b"FWS" + data[3:8] + body
    if sig == b"ZWS":
        if len(data) < 17:
            raise SwfError("truncated LZMA SWF header")
        total = struct.unpack("<I", data[4:8])[0]
        props, dict_size = data[12], struct.unpack("<I", data[13:17])[0]
        lc, rest = props % 9, props // 9
        lp, pb = rest % 5, rest // 5
        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}]
        d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        body = d.decompress(data[17:], max_length=max(0, total - 8))
        return b"FWS" + data[3:8] + body
    raise SwfError("not a SWF file (bad signature %r)" % sig)


def iter_tags(raw: bytes):
    nbits = raw[8] >> 3
    pos = 8 + (5 + nbits * 4 + 7) // 8 + 4
    end = len(raw)
    while pos + 2 <= end:
        (code_len,) = struct.unpack_from("<H", raw, pos)
        pos += 2
        code, length = code_len >> 6, code_len & 0x3F
        if length == 0x3F:
            if pos + 4 > end:
                break
            (length,) = struct.unpack_from("<I", raw, pos)
            pos += 4
        body = raw[pos:pos + length]
        pos += length
        yield code, body
        if code == 0:
            break


def _cstring(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj()
    return d.decompress(data)


def _decode_lossless(body: bytes, alpha: bool) -> tuple[int, Bitmap]:
    char_id, fmt, w, h = struct.unpack_from("<HBHH", body, 0)
    pos = 7
    if fmt == 3:
        table_size = body[pos] + 1
        pos += 1
    data = _inflate(body[pos:])
    if fmt == 5:
        need = w * h * 4
    elif fmt == 3:
        need = table_size * (4 if alpha else 3) + ((w + 3) & ~3) * h
    else:
        need = ((w * 2 + 3) & ~3) * h
    short = len(data) < need
    if short:  # truncated stream: keep what decoded and leave the rest transparent
        data = data + bytes(need - len(data))
    if fmt == 5:
        if alpha:
            rgba = images.argb_premultiplied_to_rgba(data[: w * h * 4], w, h)
        else:
            rgba = images.xrgb_to_rgba(data[: w * h * 4], w, h)
    elif fmt == 3:
        entry = 4 if alpha else 3
        table = data[: table_size * entry]
        pixels = data[table_size * entry:]
        rgba = images.colormapped_to_rgba(table, entry, pixels, w, h, premultiplied=alpha)
    elif fmt == 4 and not alpha:
        rgba = images.pix15_to_rgba(data, w, h)
    else:
        raise SwfError("unsupported lossless bitmap format %d" % fmt)
    return char_id, Bitmap(w, h, rgba=rgba), short


def _decode_jpeg_family(code: int, body: bytes, jpeg_tables: bytes | None) -> tuple[int, Bitmap]:
    (char_id,) = struct.unpack_from("<H", body, 0)
    alpha_data = None
    if code == 21:
        image = body[2:]
    elif code in (35, 90):
        (alpha_offset,) = struct.unpack_from("<I", body, 2)
        start = 6 if code == 35 else 8
        image = body[start:start + alpha_offset]
        alpha_data = body[start + alpha_offset:]
    else:  # DefineBits (6) uses the shared JPEGTables
        image = (jpeg_tables or b"") + body[2:]
    return char_id, images.decode_embedded_image(image, alpha_data)


def _decode_sound(body: bytes) -> tuple[int, Sound] | None:
    char_id, flags = struct.unpack_from("<HB", body, 0)
    fmt = flags >> 4
    data = body[7:]
    if fmt == 2:  # MP3: SeekSamples (SI16) precedes the frames
        (samples,) = struct.unpack_from("<I", body, 3)
        (seek,) = struct.unpack_from("<h", body, 7)
        params = {"format": 2, "rate": (5512, 11025, 22050, 44100)[(flags >> 2) & 3],
                  "bits": 16 if flags & 2 else 8, "channels": 2 if flags & 1 else 1,
                  "sampleCount": samples, "seekSamples": seek}
        return char_id, Sound("sound/mp3", data[2:], params)
    return char_id, Sound("application/octet-stream", data)


def read_assets(data: bytes) -> SwfAssets:
    raw = decompress(data)
    version = raw[3]
    nbits = raw[8] >> 3
    rate_pos = 8 + (5 + nbits * 4 + 7) // 8
    frame_rate = raw[rate_pos + 1] + raw[rate_pos] / 256.0

    symbols: list[tuple[int, str]] = []
    assets = SwfAssets(version=version, frame_rate=frame_rate, document_class="", symbols=symbols)
    jpeg_tables = None

    for code, body in iter_tags(raw):
        assets.tag_counts[code] += 1
        try:
            if code == 76:
                (count,) = struct.unpack_from("<H", body, 0)
                pos = 2
                for _ in range(count):
                    (cid,) = struct.unpack_from("<H", body, pos)
                    name, pos = _cstring(body, pos + 2)
                    symbols.append((cid, name))
            elif code == 56:  # ExportAssets (AS2 style naming)
                (count,) = struct.unpack_from("<H", body, 0)
                pos = 2
                for _ in range(count):
                    (cid,) = struct.unpack_from("<H", body, pos)
                    name, pos = _cstring(body, pos + 2)
                    symbols.append((cid, name))
            elif code == 87:
                (cid,) = struct.unpack_from("<H", body, 0)
                assets.binaries[cid] = bytes(body[6:])
            elif code in (20, 36):
                cid, bmp, short = _decode_lossless(body, alpha=(code == 36))
                assets.bitmaps[cid] = bmp
                if short:
                    assets.warnings.append("bitmap %d is truncated; missing pixels left transparent" % cid)
            elif code == 8:
                jpeg_tables = body
            elif code in (6, 21, 35, 90):
                cid, bmp = _decode_jpeg_family(code, body, jpeg_tables)
                assets.bitmaps[cid] = bmp
            elif code == 14:
                decoded = _decode_sound(body)
                if decoded:
                    assets.sounds[decoded[0]] = decoded[1]
            elif code in (72, 82):
                assets.abc_bytes += len(body)
        except Exception as exc:  # keep going; one bad tag should not lose the library
            assets.warnings.append("%s tag failed to decode: %s" % (TAG_NAMES.get(code, code), exc))

    for cid, name in symbols:
        if cid == 0:
            assets.document_class = name
            break
    return assets
