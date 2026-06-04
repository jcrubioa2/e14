#!/usr/bin/env python3
"""Build e14detector/acta_hashes.sqlite — the bundled document_id -> Registraduría PDF hash map.

A codes-only served snapshot ships documents.official_lookup_url NULL, which hides the "Ver el
acta oficial" link on /acta. The official URL is fully templated as
    {base}/{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{hash}.pdf
and every part but the opaque per-acta hash is encoded in the document_id, so we only need to
carry the 32-byte hash per acta. webapp.official_url_for() looks it up and rebuilds the link at
render time (see webapp.OFFICIAL_PDF_BASE / official_acta_url).

Source: data/index.csv (one row per acta: codes + mesa + archivo + enlace_oficial), produced by
e14.universe.write_index_csv. Re-run whenever the acta universe changes.

    python scripts/build_acta_hashes.py [--index data/index.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from pathlib import Path

DEFAULT_INDEX = Path("data/index.csv")
DEST = Path("e14detector/acta_hashes.sqlite")


def build(index_csv: Path, dest: Path) -> tuple[int, int]:
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(dest)
    con.execute(
        "CREATE TABLE acta_hash (document_id TEXT PRIMARY KEY, hash BLOB NOT NULL) WITHOUT ROWID"
    )
    n = skipped = 0
    with index_csv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            archivo = (row.get("archivo") or "").strip()
            url = (row.get("enlace_oficial") or "").strip()
            if not archivo or not url:
                skipped += 1
                continue
            document_id = os.path.basename(archivo).removesuffix(".pdf")
            hash_hex = url.rsplit("/", 1)[-1].removesuffix(".pdf")
            if len(hash_hex) != 64:
                skipped += 1
                continue
            con.execute(
                "INSERT OR IGNORE INTO acta_hash VALUES (?, ?)",
                (document_id, bytes.fromhex(hash_hex)),
            )
            n += 1
    con.commit()
    con.execute("VACUUM")
    con.close()
    return n, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--dest", type=Path, default=DEST)
    args = ap.parse_args()
    n, skipped = build(args.index, args.dest)
    size_mb = round(args.dest.stat().st_size / 1e6, 2)
    print(f"wrote {args.dest}: {n} actas, {skipped} skipped, {size_mb} MB")


if __name__ == "__main__":
    main()
