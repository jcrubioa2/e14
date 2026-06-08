#!/usr/bin/env python3
"""Snapshot the current (about-to-be-replaced) acta PDFs before a re-fetch.

The registraduría silently re-issues acta PDFs under a stable URL pointer: some actas we
downloaded early are stale phone-photos, while the live URL now serves a clean scan. Before
re-downloading the non-standard-geometry actas (see scripts/acta_format_census.py), preserve
our current copies as a historical "as-of" snapshot so the past version is never lost.

Copies each non-normal source PDF to ``data/actas_snapshots/<date>/<same subpath>`` and writes
an index (key, document_id, sha256, bytes, source path). Idempotent per destination.

    .venv/bin/python scripts/snapshot_actas.py --dry-run     # report file count + size
    .venv/bin/python scripts/snapshot_actas.py               # copy them
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"
ACTAS = DATA / "actas"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--actas-dir", type=Path, default=ACTAS)
    ap.add_argument("--dest", type=Path, default=DATA / "actas_snapshots" / date.today().isoformat())
    ap.add_argument("--include-normal", action="store_true", help="snapshot ALL actas, not just non-normal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recs = json.loads(args.census.read_text())
    if not args.include_normal:
        recs = [r for r in recs if r["format"] in ("wide", "other")]

    total_bytes = 0
    index = []
    copied = 0
    for r in recs:
        src = Path(r["path"])
        if not src.is_absolute():
            src = ROOT / src
        if not src.exists():
            continue
        size = src.stat().st_size
        total_bytes += size
        rel = src.relative_to(args.actas_dir.resolve()) if src.is_relative_to(args.actas_dir.resolve()) else Path(src.name)
        dest = args.dest / rel
        index.append({"document_id": r["document_id"], "format": r["format"],
                      "bytes": size, "src": str(src), "dest": str(dest)})
        if args.dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
            copied += 1

    gb = total_bytes / 1e9
    print(f"non-normal actas to snapshot: {len(index)}  ({gb:.2f} GB)")
    if args.dry_run:
        print("(dry run) nothing copied")
        return 0
    args.dest.mkdir(parents=True, exist_ok=True)
    (args.dest / "snapshot_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(f"copied {copied} new files -> {args.dest}")
    print(f"index -> {args.dest / 'snapshot_index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
