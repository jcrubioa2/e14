#!/usr/bin/env python3
"""Census every acta's page geometry to find non-standard scan formats.

The crop pipeline (e14detector/layout.py) uses fixed *normalized* coordinates
(vote column x 0.690-0.942 of the page) applied to the raw rendered page. That
only matches the dominant scan geometry. When a scan has a different aspect
ratio the printed form sits at a different fraction of the page, so the fixed
box lands off-target and the crop is unreadable.

This script reads ONLY page rectangles (no rendering) for every acta, so it is
fast and parallelizable, and clusters actas by aspect ratio into a small set of
formats:

    normal  ar < 0.45   (~860-871 x ~2600, the calibrated geometry)
    wide    0.45-0.65    (e.g. 1700x2800)
    other   ar >= 0.65   (e.g. 1654x2338)

The non-``normal`` document_ids it emits are the exact set to re-crop with
format-aware coordinates and to reset votes for.

Outputs under data/format_census/:
  manifest.json  every acta: {document_id, dep, format, w, h, aspect, pages}
  summary.txt    counts per format + per-department breakdown
  gallery.html   rendered samples per non-normal cluster, with the CURRENT crop
                 boxes overlaid so the off-target failure is visible (skip with
                 --no-gallery)

    .venv/bin/python scripts/acta_format_census.py --workers 8
    .venv/bin/python scripts/acta_format_census.py --limit 2000 --no-gallery
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.utils import parse_document_metadata  # noqa: E402

DATA = ROOT / "data"
ACTAS_DIR = DATA / "actas"
OUT_DIR = DATA / "format_census"

# Aspect-ratio (width/height) band -> format label. Boundaries chosen from the
# observed corpus clusters (normal ~0.33, wide ~0.61, other ~0.71) with wide
# margins so minor per-scan jitter never reclassifies a normal acta.
NORMAL_MAX = 0.45
WIDE_MAX = 0.65


def classify(width: float, height: float) -> str:
    ar = (width / height) if height else 0.0
    if ar < NORMAL_MAX:
        return "normal"
    if ar < WIDE_MAX:
        return "wide"
    return "other"


def scan_one(path_str: str) -> dict | None:
    """Read page-1 rect + page count for one PDF. Worker-safe (lazy fitz)."""
    import fitz  # imported in the worker to keep the parent import light

    path = Path(path_str)
    try:
        doc = fitz.open(path)
        try:
            pages = len(doc)
            if pages == 0:
                raise ValueError("no pages")
            rect = doc.load_page(0).rect
            w, h = round(rect.width, 1), round(rect.height, 1)
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - record, don't crash the census
        meta = parse_document_metadata(path)
        return {
            "document_id": meta.document_id,
            "dep": meta.department_code,
            "format": "error",
            "w": None,
            "h": None,
            "aspect": None,
            "pages": None,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }

    meta = parse_document_metadata(path)
    ar = round(w / h, 4) if h else 0.0
    return {
        "document_id": meta.document_id,
        "dep": meta.department_code,
        "format": classify(w, h),
        "w": w,
        "h": h,
        "aspect": ar,
        "pages": pages,
        "path": str(path),
    }


def iter_acta_pdfs(input_dir: Path, limit: int | None) -> list[Path]:
    pdfs = sorted(input_dir.rglob("*.pdf"))
    return pdfs[:limit] if limit else pdfs


def write_summary(records: list[dict], out: Path) -> None:
    by_format = Counter(r["format"] for r in records)
    # dep -> {format: count}
    by_dep: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        by_dep[r["dep"] or "??"][r["format"]] += 1

    total = len(records)
    lines: list[str] = []
    lines.append(f"acta format census  —  {total} actas\n")
    lines.append("format counts:")
    for fmt, n in by_format.most_common():
        pct = 100.0 * n / total if total else 0.0
        lines.append(f"  {fmt:8} {n:>8}  ({pct:5.2f}%)")
    lines.append("")
    lines.append("departments containing non-normal actas (wide / other / error):")
    lines.append(f"  {'dep':>4}  {'total':>7}  {'normal':>7}  {'wide':>6}  {'other':>6}  {'error':>6}")
    rows = []
    for dep, c in by_dep.items():
        nonnormal = c["wide"] + c["other"] + c["error"]
        if nonnormal:
            rows.append((dep, c))
    rows.sort(key=lambda kv: -(kv[1]["wide"] + kv[1]["other"] + kv[1]["error"]))
    for dep, c in rows:
        tot = sum(c.values())
        lines.append(
            f"  {dep:>4}  {tot:>7}  {c['normal']:>7}  {c['wide']:>6}  {c['other']:>6}  {c['error']:>6}"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def build_gallery(records: list[dict], out_dir: Path, per_cluster: int, dpi: int) -> None:
    """Render a few samples per non-normal cluster with the CURRENT crop boxes
    drawn on, so the off-target failure is visible to a human reviewer."""
    from PIL import ImageDraw  # noqa: E402

    from e14detector.layout import field_layouts_for_page
    from e14detector.pdf_render import render_pdf_pages

    samples_dir = out_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    # one representative per (format, w, h) so the gallery spans distinct sizes
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        if r["format"] in ("wide", "other") and r["w"]:
            buckets[(r["format"], r["w"], r["h"])].append(r)

    html: list[str] = [
        "<!doctype html><meta charset=utf-8>",
        "<title>acta format census — gallery</title>",
        "<style>body{font-family:sans-serif;background:#111;color:#eee}"
        "figure{display:inline-block;margin:8px;vertical-align:top}"
        "img{border:1px solid #444;max-height:680px}"
        "figcaption{font-size:12px;max-width:240px}"
        "h2{border-bottom:1px solid #444}</style>",
        "<h1>Non-normal acta samples — red boxes = where crops land TODAY</h1>",
    ]

    current_fmt = None
    for (fmt, w, h), rs in sorted(buckets.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        if fmt != current_fmt:
            html.append(f"<h2>{fmt}</h2>")
            current_fmt = fmt
        for r in rs[:per_cluster]:
            try:
                page = render_pdf_pages(Path(r["path"]), pages=(1,), dpi=dpi)[0]
                img = page.image.convert("RGB")
                draw = ImageDraw.Draw(img)
                for fl in field_layouts_for_page(1, page.width, page.height):
                    b = fl.field_box
                    draw.rectangle([b.x0, b.y0, b.x1, b.y1], outline=(255, 40, 40), width=3)
                thumb_w = 320
                scale = thumb_w / img.width
                img = img.resize((thumb_w, int(img.height * scale)))
                name = f"{r['document_id']}.png"
                img.save(samples_dir / name)
                html.append(
                    f"<figure><img src='samples/{name}'>"
                    f"<figcaption>{r['document_id']}<br>{w}x{h} ar={r['aspect']}</figcaption></figure>"
                )
            except Exception as exc:  # noqa: BLE001
                html.append(f"<figure><figcaption>{r['document_id']}: render error {exc}</figcaption></figure>")
        html.append("<hr>")

    (out_dir / "gallery.html").write_text("\n".join(html), encoding="utf-8")
    print(f"\ngallery -> {out_dir / 'gallery.html'}  ({sum(len(v[:per_cluster]) for v in buckets.values())} samples)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--input-dir", type=Path, default=ACTAS_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="cap PDFs scanned (debug)")
    ap.add_argument("--no-gallery", action="store_true", help="skip rendered samples")
    ap.add_argument("--gallery-samples", type=int, default=12, help="samples per (format,size)")
    ap.add_argument("--gallery-dpi", type=int, default=110)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = iter_acta_pdfs(args.input_dir, args.limit)
    print(f"scanning {len(pdfs)} actas under {args.input_dir} with {args.workers} workers ...")

    records: list[dict] = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(scan_one, [str(p) for p in pdfs], chunksize=256):
            if rec is not None:
                records.append(rec)
            done += 1
            if done % 10000 == 0:
                print(f"  {done}/{len(pdfs)}", flush=True)

    records.sort(key=lambda r: r["document_id"])
    manifest = args.out_dir / "manifest.json"
    manifest.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    print(f"\nmanifest -> {manifest}  ({len(records)} actas)\n")

    write_summary(records, args.out_dir / "summary.txt")

    if not args.no_gallery:
        build_gallery(records, args.out_dir, args.gallery_samples, args.gallery_dpi)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
