"""Build a benchmark sample for bench.html from swf2hab convert reports.

    python tools/bench/make_sample.py <report.json> [<report.json> ...] -o <bench dir> [--n 300]

Writes <bench dir>/{swf,full,sulake}/ and files.json. Serve that folder together with
bench.html (and optionally skyinflate.js, SkyHaxe's Haxe inflater compiled from SkyInflate.hx),
e.g. `python -m http.server 8799`, and open bench.html in a browser.
"""
import argparse
import json
import os
import random
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from swf2hab import convert  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("reports", nargs="+")
p.add_argument("-o", "--output", required=True)
p.add_argument("--n", type=int, default=300, help="furni count; figures/effects/pets scale from it")
p.add_argument("--seed", type=int, default=42)
ns = p.parse_args()

files = [f for r in ns.reports for f in json.load(open(r))["files"] if f["status"] == "ok"]
files = [f for f in files if open(f["source"], "rb").read(3) == b"CWS"]
rnd = random.Random(ns.seed)
pools = {"furni": [f for f in files if f.get("kind") == "furniture" and f.get("layout") == "atlas"],
         "figure": [f for f in files if f.get("kind") == "library"],
         "effect": [f for f in files if f.get("kind") == "effect"]}
want = {"furni": ns.n, "figure": ns.n // 5, "effect": ns.n // 15}
for d in ("swf", "full", "sulake"):
    os.makedirs(os.path.join(ns.output, d), exist_ok=True)
manifest, seen = [], set()
for cat, pool in pools.items():
    for f in rnd.sample(pool, min(want[cat], len(pool))):
        name = os.path.splitext(os.path.basename(f["source"]))[0]
        if name in seen:
            continue
        seen.add(name)
        data = open(f["source"], "rb").read()
        shutil.copy(f["source"], os.path.join(ns.output, "swf", name + ".swf"))
        for profile in ("full", "sulake"):
            out = convert.convert(data, profile=profile, source_name=f["source"]).data
            open(os.path.join(ns.output, profile, name + ".hab"), "wb").write(out)
        manifest.append({"name": name, "cat": cat})
json.dump(manifest, open(os.path.join(ns.output, "files.json"), "w"))
for extra in ("bench.html", "skyinflate.js"):
    src = os.path.join(os.path.dirname(__file__), extra)
    if os.path.exists(src):
        shutil.copy(src, ns.output)
print("%d libraries -> %s" % (len(manifest), ns.output))
