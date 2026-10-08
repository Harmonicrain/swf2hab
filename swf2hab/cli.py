"""Command line interface: swf2hab <command> ...   (or python -m swf2hab <command> ...)"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import __version__, compare, convert, export, hab, images, toolkit
from .ui import UI, human_bytes, human_time, plural

TARGET_HELP = {"hab": "Habbo HTML5 bundle", "swf": "Flash asset library for Habbo's AS3 client",
               "nitro": "Nitro bundle"}

CONVERSIONS = """\
conversions (each input's format is read from its content):
  .swf   -> .hab     swf2hab convert IN -o OUT
  .nitro -> .hab     swf2hab convert IN -o OUT
  .hab   -> .swf     swf2hab convert IN -o OUT --to swf
  .nitro -> .swf     swf2hab convert IN -o OUT --to swf
  .hab   -> .nitro   swf2hab convert IN -o OUT --to nitro
  .swf   -> .nitro   swf2hab convert IN -o OUT --to nitro
"""

EPILOG = CONVERSIONS + """
examples:
  swf2hab convert dcr/hof_furni -o hab/hof_furni       a whole folder, structure mirrored
  swf2hab convert hab/ -o swf/ --to swf                 back to Flash libraries
  swf2hab inspect throne.swf throne.hab                 what is inside
  swf2hab wizard                                        step by step, nothing to remember

Run "swf2hab <command> -h" for a command's options.
"""


def _workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def _files(inputs: list[str], suffixes: tuple, excludes: list[str], ui: UI) -> list[tuple[str, str, str]]:
    """(path, path relative to its input, input root) for each matching file under the inputs."""
    found = []
    for item in inputs:
        item = item.strip('"')
        if os.path.isdir(item):
            for root, dirs, names in os.walk(item):
                dirs.sort()
                for n in sorted(names):
                    if n.lower().endswith(suffixes):
                        full = os.path.join(root, n)
                        found.append((full, os.path.relpath(full, item), item))
        elif os.path.isfile(item):
            found.append((item, os.path.basename(item), os.path.dirname(item) or "."))
        else:
            ui.error("not found: %s" % item)
    return [f for f in found if not any(fnmatch.fnmatch(os.path.basename(f[0]), x) or fnmatch.fnmatch(f[1], x)
                                        for x in excludes)]


def _suffix_counts(paths) -> str:
    counts = Counter(os.path.splitext(p)[1].lower() or "(no extension)" for p in paths)
    return ", ".join("%s %s" % (format(n, ","), ext) for ext, n in counts.most_common())


# ---------------------------------------------------------------------------------------- convert

def cmd_convert(ns, ui: UI) -> int:
    target = ns.to
    misplaced = [flag for flag, value, wants in (("--profile", ns.profile, "hab"), ("--layout", ns.layout, "hab"),
                                                 ("--padding", ns.padding, "hab"), ("--max-atlas", ns.max_atlas, "hab"),
                                                 ("--small", ns.small, "swf"))
                 if value is not None and target != wants]
    if misplaced:
        ui.error("%s only %s with --to %s" % (", ".join(misplaced), "applies" if len(misplaced) == 1 else "apply",
                                             "hab" if misplaced[0] != "--small" else "swf"))
        return 1
    suffixes = tuple(s for s in toolkit.SUFFIXES if s != "." + target)
    files = _files(ns.inputs, suffixes, ns.exclude, ui)
    if not files:
        ui.error("nothing to convert: no %s files in %s" % ("/".join(suffixes), ", ".join(ns.inputs)))
        return 1
    out_root = os.path.abspath(ns.output)
    for item in ns.inputs:
        if os.path.isdir(item) and os.path.abspath(item) == out_root:
            ui.error("the output folder must not be the input folder")
            return 1

    jobs, taken, clashes, shown = [], {}, [], {}
    for src, rel, _ in files:
        rel_dest = os.path.normpath(os.path.splitext(rel)[0] + "." + target)
        dest = os.path.join(ns.output, rel_dest)
        key = os.path.normcase(os.path.abspath(dest))
        if key == os.path.normcase(os.path.abspath(src)) or key in taken:
            clashes.append((os.path.normpath(rel), taken.get(key, src)))
            continue
        taken[key] = src
        shown[src] = os.path.normpath(rel)
        jobs.append((src, dest))
    options = {"profile": ns.profile, "layout": ns.layout, "padding": ns.padding, "max_atlas": ns.max_atlas,
               "small": ns.small}

    accel = [k for k, v in images.accelerators().items() if v]
    workers = 1 if len(jobs) < 4 else max(1, ns.jobs)
    ui.line("%s %s %s %s to %s %s %s" % (
        ui.c("swf2hab " + __version__, "head"), ui.sym("dot"),
        plural(len(jobs), "file"), ui.c("(%s)" % _suffix_counts(s for s, _ in jobs), "dim"),
        ui.c("." + target, "bold"), ui.sym("dot"),
        ui.c("%s%s" % (plural(workers, "worker"), (", " + "+".join(accel)) if accel else ", standard library only"),
             "dim")))
    for src, other in clashes:
        ui.line(ui.c("  %s %s: same output as %s, left out" % (ui.sym("skip"), src, shown.get(other, other)), "warn"))

    if ns.dry_run:
        ui.field("from", ", ".join(os.path.abspath(i.strip('"')) for i in ns.inputs))
        ui.field("into", out_root)
        rows = [(shown[src], ui.sym("arrow"), os.path.relpath(dest, ns.output)) for src, dest in jobs]
        ui.table(rows if ns.verbose or len(rows) <= 40 else rows[:40])
        if len(rows) > 40 and not ns.verbose:
            ui.line(ui.c("  ... and %s more (use -v to list all)" % format(len(rows) - 40, ","), "dim"))
        ui.line("dry run: nothing written.")
        return 0

    started = time.perf_counter()
    records: list[dict] = []
    counts: Counter = Counter()
    pool = None
    try:
        if workers == 1:
            results = (toolkit.convert_file(src, dest, target, options, ns.force) for src, dest in jobs)
        else:
            pool = ProcessPoolExecutor(max_workers=workers)
            futures = [pool.submit(toolkit.convert_file, src, dest, target, options, ns.force) for src, dest in jobs]
            results = (f.result() for f in as_completed(futures))
        for rec in results:
            records.append(rec)
            counts[rec["status"]] += 1
            _report_file(ui, rec, ns.verbose, shown.get(rec["source"], rec["source"]))
            ui.progress(len(records), len(jobs), counts, started)
    except KeyboardInterrupt:
        ui.end_progress()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        ui.error("interrupted after %s; finished files are kept" % plural(len(records), "file"))
        return 130
    finally:
        if pool is not None:
            pool.shutdown()
    ui.end_progress()
    elapsed = time.perf_counter() - started
    summary = _summary(records, counts, target, elapsed, ns)
    if ns.json:
        print(json.dumps(summary))
    else:
        _print_summary(ui, records, counts, summary, ns, elapsed)
    if ns.report:
        os.makedirs(os.path.dirname(os.path.abspath(ns.report)) or ".", exist_ok=True)
        with open(ns.report, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "files": sorted(records, key=lambda r: r["source"])}, fh, indent=1)
    return 2 if counts["failed"] else 0


def _report_file(ui: UI, rec: dict, verbose: bool, name: str) -> None:
    status = rec["status"]
    if status == "failed":
        ui.line("  %s %s  %s" % (ui.c(ui.sym("fail"), "err"), name, ui.c(rec.get("error", ""), "err")),
                force=True)
    elif verbose:
        mark = {"ok": ui.c(ui.sym("ok"), "ok"), "up-to-date": ui.c("=", "dim"),
                "skipped": ui.c(ui.sym("skip"), "warn")}.get(status, status)
        detail = rec.get("reason") or ("%s %s %s" % (human_bytes(rec["bytes_in"]), ui.sym("arrow"),
                                                    human_bytes(rec.get("bytes_out", 0))))
        ui.line("  %s %s  %s" % (mark, name, ui.c(detail, "dim")))
        for w in rec.get("warnings") or []:
            ui.line("      %s %s" % (ui.c(ui.sym("warn"), "warn"), ui.c(w, "dim")))


def _summary(records: list[dict], counts: Counter, target: str, elapsed: float, ns) -> dict:
    done = [r for r in records if r["status"] in ("ok", "up-to-date")]
    bytes_in = sum(r["bytes_in"] for r in done)
    bytes_out = sum(r.get("bytes_out", 0) for r in done)
    return {"files": len(records), "counts": dict(counts), "target": target, "bytes_in": bytes_in,
            "bytes_out": bytes_out, "ratio": round(bytes_out / bytes_in, 4) if bytes_in else None,
            "seconds": round(elapsed, 1), "output": os.path.abspath(ns.output), "version": __version__,
            "skipped_reasons": dict(Counter(r.get("reason") or "?" for r in records if r["status"] == "skipped")),
            "files_with_warnings": sum(1 for r in records if r.get("warnings"))}


def _print_summary(ui: UI, records: list[dict], counts: Counter, s: dict, ns, elapsed: float) -> None:
    rate = s["files"] / elapsed if elapsed > 0 else 0
    failed = counts["failed"]
    head = ui.c(ui.sym("fail") + " Finished with failures", "err") if failed else ui.c(ui.sym("ok") + " Done", "ok")
    ui.line("%s in %s %s %s" % (head, human_time(elapsed), ui.sym("dot"),
                                ("%.0f files/s" % rate) if rate >= 10 else ("%.1f files/s" % rate)), force=True)
    rows = [("converted", counts["ok"], "")]
    if counts["up-to-date"]:
        rows.append(("up to date", counts["up-to-date"], "newer than the input; --force redoes them"))
    if counts["skipped"]:
        reasons = ", ".join("%d %s %s" % (n, ui.sym("times"), r) for r, n in
                            sorted(s["skipped_reasons"].items(), key=lambda kv: -kv[1])[:3])
        rows.append(("skipped", counts["skipped"], reasons))
    if failed:
        rows.append(("failed", failed, "listed above"))
    styles = {"converted": "ok", "up to date": "dim", "skipped": "warn", "failed": "err"}
    width = max(len(format(n, ",")) for _, n, _ in rows)
    for label, n, note in rows:
        ui.line("  %s %s  %s" % (ui.c(label.ljust(11), styles[label]), format(n, ",").rjust(width), ui.c(note, "dim")),
                force=True)
    if s["bytes_in"]:
        ui.line("  %s %s %s %s  %s" % (ui.c("size".ljust(11), "dim"), human_bytes(s["bytes_in"]), ui.sym("arrow"),
                                       human_bytes(s["bytes_out"]), ui.c("(%.0f%%)" % (100 * s["ratio"]), "dim")),
                force=True)
    if s["files_with_warnings"] and not ns.verbose:
        ui.line("  %s %s  %s" % (ui.c("warnings".ljust(11), "dim"), format(s["files_with_warnings"], ","),
                                 ui.c("files; -v or --report lists them", "dim")), force=True)
    ui.line("  %s %s" % (ui.c("output".ljust(11), "dim"), s["output"]), force=True)
    if ns.report:
        ui.line("  %s %s" % (ui.c("report".ljust(11), "dim"), os.path.abspath(ns.report)), force=True)


# ---------------------------------------------------------------------------------------- inspect

def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def cmd_inspect(ns, ui: UI) -> int:
    results, status = [], 0
    for path, _, _ in _files(ns.files, toolkit.SUFFIXES, [], ui):
        try:
            results.append(toolkit.describe(_read(path), path))
        except Exception as exc:
            results.append({"file": path, "error": "%s: %s" % (type(exc).__name__, exc)})
            status = 1
    if ns.json:
        print(json.dumps(results if len(results) != 1 else results[0], indent=1))
        return status
    for info in results:
        _print_description(ui, info, ns.verbose)
    return status


def _print_description(ui: UI, info: dict, verbose: bool) -> None:
    if "error" in info:
        ui.line("%s  %s" % (ui.c(info["file"], "bold"), ui.c(info["error"], "err")))
        return
    fmt = info["format"]
    ui.line("%s  %s" % (ui.c(info["file"], "bold"),
                        ui.c("%s, %s" % (toolkit.DESCRIPTIONS[fmt], human_bytes(info["bytes"])), "dim")))
    doc = info.get("document")
    if fmt == "swf":
        ui.field("library", "%s%s" % (info["name"] or "?", ui.c("  extends " + info["extends"], "dim")
                                      if info.get("extends") else ""))
        ui.field("kind", info["kind"] + (ui.c("  (%s)" % ", ".join(info["xml"]), "dim") if info["xml"] else ""))
        ui.field("contents", "%s %s %s %s %s %s %s" % (
            plural(info["bitmaps"], "bitmap"), ui.sym("dot"), plural(info["binaries"], "XML/binary", "XML/binary"),
            ui.sym("dot"), plural(info["sounds"], "sound"), ui.sym("dot"), plural(info["symbols"], "symbol")))
        ui.field("swf", "version %d, %s, %s" % (info["version"], {"none": "uncompressed", "zlib": "zlib-compressed",
                                                                  "lzma": "LZMA-compressed"}[info["compression"]],
                                                 plural(info["classes"], "class", "classes")))
        if info["vector_tags"]:
            ui.field("not carried", plural(info["vector_tags"], "vector/timeline/text tag") + " (a .hab cannot hold them)")
        if verbose:
            ui.field("tags", ", ".join("%s %s%d" % (k, ui.sym("times"), v) for k, v in info["tags"].items()))
        for w in info["warnings"]:
            ui.field("warning", ui.c(w, "warn"))
    else:
        if fmt == "hab":
            ui.field("name", "%s%s" % (info["name"], ui.c("  %s layout%s" % (
                info["layout"], ", %s profile" % info["profile"] if info.get("profile") else ""), "dim")))
        else:
            ui.field("name", info.get("name") or "?")
        if doc:
            kind = doc["kind"]
            if doc.get("logic") or doc.get("visualization"):
                kind += ui.c("  (%s / %s)" % (doc.get("logic"), doc.get("visualization")), "dim")
            ui.field("kind", kind)
            extra = "".join(" %s %s" % (ui.sym("dot"), plural(doc[k], k[:-1], k)) for k in ("animations", "palettes",
                                                                                          "aliases") if doc.get(k))
            ui.field("contents", "%s %s %s%s" % (plural(doc["assets"], "asset"), ui.sym("dot"),
                                                 plural(doc["frames"], "frame"), extra))
            if doc["sizes"]:
                ui.field("sizes", ", ".join(str(s) for s in doc["sizes"]) + ui.c(
                    "" if 32 in doc["sizes"] or doc["kind"] not in ("furniture", "pet") else
                    "  (no 32px art; --to swf generates it)", "dim"))
            if doc["atlases"]:
                ui.field("atlas", ", ".join("%s %sx%s" % (a["image"], a["w"], a["h"]) for a in doc["atlases"]))
        if fmt == "hab" and not doc:
            ui.field("contents", plural(len(info["entries"]), "entry", "entries")
                     + (" %s %s" % (ui.sym("dot"), plural(info["aliases"], "alias", "aliases")) if info.get("aliases")
                        else ""))
        if verbose:
            rows = [(e["name"], e.get("mimeType", ""), human_bytes(e["bytes"]))
                    for e in info.get("entries") or info.get("files") or []]
            ui.table(rows, indent=4, styles=(None, "dim", "dim"))
    ui.line()


# ---------------------------------------------------------------------------------------- extract

def cmd_extract(ns, ui: UI) -> int:
    files = _files(ns.files, toolkit.SUFFIXES, [], ui)
    if not files:
        ui.error("nothing to extract")
        return 1
    status = 0
    stems = Counter(os.path.splitext(os.path.basename(p))[0].lower() for p, _, _ in files)
    for path, _, _ in files:
        stem, ext = os.path.splitext(os.path.basename(path))
        folder = stem if stems[stem.lower()] == 1 else stem + "-" + ext.lstrip(".").lower()
        out = ns.output if len(files) == 1 else os.path.join(ns.output, folder)
        try:
            written = toolkit.extract(_read(path), path, out, frames=not ns.no_frames)
            frames = sum(1 for w in written if os.sep + "frames" + os.sep in w)
            ui.line("%s %s %s %s  %s" % (ui.c(ui.sym("ok"), "ok"), path, ui.sym("arrow"), out,
                                         ui.c(plural(len(written) - frames, "file") +
                                              (", %s" % plural(frames, "frame")) if frames else
                                              plural(len(written), "file"), "dim")))
        except Exception as exc:
            status = 1
            ui.line("%s %s  %s" % (ui.c(ui.sym("fail"), "err"), path, ui.c("%s: %s" % (type(exc).__name__, exc), "err")))
    return status


# ---------------------------------------------------------------------------------------- verify

def cmd_verify(ns, ui: UI) -> int:
    files = _files(ns.paths, toolkit.SUFFIXES, ns.exclude, ui)
    if not files:
        ui.error("nothing to verify")
        return 1
    started = time.perf_counter()
    counts: Counter = Counter()
    results = []
    for n, (path, _, _) in enumerate(files, 1):
        try:
            problems = toolkit.verify(_read(path), path)
        except Exception as exc:
            problems = [("error", "%s: %s" % (type(exc).__name__, exc))]
        errors = [m for s, m in problems if s == "error"]
        warnings = [m for s, m in problems if s == "warning"]
        state = "invalid" if errors else "warning" if warnings else "ok"
        counts[state] += 1
        results.append({"file": path, "status": state, "errors": errors, "warnings": warnings})
        if not ns.json and (errors or (warnings and ns.verbose) or (ns.verbose and state == "ok")):
            mark = {"invalid": ui.c(ui.sym("fail"), "err"), "warning": ui.c(ui.sym("warn"), "warn"),
                    "ok": ui.c(ui.sym("ok"), "ok")}[state]
            ui.line("  %s %s" % (mark, path))
            for m in errors:
                ui.line("      " + ui.c(m, "err"))
            for m in warnings if ns.verbose else []:
                ui.line("      " + ui.c(m, "warn"))
        if not ns.json:
            ui.progress(n, len(files), {"valid": counts["ok"], "warnings": counts["warning"],
                                        "invalid": counts["invalid"]}, started)
    ui.end_progress()
    if ns.json:
        print(json.dumps(results if len(results) != 1 else results[0], indent=1))
    else:
        head = ui.c(ui.sym("fail") + " Invalid files found", "err") if counts["invalid"] else \
            ui.c(ui.sym("ok") + " All files load", "ok")
        ui.line("%s %s %s checked: %s valid, %s with warnings, %s invalid" % (
            head, ui.sym("dot"), format(len(files), ","), format(counts["ok"], ","), format(counts["warning"], ","),
            format(counts["invalid"], ",")), force=True)
        if counts["warning"] and not ns.verbose:
            ui.line(ui.c("  -v shows the warnings", "dim"))
    return 1 if counts["invalid"] else 0


# ---------------------------------------------------------------------------------------- compare

def cmd_compare(ns, ui: UI) -> int:
    datas = [_read(ns.first), _read(ns.second)]
    profile = ns.profile
    if profile is None:
        # A .nitro, or a .hab without the raw library, holds what the sulake profile keeps; read a
        # .swf on the other side the same way so 32px art does not show up as a difference.
        compact = False
        for data, path in zip(datas, (ns.first, ns.second)):
            kind = toolkit.detect(data, path)
            if kind == "nitro":
                compact = True
            elif kind == "hab":
                b = hab.read(data)
                entry, doc = export._document(b)
                compact |= doc is not None and not export._full_profile(b, entry)
        profile = "sulake" if compact else "full"
    bundles = []
    for data, path in zip(datas, (ns.first, ns.second)):
        bundle, _ = export.load(data, path, profile=profile)
        bundles.append(hab.write(bundle))
    report = compare.compare(*bundles)
    same = (report["json_equal"] and report["frames_first"] == report["frames_second"] == report["frames_identical"])
    if ns.json:
        print(json.dumps(report, indent=1))
        return 0 if same else 1
    ui.line("%s  %s  %s" % (ui.c(ns.first, "bold"), ui.c("vs", "dim"), ui.c(ns.second, "bold")))
    if any(toolkit.detect(d, p) == "swf" for d, p in zip(datas, (ns.first, ns.second))):
        ui.field("swf read as", "%s profile%s" % (profile, ui.c("" if ns.profile else " (to match the other file)",
                                                               "dim")))
    if report["json_equal"]:
        ui.field("JSON", ui.c("identical", "ok"))
    else:
        ui.field("JSON", ui.c(plural(len(report["json_diffs"]), "difference"), "err")
                 + ui.c(" (first 20)" if len(report["json_diffs"]) >= 20 else "", "dim"))
        for d in report["json_diffs"]:
            ui.line("      " + d)
    frames = "%s / %s identical" % (format(report["frames_identical"], ","), format(report["frames_first"], ","))
    if report["frames_first"] != report["frames_second"]:
        frames += " (second has %s)" % format(report["frames_second"], ",")
    ui.field("frames", ui.c(frames, "ok" if report["frames_identical"] == report["frames_first"] ==
                            report["frames_second"] else "warn"))
    for key, label in (("frames_visible_diff", "visible pixel differences"),
                       ("frames_invisible_diff", "differences in fully transparent pixels only"),
                       ("frames_size_diff", "different sizes")):
        if report[key]:
            ui.field("", "%s with %s" % (plural(report[key], "frame"), label))
    for key, label in (("frames_only_first", "only in first"), ("frames_only_second", "only in second")):
        if report[key]:
            ui.field("", "frames %s: %s" % (label, ", ".join(report[key][:5]) + (" ..." if len(report[key]) > 5 else "")))
    ui.line(ui.c(ui.sym("ok") + " Same assets", "ok") if same else ui.c(ui.sym("fail") + " Different", "warn"))
    return 0 if same else 1


# ---------------------------------------------------------------------------------------- info

def cmd_info(ns, ui: UI) -> int:
    accel = images.accelerators()
    ui.line(ui.c("swf2hab " + __version__, "head") + ui.c("  Habbo asset converter: .swf, .hab and .nitro", "dim"))
    ui.field("python", "%s (%s)" % (platform.python_version(), platform.platform(terse=True)))
    for name, present in accel.items():
        ui.field(name, ui.c("installed", "ok") if present else ui.c("not installed (optional)", "dim"))
    if not all(accel.values()):
        ui.line(ui.c("  pip install numpy Pillow  makes conversion faster and PNGs smaller; output pixels match", "dim"))
    ui.field("workers", "%d by default (-j to change)" % _workers())
    ui.line()
    for line in CONVERSIONS.splitlines():
        ui.line(line)
    ui.line()
    ui.line("formats:")
    for fmt in toolkit.FORMATS:
        ui.line("  %-7s %s" % ("." + fmt, toolkit.DESCRIPTIONS[fmt]))
    return 0


# ---------------------------------------------------------------------------------------- wizard

def _ask(ui: UI, question: str, default: str | None = None, choices: list[str] | None = None) -> str:
    hint = " [%s]" % default if default else ""
    while True:
        try:
            answer = input("%s%s: " % (ui.c(question, "bold"), ui.c(hint, "dim"))).strip().strip('"').strip("'")
        except EOFError:
            raise KeyboardInterrupt
        answer = answer or (default or "")
        if not answer:
            continue
        if choices and answer not in choices:
            ui.line(ui.c("  please answer %s" % " / ".join(choices), "warn"))
            continue
        return answer


def cmd_wizard(ns, ui: UI) -> int:
    ui.line(ui.c("swf2hab %s %s step-by-step conversion" % (__version__, ui.sym("dot")), "head"))
    ui.line(ui.c("Press Enter to take the [default]. Ctrl+C stops.", "dim"))
    ui.line()
    try:
        while True:
            source = _ask(ui, "File or folder to convert (you can drag it in here)")
            if os.path.exists(source):
                break
            ui.line(ui.c("  not found: %s" % source, "warn"))
        found = _files([source], toolkit.SUFFIXES, [], ui)
        by_ext = Counter(os.path.splitext(p)[1].lower() for p, _, _ in found)
        if not found:
            ui.line(ui.c("  no .swf, .hab or .nitro files there", "warn"))
            return 1
        ui.line("  found %s" % _suffix_counts(p for p, _, _ in found))
        ui.line()
        ui.line("Convert to:")
        options = list(toolkit.FORMATS)
        default = next((str(i + 1) for i, f in enumerate(options) if by_ext.get("." + f, 0) < len(found)), "1")
        for i, f in enumerate(options, 1):
            ui.line("  %d) .%-6s %s" % (i, f, ui.c(TARGET_HELP[f], "dim")))
        target = options[int(_ask(ui, "Choice", default, ["1", "2", "3"])) - 1]
        todo = [p for p, _, _ in found if not p.lower().endswith("." + target)]
        if not todo:
            ui.line(ui.c("  everything there is already a .%s" % target, "warn"))
            return 1
        if len(todo) < len(found):
            ui.line(ui.c("  %s already .%s, left as they are" % (plural(len(found) - len(todo), "file"), target),
                         "dim"))
        argv = ["convert", source, "--to", target]
        if target == "hab" and any(p.lower().endswith(".swf") for p in todo):
            ui.line()
            ui.line("Profile for .swf files:")
            ui.line("  1) full    %s" % ui.c("everything, including 32px art and raw XML (for AS3-based clients)", "dim"))
            ui.line("  2) sulake  %s" % ui.c("what Habbo's own CDN ships", "dim"))
            argv += ["--profile", ("full", "sulake")[int(_ask(ui, "Choice", "1", ["1", "2"])) - 1]]
        if target == "swf":
            ui.line()
            small = _ask(ui, "Generate 32px art where a bundle has none? (y/n)", "y", ["y", "n", "Y", "N"]).lower()
            argv += ["--small", "auto" if small == "y" else "none"]
        base = source.rstrip("/\\")
        suggested = (os.path.splitext(base)[0] if os.path.isfile(base) else base) + "-" + target
        ui.line()
        output = _ask(ui, "Output folder", suggested)
        argv += ["-o", output]
        ui.line()
        go = _ask(ui, "Convert %s to .%s in %s? (y/n)" % (plural(len(todo), "file"), target, output), "y",
                  ["y", "n", "Y", "N"]).lower()
        if go != "y":
            ui.line("Nothing done.")
            return 0
    except KeyboardInterrupt:
        ui.line()
        ui.line("Stopped.")
        return 130
    ui.line()
    ui.line(ui.c("same as: swf2hab " + " ".join('"%s"' % a if " " in a else a for a in argv), "dim"))
    return main(argv + (["--no-color"] if not ui.color else []))


# ---------------------------------------------------------------------------------------- parser

class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=100)


def _common(p: argparse.ArgumentParser, verbose: bool = True, quiet: bool = False, as_json: bool = False) -> None:
    g = p.add_argument_group("output")
    if verbose:
        g.add_argument("-v", "--verbose", action="store_true", help="list every file and its warnings")
    if quiet:
        g.add_argument("-q", "--quiet", action="store_true", help="only the summary and failures")
    if as_json:
        g.add_argument("--json", action="store_true", help="machine-readable JSON instead of text")
    g.add_argument("--no-color", action="store_true", help="plain text, no colour")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swf2hab", formatter_class=_Formatter, epilog=EPILOG,
        description="Habbo asset toolkit: convert between Flash libraries (.swf), Habbo HTML5 bundles (.hab) "
                    "and Nitro bundles (.nitro), and inspect, extract, verify or compare any of them.")
    p.add_argument("--version", action="version", version="swf2hab " + __version__)
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")

    c = sub.add_parser("convert", aliases=["export"], formatter_class=_Formatter, epilog=CONVERSIONS,
                       help="convert files or folders to .hab, .swf or .nitro",
                       description="Convert files or whole folders. Folders are searched recursively and their "
                                   "structure is mirrored in the output folder; nothing is written next to the "
                                   "inputs. Files already converted (output newer than input) are skipped.")
    c.add_argument("inputs", nargs="+", metavar="INPUT", help=".swf, .hab or .nitro files or folders, mixed freely")
    c.add_argument("-o", "--output", required=True, metavar="DIR", help="output folder (not the input folder)")
    c.add_argument("--to", choices=toolkit.FORMATS, default="hab", help="target format: hab (default), swf, nitro")
    g = c.add_argument_group("running")
    g.add_argument("-j", "--jobs", type=int, default=_workers(), metavar="N",
                   help="parallel workers (default %d)" % _workers())
    g.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip files matching a name or relative-path glob; repeatable")
    g.add_argument("--force", action="store_true", help="convert again even if the output is up to date")
    g.add_argument("-n", "--dry-run", action="store_true", help="show what would be converted, write nothing")
    g.add_argument("--report", metavar="FILE", help="write a JSON report of every file")
    g = c.add_argument_group("--to hab")
    g.add_argument("--profile", choices=convert.PROFILES,
                   help="full: everything, incl. 32px art and raw XML (default); sulake: what Habbo's CDN ships")
    g.add_argument("--layout", choices=convert.LAYOUTS,
                   help="atlas: JSON + spritesheet; library: one entry per symbol; auto picks per file (default)")
    g.add_argument("--padding", type=int, metavar="PX", help="pixels between atlas frames (default 0)")
    g.add_argument("--max-atlas", type=int, metavar="PX", help="largest atlas side; bigger libraries are split "
                                                               "over several PNGs (default 8192)")
    g = c.add_argument_group("--to swf")
    g.add_argument("--small", choices=export.SMALL_MODES,
                   help="auto: generate half-size 32px art for furni and pets that have none, so a zoomed-out "
                        "Flash room does not draw them at double size (default); none: leave it out")
    _common(c, quiet=True, as_json=True)
    c.set_defaults(fn=cmd_convert)

    i = sub.add_parser("inspect", formatter_class=_Formatter, help="show what is inside .swf, .hab or .nitro files",
                       description="Describe each file: library name, kind, assets, frames, atlas and more. "
                                   "Folders are searched recursively.")
    i.add_argument("files", nargs="+", metavar="FILE")
    _common(i, as_json=True)
    i.set_defaults(fn=cmd_inspect)

    x = sub.add_parser("extract", aliases=["unpack"], formatter_class=_Formatter,
                       help="unpack files into folders of PNG, XML and JSON",
                       description="Write every entry or symbol as its own file. Atlas bundles also get each "
                                   "frame as a PNG under frames/. With several inputs, each gets a subfolder.")
    x.add_argument("files", nargs="+", metavar="FILE")
    x.add_argument("-o", "--output", required=True, metavar="DIR")
    x.add_argument("--no-frames", action="store_true", help="do not cut atlas frames into separate PNGs")
    _common(x, verbose=False)
    x.set_defaults(fn=cmd_extract)

    v = sub.add_parser("verify", formatter_class=_Formatter, help="check files load the way their clients load them",
                       description="Check .hab files as Habbo's HTML5 client reads them, .nitro files as Nitro reads "
                                   "them, and .swf libraries as Habbo's AS3 loader resolves them (document class, "
                                   "manifest, asset classes). Exit status 1 when a file is invalid.")
    v.add_argument("paths", nargs="+", metavar="PATH", help="files or folders")
    v.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    _common(v, as_json=True)
    v.set_defaults(fn=cmd_verify)

    k = sub.add_parser("compare", formatter_class=_Formatter, help="compare two files asset by asset (any formats)",
                       description="Compare the JSON and every frame's pixels of two files, which may be in different "
                                   "formats (each is read as a .hab bundle first). Exit status 1 when they differ.")
    k.add_argument("first")
    k.add_argument("second")
    k.add_argument("--profile", choices=convert.PROFILES,
                   help="how to read .swf inputs; default: sulake when the other file is a .nitro or a compact "
                        ".hab, else full")
    _common(k, verbose=False, as_json=True)
    k.set_defaults(fn=cmd_compare)

    w = sub.add_parser("wizard", formatter_class=_Formatter, help="convert step by step, answering questions",
                       description="Asks what to convert, to which format and where, then runs the conversion.")
    _common(w, verbose=False)
    w.set_defaults(fn=cmd_wizard)

    n = sub.add_parser("info", formatter_class=_Formatter, help="version, optional speed-ups and supported conversions")
    _common(n, verbose=False)
    n.set_defaults(fn=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 1
    ui = UI(color=False if ns.no_color else None, quiet=getattr(ns, "quiet", False) or getattr(ns, "json", False),
            verbose=getattr(ns, "verbose", False))
    try:
        return ns.fn(ns, ui)
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
