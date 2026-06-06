#!/usr/bin/env python3
"""Live, sticky single-line progress monitor for scripts/rekey_crops.py.

Reads the background job's output file and redraws one line in place every ~2s with a
progress bar, percent, rolling copy rate (30s window), and live ETA. Exits when the
copy finishes (the job prints a ``done:`` summary). Ctrl-C to stop watching (does NOT
stop the copy — that's a separate background process).

Usage:
    python scripts/watch_rekey.py [path-to-job-output]
Defaults to the current re-key job's output file.
"""
import re
import sys
import time
from collections import deque

DEFAULT = ("/tmp/claude-1000/-home-jcrubioa-e14/"
           "b48c8755-d5bd-4b98-b2fe-537e6385719b/tasks/bism8qm8a.output")
path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT


def snapshot():
    try:
        d = open(path, errors="ignore").read().replace("\r", "\n")
    except FileNotFoundError:
        return ("missing",)
    if "done:" in d:
        line = [l for l in d.splitlines() if l.startswith("done:")][-1]
        return ("done", line)
    m = re.findall(r"(\d+)/(\d+)", d)
    if m:
        return ("copy", int(m[-1][0]), int(m[-1][1]))
    lb = re.findall(r"list-bucket: (\d+) object", d)
    if lb:
        return ("list", int(lb[-1]))
    return ("start",)


def fmt_eta(secs):
    if secs == float("inf"):
        return "—"
    secs = int(secs)
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


hist = deque()  # (timestamp, done) over a rolling window for rate
try:
    while True:
        s = snapshot()
        now = time.time()
        if s[0] == "done":
            sys.stdout.write("\r" + " " * 110 + "\r" + s[1] + "   ✓ complete\n")
            sys.stdout.flush()
            break
        if s[0] == "missing":
            line = "waiting for log file…"
        elif s[0] == "start":
            line = "starting…"
        elif s[0] == "list":
            line = f"listing bucket… {s[1]:,} objects enumerated (copy not started)"
        else:  # copy
            done, tot = s[1], s[2]
            hist.append((now, done))
            while hist and now - hist[0][0] > 30:
                hist.popleft()
            rate = ((hist[-1][1] - hist[0][1]) / (hist[-1][0] - hist[0][0])
                    if len(hist) >= 2 and hist[-1][0] > hist[0][0] and hist[-1][1] > hist[0][1] else 0)
            pct = 100 * done / tot if tot else 0
            eta = (tot - done) / rate if rate > 0 else float("inf")
            filled = int(pct / 5)
            bar = "█" * filled + "░" * (20 - filled)
            line = f"[{bar}] {pct:5.1f}%  {done:,}/{tot:,}  {rate:5.0f}/s  ETA {fmt_eta(eta)}"
        sys.stdout.write("\r" + line + " " * max(0, 100 - len(line)))
        sys.stdout.flush()
        time.sleep(2)
except KeyboardInterrupt:
    sys.stdout.write("\n(stopped watching — copy keeps running in the background)\n")
