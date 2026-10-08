"""Nitro bundle (.nitro) -> .hab bundle (export.py goes the other way, using `write`).

A .nitro file (written by nitro-converter, read by nitro-renderer's NitroBundle) is

    u16 file count, then per file: u16 name length, name (UTF-8), u32 data length, data

all big-endian, each file compressed with zlib or, in some bundles, gzip. A converted asset holds
`<name>.json` and `<name>.png`. The JSON already uses the layout Habbo's own .hab files carry --
nitro-converter's mapping is where that layout comes from (see NOTICE.md) -- so conversion is a
repackaging, with two adjustments that make the JSON match Habbo's:

- Habbo adds `documentClass`, the asset library's class name (the bundle name), before
  `spritesheet`;
- for asset libraries without room-object logic (effects and figure parts), Habbo's JSON has no
  `name`; the name becomes `documentClass` instead.

Checked against swf2hab's own `--profile sulake` output from the matching SWFs: the JSON is
identical apart from atlas positions, and every frame's pixels match (`swf2hab compare`).

A .nitro holds only what nitro-converter kept, which is what the `sulake` profile keeps: no 32px
art, no `sh_` figure parts and no raw XML/binary symbols. Converting from the original .swf with
`--profile full` keeps those; converting from .nitro cannot recover them.
"""

from __future__ import annotations

import json
import os
import struct
import zlib

from . import hab
from .convert import Result


class NitroError(Exception):
    pass


def read(data: bytes) -> dict[str, bytes]:
    """All files in a .nitro bundle, decompressed, in bundle order."""
    files: dict[str, bytes] = {}
    if len(data) < 2:
        raise NitroError("not a .nitro bundle: too short")
    count, = struct.unpack_from(">H", data, 0)
    pos = 2
    for _ in range(count):
        if pos + 2 > len(data):
            raise NitroError("truncated .nitro bundle")
        name_length, = struct.unpack_from(">H", data, pos)
        pos += 2
        name = data[pos:pos + name_length].decode("utf-8")
        pos += name_length
        if pos + 4 > len(data):
            raise NitroError("truncated .nitro bundle")
        size, = struct.unpack_from(">I", data, pos)
        pos += 4
        if pos + size > len(data):
            raise NitroError("truncated entry %r" % name)
        files[name] = _inflate(data[pos:pos + size], name)
        pos += size
    if pos != len(data):
        raise NitroError("%d unexpected trailing bytes" % (len(data) - pos))
    return files


def write(files: list[tuple[str, bytes]], level: int = 9) -> bytes:
    """A .nitro bundle of (name, data) files, each zlib-compressed as nitro-converter writes them."""
    if len(files) > 0xFFFF:
        raise NitroError("too many files for a .nitro bundle")
    out = bytearray(struct.pack(">H", len(files)))
    for name, data in files:
        raw = name.encode("utf-8")
        packed = zlib.compress(data, level)
        out += struct.pack(">H", len(raw)) + raw + struct.pack(">I", len(packed)) + packed
    return bytes(out)


def _inflate(packed: bytes, name: str) -> bytes:
    try:
        return zlib.decompress(packed, 47)   # zlib or gzip, as pako.inflate accepts
    except zlib.error as exc:
        raise NitroError("entry %r is neither zlib nor gzip: %s" % (name, exc)) from None


def _mime(name: str, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if name.lower().endswith(".json"):
        return "application/json"
    head = data[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"<"):
        return "text/xml"
    return "application/octet-stream"


def _habbo_json(doc: dict, document_class: str) -> tuple[dict, str]:
    """The Habbo form of a nitro-converter JSON document, and the asset kind it describes."""
    room_object = "logicType" in doc or "visualizationType" in doc
    if room_object:
        kind = "pet" if doc.get("logicType") == "pet" else "furniture"
    else:
        kind = "effect" if "animations" in doc else "library"
    out: dict = {}
    for key, value in doc.items():
        if key == "name" and not room_object:
            continue
        if key == "spritesheet":
            out["documentClass"] = document_class
        out[key] = value
    out.setdefault("documentClass", document_class)
    return out, kind


def convert(data: bytes, source_name: str = "") -> Result:
    files = read(data)
    stem = os.path.splitext(os.path.basename(source_name))[0] if source_name else ""
    json_names = [n for n in files if n.lower().endswith(".json")]
    if len(json_names) != 1:
        raise NitroError("expected one JSON document, found %d" % len(json_names))
    doc = json.loads(files[json_names[0]].decode("utf-8"))
    if not isinstance(doc, dict):
        raise NitroError("JSON document is not an object")
    document_class = doc.get("name") or json_names[0][:-len(".json")] or stem or "library"
    habbo, kind = _habbo_json(doc, document_class)

    bundle = hab.Bundle(document_class)
    warnings: list[str] = []
    if any(key in doc for key in ("type", "dimensions", "directions")):
        # Older nitro-converter "generic" bundles (tile_cursor, place_holder, ...) use their own
        # schema rather than the one Habbo's .hab files share; they are repackaged unchanged.
        warnings.append("legacy nitro-converter schema (type/dimensions/directions); "
                        "JSON repackaged unchanged, not in Habbo's layout")
    images = 0
    atlas = (0, 0)
    for name, content in files.items():
        if name == json_names[0]:
            bundle.add(document_class + ".json",
                       json.dumps(habbo, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                       "application/json")
            continue
        mime = _mime(name, content)
        if mime == "image/png":
            images += 1
            if content[12:16] == b"IHDR":
                atlas = struct.unpack(">II", content[16:24])
        bundle.add(name, content, mime)
    sheet = habbo.get("spritesheet") or {}
    image = (sheet.get("meta") or {}).get("image")
    if image and bundle.get(image) is None:
        warnings.append("spritesheet image %r is not in the bundle" % image)
    frames = len(sheet.get("frames") or {})
    return Result(data=hab.write(bundle), kind=kind, document_class=document_class, layout="atlas",
                  images=images, frames=frames, atlas=atlas, warnings=warnings)
