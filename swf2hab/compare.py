"""Compare two .hab bundles: JSON (minus atlas layout) and every frame's pixels."""

from __future__ import annotations

import json

from . import hab, images


def _json_entry(bundle: hab.Bundle) -> dict | None:
    for e in bundle.entries:
        if e.mime == "application/json":
            return json.loads(e.data)
    return None


def _frames(bundle: hab.Bundle, data: dict) -> dict[str, tuple[int, int, bytes]]:
    sheet = data.get("spritesheet") if data else None
    if not sheet:
        return {}
    png = bundle.get(sheet["meta"]["image"])
    if png is None:
        return {}
    aw, ah, atlas = images.decode_png_rgba(png.data)
    out = {}
    for name, f in sheet["frames"].items():
        fr, sss, src = f["frame"], f["spriteSourceSize"], f["sourceSize"]
        w, h = src["w"], src["h"]
        canvas = bytearray(w * h * 4)
        piece = images.crop(atlas, aw, fr["x"], fr["y"], fr["w"], fr["h"])
        images.blit(canvas, w, piece, fr["w"], fr["h"], sss["x"], sss["y"])
        out[name] = (w, h, bytes(canvas))
    return out


def _diff(a, b, path="", out=None, limit=20):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append("%s.%s only in second" % (path, k))
            elif k not in b:
                out.append("%s.%s only in first" % (path, k))
            else:
                _diff(a[k], b[k], "%s.%s" % (path, k), out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s length %d != %d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, "%s[%d]" % (path, i), out, limit)
    elif a != b:
        out.append("%s: %r != %r" % (path, a, b))
    return out


def compare(first: bytes, second: bytes) -> dict:
    a, b = hab.read(first), hab.read(second)
    ja, jb = _json_entry(a), _json_entry(b)
    strip = lambda j: {k: v for k, v in (j or {}).items() if k != "spritesheet"}
    report = {"json_equal": strip(ja) == strip(jb), "json_diffs": _diff(strip(ja), strip(jb))}
    fa, fb = _frames(a, ja), _frames(b, jb)
    report["frames_first"], report["frames_second"] = len(fa), len(fb)
    report["frames_only_first"] = sorted(set(fa) - set(fb))[:20]
    report["frames_only_second"] = sorted(set(fb) - set(fa))[:20]
    same = visible = invisible = size = 0
    for name in set(fa) & set(fb):
        (w1, h1, p1), (w2, h2, p2) = fa[name], fb[name]
        if (w1, h1) != (w2, h2):
            size += 1
            continue
        if p1 == p2:
            same += 1
            continue
        # differences confined to fully transparent pixels are invisible
        vis = any(p1[i + 3] or p2[i + 3] for i in range(0, len(p1), 4) if p1[i:i + 4] != p2[i:i + 4])
        visible += vis
        invisible += not vis
    report.update(frames_identical=same, frames_invisible_diff=invisible, frames_visible_diff=visible,
                  frames_size_diff=size)
    names_a = {e.name for e in a.entries}
    names_b = {e.name for e in b.entries}
    report["entries_only_first"] = sorted(names_a - names_b)
    report["entries_only_second"] = sorted(names_b - names_a)
    return report
