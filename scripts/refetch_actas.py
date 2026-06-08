#!/usr/bin/env python3
"""Re-download the non-standard-geometry actas fresh from the registraduria.

Many "failed" (non-normal) actas are stale: the registraduria re-issued a clean scan under
the same URL pointer after we first downloaded a phone-photo. This force-re-downloads the
census non-normal set (overwriting data/actas in place — snapshot first with
scripts/snapshot_actas.py!) so a later reprocess can extract real numbers.

Needs network to the registraduria. Reuses the production downloader + manifest.

    .venv/bin/python scripts/refetch_actas.py --dry-run        # how many keys would re-fetch
    .venv/bin/python scripts/refetch_actas.py --concurrency 8  # do it
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14.cli import ACTAS_DIR, MANIFEST_DB, _load_records  # noqa: E402
from e14.downloader import run_download  # noqa: E402
from e14.manifest import Manifest  # noqa: E402

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"


def nonnormal_keys(census: Path) -> set[str]:
    """document_id E14_PRE_{dep}_{muni}_{zona}_{puesto}_{mesa}_delegados -> key dep_muni_zona_puesto_mesa."""
    keys = set()
    for r in json.loads(census.read_text()):
        if r["format"] not in ("wide", "other"):
            continue
        p = r["document_id"].split("_")  # E14 PRE dep muni zona puesto mesa delegados
        if len(p) >= 7:
            keys.add("_".join(p[2:7]))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--refresh", action="store_true", default=True,
                    help="refresh the universe first to pick up re-pointed actas (default on)")
    ap.add_argument("--no-refresh", dest="refresh", action="store_false")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--rate", type=float, default=6.0, help="requests/sec ceiling (be polite)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    keys = nonnormal_keys(args.census)
    print(f"non-normal keys to re-fetch: {len(keys)}")

    recs = _load_records(refresh=args.refresh)
    todo = [r for r in recs if r.key in keys]
    print(f"matched {len(todo)} of {len(keys)} keys in the universe")
    missing = keys - {r.key for r in todo}
    if missing:
        print(f"  ⚠ {len(missing)} keys not in current universe (e.g. {sorted(missing)[:3]})")

    if args.dry_run:
        print("(dry run) nothing downloaded")
        return 0

    manifest = Manifest(MANIFEST_DB)
    results_path = ROOT / "logs" / "refetch_results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    out = run_download(
        todo, manifest, ACTAS_DIR, variant="delegados",
        concurrency=args.concurrency, rate=args.rate, force=True,
        results_path=results_path,
    )
    print(f"\ndone={out['done']} failed={out['failed']} skipped={out['skipped']}")
    if out.get("reasons"):
        for reason, n in sorted(out["reasons"].items(), key=lambda x: -x[1]):
            print(f"    {n:>6}  {reason}")
    print(f"per-acta log: {results_path}")
    print("\nNext: re-run the census + reprocess on the re-fetched actas:")
    print("  .venv/bin/python scripts/reprocess_refetched.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
