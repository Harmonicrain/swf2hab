""".hab bundle -> Flash asset library (.swf) or Nitro bundle (.nitro).

Both directions start from a .hab, so anything swf2hab reads can be exported: a .nitro or .swf
input is first converted to a .hab in memory.

To .swf
    The SWF has the layout of Habbo's own libraries (see swfwrite.py and abc.py): a document class
    with one static Class property per asset, bound by SymbolClass to DefineBitsLossless2,
    DefineBinaryData and DefineSound tags. The XML documents are the bundle's own raw entries when
    it carries them (`--profile full`), otherwise they are regenerated from the JSON (toxml.py).
    Bitmaps are cut out of the atlas at their original size, trimmed border restored.

    Bundles without 32px art (Habbo's .hab files, the `sulake` profile, .nitro) would draw their
    64px art at double size in a zoomed-out Flash room, because the AS3 client picks the nearest
    size the visualization has. Unless `small="none"`, a 32px visualization is added with art
    scaled to half size and offsets halved. It is an approximation: Habbo's own 32px art is drawn
    by hand.

To .nitro
    A .nitro holds one JSON document and one atlas, in the layout nitro-converter writes. JSON
    differences are undone (no `documentClass`; libraries and effects get their `name` back), and
    32px art is dropped as nitro-converter does. The atlas is passed through untouched when every
    remaining frame is on it, else the frames are repacked into one.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field

from . import convert, hab, images, nitro, packer, swfwrite, toxml

TARGETS = ("swf", "nitro")
SMALL_MODES = ("auto", "none")


class ExportError(Exception):
    pass


@dataclass
class ExportResult:
    data: bytes | None
    kind: str
    name: str
    symbols: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def load(data: bytes, source_name: str = "", profile: str = "full") -> tuple[hab.Bundle, list[str]]:
    """A .hab, .nitro or .swf as a .hab bundle, plus conversion warnings."""
    if data[:4] == hab.MAGIC:
        return hab.read(data), []
    if data[:3] in (b"FWS", b"CWS", b"ZWS"):
        result = convert.convert(data, profile=profile, source_name=source_name)
        if result.data is None:
            raise ExportError(result.skipped_reason or "nothing to convert")
        return hab.read(result.data), list(result.warnings)
    result = nitro.convert(data, source_name=source_name)
    return hab.read(result.data), list(result.warnings)


def _document(bundle: hab.Bundle) -> tuple[hab.Entry | None, dict | None]:
    for e in bundle.entries:
        if e.mime == "application/json":
            return e, json.loads(e.data.decode("utf-8"))
    return None, None


def _kind(doc: dict) -> str:
    if "logicType" in doc or "visualizationType" in doc:
        return "pet" if doc.get("logicType") == "pet" else "furniture"
    return "effect" if "animations" in doc else "library"


def _full_profile(bundle: hab.Bundle, json_entry: hab.Entry | None) -> bool:
    """A `--profile full` bundle: it carries the library's raw manifest beside the JSON."""
    return any(e.name == "manifest" for e in bundle.entries if e is not json_entry)


def _sharing(doc: dict, shorts: dict[str, dict], full_profile: bool):
    """Which frames are further class names of another frame's bitmap, as a function that maps a
    frame to the frame owning its bitmap (itself when it owns one).

    Habbo's SWFs bind several class names to one bitmap. The JSON records that through `source`
    (an asset drawing another's bitmap). A full-profile bundle has every class name of a shared
    bitmap as a frame, so there a name that is no asset at all (a manifest-only symbol such as
    "<name>_empty") on an earlier frame's rectangle shares it too. Elsewhere each frame is its own
    symbol: frames that merely have identical pixels, which packers fold into one rectangle, are
    not shared.
    """
    assets = doc.get("assets") or {}
    shared: dict[str, str] = {}
    first_with_key: dict[tuple, str] = {}
    sources = {a.get("source") for a in assets.values() if a.get("source")}
    for short, frame in shorts.items():
        key = _frame_key(frame)
        src = (assets.get(short) or {}).get("source")
        if src and src != short and src in shorts and _frame_key(shorts[src]) == key:
            shared[short] = src
        elif full_profile and short not in assets and short not in sources and key in first_with_key:
            shared[short] = first_with_key[key]
        first_with_key.setdefault(key, short)

    def root(name: str) -> str:
        seen = set()
        while name in shared and name not in seen:
            seen.add(name)
            name = shared[name]
        return name
    return root


def _strip(name: str, prefix: str) -> str:
    return name[len(prefix):] if name.startswith(prefix) else name


def _small(name: str) -> bool:
    return "_32_" in name or name.startswith("sh_")


# ---------------------------------------------------------------------------------------- frames

class _Atlases:
    """Decodes each atlas image once."""

    def __init__(self, bundle: hab.Bundle):
        self.bundle = bundle
        self.cache: dict[str, tuple[int, int, bytes]] = {}

    def get(self, image: str) -> tuple[int, int, bytes]:
        if image not in self.cache:
            entry = self.bundle.get(image)
            if entry is None:
                raise ExportError("atlas image %r is not in the bundle" % image)
            self.cache[image] = images.decode_png_rgba(entry.data)
        return self.cache[image]


def _cut(atlases: _Atlases, main_image: str, frame: dict) -> tuple[int, int, bytes]:
    """A frame at its source size, trimmed transparent border restored."""
    aw, _, atlas = atlases.get(frame.get("image") or main_image)
    return images.cut_frame(atlas, aw, frame)


def _frame_key(frame: dict) -> tuple:
    fr, sss, src = frame["frame"], frame.get("spriteSourceSize") or {}, frame.get("sourceSize") or {}
    return (frame.get("image"), fr["x"], fr["y"], fr["w"], fr["h"], bool(frame.get("rotated")), sss.get("x"),
            sss.get("y"), src.get("w"), src.get("h"))


# ---------------------------------------------------------------------------------------- 32px art

def _halve(v):
    return math.floor(v / 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _halve_layer(layer: dict) -> None:
    for k in ("x", "y"):
        if k in layer:
            layer[k] = _halve(layer[k])


def _add_small(doc: dict, bitmaps: dict[str, tuple[int, int, bytes]], warnings: list[str]) -> list[str]:
    """Add a 32px visualization, assets and bitmaps derived from the 64px ones. Returns new bitmaps."""
    sizes = {v.get("size") for v in doc.get("visualizations") or []}
    big = next((v for v in doc.get("visualizations") or [] if v.get("size") == 64), None)
    if 32 in sizes or big is None:
        return []
    small_vis = copy.deepcopy(big)
    small_vis["size"] = 32
    for layer in (small_vis.get("layers") or {}).values():
        _halve_layer(layer)
    for direction in (small_vis.get("directions") or {}).values():
        for layer in (direction.get("layers") or {}).values():
            _halve_layer(layer)
    for anim in (small_vis.get("animations") or {}).values():
        for layer in (anim.get("layers") or {}).values():
            for seq in (layer.get("frameSequences") or {}).values():
                for fr in (seq.get("frames") or {}).values():
                    for k in ("x", "y", "randomX", "randomY"):
                        if k in fr:
                            fr[k] = _halve(fr[k])
                    for off in (fr.get("offsets") or {}).values():
                        for k in ("x", "y"):
                            if k in off:
                                off[k] = _halve(off[k])
    visualizations = doc["visualizations"]
    visualizations.insert(visualizations.index(big), small_vis)

    prefix = (doc.get("name") or "") + "_64_"
    small = lambda n: n.replace("_64_", "_32_", 1) if n.startswith(prefix) else None
    assets = doc.get("assets") or {}
    added = {}
    for name, asset in assets.items():
        target = small(name)
        if target is None or target in assets:
            continue
        a = dict(asset)
        for k in ("x", "y"):
            if k in a:
                a[k] = _halve(a[k])
        if a.get("source"):
            a["source"] = small(a["source"]) or a["source"]
        added[target] = a
    # keep the asset list ordered as Habbo's is: 32px entries before 64px ones
    doc["assets"] = {**added, **assets}
    made = []
    for name in list(bitmaps):
        target = small(name)
        if target is None or target in bitmaps:
            continue
        w, h, px = bitmaps[name]
        bitmaps[target] = images.downscale_half(px, w, h)
        made.append(target)
    warnings.append("no 32px art: generated %d bitmaps at half size (Habbo's own 32px art is drawn by hand)"
                    % len(made))
    return made


# ---------------------------------------------------------------------------------------- to .swf

def to_swf(bundle: hab.Bundle, small: str = "auto", warnings: list[str] | None = None) -> ExportResult:
    warnings = [] if warnings is None else warnings
    if small not in SMALL_MODES:
        raise ValueError("unknown small mode %r" % small)
    name = bundle.name
    json_entry, doc = _document(bundle)
    if doc is None:
        symbols = _library_symbols(bundle, warnings)
        kind = "library"
    else:
        name = doc.get("documentClass") or name
        kind = _kind(doc)
        symbols = _atlas_symbols(bundle, json_entry, doc, name, kind, small, warnings)
    if not symbols:
        return ExportResult(None, kind, name, skipped_reason="bundle has no assets")
    data = swfwrite.write_library(name, symbols)
    return ExportResult(data, kind, name, symbols=len(symbols), warnings=warnings)


def _entry_symbol(e: hab.Entry, short: str, warnings: list[str]) -> swfwrite.Symbol | None:
    if e.mime.startswith("image/"):
        try:
            w, h, px = images.decode_png_rgba(e.data) if e.mime == "image/png" else _decode_other(e.data)
        except Exception as exc:
            warnings.append("image %s could not be decoded (%s); left out" % (short, exc))
            return None
        return swfwrite.Symbol(short, "bitmap", px, w, h)
    if e.mime == "sound/mp3":
        if not e.params:
            warnings.append("sound %s has no DefineSound parameters; left out" % short)
            return None
        return swfwrite.Symbol(short, "sound", e.data, params=e.params)
    return swfwrite.Symbol(short, "binary", e.data)


def _decode_other(data: bytes) -> tuple[int, int, bytes]:
    if images.PILImage is None:
        raise ExportError("needs Pillow")
    import io
    im = images.PILImage.open(io.BytesIO(data)).convert("RGBA")
    return im.size[0], im.size[1], im.tobytes()


def _library_symbols(bundle: hab.Bundle, warnings: list[str]) -> list[swfwrite.Symbol]:
    """Library layout: one symbol per entry, the manifest from the index, aliases re-bound."""
    symbols = []
    by_name = {}
    manifest = bundle.metadata.get("manifest")
    if manifest:
        symbols.append(swfwrite.Symbol("manifest", "binary", manifest.encode("utf-8")))
    for e in bundle.entries:
        sym = _entry_symbol(e, e.name, warnings)
        if sym is not None:
            symbols.append(sym)
            by_name[e.name] = sym
    for alias in bundle.metadata.get("aliases") or []:
        target = by_name.get(alias.get("ref"))
        if target is None:
            warnings.append("alias %s points at missing %s" % (alias.get("name"), alias.get("ref")))
            continue
        target.aliases.append(alias["name"])
    return symbols


def _atlas_symbols(bundle: hab.Bundle, json_entry: hab.Entry, doc: dict, name: str, kind: str, small: str,
                   warnings: list[str]) -> list[swfwrite.Symbol]:
    prefix = name + "_"
    sheet = doc.get("spritesheet") or {}
    frames = sheet.get("frames") or {}
    main_image = (sheet.get("meta") or {}).get("image")
    atlas_names = {main_image} | {m.get("image") for m in (sheet.get("meta") or {}).get("images") or []}
    atlas_names |= {f.get("image") for f in frames.values() if f.get("image")}
    atlases = _Atlases(bundle)

    # Bitmaps: one per frame, except where Habbo's SWF bound several class names to one bitmap
    # (see _sharing).
    shorts = {_strip(n, prefix): f for n, f in frames.items()}
    # a frame's name is the class name the asset had in its SWF; keep it even when it does not
    # start with the library name
    frame_classes = {_strip(n, prefix): n for n in frames if not n.startswith(prefix)}
    root = _sharing(doc, shorts, _full_profile(bundle, json_entry))

    bitmaps: dict[str, tuple[int, int, bytes]] = {}
    aliases: dict[str, list[str]] = {}
    cut_cache: dict[tuple, tuple[int, int, bytes]] = {}
    for short, frame in shorts.items():
        owner = root(short)
        if owner != short:
            continue
        key = _frame_key(frame)
        if key not in cut_cache:
            cut_cache[key] = _cut(atlases, main_image, frame)
        bitmaps[short] = cut_cache[key]
        aliases[short] = []
    for short in shorts:
        owner = root(short)
        if owner != short:
            if owner not in aliases:     # a sharing cycle: give the frame its own bitmap
                bitmaps[short] = _cut(atlases, main_image, shorts[short])
                aliases[short] = []
            else:
                aliases[owner].append(short)

    raw = {e.name: e for e in bundle.entries if e is not json_entry and e.name not in atlas_names}
    regenerate = "manifest" not in raw
    work = copy.deepcopy(doc)
    if regenerate:
        # Pet palettes are raw RGB triplets in the SWF; Nitro bundles keep only the JSON `rgb` list.
        for palette in (work.get("palettes") or {}).values():
            source = str(palette.get("source"))
            if source not in raw and palette.get("rgb"):
                blob = bytes(c & 0xFF for rgb in palette["rgb"] for c in rgb[:3])
                raw[source] = hab.Entry(source, blob, "application/octet-stream")
    if regenerate and small == "auto" and kind in ("furniture", "pet"):
        for made in _add_small(work, bitmaps, warnings):
            aliases[made] = []

    symbols: list[swfwrite.Symbol] = []
    if regenerate:
        for short, xml in _documents(work, name, kind, bitmaps, aliases, raw):
            symbols.append(swfwrite.Symbol(short, "binary", xml))
            raw.pop(short, None)
    for short, e in raw.items():
        sym = _entry_symbol(e, short, warnings)
        if sym is not None:
            symbols.append(sym)
    for short, (w, h, px) in bitmaps.items():
        names = [short] + aliases.get(short, [])
        symbols.append(swfwrite.Symbol(short, "bitmap", px, w, h, aliases=names[1:],
                                       classes={n: frame_classes[n] for n in names if n in frame_classes}))
    if regenerate:
        # library-style manifests list assets that share another asset's bitmap; bind them to it
        if kind in ("effect", "library"):
            bound = {s.name: s for s in symbols if s.kind == "bitmap"}
            for alias_short in list(bound):
                for a in bound[alias_short].aliases:
                    bound[a] = bound[alias_short]
            for asset_name, asset in (work.get("assets") or {}).items():
                if asset_name in bound:
                    continue
                target = bound.get(asset.get("source") or "")
                if target is not None:
                    target.aliases.append(asset_name)
                    bound[asset_name] = target
    return symbols


def _documents(doc: dict, name: str, kind: str, bitmaps: dict, aliases: dict[str, list[str]],
               raw: dict[str, hab.Entry]) -> list[tuple[str, bytes]]:
    image_names = sorted(set(bitmaps) | {a for names in aliases.values() for a in names})
    if kind in ("furniture", "pet"):
        # each document only where the JSON shows the library had it
        docs = [("index", toxml.index_xml(doc))]
        if "visualizations" in doc:
            docs.append((name + "_visualization", toxml.visualization_xml(doc)))
        if "assets" in doc or "palettes" in doc:
            docs.append((name + "_assets", toxml.assets_xml(doc)))
        if "logic" in doc:
            docs.append((name + "_logic", toxml.logic_xml(doc)))
        entries = [(short, "text/xml", None) for short, _ in docs]
        entries += [(n, "image/png", None) for n in image_names]
        palettes = {str(p.get("source")) for p in (doc.get("palettes") or {}).values()}
        entries += [(n, "application/octet-stream", None) for n in sorted(palettes) if n in raw]
        return [("manifest", toxml.manifest_xml(name, entries))] + docs
    # Effects and figure libraries: the manifest carries the assets and their offsets. Every asset
    # is listed, including the few Habbo's own manifests list without a symbol behind them.
    entries = []
    for asset_name, asset in (doc.get("assets") or {}).items():
        offset = (asset.get("x"), asset.get("y")) if "x" in asset else None
        if offset is not None and offset[0] is None:
            offset = ("NaN", offset[1])
        entries.append((asset_name, "image/png", offset))
    out = [("manifest", toxml.manifest_xml(name, entries, doc.get("aliases")))]
    if "name" in doc:   # a library with an index (game content); .nitro effects and figures have no index
        out.append(("index", toxml.index_xml(doc)))
    animation = toxml.animation_xml(doc)
    if kind == "effect" and animation is not None:
        out.append(("animation", animation))
    return out


# ---------------------------------------------------------------------------------------- to .nitro

def to_nitro(bundle: hab.Bundle, warnings: list[str] | None = None) -> ExportResult:
    warnings = [] if warnings is None else warnings
    json_entry, doc = _document(bundle)
    if doc is None:
        return ExportResult(None, "library", bundle.name,
                            skipped_reason="library-layout bundle (room content or UI library): "
                                           "Nitro has no equivalent")
    kind = _kind(doc)
    document_class = doc.get("documentClass") or bundle.name
    out: dict = {}
    for key, value in doc.items():
        if key == "documentClass":
            if "name" not in doc:
                out["name"] = document_class   # libraries and effects carry their name here
            continue
        out[key] = value
    out.setdefault("name", document_class)
    name = document_class
    _drop_small(out)

    sheet = out.get("spritesheet")
    files: list[tuple[str, bytes]] = []
    if sheet:
        # Small art goes: the frames swf2hab's full profile puts on the separate small-art atlas.
        # Small frames on the main atlas stay; they own bitmaps that other assets draw (nitro-converter
        # keeps those too), as do frames a kept asset uses through `source` or an alias link.
        main = sheet["meta"]["image"]
        prefix = document_class + "_"
        used = {a.get("source") for a in (out.get("assets") or {}).values()}
        used |= {a.get("link") for a in (out.get("aliases") or {}).values()}
        frames = {n: f for n, f in (sheet.get("frames") or {}).items()
                  if not (f.get("image") not in (None, main) and _small(_strip(n, prefix)))
                  or _strip(n, prefix) in used}
        if _full_profile(bundle, json_entry):
            # every class name of a shared bitmap is a frame here; a .nitro has the owner only
            root = _sharing(doc, {_strip(n, prefix): f for n, f in sheet["frames"].items()}, True)
            frames = {n: f for n, f in frames.items() if root(_strip(n, prefix)) == _strip(n, prefix)}
        main_image = sheet["meta"]["image"]
        if all(not f.get("image") or f.get("image") == main_image for f in frames.values()) \
                and bundle.get(main_image) is not None:
            for f in frames.values():
                f.pop("image", None)
            png = bundle.get(main_image).data
        else:
            png, frames, size = _repack(bundle, sheet, frames)
            sheet["meta"]["size"] = {"w": size[0], "h": size[1]}
            warnings.append("frames spread over several atlases were repacked into one")
        sheet["frames"] = frames
        sheet["meta"].pop("images", None)
        image_name = name + ".png"
        sheet["meta"]["image"] = image_name
        files.append((image_name, png))
    body = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files.insert(0, (name + ".json", body))
    return ExportResult(nitro.write(files), kind, name, symbols=len(files), warnings=warnings)


def _drop_small(doc: dict) -> None:
    """What nitro-converter leaves out: 32px visualizations, assets and particle systems, sh_ parts."""
    if doc.get("visualizations"):
        doc["visualizations"] = [v for v in doc["visualizations"] if v.get("size") != 32]
    if doc.get("assets"):
        doc["assets"] = {n: a for n, a in doc["assets"].items() if not _small(n)}
    if doc.get("aliases"):
        doc["aliases"] = {n: a for n, a in doc["aliases"].items() if not _small(a.get("link") or "")}
    logic = doc.get("logic") or {}
    if logic.get("particleSystems"):
        logic["particleSystems"] = [p for p in logic["particleSystems"] if p.get("size") != 32]


def _repack(bundle: hab.Bundle, sheet: dict, frames: dict) -> tuple[bytes, dict, tuple[int, int]]:
    atlases = _Atlases(bundle)
    main_image = sheet["meta"]["image"]
    keys, sizes, pixels = {}, [], []
    for f in frames.values():
        key = _frame_key(f) + (f.get("image") or main_image,)
        if key not in keys:
            aw, _, atlas = atlases.get(key[-1])
            trimmed = dict(f, spriteSourceSize=None, sourceSize=None)   # the stored piece, upright
            w, h, px = images.cut_frame(atlas, aw, trimmed)
            keys[key] = len(sizes)
            sizes.append((w, h))
            pixels.append(px)
    bins = packer.pack_multi(sizes, padding=0, max_side=1 << 15)
    if len(bins) != 1:
        raise ExportError("frames do not fit in one atlas")
    aw, ah, pos = bins[0]
    atlas = bytearray(aw * ah * 4)
    for i, (x, y) in pos.items():
        images.blit(atlas, aw, pixels[i], sizes[i][0], sizes[i][1], x, y)
    out = {}
    for n, f in frames.items():
        x, y = pos[keys[_frame_key(f) + (f.get("image") or main_image,)]]
        g = dict(f)
        g.pop("image", None)
        g["rotated"] = False
        g["frame"] = dict(f["frame"], x=x, y=y)
        out[n] = g
    return images.encode_png(bytes(atlas), aw, ah), out, (aw, ah)


# ---------------------------------------------------------------------------------------- entry point

def export(data: bytes, target: str, source_name: str = "", small: str = "auto") -> ExportResult:
    if target not in TARGETS:
        raise ValueError("unknown target %r" % target)
    # .swf -> .nitro goes through the sulake profile: exactly what nitro-converter keeps
    bundle, warnings = load(data, source_name, profile="sulake" if target == "nitro" else "full")
    if target == "swf":
        return to_swf(bundle, small=small, warnings=warnings)
    return to_nitro(bundle, warnings=warnings)
