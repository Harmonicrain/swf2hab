"""Command line interface: python -m swf2hab <command> ..."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import __version__, compare, convert, hab, images


def _collect(inputs: list[str], excludes: list[str]) -> list[tuple[str, str]]:
    """(absolute path, path relative to its input root) for every .swf to convert."""
    found = []
    for item in inputs:
        if os.path.isdir(item):
            for root, dirs, files in os.walk(item):
                dirs.sort()
                for f in sorted(files):
                    if f.lower().endswith(".swf"):
                        full = os.path.join(root, f)
                        found.append((full, os.path.relpath(full, item)))
        elif os.path.isfile(item):
            found.append((item, os.path.basename(item)))
        else:
            print("not found: %s" % item, file=sys.stderr)
    return [(p, r) for p, r in found if not any(fnmatch.fnmatch(os.path.basename(p), x) or fnmatch.fnmatch(r, x)
                                                for x in excludes)]


def _job(args: tuple) -> dict:
    src, dest, profile, padding, force, layout, max_atlas = args
    rec = {"source": src, "output": dest, "bytes_in": os.path.getsize(src)}
    try:
        if not force and os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src):
            rec.update(status="up-to-date", bytes_out=os.path.getsize(dest))
            return rec
        t = time.perf_counter()
        with open(src, "rb") as fh:
            data = fh.read()
        result = convert.convert(data, profile=profile, source_name=src, padding=padding, layout=layout,
                                 max_atlas=max_atlas)
        rec.update(kind=result.kind, layout=result.layout, document_class=result.document_class, images=result.images,
                   frames=result.frames, atlas=list(result.atlas), warnings=result.warnings)
        if result.data is None:
            rec.update(status="skipped", reason=result.skipped_reason)
        else:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(result.data)
            os.replace(tmp, dest)
            rec.update(status="ok", bytes_out=len(result.data))
        rec["seconds"] = round(time.perf_counter() - t, 3)
    except Exception as exc:
        rec.update(status="failed", error="%s: %s" % (type(exc).__name__, exc))
    return rec


def cmd_convert(ns) -> int:
    files = _collect(ns.inputs, ns.exclude)
    if not files:
        print("no .swf files found", file=sys.stderr)
        return 1
    jobs = []
    for src, rel in files:
        dest = os.path.join(ns.output, os.path.splitext(rel)[0] + ".hab")
        jobs.append((src, dest, ns.profile, ns.padding, ns.force, ns.layout, ns.max_atlas))
    print("swf2hab %s: %d files, profile=%s, jobs=%d, accelerators=%s"
          % (__version__, len(jobs), ns.profile, ns.jobs, images.accelerators()))
    started = time.perf_counter()
    records = []
    counts: dict[str, int] = {}
    if ns.jobs <= 1:
        it = (_job(j) for j in jobs)
        iterator = it
    else:
        pool = ProcessPoolExecutor(max_workers=ns.jobs)
        futures = [pool.submit(_job, j) for j in jobs]
        iterator = (f.result() for f in as_completed(futures))
    for n, rec in enumerate(iterator, 1):
        records.append(rec)
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        if rec["status"] == "failed" or ns.verbose:
            print("[%s] %s %s" % (rec["status"], rec["source"], rec.get("error", rec.get("reason", ""))))
        if n % 500 == 0 or n == len(jobs):
            print("  %d/%d  %s  %.0fs" % (n, len(jobs), counts, time.perf_counter() - started), flush=True)
    if ns.jobs > 1:
        pool.shutdown()
    bytes_in = sum(r["bytes_in"] for r in records if r["status"] in ("ok", "up-to-date"))
    bytes_out = sum(r.get("bytes_out", 0) for r in records if r["status"] in ("ok", "up-to-date"))
    summary = {"files": len(records), "counts": counts, "swf_bytes": bytes_in, "hab_bytes": bytes_out,
               "ratio": round(bytes_out / bytes_in, 4) if bytes_in else None,
               "seconds": round(time.perf_counter() - started, 1), "profile": ns.profile, "version": __version__}
    print(json.dumps(summary))
    if ns.report:
        os.makedirs(os.path.dirname(os.path.abspath(ns.report)), exist_ok=True)
        with open(ns.report, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "files": sorted(records, key=lambda r: r["source"])}, fh, indent=1)
    return 0 if counts.get("failed", 0) == 0 else 2


def cmd_inspect(ns) -> int:
    for path in ns.files:
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] == hab.MAGIC:
            b = hab.read(data)
            print("%s: hab name=%s entries=%d meta=%s" % (path, b.name, len(b.entries), sorted(b.metadata)))
            for e in b.entries:
                print("  %-48s %-28s %9d" % (e.name, e.mime, len(e.data)))
        else:
            from .swf import TAG_NAMES, read_assets
            a = read_assets(data)
            print("%s: swf v%d doc=%s symbols=%d bitmaps=%d binaries=%d sounds=%d abc=%dB"
                  % (path, a.version, a.document_class, len(a.symbols), len(a.bitmaps), len(a.binaries),
                     len(a.sounds), a.abc_bytes))
            print("  tags:", {TAG_NAMES.get(k, k): v for k, v in sorted(a.tag_counts.items())})
            for w in a.warnings:
                print("  warning:", w)
    return 0


def cmd_unpack(ns) -> int:
    with open(ns.file, "rb") as fh:
        b = hab.read(fh.read())
    os.makedirs(ns.output, exist_ok=True)
    for e in b.entries:
        target = os.path.join(ns.output, e.name.replace("/", os.sep))
        os.makedirs(os.path.dirname(target) or ns.output, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(e.data)
    with open(os.path.join(ns.output, "_index.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": b.name, **b.metadata,
                   "entries": [{"name": e.name, "mimeType": e.mime, "length": len(e.data)} for e in b.entries]},
                  fh, indent=1)
    print("%d entries -> %s" % (len(b.entries), ns.output))
    return 0


def cmd_verify(ns) -> int:
    bad = total = 0
    for item in ns.paths:
        paths = [item] if os.path.isfile(item) else [os.path.join(r, f) for r, _, fs in os.walk(item)
                                                     for f in fs if f.endswith(".hab")]
        for p in paths:
            total += 1
            try:
                with open(p, "rb") as fh:
                    hab.read(fh.read())
            except Exception as exc:
                bad += 1
                print("INVALID %s: %s" % (p, exc))
    print("%d checked, %d invalid" % (total, bad))
    return 1 if bad else 0


def cmd_compare(ns) -> int:
    with open(ns.first, "rb") as a, open(ns.second, "rb") as b:
        print(json.dumps(compare.compare(a.read(), b.read()), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swf2hab", description="Convert Habbo .swf asset libraries to .hab bundles.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("convert", help="convert .swf files or folders (folder structure is mirrored)")
    c.add_argument("inputs", nargs="+")
    c.add_argument("-o", "--output", required=True, help="output folder (never the input folder)")
    c.add_argument("--profile", choices=convert.PROFILES, default="full",
                   help="full: everything incl. 32px assets and raw XML (default); "
                        "sulake: exactly what Habbo's CDN ships (64px + icons, JSON + atlas)")
    c.add_argument("--layout", choices=convert.LAYOUTS, default="auto",
                   help="atlas: JSON + one spritesheet (furni, pets, figures, effects); "
                        "library: manifest + one entry per symbol (room content, UI libraries); "
                        "auto picks per file (default)")
    c.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    c.add_argument("--exclude", action="append", default=[], help="glob on file name or relative path; repeatable")
    c.add_argument("--padding", type=int, default=0, help="pixels between atlas frames")
    c.add_argument("--max-atlas", type=int, default=8192,
                   help="largest atlas side in px; bigger libraries are split over several PNGs")
    c.add_argument("--force", action="store_true", help="reconvert even if the .hab is newer than the .swf")
    c.add_argument("--report", help="write a JSON report of every file")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(fn=cmd_convert)

    i = sub.add_parser("inspect", help="describe a .swf or .hab")
    i.add_argument("files", nargs="+")
    i.set_defaults(fn=cmd_inspect)

    u = sub.add_parser("unpack", help="extract a .hab into a folder")
    u.add_argument("file")
    u.add_argument("-o", "--output", required=True)
    u.set_defaults(fn=cmd_unpack)

    v = sub.add_parser("verify", help="validate .hab files the way the client does")
    v.add_argument("paths", nargs="+")
    v.set_defaults(fn=cmd_verify)

    k = sub.add_parser("compare", help="compare two .hab bundles (JSON and frame pixels)")
    k.add_argument("first")
    k.add_argument("second")
    k.set_defaults(fn=cmd_compare)

    ns = p.parse_args(argv)
    if ns.command == "convert":
        out = os.path.abspath(ns.output)
        for item in ns.inputs:
            src = os.path.abspath(item)
            if os.path.isdir(src) and (out == src):
                p.error("output folder must not be the input folder")
    return ns.fn(ns)
