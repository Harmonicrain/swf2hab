"""Convert Sulake's own .swf files and compare against the .hab files Sulake serves.

Usage: python tools/oracle_check.py <folder with NAME.swf + NAME.hab pairs> [...]
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from swf2hab import compare, convert  # noqa: E402


def main(folders):
    totals = collections.Counter()
    sizes = collections.Counter()
    problems = []
    for folder in folders:
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".swf"):
                continue
            stem = f[:-4]
            hab_path = os.path.join(folder, stem + ".hab")
            if not os.path.exists(hab_path):
                continue
            swf = open(os.path.join(folder, f), "rb").read()
            theirs = open(hab_path, "rb").read()
            try:
                ours = convert.convert(swf, profile="sulake", source_name=f)
            except Exception as exc:
                problems.append((stem, "convert failed: %r" % exc))
                totals["failed"] += 1
                continue
            rep = compare.compare(ours.data, theirs)
            totals["files"] += 1
            totals["json_equal"] += rep["json_equal"]
            for k in ("frames_identical", "frames_invisible_diff", "frames_visible_diff", "frames_size_diff"):
                totals[k] += rep[k]
            totals["frames_missing_ours"] += len(rep["frames_only_second"])
            totals["frames_extra_ours"] += len(rep["frames_only_first"])
            sizes["swf"] += len(swf)
            sizes["sulake_hab"] += len(theirs)
            sizes["our_hab"] += len(ours.data)
            if not rep["json_equal"] or rep["frames_visible_diff"] or rep["frames_size_diff"] or \
                    rep["frames_only_first"] or rep["frames_only_second"] or rep["entries_only_first"] or \
                    rep["entries_only_second"]:
                problems.append((stem, {k: v for k, v in rep.items() if v and k not in
                                        ("frames_first", "frames_second", "frames_identical", "json_equal")}))
    print(json.dumps({"totals": totals, "bytes": sizes}, indent=1))
    for stem, p in problems[:40]:
        print("--", stem, json.dumps(p)[:600])


if __name__ == "__main__":
    main(sys.argv[1:])
