"""Self-contained tests: they build a tiny furni SWF in memory, so no Habbo assets are needed.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import random
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swf2hab import convert, hab, packer  # noqa: E402
from swf2hab.swf import read_assets  # noqa: E402


def tag(code, body):
    if len(body) < 0x3F:
        return struct.pack("<H", (code << 6) | len(body)) + body
    return struct.pack("<HI", (code << 6) | 0x3F, len(body)) + body


def lossless2(cid, w, h, argb_pixels):
    data = zlib.compress(bytes(argb_pixels))
    return tag(36, struct.pack("<HBHH", cid, 5, w, h) + data)


def binary(cid, text):
    return tag(87, struct.pack("<HI", cid, 0) + text.encode())


def symbols(pairs):
    body = struct.pack("<H", len(pairs)) + b"".join(struct.pack("<H", c) + n.encode() + b"\0" for c, n in pairs)
    return tag(76, body)


def make_swf():
    # 2x2 image: opaque red, 50% premultiplied green, transparent, opaque blue
    px = [255, 255, 0, 0, 128, 0, 64, 0, 0, 0, 0, 0, 255, 0, 0, 255]
    tags = [
        lossless2(1, 2, 2, px),
        binary(2, '<object type="chair" visualization="furniture_static" logic="furniture_basic"/>'),
        binary(3, '<manifest><library name="chair"><assets>'
                  '<asset name="chair_64_a_0_0" mimeType="image/png"/></assets></library></manifest>'),
        binary(4, '<assets><asset name="chair_64_a_0_0" x="-10" y="20"/>'
                  '<asset name="chair_64_a_2_0" source="chair_64_a_0_0" x="-10" y="20" flipH="1"/></assets>'),
        binary(5, '<objectData type="chair"><model><dimensions x="1" y="1" z="1.0"/>'
                  '<directions><direction id="0"/><direction id="90"/></directions></model></objectData>'),
        binary(6, '<visualizationData type="chair"><graphics>'
                  '<visualization size="64" layerCount="1" angle="45"><layers><layer id="0" z="1"/></layers>'
                  '<directions><direction id="0"/></directions></visualization>'
                  '<visualization size="32" layerCount="1" angle="45"/></graphics></visualizationData>'),
        symbols([(1, "chair_chair_64_a_0_0"), (2, "chair_index"), (3, "chair_manifest"),
                 (4, "chair_chair_assets"), (5, "chair_chair_logic"), (6, "chair_chair_visualization"),
                 (0, "chair")]),
        tag(1, b""), tag(0, b""),
    ]
    body = b"\x00" + struct.pack("<HH", 24 << 8, 1) + b"".join(tags)  # RECT with nbits=0 is one byte
    raw_len = 8 + len(body)
    return b"CWS" + bytes([10]) + struct.pack("<I", raw_len) + zlib.compress(body)


class SwfTests(unittest.TestCase):
    def test_reader_unpremultiplies(self):
        a = read_assets(make_swf())
        self.assertEqual(a.document_class, "chair")
        rgba = a.bitmaps[1].rgba
        self.assertEqual(rgba[0:4], bytes([255, 0, 0, 255]))
        self.assertEqual(rgba[4:8], bytes([0, 128, 0, 128]))   # round(64 * 255 / 128)
        self.assertEqual(rgba[8:12], bytes([0, 0, 0, 0]))


class ConvertTests(unittest.TestCase):
    def test_sulake_profile_schema(self):
        r = convert.convert(make_swf(), profile="sulake")
        b = hab.read(r.data)
        self.assertEqual([e.name for e in b.entries], ["chair.json", "chair.png"])
        j = json.loads(b.entries[0].data)
        self.assertEqual(j["name"], "chair")
        self.assertEqual(j["logicType"], "furniture_basic")
        self.assertEqual(j["assets"]["chair_64_a_2_0"], {"source": "chair_64_a_0_0", "x": -10, "y": 20, "flipH": True})
        self.assertEqual(j["logic"]["model"], {"dimensions": {"x": 1, "y": 1, "z": 1}, "directions": [0, 90]})
        self.assertEqual([v["size"] for v in j["visualizations"]], [64])      # 32px dropped
        self.assertIn("chair_chair_64_a_0_0", j["spritesheet"]["frames"])

    def test_full_profile_keeps_everything(self):
        r = convert.convert(make_swf(), profile="full")
        b = hab.read(r.data)
        names = {e.name for e in b.entries}
        self.assertTrue({"chair.json", "chair.png", "index", "manifest", "chair_visualization"} <= names)
        j = json.loads(b.get("chair.json").data)
        self.assertEqual([v["size"] for v in j["visualizations"]], [64, 32])


class HabTests(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        bundle = hab.Bundle("x", metadata={"manifest": "<m/>"})
        bundle.add("a.txt", b"hello" * 100, "text/plain")
        bundle.add("b.bin", os.urandom(64), "application/octet-stream")
        data = hab.write(bundle)
        back = hab.read(data)
        self.assertEqual(back.get("a.txt").data, b"hello" * 100)
        self.assertEqual(back.metadata["manifest"], "<m/>")
        with self.assertRaises(hab.HabError):
            hab.read(data + b"x")                 # trailing bytes
        with self.assertRaises(hab.HabError):
            hab.read(b"HAB\0" + data[4:6] + b"\x02\x00" + data[8:])   # unknown flags


class PackerTests(unittest.TestCase):
    def test_no_overlap(self):
        rnd = random.Random(1)
        sizes = [(rnd.randint(1, 90), rnd.randint(1, 120)) for _ in range(300)]
        w, h, pos = packer.pack(sizes)
        boxes = [(x, y, x + sw, y + sh) for (x, y), (sw, sh) in zip(pos, sizes)]
        for x0, y0, x1, y1 in boxes:
            self.assertTrue(0 <= x0 and x1 <= w and 0 <= y0 and y1 <= h)
        grid = {}
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    self.assertNotIn((xx, yy), grid)
                    grid[(xx, yy)] = i

    def test_multi_respects_limit(self):
        bins = packer.pack_multi([(100, 100)] * 50, max_side=256)
        self.assertGreater(len(bins), 1)
        for w, h, _ in bins:
            self.assertLessEqual(max(w, h), 256)



def make_nitro(files, gzip_files=()):
    """A .nitro bundle as nitro-converter writes it (big-endian, zlib or gzip per file)."""
    out = struct.pack(">H", len(files))
    for name, data in files.items():
        if name in gzip_files:
            c = zlib.compressobj(9, zlib.DEFLATED, 31)
            packed = c.compress(data) + c.flush()
        else:
            packed = zlib.compress(data)
        raw = name.encode()
        out += struct.pack(">H", len(raw)) + raw + struct.pack(">I", len(packed)) + packed
    return out


# 1x1 opaque red RGBA PNG
def _png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


# 1x1 opaque red RGBA PNG
TINY_PNG = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + _png_chunk(b"IEND", b""))


class NitroTests(unittest.TestCase):
    def sheet(self):
        return {"frames": {"x_x_64_a_0_0": {"frame": {"x": 0, "y": 0, "w": 1, "h": 1}, "rotated": False,
                                            "trimmed": False, "spriteSourceSize": {"x": 0, "y": 0, "w": 1, "h": 1},
                                            "sourceSize": {"w": 1, "h": 1}, "pivot": {"x": 0.5, "y": 0.5}}},
                "meta": {"image": "x.png", "format": "RGBA8888", "size": {"w": 1, "h": 1}, "scale": 1}}

    def test_furni_gets_document_class_before_spritesheet(self):
        from swf2hab import nitro
        doc = {"name": "x", "logicType": "furniture_basic", "visualizationType": "furniture_static",
               "assets": {}, "logic": {}, "visualizations": [], "spritesheet": self.sheet()}
        result = nitro.convert(make_nitro({"x.json": json.dumps(doc).encode(), "x.png": TINY_PNG}), "x.nitro")
        bundle = hab.read(result.data)
        self.assertEqual(bundle.name, "x")
        self.assertEqual([e.name for e in bundle.entries], ["x.json", "x.png"])
        out = json.loads(bundle.get("x.json").data)
        keys = list(out)
        self.assertEqual(keys.index("documentClass") + 1, keys.index("spritesheet"))
        self.assertEqual(out["documentClass"], "x")
        self.assertEqual(out["name"], "x")
        self.assertEqual(bundle.get("x.png").data, TINY_PNG)
        self.assertEqual((result.kind, result.frames, result.atlas), ("furniture", 1, (1, 1)))

    def test_library_name_becomes_document_class_and_gzip_is_read(self):
        from swf2hab import nitro
        doc = {"assets": {}, "name": "hh_test", "spritesheet": self.sheet()}
        data = make_nitro({"hh_test.json": json.dumps(doc).encode(), "x.png": TINY_PNG}, gzip_files={"x.png"})
        out = json.loads(hab.read(nitro.convert(data, "hh_test.nitro").data).get("hh_test.json").data)
        self.assertNotIn("name", out)
        self.assertEqual(out["documentClass"], "hh_test")

    def test_truncated_bundle_is_rejected(self):
        from swf2hab import nitro
        data = make_nitro({"x.json": b"{}"})
        with self.assertRaises(nitro.NitroError):
            nitro.read(data[:-3])


class AbcTests(unittest.TestCase):
    def test_library_classes(self):
        from swf2hab.abc import read_abc, library_abc
        code = library_abc("chair", [("manifest", "chair_manifest", "binary"),
                                     ("chair_64_a_0_0", "chair_chair_64_a_0_0", "bitmap"),
                                     ("beep", "chair_beep", "sound")])
        summary = read_abc(code)
        supers = {c.name: c.super_name for c in summary.classes}
        self.assertEqual(supers, {"chair_manifest": "flash.utils::ByteArray",
                                  "chair_chair_64_a_0_0": "flash.display::Bitmap",
                                  "chair_beep": "flash.media::Sound", "chair": "flash.display::Sprite"})
        doc = summary.classes[-1]
        self.assertEqual([n for n, _ in doc.static_traits], ["manifest", "chair_64_a_0_0", "beep"])
        self.assertEqual(summary.scripts, 1)


class ExportTests(unittest.TestCase):
    def roundtrip(self, profile, small="none"):
        from swf2hab import compare, export
        first = convert.convert(make_swf(), profile=profile).data
        swf = export.to_swf(hab.read(first), small=small)
        self.assertEqual(swf.name, "chair")
        second = convert.convert(swf.data, profile=profile).data
        return first, second, compare.compare(first, second), swf

    def test_full_profile_bundle_back_to_swf(self):
        first, second, report, swf = self.roundtrip("full")
        self.assertTrue(report["json_equal"], report["json_diffs"])
        self.assertEqual(report["frames_identical"], report["frames_first"])
        a = read_assets(swf.data)
        self.assertEqual(a.document_class, "chair")
        self.assertEqual(a.bitmaps[[c for c, n in a.symbols if n == "chair_chair_64_a_0_0"][0]].rgba,
                         read_assets(make_swf()).bitmaps[1].rgba)       # pixels bit-identical
        self.assertEqual(a.binary_by_name("chair_index"), read_assets(make_swf()).binary_by_name("chair_index"))

    def test_sulake_bundle_regenerates_xml(self):
        first, second, report, swf = self.roundtrip("sulake")
        self.assertTrue(report["json_equal"], report["json_diffs"])
        self.assertEqual(report["frames_identical"], report["frames_first"])
        names = {n for _, n in read_assets(swf.data).symbols}
        self.assertTrue({"chair_manifest", "chair_index", "chair_chair_assets", "chair_chair_logic",
                         "chair_chair_visualization", "chair"} <= names)

    def test_small_art_is_generated_when_missing(self):
        first, second, report, swf = self.roundtrip("sulake", small="auto")
        self.assertTrue(any("32px" in w for w in swf.warnings))
        full = json.loads(hab.read(convert.convert(swf.data, profile="full").data).get("chair.json").data)
        self.assertEqual(sorted(v["size"] for v in full["visualizations"]), [32, 64])
        self.assertEqual(full["assets"]["chair_32_a_0_0"], {"x": -5, "y": 10})
        self.assertIn("chair_chair_32_a_0_0", full["spritesheet"]["frames"])

    def test_nitro_bundle_round_trip(self):
        from swf2hab import export, nitro
        doc = {"assets": {"h_std_x_1_0_0": {"x": -3, "y": 4}}, "name": "hh_test",
               "spritesheet": NitroTests().sheet()}
        doc["spritesheet"]["frames"] = {"hh_test_h_std_x_1_0_0": doc["spritesheet"]["frames"].pop("x_x_64_a_0_0")}
        doc["spritesheet"]["meta"]["image"] = "hh_test.png"
        original = make_nitro({"hh_test.json": json.dumps(doc).encode(), "hh_test.png": TINY_PNG})
        bundle = hab.read(nitro.convert(original, "hh_test.nitro").data)
        back = nitro.read(export.to_nitro(bundle).data)
        self.assertEqual(list(back), ["hh_test.json", "hh_test.png"])
        self.assertEqual(json.loads(back["hh_test.json"]), doc)
        self.assertEqual(back["hh_test.png"], TINY_PNG)
        # and through a SWF: the figure library's manifest carries the offset
        swf = export.to_swf(bundle)
        manifest = read_assets(swf.data).binary_by_name("hh_test_manifest").decode()
        self.assertIn('<param key="offset" value="-3,4"/>', manifest)

    def test_json_to_xml_is_the_inverse_of_mapping(self):
        from swf2hab import mapping, toxml
        doc = {"name": "lamp", "logicType": "furniture_multistate", "visualizationType": "furniture_animated",
               "assets": {"lamp_64_a_0_0": {"x": -1, "y": None}, "lamp_64_a_2_0": {"source": "lamp_64_a_0_0", "x": 3,
                                                                                 "y": 4, "flipH": True}},
               "logic": {"model": {"dimensions": {"x": 1, "y": 2, "z": 1.5}, "directions": [0, 90]},
                         "action": {"link": "http://x", "startState": 1}, "maskType": "window",
                         "soundSample": {"id": 7, "noPitch": True},
                         "particleSystems": [{"size": 64, "canvasId": 1, "offsetY": 2.5, "emitters": [
                             {"id": 0, "name": "e", "spriteId": 2, "burstPulse": 1,
                              "simulation": {"force": 0.5, "shape": "cone"},
                              "particles": [{"isEmitter": False, "lifeTime": 10, "fade": True, "frames": ["a"]}]}]}],
                         "customVars": {"variables": ["v1"]}},
               "visualizations": [{"angle": 45, "layerCount": 2, "size": 64,
                                   "layers": {"0": {"z": 2, "ink": "ADD", "ignoreMouse": True, "alpha": 128}},
                                   "directions": {"2": {"layers": {"1": {"x": 1, "y": -2}}}},
                                   "colors": {"1": {"layers": {"0": {"color": 16711935}}}},
                                   "animations": {"0": {"transitionTo": 1, "layers": {"0": {"frameRepeat": 2,
                                       "frameSequences": {"0": {"loopCount": 0, "frames": {
                                           "0": {"id": 1, "x": 2, "offsets": {"0": {"direction": 2, "x": 1, "y": 0}}},
                                           "1": {"id": 0}}}}}}},
                                                  "0_1": {"randomStart": True}}}]}
        out = {}
        m = mapping.Mapper("lamp", {}, True)
        m.index(mapping.parse_xml(toxml.index_xml(doc)), out)
        m.assets(mapping.parse_xml(toxml.assets_xml(doc)), out)
        m.logic(mapping.parse_xml(toxml.logic_xml(doc)), out)
        m.visualization(mapping.parse_xml(toxml.visualization_xml(doc)), out)
        self.assertEqual(out, doc)


class CliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="swf2hab-test-")
        self.src = os.path.join(self.tmp, "in")
        os.makedirs(os.path.join(self.src, "sub"))
        with open(os.path.join(self.src, "sub", "chair.swf"), "wb") as fh:
            fh.write(make_swf())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        import contextlib
        import io
        from swf2hab import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args) + ["--no-color"])
        return code, out.getvalue(), err.getvalue()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_convert_every_direction(self):
        code, out, _ = self.run_cli("convert", self.src, "-o", self.path("hab"), "-j", "1")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.path("hab", "sub", "chair.hab")))     # structure mirrored
        self.assertIn("converted", out)
        for target, source in (("swf", "hab"), ("nitro", "hab"), ("swf", "hab2nitro"), ("nitro", "in")):
            code, out, _ = self.run_cli("convert", self.path(source), "-o", self.path(source + "2" + target),
                                        "--to", target)
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.exists(self.path(source + "2" + target, "sub", "chair." + target)), out)
        a = read_assets(open(self.path("hab2nitro2swf", "sub", "chair.swf"), "rb").read())
        self.assertEqual(a.document_class, "chair")

    def test_up_to_date_dry_run_and_json(self):
        self.run_cli("convert", self.src, "-o", self.path("hab"))
        code, out, _ = self.run_cli("convert", self.src, "-o", self.path("hab"), "--json")
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(summary["counts"], {"up-to-date": 1})
        code, out, _ = self.run_cli("convert", self.src, "-o", self.path("dry"), "-n")
        self.assertEqual(code, 0)
        self.assertIn("chair.swf", out)
        self.assertFalse(os.path.exists(self.path("dry")))

    def test_option_checks(self):
        code, _, err = self.run_cli("convert", self.src, "-o", self.path("x"), "--to", "swf", "--profile", "sulake")
        self.assertEqual(code, 1)
        self.assertIn("--profile only applies with --to hab", err)
        code, _, err = self.run_cli("convert", self.src, "-o", self.src)
        self.assertEqual(code, 1)
        self.assertIn("must not be the input folder", err)

    def test_inspect_extract_verify_compare(self):
        swf = self.path("in", "sub", "chair.swf")
        self.run_cli("convert", self.src, "-o", self.path("hab"))
        hab_file = self.path("hab", "sub", "chair.hab")
        code, out, _ = self.run_cli("inspect", swf, "--json")
        info = json.loads(out)
        self.assertEqual((info["format"], info["name"], info["kind"], info["bitmaps"]), ("swf", "chair", "furniture", 1))
        code, out, _ = self.run_cli("inspect", hab_file)
        self.assertIn("furniture", out)
        code, out, _ = self.run_cli("extract", hab_file, swf, "-o", self.path("ex"))
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.path("ex", "chair-hab", "frames", "chair_chair_64_a_0_0.png")))
        self.assertTrue(os.path.exists(self.path("ex", "chair-swf", "chair_64_a_0_0.png")))
        self.assertTrue(os.path.exists(self.path("ex", "chair-swf", "manifest.xml")))
        self.run_cli("convert", self.path("hab"), "-o", self.path("swf"), "--to", "swf")
        code, out, _ = self.run_cli("verify", self.path("hab"), self.path("swf"))   # exports carry ActionScript
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("verify", swf)        # the hand-made test SWF has none: Flash could not load it
        self.assertEqual(code, 1)
        self.assertIn("not defined in the ActionScript", out)
        with open(self.path("broken.hab"), "wb") as fh:
            fh.write(open(hab_file, "rb").read()[:-5])
        code, out, _ = self.run_cli("verify", self.path("broken.hab"))
        self.assertEqual(code, 1)
        code, out, _ = self.run_cli("compare", swf, hab_file)
        self.assertEqual(code, 0, out)
        self.assertIn("identical", out)


class ImageTests(unittest.TestCase):
    def test_pure_png_decoder_reads_our_png_and_pillow_trap(self):
        from swf2hab import images
        px = bytes([0, 0, 0, 0, 0, 0, 0, 10, 255, 0, 0, 255, 0, 0, 0, 255])
        for png in (images.encode_png(px, 4, 1), images._png(4, 1, 6, b"\0" + px, [])):
            self.assertEqual(images._decode_png_pure(png), (4, 1, px))
            self.assertEqual(images.decode_png_rgba(png), (4, 1, px))
        trap = images._png(2, 1, 3, bytes([0, 0, 1]), [images._chunk(b"PLTE", bytes(6)),
                                                         images._chunk(b"tRNS", b"\x00\x0a")])
        self.assertEqual(images.decode_png_rgba(trap)[2], bytes([0, 0, 0, 0, 0, 0, 0, 10]))

    def test_premultiply_inverts_habbo_unpremultiply(self):
        from swf2hab import images
        argb = bytes(v for a in range(256) for p in range(0, a + 1, 7) for v in (a, p, p // 2, 0))
        n = len(argb) // 4
        rgba = images.argb_premultiplied_to_rgba(argb, n, 1)
        self.assertEqual(images.rgba_to_argb_premultiplied(rgba, n, 1), argb)

    def test_rotated_frame(self):
        from swf2hab import images
        # a 3x2 sprite stored turned 90 degrees clockwise (2 wide, 3 high) at (1, 0) in a 3x3 atlas
        sprite = [[1, 2, 3], [4, 5, 6]]
        turned = [[sprite[1][0], sprite[0][0]], [sprite[1][1], sprite[0][1]], [sprite[1][2], sprite[0][2]]]
        atlas = bytearray(3 * 3 * 4)
        for y, row in enumerate(turned):
            for x, v in enumerate(row):
                atlas[(y * 3 + x + 1) * 4:(y * 3 + x + 1) * 4 + 4] = bytes([v, v, v, 255])
        w, h, px = images.cut_frame(bytes(atlas), 3, {"frame": {"x": 1, "y": 0, "w": 3, "h": 2}, "rotated": True})
        self.assertEqual((w, h), (3, 2))
        self.assertEqual([px[i] for i in range(0, len(px), 4)], [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
