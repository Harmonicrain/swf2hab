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


if __name__ == "__main__":
    unittest.main()
