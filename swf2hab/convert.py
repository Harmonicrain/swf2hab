"""SWF asset library -> .hab bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from . import __version__, hab, images, mapping, packer
from .swf import SwfAssets, read_assets

PROFILES = ("full", "sulake")
LAYOUTS = ("auto", "atlas", "library")


@dataclass
class Result:
    data: bytes | None
    kind: str
    document_class: str
    layout: str = "atlas"
    images: int = 0
    frames: int = 0
    atlas: tuple[int, int] = (0, 0)
    warnings: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _strip_prefix(name: str, doc: str) -> str:
    prefix = doc + "_"
    return name[len(prefix):] if doc and name.startswith(prefix) else name


def _sniff_mime(data: bytes) -> str:
    head = data[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"<"):
        return "text/xml"
    return "application/octet-stream"


def _image_sources(assets: SwfAssets, keep_small: bool) -> dict[str, str]:
    """Symbol names that reuse another symbol's bitmap -> the name that owns it (prefixes stripped)."""
    doc = assets.document_class
    canonical = _bitmap_owners(assets, keep_small)
    sources = {}
    for cid, name in assets.symbols:
        owner = canonical.get(cid)
        if owner is None or owner == name or _skipped(name, keep_small):
            continue
        sources[_strip_prefix(name, doc)] = _strip_prefix(owner, doc)
    return sources


def _skipped(name: str, keep_small: bool) -> bool:
    return not keep_small and (name.startswith("sh_") or "_32_" in name)


def _bitmap_owners(assets: SwfAssets, keep_small: bool) -> dict[int, str]:
    """The symbol that owns each bitmap: the first exported full-size name, else (full profile only)
    the first small-zoom name. Matches Habbo's choice, and keeps shared art in the main atlas."""
    owners: dict[int, str] = {}
    for cid, name in assets.symbols:
        if cid in assets.bitmaps and cid not in owners and not _skipped(name, False):
            owners[cid] = name
    if keep_small:
        for cid, name in assets.symbols:
            if cid in assets.bitmaps and cid not in owners:
                owners[cid] = name
    return owners


def _snake(doc: str) -> str:
    """'TileCursor' -> '_tile_cursor' (the reference converter's fallback naming)."""
    return re.sub(r"(?:^|\.?)([A-Z])", lambda m: "_" + m.group(1).lower(), doc)


def _classify(assets: SwfAssets) -> tuple[str, dict[str, bytes]]:
    doc = assets.document_class
    xml = {}
    for key, twice in (("index", False), ("manifest", False), ("assets", True), ("logic", True),
                       ("visualization", True), ("animation", False)):
        candidates = [(doc + "_" + doc + "_" + key), (doc + _snake(doc) + "_" + key)] if twice else [doc + "_" + key]
        for name in candidates:
            blob = assets.binary_by_name(name)
            if blob is not None:
                xml[key] = blob
                break
    if "index" in xml:
        kind = "furniture"
    elif "animation" in xml:
        kind = "effect"
    elif "manifest" in xml:
        kind = "library"
    else:
        kind = "generic"
    return kind, xml


def build_json(assets: SwfAssets, xml: dict[str, bytes], keep_small: bool, warnings: list[str]) -> dict:
    m = mapping.Mapper(assets.document_class, _image_sources(assets, keep_small), keep_small)
    out: dict = {}
    for key, fn in (("index", m.index), ("manifest", m.manifest), ("assets", m.assets), ("animation", m.animation),
                    ("logic", m.logic), ("visualization", m.visualization)):
        if key not in xml:
            continue
        root = mapping.parse_xml(xml[key])
        if root is None:
            warnings.append("%s XML could not be parsed" % key)
            continue
        if key == "assets":
            fn(root, out)
            # palette colours come from the raw palette binaries
            for pid, pal in list(out.get("palettes", {}).items()):
                blob = assets.binary_by_name(assets.document_class + "_" + str(pal.get("source")))
                if blob is None:
                    del out["palettes"][pid]
                    continue
                pal["rgb"] = [[blob[i], blob[i + 1], blob[i + 2]] for i in range(0, len(blob) - 2, 3)]
        else:
            fn(root, out)
    return out


def _library_bundle(assets: SwfAssets, xml: dict[str, bytes], raw_extras: bool, warnings: list[str]) -> hab.Bundle:
    """Component-style bundle: manifest in the index, one entry per symbol (Habbo's client libraries)."""
    doc = assets.document_class
    bundle = hab.Bundle(name=doc)
    declared = {}
    if "manifest" in xml:
        text = xml["manifest"].decode("utf-8-sig", "replace")
        bundle.metadata["manifest"] = text
        root = mapping.parse_xml(xml["manifest"])
        if root is not None:
            for el in root.iter("asset"):
                if el.get("name") and el.get("mimeType"):
                    declared[el.get("name")] = el.get("mimeType")
    owner: dict[int, str] = {}
    aliases = []
    used = set()
    for cid, name in assets.symbols:
        if cid == 0:
            continue
        short = _strip_prefix(name, doc)
        if short in used or short == "manifest":
            continue
        if cid in assets.bitmaps:
            bmp = assets.bitmaps[cid]
            if cid in owner:
                aliases.append({"name": short, "ref": owner[cid], "mimeType": "image/png"})
                used.add(short)
                continue
            owner[cid] = short
            if bmp.rgba is None:
                bundle.add(short, bmp.encoded or b"", bmp.encoded_mime or "application/octet-stream")
                warnings.append("bitmap %s kept as %s (install Pillow to convert it)" % (short, bmp.encoded_mime))
            else:
                bundle.add(short, images.encode_png(bmp.rgba, bmp.width, bmp.height), "image/png")
        elif cid in assets.binaries:
            blob = assets.binaries[cid]
            bundle.add(short, blob, declared.get(short) or _sniff_mime(blob))
        elif cid in assets.sounds:
            snd = assets.sounds[cid]
            bundle.add(short, snd.data, snd.mime, snd.params)
        else:
            continue
        used.add(short)
    if aliases:
        bundle.metadata["aliases"] = aliases
    if raw_extras:
        bundle.metadata["generator"] = "swf2hab/" + __version__
    return bundle


def _auto_layout(kind: str, xml: dict[str, bytes]) -> str:
    if kind == "generic":
        return "library"
    if "index" in xml:
        root = mapping.parse_xml(xml["index"])
        # Room content (floors, walls, landscapes) is loaded as a plain asset library.
        if root is not None and root.get("visualization") == "room":
            return "library"
    return "atlas"


def convert(data: bytes, profile: str = "full", source_name: str = "", padding: int = 0,
            layout: str = "auto", max_atlas: int = 8192) -> Result:
    if profile not in PROFILES:
        raise ValueError("unknown profile %r" % profile)
    if layout not in LAYOUTS:
        raise ValueError("unknown layout %r" % layout)
    keep_small = profile == "full"
    raw_extras = profile == "full"

    assets = read_assets(data)
    doc = assets.document_class or os.path.splitext(os.path.basename(source_name))[0] or "library"
    assets.document_class = doc
    warnings = list(assets.warnings)
    kind, xml = _classify(assets)

    if not assets.bitmaps and not assets.binaries and not assets.sounds:
        return Result(None, kind, doc, skipped_reason="no exportable assets (code-only SWF?)")
    if assets.vector_tag_count:
        warnings.append("%d vector/timeline/text tags are not carried into the bundle" % assets.vector_tag_count)

    if layout == "auto":
        layout = _auto_layout(kind, xml)
    if layout == "library":
        if kind == "generic":
            warnings.append("no Habbo manifest/index/animation XML: bundled raw symbols only")
        bundle = _library_bundle(assets, xml, raw_extras, warnings)
        out = hab.write(bundle)
        hab.read(out)
        return Result(out, kind, doc, layout="library", images=len(assets.bitmaps), warnings=warnings)

    payload = build_json(assets, xml, keep_small, warnings)

    bundle = hab.Bundle(name=doc)

    # ---------------------------------------------------------------- images -> one atlas
    primary_of = _bitmap_owners(assets, keep_small)
    owners: dict[int, list[str]] = {cid: [name] for cid, name in primary_of.items()}
    exported = set()
    for cid, name in assets.symbols:
        if cid in assets.bitmaps:
            exported.add(cid)
            if cid in owners and name not in owners[cid] and not _skipped(name, keep_small):
                owners[cid].append(name)

    unnamed = [cid for cid in assets.bitmaps if cid not in exported]
    if unnamed:
        warnings.append("%d bitmaps are not exported by any symbol and were dropped" % len(unnamed))

    pieces = []        # (frame names, trimmed rgba, trim box, source size)
    for cid, names in owners.items():
        bmp = assets.bitmaps[cid]
        primary = names[0]
        if bmp.rgba is None:
            # Could not decode (JPEG without Pillow): ship the encoded image as its own entry.
            for n in names:
                bundle.add(_strip_prefix(n, doc), bmp.encoded or b"", bmp.encoded_mime or "application/octet-stream")
            warnings.append("bitmap %s kept as %s (install Pillow to pack it)" % (primary, bmp.encoded_mime))
            continue
        frame_names = names if raw_extras else [primary]
        box = images.trim_box(bmp.rgba, bmp.width, bmp.height)
        if box is None:
            box = (0, 0, 1, 1)  # fully transparent: keep a 1x1 clear frame
            trimmed = b"\0\0\0\0"
        else:
            trimmed = images.crop(bmp.rgba, bmp.width, *box)
        pieces.append((frame_names, trimmed, box, (bmp.width, bmp.height)))

    frames_json = {}
    atlas_size = (0, 0)
    if pieces:
        # Small-zoom art (32px furni, sh_ figure parts) goes in its own atlas so a room at normal
        # zoom never decodes it. Within a group, identical pixels share one rectangle.
        groups: dict[str, list[int]] = {"main": [], "small": []}
        for k, (names, _, _, _) in enumerate(pieces):
            shorts = [_strip_prefix(n, doc) for n in names]
            small = all("_32_" in n or n.startswith("sh_") for n in shorts)
            groups["small" if small else "main"].append(k)
        if not groups["main"]:
            groups = {"main": groups["small"], "small": []}
        image_meta = []
        pngs = []
        piece_place: dict[int, tuple[str, int, int]] = {}   # piece -> (image name, x, y)
        for group, members in groups.items():
            if not members:
                continue
            unique: dict[bytes, int] = {}
            uniq_sizes, uniq_data, slot_of = [], [], {}
            for k in members:
                _, trimmed, box, _ = pieces[k]
                key = hashlib.blake2b(trimmed + box[2].to_bytes(4, "little") + box[3].to_bytes(4, "little"),
                                      digest_size=16).digest()
                if key not in unique:
                    unique[key] = len(uniq_sizes)
                    uniq_sizes.append((box[2], box[3]))
                    uniq_data.append(trimmed)
                slot_of[k] = unique[key]
            bins = packer.pack_multi(uniq_sizes, padding=padding, max_side=max_atlas)
            stem = doc if group == "main" else doc + "-32"
            where = {}
            for b, (aw, ah, pos) in enumerate(bins):
                image = stem + (".png" if b == 0 else "-%d.png" % b)
                atlas = bytearray(aw * ah * 4)
                for i, (x, y) in pos.items():
                    w, h = uniq_sizes[i]
                    images.blit(atlas, aw, uniq_data[i], w, h, x, y)
                    where[i] = (image, x, y)
                pngs.append((image, images.encode_png(bytes(atlas), aw, ah)))
                image_meta.append({"image": image, "size": {"w": aw, "h": ah}})
            for k in members:
                piece_place[k] = where[slot_of[k]]
        main_image = image_meta[0]["image"]
        for k, (names, _, box, (sw, sh)) in enumerate(pieces):
            image, x, y = piece_place[k]
            fx, fy, fw, fh = box
            trimmed_flag = (fx, fy, fw, fh) != (0, 0, sw, sh)
            frame = {"frame": {"x": x, "y": y, "w": fw, "h": fh}, "rotated": False, "trimmed": trimmed_flag,
                     "spriteSourceSize": {"x": fx, "y": fy, "w": fw, "h": fh}, "sourceSize": {"w": sw, "h": sh},
                     "pivot": {"x": 0.5, "y": 0.5}}
            if image != main_image:
                frame["image"] = image   # only present on frames that live outside the main atlas
            for n in names:
                frames_json[n] = frame
        first = image_meta[0]["size"]
        atlas_size = (first["w"], first["h"])
        payload["documentClass"] = doc
        meta = {"image": main_image, "format": "RGBA8888", "size": dict(first), "scale": 1}
        if len(image_meta) > 1:
            meta["images"] = image_meta
        if sum(1 for m in image_meta if not m["image"].startswith(doc + "-32")) > 1:
            warnings.append("split into several atlases to stay under %d px" % max_atlas)
        payload["spritesheet"] = {"frames": frames_json, "meta": meta}
        for image, png in reversed(pngs):
            bundle.entries.insert(0, hab.Entry(image, png, "image/png"))
    else:
        payload["documentClass"] = doc

    json_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    bundle.entries.insert(0, hab.Entry(doc + ".json", json_bytes, "application/json"))

    # ---------------------------------------------------------------- raw symbols
    palette_sources = {str(p.get("source")) for p in payload.get("palettes", {}).values()}
    used = {e.name for e in bundle.entries}
    for cid, name in assets.symbols:
        short = _strip_prefix(name, doc)
        if cid in assets.binaries:
            if not raw_extras and short not in palette_sources:
                continue
            if short in used:
                continue
            blob = assets.binaries[cid]
            bundle.add(short, blob, _sniff_mime(blob))
            used.add(short)
        elif cid in assets.sounds and raw_extras and short not in used:
            snd = assets.sounds[cid]
            bundle.add(short, snd.data, snd.mime, snd.params)
            used.add(short)

    if raw_extras:
        bundle.metadata["generator"] = "swf2hab/" + __version__
    out = hab.write(bundle)
    hab.read(out)  # validate exactly as the client would
    return Result(out, kind, doc, layout="atlas", images=len(pieces), frames=len(frames_json), atlas=atlas_size,
                  warnings=warnings)
