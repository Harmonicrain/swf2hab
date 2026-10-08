"""Write Habbo-style asset-library SWFs.

Tag layout follows Habbo's own Flex-built libraries: FileAttributes, ScriptLimits,
SetBackgroundColor, FrameLabel, then every DefineBinaryData / DefineBitsLossless2 / DefineSound,
DoABC2 with the library classes (see abc.py), SymbolClass, ShowFrame, End. Bitmaps are stored the
way Habbo stores them (DefineBitsLossless2, 32-bit premultiplied ARGB) so the pixels read back
exactly as they went in.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from . import abc, images


@dataclass
class Symbol:
    name: str                       # asset name (the property on the document class)
    kind: str                       # "bitmap", "binary" or "sound"
    data: bytes = b""               # binary bytes / sound frames; straight RGBA for bitmaps
    width: int = 0
    height: int = 0
    params: dict | None = None      # sound header (format, rate, bits, channels, sampleCount, seekSamples)
    aliases: list[str] = field(default_factory=list)   # further asset names bound to the same character
    classes: dict[str, str] = field(default_factory=dict)   # asset name -> class name, when not '<library>_<asset>'


def _tag(code: int, body: bytes) -> bytes:
    if len(body) < 0x3F and code not in (20, 21, 35, 36, 87):
        return struct.pack("<H", (code << 6) | len(body)) + body
    # bitmap and binary tags always use the long form, as Flash authoring tools write them
    return struct.pack("<HI", (code << 6) | 0x3F, len(body)) + body


def _rect(width_px: int, height_px: int) -> bytes:
    values = [0, width_px * 20, 0, height_px * 20]
    nbits = max(v.bit_length() for v in values) + 1
    bits = format(nbits, "05b") + "".join(format(v, "0%db" % nbits) for v in values)
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def _sound_body(char_id: int, sym: Symbol) -> bytes:
    p = sym.params or {}
    rates = {5512: 0, 5500: 0, 11025: 1, 22050: 2, 44100: 3}
    flags = (2 << 4) | (rates.get(int(p.get("rate", 44100)), 3) << 2)
    flags |= (2 if int(p.get("bits", 16)) == 16 else 0) | (1 if int(p.get("channels", 1)) == 2 else 0)
    return (struct.pack("<HBI", char_id, flags, int(p.get("sampleCount", 0)))
            + struct.pack("<h", int(p.get("seekSamples", 0))) + sym.data)


def class_name(document_class: str, asset: str) -> str:
    """Class bound to an asset, named the way Flex names [Embed] classes: '<library>_<asset>'."""
    return document_class + "_" + asset


def write_library(document_class: str, symbols: list[Symbol], version: int = 10, compress: bool = True,
                  frame_size: tuple[int, int] = (500, 375), frame_rate: int = 24) -> bytes:
    defines = bytearray()
    bindings: list[tuple[int, str]] = []
    abc_assets: list[tuple[str, str, str]] = []
    used: set[str] = set()
    for char_id, sym in enumerate(symbols, 1):
        if sym.kind == "bitmap":
            argb = images.rgba_to_argb_premultiplied(sym.data, sym.width, sym.height)
            body = struct.pack("<HBHH", char_id, 5, sym.width, sym.height) + zlib.compress(argb, 9)
            defines += _tag(36, body)
        elif sym.kind == "binary":
            defines += _tag(87, struct.pack("<HI", char_id, 0) + sym.data)
        elif sym.kind == "sound":
            defines += _tag(14, _sound_body(char_id, sym))
        else:
            raise ValueError("unknown symbol kind %r" % sym.kind)
        for name in [sym.name] + sym.aliases:
            if name in used:
                raise ValueError("asset %r appears twice" % name)
            used.add(name)
            cls = sym.classes.get(name) or class_name(document_class, name)
            bindings.append((char_id, cls))
            abc_assets.append((name, cls, sym.kind))
    if len(symbols) >= 0xFFFF:
        raise ValueError("too many symbols for one SWF")

    code = abc.library_abc(document_class, abc_assets)
    tags = bytearray()
    tags += _tag(69, struct.pack("<I", 0x08 | 0x01))                  # AS3, network sandbox
    tags += _tag(65, struct.pack("<HH", 1000, 60))                    # ScriptLimits
    tags += _tag(9, b"\xff\xff\xff")                                  # SetBackgroundColor
    tags += _tag(43, document_class.encode("utf-8") + b"\0")          # FrameLabel
    tags += defines
    tags += _tag(82, struct.pack("<I", 1) + b"frame1\0" + code)       # DoABC2, lazy initialise
    symbol_class = bytearray(struct.pack("<H", len(bindings) + 1))
    for char_id, cls in bindings:
        symbol_class += struct.pack("<H", char_id) + cls.encode("utf-8") + b"\0"
    symbol_class += struct.pack("<H", 0) + document_class.encode("utf-8") + b"\0"
    tags += _tag(76, bytes(symbol_class))
    tags += _tag(1, b"") + _tag(0, b"")

    body = _rect(*frame_size) + struct.pack("<HH", frame_rate << 8, 1) + bytes(tags)
    length = 8 + len(body)
    if compress:
        return b"CWS" + bytes([version]) + struct.pack("<I", length) + zlib.compress(body, 9)
    return b"FWS" + bytes([version]) + struct.pack("<I", length) + body
