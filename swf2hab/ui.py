"""Terminal output for the command line: colour, a live progress line, tables, sizes and times.

Colour and the redrawn progress line are used only on an interactive terminal; piped or
redirected output gets plain lines, so logs and scripts stay readable. NO_COLOR is honoured.
"""

from __future__ import annotations

import os
import sys
import time

STYLES = {"ok": "32", "warn": "33", "err": "31", "dim": "2", "bold": "1", "accent": "36", "head": "1;36"}


def _enable_windows_ansi(stream) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))   # VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


class UI:
    def __init__(self, color: bool | None = None, quiet: bool = False, verbose: bool = False, stream=None):
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self.verbose = verbose
        try:
            self.tty = self.stream.isatty()
        except Exception:
            self.tty = False
        if color is None:
            color = self.tty and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        self.color = bool(color) and _enable_windows_ansi(self.stream)
        encoding = (getattr(self.stream, "encoding", None) or "").lower().replace("-", "")
        self.unicode = encoding.startswith("utf")
        try:
            self.stream.reconfigure(errors="replace")
        except Exception:
            pass
        self._progress_shown = False
        self._last_draw = 0.0
        self._last_plain = 0

    # ------------------------------------------------------------------ text
    def c(self, text, style: str) -> str:
        text = str(text)
        if not self.color or style not in STYLES:
            return text
        return "\033[%sm%s\033[0m" % (STYLES[style], text)

    def sym(self, name: str) -> str:
        fancy = {"ok": "✓", "fail": "✗", "warn": "!", "skip": "–", "arrow": "→", "dot": "·", "times": "×"}
        plain = {"ok": "ok", "fail": "x", "warn": "!", "skip": "-", "arrow": "->", "dot": "|", "times": "x"}
        return (fancy if self.unicode else plain)[name]

    def _clear_progress(self) -> None:
        if self._progress_shown:
            self.stream.write("\r\033[K" if self.color else "\r" + " " * 100 + "\r")
            self._progress_shown = False

    def line(self, text: str = "", force: bool = False) -> None:
        if self.quiet and not force:
            return
        self._clear_progress()
        self.stream.write(text + "\n")
        self.stream.flush()

    def error(self, text: str) -> None:
        self._clear_progress()
        sys.stderr.write(("error: " if not self.color else "\033[31merror:\033[0m ") + text + "\n")
        sys.stderr.flush()

    # ------------------------------------------------------------------ progress
    def progress(self, done: int, total: int, counts: dict, started: float, every: int = 500) -> None:
        if self.quiet:
            return
        now = time.perf_counter()
        elapsed = max(now - started, 1e-6)
        rate = done / elapsed
        left = (total - done) / rate if rate > 0 else 0
        if not self.tty:
            if done - self._last_plain >= every or done == total:
                self._last_plain = done
                self.stream.write("  %d/%d  %s  %s\n" % (done, total, self.counts_text(counts, plain=True),
                                                         human_time(elapsed)))
                self.stream.flush()
            return
        if done != total and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        width = 24
        filled = int(width * done / total) if total else width
        full, empty = ("█", "░") if self.unicode else ("#", "-")
        bar = self.c(full * filled, "accent") + self.c(empty * (width - filled), "dim")
        text = " %s %*d/%d  %s  %s/s  %s" % (bar, len(str(total)), done, total, self.counts_text(counts),
                                            ("%.0f" % rate) if rate >= 10 else ("%.1f" % rate),
                                            ("eta " + human_time(left)) if done < total else human_time(elapsed))
        self.stream.write("\r" + text + ("\033[K" if self.color else ""))
        self.stream.flush()
        self._progress_shown = True

    def end_progress(self) -> None:
        if self._progress_shown:
            self.stream.write("\n")
            self.stream.flush()
            self._progress_shown = False

    def counts_text(self, counts: dict, plain: bool = False) -> str:
        parts = []
        for key, style in (("ok", "ok"), ("valid", "ok"), ("up-to-date", "dim"), ("skipped", "warn"),
                           ("warnings", "warn"), ("failed", "err"), ("invalid", "err")):
            if counts.get(key):
                text = "%s %d" % (key, counts[key])
                parts.append(text if plain else self.c(text, style))
        return (" " + self.sym("dot") + " ").join(parts) if parts else "starting"

    # ------------------------------------------------------------------ layout
    def table(self, rows: list[tuple], indent: int = 2, styles: tuple | None = None) -> None:
        """Left-aligned columns; numbers right-aligned. `styles` optionally styles each column."""
        if not rows:
            return
        widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            cells = []
            for i, v in enumerate(r):
                text = str(v)
                text = text.rjust(widths[i]) if isinstance(v, (int, float)) and not isinstance(v, bool) \
                    else text.ljust(widths[i])
                if styles and i < len(styles) and styles[i]:
                    text = self.c(text, styles[i])
                cells.append(text)
            self.line(" " * indent + "  ".join(cells).rstrip())

    def field(self, label: str, value, indent: int = 2, width: int = 12) -> None:
        self.line(" " * indent + self.c(label.ljust(width), "dim") + str(value))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def human_time(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 1:
        return "%.0f ms" % (seconds * 1000)
    if seconds < 60:
        return "%.1f s" % seconds
    m, s = divmod(int(seconds + 0.5), 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def plural(n: int, word: str, words: str | None = None) -> str:
    return "%s %s" % (format(n, ","), word if n == 1 else (words or word + "s"))
