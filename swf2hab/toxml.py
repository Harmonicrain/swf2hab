"""Habbo bundle JSON -> the XML documents of a Flash asset library (the inverse of mapping.py).

Each function writes back exactly what mapping.py reads, with the attribute spellings and value
forms Habbo's XML uses ("1"/"0" for asset flips, "true"/"false" for palette and particle flags,
hex colours, ...), so converting the regenerated XML with mapping.py gives back the same JSON.
What mapping.py drops on the way in (unparsed attributes, number formatting) cannot come back.
"""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'


class El:
    def __init__(self, tag: str, attrs: list[tuple[str, object]] | None = None, children: list["El"] | None = None):
        self.tag = tag
        self.attrs = [(k, v) for k, v in (attrs or []) if v is not None]
        self.children = children or []

    def add(self, child: "El") -> "El":
        self.children.append(child)
        return child

    def render(self, indent: int = 0, out: list[str] | None = None) -> list[str]:
        out = [] if out is None else out
        pad = "   " * indent
        attrs = "".join(" %s=%s" % (k, quoteattr(_text(v))) for k, v in self.attrs)
        if not self.children:
            out.append("%s<%s%s/>" % (pad, self.tag, attrs))
            return out
        out.append("%s<%s%s>" % (pad, self.tag, attrs))
        for c in self.children:
            c.render(indent + 1, out)
        out.append("%s</%s>" % (pad, self.tag))
        return out


def _text(v) -> str:
    if isinstance(v, bool):
        raise TypeError("booleans must be spelled explicitly")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def document(root: El) -> bytes:
    return (DECLARATION + "\n".join(root.render()) + "\n").encode("utf-8")


def _n(d: dict, key: str):
    """d[key] for an attribute; a key mapping.py stored as None (NaN from parseInt) comes back as
    "NaN", which parses to None again. Booleans and containers are never passed through here."""
    if key in d and d[key] is None:
        return "NaN"
    return d.get(key)


def _flag(value, true: str = "1", false: str = "0"):
    if value is None:
        return None
    return true if value else false


# ---------------------------------------------------------------------------------------- index

def index_xml(doc: dict) -> bytes:
    return document(El("object", [("type", _n(doc, "name")), ("visualization", _n(doc, "visualizationType")),
                                  ("logic", _n(doc, "logicType"))]))


# ---------------------------------------------------------------------------------------- manifest

def manifest_xml(library: str, entries: list[tuple[str, str, tuple | None]],
                 aliases: dict | None = None) -> bytes:
    """`entries` is (asset name, mime type, (x, y) offset or None) in manifest order."""
    lib = El("library", [("name", library), ("version", "0.1")])
    assets = lib.add(El("assets"))
    for name, mime, offset in entries:
        el = assets.add(El("asset", [("name", name), ("mimeType", mime)]))
        if offset is not None:
            x, y = offset
            el.add(El("param", [("key", "offset"), ("value", "%s,%s" % (_text(x), _text(y)) if y is not None
                                                     else _text(x))]))
    if aliases:
        al = lib.add(El("aliases"))
        for name, alias in aliases.items():
            al.add(El("alias", [("name", name), ("link", _n(alias, "link")),
                                ("fliph", _flag(alias.get("flipH"))), ("flipv", _flag(alias.get("flipV")))]))
    return document(El("manifest", children=[lib]))


# ---------------------------------------------------------------------------------------- assets

def assets_xml(doc: dict) -> bytes:
    root = El("assets")
    for name, a in (doc.get("assets") or {}).items():
        root.add(El("asset", [("name", name), ("x", _n(a, "x")), ("y", _n(a, "y")),
                              ("flipH", _flag(a.get("flipH"))), ("flipV", _flag(a.get("flipV"))),
                              ("source", _n(a, "source")), ("usesPalette", _flag(a.get("usesPalette")))]))
    for p in (doc.get("palettes") or {}).values():
        tags = p.get("tags")
        root.add(El("palette", [("id", _n(p, "id")), ("source", _n(p, "source")),
                                ("master", _flag(p.get("master"), "true", "false")),
                                ("tags", ",".join(tags) if tags is not None else None), ("breed", _n(p, "breed")),
                                ("colortag", _n(p, "colorTag")), ("color1", _n(p, "color1")),
                                ("color2", _n(p, "color2"))]))
    return document(root)


# ---------------------------------------------------------------------------------------- logic

def logic_xml(doc: dict) -> bytes:
    logic = doc.get("logic") or {}
    root = El("objectData", [("type", _n(doc, "name"))])
    model = logic.get("model")
    if model is not None:
        m = root.add(El("model"))
        dims = model.get("dimensions")
        if dims is not None:
            m.add(El("dimensions", [("x", _n(dims, "x")), ("y", _n(dims, "y")), ("z", _n(dims, "z")),
                                    ("centerZ", _n(dims, "centerZ"))]))
        if model.get("directions") is not None:
            m.add(El("directions", children=[El("direction", [("id", d)]) for d in model["directions"]]))
    action = logic.get("action")
    if action is not None:
        root.add(El("action", [("link", _n(action, "link")), ("startState", _n(action, "startState"))]))
    if "maskType" in logic:
        root.add(El("mask", [("type", logic["maskType"])]))
    if "credits" in logic:
        root.add(El("credits", [("value", logic["credits"])]))
    sample = logic.get("soundSample")
    if sample is not None:
        root.add(El("sound", children=[El("sample", [("id", _n(sample, "id")),
                                                     ("nopitch", _flag(sample.get("noPitch"), "true", "false"))])]))
    if logic.get("planetSystems") is not None:
        ps = root.add(El("planetsystem"))
        for o in logic["planetSystems"]:
            ps.add(El("object", [("id", _n(o, "id")), ("name", _n(o, "name")), ("parent", _n(o, "parent")),
                                 ("radius", _n(o, "radius")), ("arcspeed", _n(o, "arcSpeed")),
                                 ("arcoffset", _n(o, "arcOffset")), ("blend", _n(o, "blend")),
                                 ("height", _n(o, "height"))]))
    if logic.get("particleSystems") is not None:
        systems = root.add(El("particlesystems"))
        for s in logic["particleSystems"]:
            el = systems.add(El("particlesystem", [("size", _n(s, "size")), ("canvas_id", _n(s, "canvasId")),
                                                   ("offset_y", _n(s, "offsetY")), ("blend", _n(s, "blend")),
                                                   ("bgcolor", _n(s, "bgColor"))]))
            for e in s.get("emitters") or []:
                el.add(_emitter(e))
    custom = logic.get("customVars")
    if custom is not None:
        root.add(El("customvars", children=[El("variable", [("name", v)]) for v in custom.get("variables") or []]))
    return document(root)


def _emitter(e: dict) -> El:
    el = El("emitter", [("id", _n(e, "id")), ("name", _n(e, "name")), ("sprite_id", _n(e, "spriteId")),
                        ("max_num_particles", _n(e, "maxNumParticles")),
                        ("particles_per_frame", _n(e, "particlesPerFrame")), ("burst_pulse", _n(e, "burstPulse")),
                        ("fuse_time", _n(e, "fuseTime"))])
    sim = e.get("simulation")
    if sim is not None:
        el.add(El("simulation", [("force", _n(sim, "force")), ("direction", _n(sim, "direction")),
                                 ("gravity", _n(sim, "gravity")), ("airfriction", _n(sim, "airFriction")),
                                 ("shape", _n(sim, "shape")), ("energy", _n(sim, "energy"))]))
    if e.get("particles") is not None:
        parts = el.add(El("particles"))
        for p in e["particles"]:
            pe = parts.add(El("particle", [("is_emitter", _flag(p.get("isEmitter"), "true", "false")),
                                           ("lifetime", _n(p, "lifeTime")),
                                           ("fade", _flag(p.get("fade"), "true", "false"))]))
            for name in p.get("frames") or []:
                pe.add(El("frame", [("name", name)]))
    return el


# ---------------------------------------------------------------------------------------- visualization

def _layer_attrs(layer_id: str, d: dict) -> list[tuple[str, object]]:
    return [("id", layer_id), ("x", _n(d, "x")), ("y", _n(d, "y")), ("z", _n(d, "z")), ("alpha", _n(d, "alpha")),
            ("ink", _n(d, "ink")), ("tag", _n(d, "tag")), ("ignoreMouse", _flag(d.get("ignoreMouse")))]


def _animation_id(key: str) -> str:
    # mapping.py suffixes repeated ids as "<id>_1", "<id>_1_2", ...; the XML id is the part before
    return key.split("_", 1)[0]


def visualization_xml(doc: dict) -> bytes:
    graphics = El("graphics")
    for v in doc.get("visualizations") or []:
        el = graphics.add(El("visualization", [("size", _n(v, "size")), ("layerCount", _n(v, "layerCount")),
                                               ("angle", _n(v, "angle"))]))
        if v.get("layers") is not None:
            el.add(El("layers", children=[El("layer", _layer_attrs(k, d)) for k, d in v["layers"].items()]))
        if v.get("directions") is not None:
            dirs = el.add(El("directions"))
            for k, d in v["directions"].items():
                de = dirs.add(El("direction", [("id", k)]))
                for lk, ld in (d.get("layers") or {}).items():
                    de.add(El("layer", _layer_attrs(lk, ld)))
        if v.get("colors") is not None:
            colors = el.add(El("colors"))
            for k, c in v["colors"].items():
                ce = colors.add(El("color", [("id", k)]))
                for lk, ld in (c.get("layers") or {}).items():
                    color = ld.get("color")
                    if isinstance(color, int):
                        color = "%06X" % color if color >= 0 else "-%X" % -color
                    else:
                        color = _n(ld, "color")
                    ce.add(El("colorLayer", [("id", lk), ("color", color)]))
        if v.get("animations") is not None:
            anims = el.add(El("animations"))
            for k, a in v["animations"].items():
                ae = anims.add(El("animation", [("id", _animation_id(k)), ("transitionTo", _n(a, "transitionTo")),
                                                ("transitionFrom", _n(a, "transitionFrom")),
                                                ("immediateChangeFrom", _n(a, "immediateChangeFrom")),
                                                ("randomStart", _flag(a.get("randomStart")))]))
                for lk, ld in (a.get("layers") or {}).items():
                    le = ae.add(El("animationLayer", [("id", lk), ("frameRepeat", _n(ld, "frameRepeat")),
                                                      ("loopCount", _n(ld, "loopCount")), ("random", _n(ld, "random"))]))
                    for seq in (ld.get("frameSequences") or {}).values():
                        se = le.add(El("frameSequence", [("loopCount", _n(seq, "loopCount")),
                                                         ("random", _n(seq, "random"))]))
                        for fr in (seq.get("frames") or {}).values():
                            fe = se.add(El("frame", [("id", _n(fr, "id")), ("x", _n(fr, "x")), ("y", _n(fr, "y")),
                                                     ("randomX", _n(fr, "randomX")), ("randomY", _n(fr, "randomY"))]))
                            if fr.get("offsets"):
                                fe.add(El("offsets", children=[
                                    El("offset", [("direction", _n(o, "direction")), ("x", _n(o, "x")), ("y", _n(o, "y"))])
                                    for o in fr["offsets"].values()]))
        postures = v.get("postures")
        if postures is not None:
            el.add(El("postures", [("defaultPosture", _n(postures, "defaultPosture"))],
                      [El("posture", [("id", _n(p, "id")), ("animationId", _n(p, "animationId"))])
                       for p in postures.get("postures") or []]))
        if v.get("gestures") is not None:
            el.add(El("gestures", children=[El("gesture", [("id", _n(g, "id")), ("animationId", _n(g, "animationId"))])
                                            for g in v["gestures"]]))
    return document(El("visualizationData", [("type", _n(doc, "name"))], [graphics]))


# ---------------------------------------------------------------------------------------- effect animation

def animation_xml(doc: dict) -> bytes | None:
    animations = doc.get("animations") or {}
    if not animations:
        return None
    anim = next(iter(animations.values()))
    root = El("animation", [("name", _n(anim, "name")), ("desc", _n(anim, "desc")),
                            ("resetOnToggle", _flag(anim.get("resetOnToggle"), "true", "false"))])
    for d in anim.get("directions") or []:
        root.add(El("direction", [("offset", _n(d, "offset"))]))
    for s in anim.get("shadows") or []:
        root.add(El("shadow", [("id", _n(s, "id"))]))
    for a in anim.get("adds") or []:
        root.add(El("add", [("id", _n(a, "id")), ("align", _n(a, "align")), ("blend", _n(a, "blend")),
                            ("ink", _n(a, "ink")), ("base", _n(a, "base"))]))
    for r in anim.get("removes") or []:
        root.add(El("remove", [("id", _n(r, "id"))]))
    for s in anim.get("sprites") or []:
        root.add(El("sprite", [("id", _n(s, "id")), ("member", _n(s, "member")), ("directions", _n(s, "directions")),
                               ("staticY", _n(s, "staticY")), ("ink", _n(s, "ink"))],
                    [El("direction", [("id", _n(d, "id")), ("dx", _n(d, "dx")), ("dy", _n(d, "dy")), ("dz", _n(d, "dz"))])
                     for d in s.get("directionList") or []]))
    for f in anim.get("frames") or []:
        root.add(_frame(f))
    for a in anim.get("avatars") or []:
        root.add(El("avatar", [("ink", _n(a, "ink")), ("foreground", _n(a, "foreground")),
                               ("background", _n(a, "background"))]))
    for o in anim.get("overrides") or []:
        root.add(El("override", [("name", _n(o, "name")), ("override", _n(o, "override"))],
                    [_frame(f) for f in o.get("frames") or []]))
    return document(root)


def _frame(f: dict) -> El:
    el = El("frame", [("repeats", _n(f, "repeats"))])
    for key, tag in (("bodyparts", "bodypart"), ("fxs", "fx")):
        for p in f.get(key) or []:
            el.add(El(tag, [("id", _n(p, "id")), ("action", _n(p, "action")), ("frame", _n(p, "frame")),
                            ("base", _n(p, "base")), ("dx", _n(p, "dx")), ("dy", _n(p, "dy")), ("dz", _n(p, "dz")),
                            ("dd", _n(p, "dd"))],
                      [El("item", [("id", _n(i, "id")), ("base", _n(i, "base"))]) for i in p.get("items") or []]))
    return el
