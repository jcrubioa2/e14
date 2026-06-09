#!/usr/bin/env python3
"""Render the quarantined (non-recoverable photo/scan) actas into a browsable HTML gallery.

Produces a single self-contained contact sheet so the quarantine set can be eyeballed at a
glance, with each acta linking to its live page on the public site.

    .venv/bin/python scripts/quarantine_gallery.py            # all 899
    .venv/bin/python scripts/quarantine_gallery.py --limit 60 # quick subset

Output: data/format_census/quarantine_gallery/index.html  (open in any browser).
On WSL: `explorer.exe "$(wslpath -w data/format_census/quarantine_gallery/index.html)"`
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"
QLIST = DATA / "format_census" / "quarantine_list.txt"
OUT = DATA / "format_census" / "quarantine_gallery"
SITE = "https://veeduria-ciudadana-elecciones-colombia-2026.com/acta/"


def _thumb(rec: dict, max_px: int = 360) -> dict | None:
    p = Path(rec["path"])
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    try:
        doc = fitz.open(p)
        pg = doc.load_page(0)
        z = min(1.5, max_px / max(pg.rect.width, pg.rect.height))
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z))
        png = pix.tobytes("png")
        doc.close()
    except Exception as e:  # noqa: BLE001
        return None
    return {
        "id": rec["document_id"],
        "dept": rec["document_id"].split("_")[2],
        "wh": f"{int(rec['w'])}x{int(rec['h'])}",
        "b64": base64.b64encode(png).decode("ascii"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="cap number of actas (0 = all)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    recs = {r["document_id"]: r for r in json.loads(CENSUS.read_text())}
    ql = [l.strip() for l in QLIST.read_text().splitlines() if l.strip()]
    todo = [recs[d] for d in ql if d in recs]
    todo.sort(key=lambda r: (r["document_id"].split("_")[2], r["document_id"]))
    if args.limit:
        todo = todo[: args.limit]

    print(f"rendering {len(todo)} quarantined actas with {args.workers} workers...")
    cards: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, c in enumerate(ex.map(_thumb, todo, chunksize=8), 1):
            if c:
                cards.append(c)
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}")

    # group counts by dept for the header
    from collections import Counter
    by_dept = Counter(c["dept"] for c in cards)
    dept_line = "  ".join(f"{d}:{n}" for d, n in sorted(by_dept.items(), key=lambda x: -x[1]))

    tiles = "\n".join(
        f'<a class="t" href="{SITE}{c["id"]}" target="_blank" title="{c["id"]} ({c["wh"]})">'
        f'<img loading="lazy" src="data:image/png;base64,{c["b64"]}">'
        f'<span>dept {c["dept"]} · {c["wh"]}</span></a>'
        for c in cards
    )
    html = f"""<!doctype html><meta charset=utf-8>
<title>Actas en cuarentena ({len(cards)})</title>
<style>
 body{{font:14px system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{padding:16px 20px;background:#171a21;position:sticky;top:0;border-bottom:1px solid #262b36}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#9aa4b2}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:16px 20px}}
 .t{{display:block;background:#1b1f27;border:1px solid #2a3140;border-radius:8px;overflow:hidden;text-decoration:none;color:#cbd3df}}
 .t img{{display:block;width:100%;height:300px;object-fit:contain;background:#000}}
 .t span{{display:block;padding:6px 8px;font-size:12px;color:#9aa4b2}}
 .t:hover{{border-color:#4c8bf5}}
</style>
<header>
 <h1>Actas en cuarentena — {len(cards)} fotos/escaneos no estándar</h1>
 <div class=sub>Click any acta to open it on the live site. Por departamento — {dept_line}</div>
</header>
<div class=grid>
{tiles}
</div>
"""
    OUT.mkdir(parents=True, exist_ok=True)
    idx = OUT / "index.html"
    idx.write_text(html, encoding="utf-8")
    print(f"\nwrote {idx}  ({idx.stat().st_size/1e6:.1f} MB, {len(cards)} actas)")
    print("open with:")
    print(f'  explorer.exe "$(wslpath -w {idx})"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
