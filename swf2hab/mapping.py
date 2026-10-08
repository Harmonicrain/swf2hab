"""Habbo asset XML -> the JSON schema used inside Habbo's .hab furni/figure bundles.

The schema is the one Habbo serves from images.habbo.com (and that Nitro's
``.nitro`` bundles use). The element-by-element mapping rules follow
nitro-converter (GPL-3.0, https://git.krews.org/nitro/nitro-converter); see
NOTICE.md. Numbers are parsed with JavaScript's parseInt/parseFloat semantics so
the JSON matches what the reference converter produces.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_INT = re.compile(r"^\s*([+-]?)(0[xX][0-9a-fA-F]+|\d+)")
_FLOAT = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def js_int(value):
    """JavaScript parseInt(value) (radix 10/16 autodetect). None stands for NaN."""
    if value is None:
        return None
    m = _INT.match(value)
    if not m:
        return None
    sign, digits = m.groups()
    n = int(digits, 16) if digits[:2].lower() == "0x" else int(digits)
    return -n if sign == "-" else n


_HEX = re.compile(r"^\s*([+-]?)(?:0[xX])?([0-9a-fA-F]+)")


def js_hex(value):
    """JavaScript parseInt(value, 16)."""
    if value is None:
        return None
    m = _HEX.match(value)
    if not m:
        return None
    n = int(m.group(2), 16)
    return -n if m.group(1) == "-" else n


def js_float(value):
    if value is None:
        return None
    m = _FLOAT.match(value)
    if not m:
        return None
    f = float(m.group(0))
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def parse_xml(data: bytes) -> ET.Element | None:
    """Parse an embedded XML asset, tolerating BOMs and the odd unescaped ampersand."""
    text = data.decode("utf-8-sig", "replace").strip("\0 \r\n\t")
    if not text.startswith("<"):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        fixed = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        try:
            return ET.fromstring(fixed)
        except ET.ParseError:
            return None


def _set(out: dict, key: str, value) -> None:
    out[key] = value


class Mapper:
    def __init__(self, document_class: str, image_sources: dict[str, str], keep_small: bool):
        self.doc = document_class
        self.image_sources = image_sources
        self.keep_small = keep_small

    def skipped(self, name: str) -> bool:
        return not self.keep_small and (name.startswith("sh_") or "_32_" in name)

    # ------------------------------------------------------------------ index
    def index(self, root: ET.Element, out: dict) -> None:
        if root.tag != "object":
            return
        a = root.attrib
        if "type" in a:
            out["name"] = a["type"]
        if "logic" in a:
            out["logicType"] = a["logic"]
        if "visualization" in a:
            out["visualizationType"] = a["visualization"]

    # ------------------------------------------------------------------ manifest
    def manifest(self, root: ET.Element, out: dict) -> None:
        if root.tag != "manifest":
            return
        library = root.find("library")
        if library is None:
            return
        assets = [a for parent in library.findall("assets") for a in parent.findall("asset")]
        if assets:
            out["assets"] = target = {}
            for el in assets:
                name = el.get("name")
                if name is None or self.skipped(name):
                    continue
                asset = {}
                param = el.find("param")
                if param is not None and param.get("value") is not None:
                    parts = param.get("value").split(",")
                    asset["x"] = js_int(parts[0])
                    asset["y"] = js_int(parts[1]) if len(parts) > 1 else None
                if name in self.image_sources:
                    asset["source"] = self.image_sources[name]
                target[name] = asset
        aliases = [a for parent in library.findall("aliases") for a in parent.findall("alias")]
        if aliases:
            out["aliases"] = target = {}
            for el in aliases:
                name = el.get("name")
                if name is None:
                    continue
                alias = {}
                link = el.get("link")
                if link is not None:
                    if self.skipped(link):
                        continue
                    alias["link"] = link
                if el.get("fliph") is not None:
                    alias["flipH"] = el.get("fliph") == "1"
                    alias["flipV"] = el.get("flipv") == "1" if el.get("flipv") is not None else None
                target[name] = alias

    # ------------------------------------------------------------------ assets
    def assets(self, root: ET.Element, out: dict) -> None:
        if root.tag != "assets":
            return
        assets = root.findall("asset")
        if assets:
            out["assets"] = target = {}
            for el in assets:
                name = el.get("name")
                if name is None or self.skipped(name):
                    continue
                asset = {}
                src = el.get("source")
                if src is not None:
                    asset["source"] = self.image_sources.get(src, src)
                if name in self.image_sources:
                    asset["source"] = self.image_sources[name]
                if el.get("x") is not None:
                    asset["x"] = js_int(el.get("x"))
                    asset["y"] = js_int(el.get("y"))
                if el.get("flipH") is not None:
                    asset["flipH"] = el.get("flipH") == "1"
                if el.get("flipV") is not None:
                    asset["flipV"] = el.get("flipV") == "1"
                if el.get("usesPalette") is not None:
                    asset["usesPalette"] = el.get("usesPalette") == "1"
                target[name] = asset
        palettes = root.findall("palette")
        if palettes:
            out["palettes"] = target = {}
            for el in palettes:
                a = el.attrib
                p = {}
                if "id" in a:
                    p["id"] = js_int(a["id"])
                if "source" in a:
                    p["source"] = a["source"]
                if "master" in a:
                    p["master"] = a["master"] == "true"
                if "tags" in a:
                    p["tags"] = a["tags"].split(",")
                if "breed" in a:
                    p["breed"] = js_int(a["breed"])
                if "colortag" in a:
                    p["colorTag"] = js_int(a["colortag"])
                if "color1" in a:
                    p["color1"] = a["color1"]
                if "color2" in a:
                    p["color2"] = a["color2"]
                target[str(p.get("id"))] = p

    # ------------------------------------------------------------------ effect animation
    def animation(self, root: ET.Element, out: dict) -> None:
        if root.tag != "animation":
            return
        out["animations"] = target = {}
        a = root.attrib
        anim = {}
        if "name" in a:
            anim["name"] = a["name"]
        if "desc" in a:
            anim["desc"] = a["desc"]
        if "resetOnToggle" in a:
            anim["resetOnToggle"] = a["resetOnToggle"] == "true"

        def ints(el, keys, rename=None):
            d = {}
            for k in keys:
                if el.get(k) is not None:
                    d[(rename or {}).get(k, k)] = js_int(el.get(k))
            return d

        directions = root.findall("direction")
        if directions:
            anim["directions"] = [ints(d, ["offset"]) for d in directions]
        shadows = root.findall("shadow")
        if shadows:
            anim["shadows"] = [{"id": s.get("id")} if s.get("id") is not None else {} for s in shadows]
        adds = root.findall("add")
        if adds:
            lst = []
            for el in adds:
                d = {}
                for k in ("id", "align", "blend"):
                    if el.get(k) is not None:
                        d[k] = el.get(k)
                if el.get("ink") is not None:
                    d["ink"] = js_int(el.get("ink"))
                if el.get("base") is not None:
                    d["base"] = el.get("base")
                lst.append(d)
            anim["adds"] = lst
        removes = root.findall("remove")
        if removes:
            anim["removes"] = [{"id": r.get("id")} if r.get("id") is not None else {} for r in removes]
        sprites = root.findall("sprite")
        if sprites:
            lst = []
            for el in sprites:
                d = {}
                if el.get("id") is not None:
                    d["id"] = el.get("id")
                if el.get("directions") is not None:
                    d["directions"] = js_int(el.get("directions"))
                if el.get("member") is not None:
                    d["member"] = el.get("member")
                if el.get("ink") is not None:
                    d["ink"] = js_int(el.get("ink"))
                if el.get("staticY") is not None:
                    d["staticY"] = js_int(el.get("staticY"))
                dirs = el.findall("direction")
                if dirs:
                    d["directionList"] = [ints(x, ["id", "dx", "dy", "dz"]) for x in dirs]
                lst.append(d)
            anim["sprites"] = lst

        def frames(parent):
            result = []
            for fr in parent.findall("frame"):
                f = {}
                if fr.get("repeats") is not None:
                    f["repeats"] = js_int(fr.get("repeats"))
                for tag, key in (("fx", "fxs"), ("bodypart", "bodyparts")):
                    parts = fr.findall(tag)
                    if parts:
                        f[key] = [part(p) for p in parts]
                result.append(f)
            return result

        def part(el):
            d = {}
            if el.get("id") is not None:
                d["id"] = el.get("id")
            if el.get("frame") is not None:
                d["frame"] = js_int(el.get("frame"))
            if el.get("base") is not None:
                d["base"] = el.get("base")
            if el.get("action") is not None:
                d["action"] = el.get("action")
            for k in ("dx", "dy", "dz", "dd"):
                if el.get(k) is not None:
                    d[k] = js_int(el.get(k))
            items = el.findall("item")
            if items:
                lst = []
                for it in items:
                    i = {}
                    if it.get("id") is not None:
                        i["id"] = it.get("id")
                    if it.get("base") is not None:
                        i["base"] = it.get("base")
                    lst.append(i)
                d["items"] = lst
            return d

        if root.findall("frame"):
            anim["frames"] = frames(root)
        avatars = root.findall("avatar")
        if avatars:
            lst = []
            for el in avatars:
                d = {}
                if el.get("background") is not None:
                    d["background"] = el.get("background")
                if el.get("foreground") is not None:
                    d["foreground"] = el.get("foreground")
                if el.get("ink") is not None:
                    d["ink"] = js_int(el.get("ink"))
                lst.append(d)
            anim["avatars"] = lst
        overrides = root.findall("override")
        if overrides:
            lst = []
            for el in overrides:
                d = {}
                if el.get("name") is not None:
                    d["name"] = el.get("name")
                if el.get("override") is not None:
                    d["override"] = el.get("override")
                if el.findall("frame"):
                    d["frames"] = frames(el)
                lst.append(d)
            anim["overrides"] = lst
        target[str(a.get("desc"))] = anim

    # ------------------------------------------------------------------ logic
    def logic(self, root: ET.Element, out: dict) -> None:
        if root.tag != "objectData":
            return
        out["logic"] = logic = {}
        model = root.find("model")
        if model is not None:
            logic["model"] = m = {}
            dims = model.find("dimensions")
            if dims is not None:
                m["dimensions"] = {"x": js_float(dims.get("x")), "y": js_float(dims.get("y"))}
                if dims.get("z") is not None:
                    m["dimensions"]["z"] = js_float(dims.get("z"))
                if dims.get("centerZ") is not None:
                    m["dimensions"]["centerZ"] = js_float(dims.get("centerZ"))
            dir_parents = model.findall("directions")
            if dir_parents:
                ids = [js_int(d.get("id")) for p in dir_parents for d in p.findall("direction")]
                m["directions"] = ids if ids else [0]
        action = root.find("action")
        if action is not None:
            logic["action"] = act = {}
            if action.get("link") is not None:
                act["link"] = action.get("link")
            if action.get("startState") is not None:
                act["startState"] = js_int(action.get("startState"))
        mask = root.find("mask")
        if mask is not None:
            logic["maskType"] = mask.get("type")
        credits = root.find("credits")
        if credits is not None:
            logic["credits"] = credits.get("value")
        sound = root.find("sound")
        if sound is not None and sound.find("sample") is not None:
            s = sound.find("sample")
            logic["soundSample"] = {"id": js_int(s.get("id")),
                                    "noPitch": (s.get("nopitch") == "true") if s.get("nopitch") is not None else None}
        planet = root.find("planetsystem")
        if planet is not None:
            logic["planetSystems"] = lst = []
            for o in planet.findall("object"):
                d = {}
                if o.get("id") is not None:
                    d["id"] = js_int(o.get("id"))
                for k in ("name", "parent"):
                    if o.get(k) is not None:
                        d[k] = o.get(k)
                for k, key in (("radius", "radius"), ("arcspeed", "arcSpeed"), ("arcoffset", "arcOffset"),
                               ("blend", "blend"), ("height", "height")):
                    if o.get(k) is not None:
                        d[key] = js_float(o.get(k))
                lst.append(d)
        particles = root.find("particlesystems")
        if particles is not None:
            logic["particleSystems"] = lst = []
            for ps in particles.findall("particlesystem"):
                size = js_int(ps.get("size")) if ps.get("size") is not None else None
                if size == 32 and not self.keep_small:
                    continue
                d = {}
                if ps.get("size") is not None:
                    d["size"] = size
                if ps.get("canvas_id") is not None:
                    d["canvasId"] = js_int(ps.get("canvas_id"))
                if ps.get("offset_y") is not None:
                    d["offsetY"] = js_float(ps.get("offset_y"))
                if ps.get("blend") is not None:
                    d["blend"] = js_float(ps.get("blend"))
                if ps.get("bgcolor") is not None:
                    d["bgColor"] = ps.get("bgcolor")
                emitters = ps.findall("emitter")
                if emitters:
                    d["emitters"] = [self._emitter(e) for e in emitters]
                lst.append(d)
        custom = root.find("customvars")
        if custom is not None:
            logic["customVars"] = cv = {}
            variables = custom.findall("variable")
            cv["variables"] = [v.get("name") for v in variables if v.get("name") is not None]

    def _emitter(self, e: ET.Element) -> dict:
        d = {}
        if e.get("id") is not None:
            d["id"] = js_int(e.get("id"))
        if e.get("name") is not None:
            d["name"] = e.get("name")
        for k, key in (("sprite_id", "spriteId"), ("max_num_particles", "maxNumParticles"),
                       ("particles_per_frame", "particlesPerFrame")):
            if e.get(k) is not None:
                d[key] = js_int(e.get(k))
        d["burstPulse"] = js_int(e.get("burst_pulse")) if e.get("burst_pulse") is not None else 1
        if e.get("fuse_time") is not None:
            d["fuseTime"] = js_int(e.get("fuse_time"))
        sim = e.find("simulation")
        if sim is not None:
            s = {}
            for k, key in (("force", "force"), ("direction", "direction"), ("gravity", "gravity"),
                           ("airfriction", "airFriction")):
                if sim.get(k) is not None:
                    s[key] = js_float(sim.get(k))
            if sim.get("shape") is not None:
                s["shape"] = sim.get("shape")
            if sim.get("energy") is not None:
                s["energy"] = js_float(sim.get("energy"))
            d["simulation"] = s
        parts = e.find("particles")
        if parts is not None:
            plist = parts.findall("particle")
            if plist:
                d["particles"] = []
                for p in plist:
                    pd = {}
                    if p.get("is_emitter") is not None:
                        pd["isEmitter"] = p.get("is_emitter") == "true"
                    if p.get("lifetime") is not None:
                        pd["lifeTime"] = js_int(p.get("lifetime"))
                    if p.get("fade") is not None:
                        pd["fade"] = p.get("fade") == "true"
                    frames = [f.get("name") for f in p.findall("frame")]
                    if frames:
                        pd["frames"] = frames
                    d["particles"].append(pd)
        return d

    # ------------------------------------------------------------------ visualization
    def visualization(self, root: ET.Element, out: dict) -> None:
        if root.tag != "visualizationData":
            return
        vis = [v for g in root.findall("graphics") for v in g.findall("visualization")]
        if not vis:
            return
        out["visualizations"] = lst = []
        for v in vis:
            size = js_int(v.get("size")) if v.get("size") is not None else None
            if size == 32 and not self.keep_small:
                continue
            d = {}
            if v.get("angle") is not None:
                d["angle"] = js_int(v.get("angle"))
            if v.get("layerCount") is not None:
                d["layerCount"] = js_int(v.get("layerCount"))
            if v.get("size") is not None:
                d["size"] = size
            layers = [l for p in v.findall("layers") for l in p.findall("layer")]
            if layers:
                d["layers"] = self._layers(layers)
            directions = [x for p in v.findall("directions") for x in p.findall("direction")]
            if directions:
                d["directions"] = dd = {}
                for x in directions:
                    entry = {}
                    dl = x.findall("layer")
                    if dl:
                        entry["layers"] = self._layers(dl)
                    dd[str(js_int(x.get("id")))] = entry
            colors = [c for p in v.findall("colors") for c in p.findall("color")]
            if colors:
                d["colors"] = cd = {}
                for c in colors:
                    entry = {}
                    cls = c.findall("colorLayer")
                    if cls:
                        entry["layers"] = {}
                        for cl in cls:
                            layer = {}
                            if cl.get("color") is not None:
                                layer["color"] = js_hex(cl.get("color"))
                            entry["layers"][str(js_int(cl.get("id")))] = layer
                    cd[str(js_int(c.get("id")))] = entry
            animations = [an for p in v.findall("animations") for an in p.findall("animation")]
            if animations:
                d["animations"] = ad = {}
                for an in animations:
                    entry = {}
                    for k in ("transitionTo", "transitionFrom"):
                        if an.get(k) is not None:
                            entry[k] = js_int(an.get(k))
                    if an.get("immediateChangeFrom") is not None:
                        entry["immediateChangeFrom"] = an.get("immediateChangeFrom")
                    if an.get("randomStart") is not None:
                        entry["randomStart"] = an.get("randomStart") == "1"
                    al = an.findall("animationLayer")
                    if al:
                        entry["layers"] = {}
                        for layer in al:
                            le = {}
                            for k in ("frameRepeat", "loopCount", "random"):
                                if layer.get(k) is not None:
                                    le[k] = js_int(layer.get(k))
                            seqs = layer.findall("frameSequence")
                            if seqs:
                                le["frameSequences"] = {str(i): self._sequence(s) for i, s in enumerate(seqs)}
                            entry["layers"][str(js_int(layer.get("id")))] = le
                    key = self._next_id(js_int(an.get("id")), ad)
                    if key is not None:
                        ad[key] = entry
            postures_el = v.find("postures")
            # The reference converter only emits postures when <posture> children exist.
            if postures_el is not None and postures_el.findall("posture"):
                d["postures"] = pd = {}
                if postures_el.get("defaultPosture") is not None:
                    pd["defaultPosture"] = postures_el.get("defaultPosture")
                pd["postures"] = [self._id_anim(p) for p in postures_el.findall("posture")]
            gestures = [g for p in v.findall("gestures") for g in p.findall("gesture")]
            if gestures:
                d["gestures"] = [self._id_anim(g) for g in gestures]
            lst.append(d)

    @staticmethod
    def _id_anim(el: ET.Element) -> dict:
        d = {}
        if el.get("id") is not None:
            d["id"] = el.get("id")
        if el.get("animationId") is not None:
            d["animationId"] = js_int(el.get("animationId"))
        return d

    @staticmethod
    def _next_id(request, output: dict):
        key = str(request) if request is not None else "NaN"
        if key not in output:
            return key
        i = 1
        while i < 6:  # mirrors the reference converter's suffix scheme
            key += "_" + str(i)
            if key not in output:
                return key
            i += 1
        return None

    def _layers(self, layers: list[ET.Element]) -> dict:
        out = {}
        for l in layers:
            d = {}
            if l.get("x") is not None:
                d["x"] = js_int(l.get("x"))
            if l.get("y") is not None:
                d["y"] = js_int(l.get("y"))
            if l.get("z") is not None:
                d["z"] = js_int(l.get("z"))
            if l.get("alpha") is not None:
                d["alpha"] = js_int(l.get("alpha"))
            if l.get("ink") is not None:
                d["ink"] = l.get("ink")
            if l.get("tag") is not None:
                d["tag"] = l.get("tag")
            if l.get("ignoreMouse") is not None:
                d["ignoreMouse"] = l.get("ignoreMouse") == "1"
            out[str(js_int(l.get("id")))] = d
        return out

    @staticmethod
    def _sequence(s: ET.Element) -> dict:
        d = {}
        if s.get("loopCount") is not None:
            d["loopCount"] = js_int(s.get("loopCount"))
        if s.get("random") is not None:
            d["random"] = js_int(s.get("random"))
        frames = s.findall("frame")
        if frames:
            d["frames"] = {}
            for i, f in enumerate(frames):
                fd = {}
                fid = f.get("id")
                fd["id"] = 0 if fid is None or fid == "NaN" else js_int(fid)
                for k in ("x", "y", "randomX", "randomY"):
                    if f.get(k) is not None:
                        fd[k] = js_int(f.get(k))
                offsets = [o for p in f.findall("offsets") for o in p.findall("offset")]
                if offsets:
                    fd["offsets"] = {}
                    for j, o in enumerate(offsets):
                        od = {}
                        for k in ("direction", "x", "y"):
                            if o.get(k) is not None:
                                od[k] = js_int(o.get(k))
                        fd["offsets"][str(j)] = od
                d["frames"][str(i)] = fd
        return d
