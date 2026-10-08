"""Format-independent operations behind the command line: detect, convert, describe, verify, extract.

Every function takes the file's bytes, works out what the file is from its content (not its
extension), and handles .swf, .hab and .nitro alike.
"""

from __future__ import annotations

import json
import os
import re
import struct
import time

from . import abc, convert, export, hab, images, nitro
from .swf import TAG_NAMES, decompress, iter_tags, read_assets

FORMATS = ("hab", "swf", "nitro")
SUFFIXES = tuple("." + f for f in FORMATS)
DESCRIPTIONS = {"swf": "Flash asset library", "hab": "Habbo HTML5 bundle", "nitro": "Nitro bundle"}


def detect(data: bytes, name: str = "") -> str | None:
    """'hab', 'swf' or 'nitro' from the content; the extension only breaks a tie."""
    if data[:4] == hab.MAGIC:
        return "hab"
    if data[:3] in (b"FWS", b"CWS", b"ZWS") and len(data) >= 8:
        return "swf"
    if len(data) >= 8:
        count, length = struct.unpack_from(">HH", data, 0)
        head = data[4:4 + length]
        if 0 < count < 0x10000 and 0 < length < 512 and len(head) == length \
                and all(32 <= b < 127 for b in head):
            return "nitro"
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return ext if ext in FORMATS else None


# ---------------------------------------------------------------------------------------- convert

def convert_file(src: str, dest: str, target: str, options: dict, force: bool = False) -> dict:
    """Convert one file; returns a record for the report (also used in worker processes)."""
    rec = {"source": src, "output": dest, "target": target, "bytes_in": os.path.getsize(src)}
    try:
        if not force and os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src):
            rec.update(status="up-to-date", bytes_out=os.path.getsize(dest))
            return rec
        t = time.perf_counter()
        with open(src, "rb") as fh:
            data = fh.read()
        kind = detect(data, src)
        rec["format"] = kind
        if kind is None:
            rec.update(status="skipped", reason="not a .swf, .hab or .nitro file")
            return rec
        if kind == target:
            rec.update(status="skipped", reason="already a .%s" % target)
            return rec
        if target == "hab":
            if kind == "nitro":
                result = nitro.convert(data, source_name=src)
            else:
                result = convert.convert(data, profile=options.get("profile") or "full", source_name=src,
                                         padding=options.get("padding") or 0, layout=options.get("layout") or "auto",
                                         max_atlas=options.get("max_atlas") or 8192)
            out, warnings, skipped = result.data, result.warnings, result.skipped_reason
            rec.update(kind=result.kind, name=result.document_class)
        else:
            result = export.export(data, target, source_name=src, small=options.get("small") or "auto")
            out, warnings, skipped = result.data, result.warnings, result.skipped_reason
            rec.update(kind=result.kind, name=result.name)
        rec["warnings"] = warnings
        if out is None:
            rec.update(status="skipped", reason=skipped)
        else:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(out)
            os.replace(tmp, dest)
            rec.update(status="ok", bytes_out=len(out))
        rec["seconds"] = round(time.perf_counter() - t, 3)
    except Exception as exc:
        rec.update(status="failed", error="%s: %s" % (type(exc).__name__, exc))
    return rec


# ---------------------------------------------------------------------------------------- describe

def _doc_summary(doc: dict) -> dict:
    sheet = doc.get("spritesheet") or {}
    meta = sheet.get("meta") or {}
    atlases = meta.get("images") or ([{"image": meta.get("image"), "size": meta.get("size")}] if meta.get("image")
                                      else [])
    out = {"kind": export._kind(doc), "name": doc.get("name") or doc.get("documentClass"),
           "logic": doc.get("logicType"), "visualization": doc.get("visualizationType"),
           "assets": len(doc.get("assets") or {}), "frames": len(sheet.get("frames") or {}),
           "sizes": sorted({v.get("size") for v in doc.get("visualizations") or [] if v.get("size") is not None}),
           "atlases": [{"image": a.get("image"), "w": (a.get("size") or {}).get("w"),
                        "h": (a.get("size") or {}).get("h")} for a in atlases]}
    for key in ("animations", "palettes", "aliases"):
        if doc.get(key):
            out[key] = len(doc[key])
    return out


def _abc_classes(raw: bytes) -> dict[str, abc.ClassInfo]:
    classes = {}
    for code, body in iter_tags(raw):
        if code in (72, 82):
            block = body if code == 72 else body[body.index(b"\0", 4) + 1:]
            for c in abc.read_abc(block).classes:
                classes[c.name] = c
    return classes


def describe(data: bytes, name: str = "") -> dict:
    kind = detect(data, name)
    info: dict = {"file": name, "format": kind, "bytes": len(data)}
    if kind == "hab":
        b = hab.read(data)
        json_entry, doc = export._document(b)
        info.update(name=b.name, layout="atlas" if doc else "library",
                    profile=("full" if export._full_profile(b, json_entry) else "compact") if doc else None,
                    entries=[{"name": e.name, "mimeType": e.mime, "bytes": len(e.data)} for e in b.entries],
                    metadata=sorted(b.metadata))
        if doc:
            info["document"] = _doc_summary(doc)
        elif b.metadata.get("aliases"):
            info["aliases"] = len(b.metadata["aliases"])
    elif kind == "nitro":
        files = nitro.read(data)
        info["files"] = [{"name": n, "bytes": len(v)} for n, v in files.items()]
        docs = [v for n, v in files.items() if n.lower().endswith(".json")]
        if docs:
            doc = json.loads(docs[0].decode("utf-8"))
            info["name"] = doc.get("name") or os.path.splitext(os.path.basename(name))[0]
            info["document"] = _doc_summary(doc)
    elif kind == "swf":
        raw = decompress(data)
        a = read_assets(data)
        classify, xml = convert._classify(a)
        classes = {}
        try:
            classes = _abc_classes(raw)
        except Exception as exc:
            a.warnings.append("ActionScript could not be read: %s" % exc)
        doc_class = classes.get(a.document_class)
        info.update(name=a.document_class, version=a.version,
                    compression={b"FWS": "none", b"CWS": "zlib", b"ZWS": "lzma"}[data[:3]],
                    kind=classify, xml=sorted(xml), symbols=len(a.symbols), bitmaps=len(a.bitmaps),
                    binaries=len(a.binaries), sounds=len(a.sounds), classes=len(classes),
                    extends=doc_class.super_name if doc_class else None, abc_bytes=a.abc_bytes,
                    tags={TAG_NAMES.get(k, str(k)): v for k, v in sorted(a.tag_counts.items())},
                    vector_tags=a.vector_tag_count, warnings=a.warnings)
    else:
        raise ValueError("not a .swf, .hab or .nitro file")
    return info


# ---------------------------------------------------------------------------------------- verify

def _check_frames(doc: dict, atlas_sizes: dict[str, tuple[int, int]], problems: list) -> None:
    sheet = doc.get("spritesheet") or {}
    main = (sheet.get("meta") or {}).get("image")
    for frame_name, f in (sheet.get("frames") or {}).items():
        image = f.get("image") or main
        if image not in atlas_sizes:
            problems.append(("error", "frame %s is on missing atlas %s" % (frame_name, image)))
            continue
        aw, ah = atlas_sizes[image]
        fr = f.get("frame") or {}
        w, h = (fr.get("h"), fr.get("w")) if f.get("rotated") else (fr.get("w"), fr.get("h"))
        if None in (fr.get("x"), fr.get("y"), w, h) or fr["x"] < 0 or fr["y"] < 0 \
                or fr["x"] + w > aw or fr["y"] + h > ah:
            problems.append(("error", "frame %s lies outside its %dx%d atlas" % (frame_name, aw, ah)))


def verify(data: bytes, name: str = "") -> list[tuple[str, str]]:
    """Problems as (severity, message), severity 'error' or 'warning'; empty when the file is sound.
    Each format is checked the way its client reads it."""
    problems: list[tuple[str, str]] = []
    kind = detect(data, name)
    if kind == "hab":
        b = hab.read(data)                       # the HTML5 client's own checks
        json_entry, doc = export._document(b)
        if doc is not None:
            sizes = {}
            for e in b.entries:
                if e.mime == "image/png":
                    w, h, _ = images.decode_png_rgba(e.data)
                    sizes[e.name] = (w, h)
            _check_frames(doc, sizes, problems)
        elif not b.metadata.get("manifest"):
            problems.append(("warning", "library-layout bundle without a manifest"))
    elif kind == "nitro":
        files = nitro.read(data)
        docs = [n for n in files if n.lower().endswith(".json")]
        if len(docs) != 1:
            problems.append(("error", "expected one JSON document, found %d" % len(docs)))
            return problems
        doc = json.loads(files[docs[0]].decode("utf-8"))
        sizes = {n: images.decode_png_rgba(v)[:2] for n, v in files.items() if v[:8] == b"\x89PNG\r\n\x1a\n"}
        _check_frames(doc, sizes, problems)
    elif kind == "swf":
        raw = decompress(data)
        a = read_assets(data)
        problems += [("warning", w) for w in a.warnings]
        if not a.document_class:
            problems.append(("error", "no document class (SymbolClass id 0)"))
            return problems
        classes = _abc_classes(raw)
        doc_class = classes.get(a.document_class)
        if doc_class is None:
            problems.append(("error", "document class %s is not defined in the ActionScript" % a.document_class))
            return problems
        missing = [n for cid, n in a.symbols if cid and n not in classes]
        if missing:
            problems.append(("error", "%d symbols bound to undefined classes, e.g. %s" % (len(missing), missing[0])))
        statics = {n for n, _ in doc_class.static_traits}
        manifest = a.binary_by_name(a.document_class + "_manifest")
        if "manifest" not in statics:
            problems.append(("warning", "document class has no static 'manifest' (Habbo's loader needs one)"))
        elif manifest is not None:
            from .mapping import parse_xml
            root = parse_xml(manifest)
            if root is None:
                problems.append(("error", "manifest XML does not parse"))
            else:
                listed = [el.get("name") for el in root.iter("asset") if el.get("name")]
                absent = [n for n in listed if n not in statics]
                if absent:
                    problems.append(("warning", "%d manifest assets have no class on the document class, e.g. %s"
                                     % (len(absent), absent[0])))
    else:
        problems.append(("error", "not a .swf, .hab or .nitro file"))
    return problems


# ---------------------------------------------------------------------------------------- extract

_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _safe(name: str) -> str:
    name = _UNSAFE.sub("_", name).strip(". ") or "_"
    return "/".join(part.strip(". ") or "_" for part in name.split("/"))


def _write(root: str, rel: str, data: bytes, written: list[str]) -> None:
    path = os.path.join(root, *_safe(rel).split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    written.append(path)


def _frames_out(doc: dict, atlases: dict[str, bytes], root: str, written: list[str]) -> None:
    sheet = doc.get("spritesheet") or {}
    main = (sheet.get("meta") or {}).get("image")
    decoded = {}
    for frame_name, f in (sheet.get("frames") or {}).items():
        image = f.get("image") or main
        if image not in atlases:
            continue
        if image not in decoded:
            decoded[image] = images.decode_png_rgba(atlases[image])
        aw, _, atlas = decoded[image]
        w, h, px = images.cut_frame(atlas, aw, f)
        name = frame_name if frame_name.lower().endswith(".png") else frame_name + ".png"
        _write(root, "frames/" + name, images.encode_png(px, w, h), written)


def extract(data: bytes, name: str, out_dir: str, frames: bool = True) -> list[str]:
    """Unpack a file into `out_dir`: every entry or symbol as a file, plus (atlas bundles, unless
    `frames` is False) each spritesheet frame as its own PNG under frames/."""
    kind = detect(data, name)
    written: list[str] = []
    os.makedirs(out_dir, exist_ok=True)
    if kind == "hab":
        b = hab.read(data)
        for e in b.entries:
            _write(out_dir, e.name, e.data, written)
        index = {"name": b.name, **b.metadata,
                 "entries": [{"name": e.name, "mimeType": e.mime, "length": len(e.data),
                              **({"params": e.params} if e.params else {})} for e in b.entries]}
        _write(out_dir, "_index.json", json.dumps(index, indent=1, ensure_ascii=False).encode("utf-8"), written)
        _, doc = export._document(b)
        if doc and frames:
            _frames_out(doc, {e.name: e.data for e in b.entries if e.mime == "image/png"}, out_dir, written)
    elif kind == "nitro":
        files = nitro.read(data)
        for n, v in files.items():
            _write(out_dir, n, v, written)
        docs = [v for n, v in files.items() if n.lower().endswith(".json")]
        if docs and frames:
            _frames_out(json.loads(docs[0].decode("utf-8")), files, out_dir, written)
    elif kind == "swf":
        a = read_assets(data)
        doc = a.document_class
        names: dict[int, list[str]] = {}
        for cid, cls in a.symbols:
            if cid:
                names.setdefault(cid, []).append(cls)
        listing = []
        for cid, classes in names.items():
            short = classes[0][len(doc) + 1:] if doc and classes[0].startswith(doc + "_") else classes[0]
            entry = {"id": cid, "classes": classes}
            if cid in a.bitmaps:
                bmp = a.bitmaps[cid]
                if bmp.rgba is not None:
                    entry["file"] = short + ".png"
                    _write(out_dir, entry["file"], images.encode_png(bmp.rgba, bmp.width, bmp.height), written)
                elif bmp.encoded:
                    entry["file"] = short + {"image/jpeg": ".jpg", "image/gif": ".gif"}.get(bmp.encoded_mime, ".png")
                    _write(out_dir, entry["file"], bmp.encoded, written)
            elif cid in a.binaries:
                blob = a.binaries[cid]
                entry["file"] = short + (".xml" if blob[:64].lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<") else ".bin")
                _write(out_dir, entry["file"], blob, written)
            elif cid in a.sounds:
                snd = a.sounds[cid]
                entry["file"] = short + (".mp3" if snd.mime == "sound/mp3" else ".bin")
                entry["params"] = snd.params
                _write(out_dir, entry["file"], snd.data, written)
            listing.append(entry)
        _write(out_dir, "_symbols.json", json.dumps({"documentClass": doc, "version": a.version,
                                                      "symbols": listing}, indent=1).encode("utf-8"), written)
    else:
        raise ValueError("not a .swf, .hab or .nitro file")
    return written
