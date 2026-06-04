#!/usr/bin/env python3
"""Build e14detector/acta_hashes.sqlite — the bundled document_id -> Registraduría PDF hash map.

A codes-only served snapshot ships documents.official_lookup_url NULL, which hides the "Ver el
acta oficial" link on /acta. The official URL is fully templated as
    {base}/{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{hash}.pdf
and every part but the opaque per-acta hash is encoded in the document_id, so we only need to
carry the 32-byte hash per acta. webapp.official_url_for() looks it up and rebuilds the link at
render time (see webapp.OFFICIAL_PDF_BASE / official_acta_url).

Sources (both produced by e14.universe):
  - data/mesa_universe.csv (PRIMARY): one row per mesa with dep/muni/zona/puesto/mesa +
    expected_name ("{hash}.pdf"). The fullest list — includes special zona-099 mesas (exterior /
    consulados) that index.csv omits.
  - data/index.csv (SUPPLEMENT): codes + mesa + archivo + enlace_oficial; fills any acta the
    universe file happens to miss.
Re-run whenever the acta universe changes.

    python scripts/build_acta_hashes.py [--universe data/mesa_universe.csv] [--index data/index.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from pathlib import Path

DEFAULT_UNIVERSE = Path("data/mesa_universe.csv")
DEFAULT_INDEX = Path("data/index.csv")
DEST = Path("e14detector/acta_hashes.sqlite")


def _doc_id(dep: str, muni: str, zona: str, puesto: str, mesa: str) -> str:
    return (f"E14_PRE_{dep.zfill(2)}_{muni.zfill(3)}_{zona.zfill(3)}"
            f"_{puesto.zfill(2)}_{mesa.zfill(3)}_delegados")


def _collect(universe_csv: Path, index_csv: Path) -> tuple[dict[str, str], int]:
    """document_id -> 64-hex hash, unioned across both sources (universe wins on conflict)."""
    out: dict[str, str] = {}
    conflicts = 0

    if index_csv.exists():
        with index_csv.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                archivo = (row.get("archivo") or "").strip()
                url = (row.get("enlace_oficial") or "").strip()
                if not archivo or not url:
                    continue
                did = os.path.basename(archivo).removesuffix(".pdf")
                h = url.rsplit("/", 1)[-1].removesuffix(".pdf")
                if len(h) == 64:
                    out[did] = h

    if universe_csv.exists():
        with universe_csv.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                h = (row.get("expected_name") or "").strip().removesuffix(".pdf")
                if len(h) != 64:
                    continue
                did = _doc_id(row["dep"], row["muni"], row["zona"], row["puesto"], row["mesa"])
                if did in out and out[did] != h:
                    conflicts += 1
                out[did] = h  # universe is authoritative
    return out, conflicts


def build(universe_csv: Path, index_csv: Path, dest: Path) -> tuple[int, int]:
    mapping, conflicts = _collect(universe_csv, index_csv)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(dest)
    con.execute(
        "CREATE TABLE acta_hash (document_id TEXT PRIMARY KEY, hash BLOB NOT NULL) WITHOUT ROWID"
    )
    con.executemany(
        "INSERT OR IGNORE INTO acta_hash VALUES (?, ?)",
        ((did, bytes.fromhex(h)) for did, h in mapping.items()),
    )
    con.commit()
    con.execute("VACUUM")
    con.close()
    return len(mapping), conflicts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--dest", type=Path, default=DEST)
    args = ap.parse_args()
    n, conflicts = build(args.universe, args.index, args.dest)
    size_mb = round(args.dest.stat().st_size / 1e6, 2)
    print(f"wrote {args.dest}: {n} actas, {conflicts} hash conflicts (universe won), {size_mb} MB")


if __name__ == "__main__":
    main()
