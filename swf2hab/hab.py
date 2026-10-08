"""HAB v1 container: read, write and validate.

Layout (little-endian), as read by Habbo's HTML5 client:

    0   char[4]  "HAB\\0"
    4   u16      version = 1
    6   u16      flags   = 1
    8   u32      index stored length   (zlib-compressed JSON)
    12  u32      index original length (JSON bytes)
    16  u32      payload length
    20  ...      zlib(index JSON), then the payload

The index is ``{"format": "hab", "version": 1, "name": ..., [metadata...],
"entries": [{name, mimeType, offset, storedLength, originalLength,
compression: "deflate" | "none", [params]}]}``. An entry is stored deflated
only when that makes it smaller, the same rule the client's own writer uses.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"HAB\0"
VERSION = 1
FLAGS = 1
HEADER = 20
MAX_ENTRIES = 100_000          # client-side safety limits
MAX_INDEX_BYTES = 64 * 1024 * 1024


class HabError(Exception):
    pass


@dataclass
class Entry:
    name: str
    data: bytes
    mime: str
    params: dict | None = None


@dataclass
class Bundle:
    name: str
    entries: list[Entry] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)   # manifest, aliases, ... (top-level index keys)

    def add(self, name: str, data: bytes, mime: str, params: dict | None = None) -> None:
        self.entries.append(Entry(name, bytes(data), mime, params))

    def get(self, name: str) -> Entry | None:
        for e in self.entries:
            if e.name == name:
                return e
        return None


def write(bundle: Bundle, level: int = 9) -> bytes:
    seen = set()
    index_entries = []
    payload = bytearray()
    for e in bundle.entries:
        if not e.name:
            raise HabError("entry names cannot be empty")
        if e.name in seen:
            raise HabError("duplicate entry %r" % e.name)
        seen.add(e.name)
        packed = zlib.compress(e.data, level)
        stored, how = (packed, "deflate") if len(packed) < len(e.data) else (e.data, "none")
        item = {"name": e.name, "mimeType": e.mime, "offset": len(payload), "storedLength": len(stored),
                "originalLength": len(e.data), "compression": how}
        if e.params:
            item["params"] = e.params
        index_entries.append(item)
        payload += stored
    index = {"format": "hab", "version": VERSION, "name": bundle.name}
    index.update(bundle.metadata)
    index["entries"] = index_entries
    raw_index = json.dumps(index, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw_index) > MAX_INDEX_BYTES:
        raise HabError("index exceeds %d bytes" % MAX_INDEX_BYTES)
    packed_index = zlib.compress(raw_index, level)
    header = MAGIC + struct.pack("<HHIII", VERSION, FLAGS, len(packed_index), len(raw_index), len(payload))
    return header + packed_index + bytes(payload)


def read(data: bytes, strict: bool = True) -> Bundle:
    """Parse a .hab file, applying the same checks as the client's reader."""
    if len(data) < HEADER:
        raise HabError("bundle is shorter than its header")
    if data[:4] != MAGIC:
        raise HabError("invalid magic signature")
    version, flags, stored, original, payload_len = struct.unpack_from("<HHIII", data, 4)
    if version != VERSION:
        raise HabError("unsupported version %d" % version)
    if flags != FLAGS:
        raise HabError("unsupported flags 0x%x" % flags)
    if original > MAX_INDEX_BYTES:
        raise HabError("index exceeds safety limit")
    end = HEADER + stored + payload_len
    if end != len(data):
        raise HabError("length mismatch: expected %d, got %d" % (end, len(data)))
    raw_index = zlib.decompress(data[HEADER:HEADER + stored])
    if len(raw_index) != original:
        raise HabError("index length mismatch")
    index = json.loads(raw_index.decode("utf-8"))
    if index.get("format") != "hab" or index.get("version") != VERSION or not isinstance(index.get("entries"), list):
        raise HabError("index has an invalid format or version")
    entries = index["entries"]
    if len(entries) > MAX_ENTRIES:
        raise HabError("too many entries")
    payload = data[HEADER + stored:]
    bundle = Bundle(name=index.get("name", ""),
                    metadata={k: v for k, v in index.items() if k not in ("format", "version", "name", "entries")})
    seen = set()
    for item in entries:
        name = item.get("name")
        if not isinstance(name, str) or not name or not isinstance(item.get("mimeType"), str):
            raise HabError("invalid entry descriptor")
        if name in seen:
            raise HabError("duplicate entry %r" % name)
        seen.add(name)
        off, sl, ol = item.get("offset"), item.get("storedLength"), item.get("originalLength")
        if not all(isinstance(v, int) and v >= 0 for v in (off, sl, ol)):
            raise HabError("entry %r has an invalid offset or length" % name)
        if off + sl > payload_len:
            raise HabError("entry %r exceeds the payload" % name)
        blob = payload[off:off + sl]
        comp = item.get("compression")
        if comp == "deflate":
            blob = zlib.decompress(blob)
        elif comp != "none":
            raise HabError("entry %r uses unsupported compression %r" % (name, comp))
        if strict and len(blob) != ol:
            raise HabError("entry %r length mismatch" % name)
        bundle.entries.append(Entry(name, blob, item["mimeType"], item.get("params")))
    return bundle
