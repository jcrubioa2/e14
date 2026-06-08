#!/usr/bin/env python3
"""Measure rectification success across the non-normal actas + build a review gallery.

Reads the census manifest, samples non-normal actas, runs e14detector.rectify on
page 1 of each, and reports how many clear the sanity gate (=> rectifiable, votable)
vs fail (=> quarantine). Writes data/format_census/rectify_gallery.html with the
rectified page + overlaid r1 boxes for PASS, and the raw page for FAIL, so the
pass/fail split can be eyeballed and the gate tuned.

    .venv/bin/python scripts/rectify_validate.py --per-size 12 --workers 8
    .venv/bin/python scripts/rectify_validate.py --all --workers 8   # every non-normal acta
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

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"
OUT_DIR = DATA / "format_census"


def _eval_one(args: tuple[str, str, bool]) -> dict:
    path, document_id, make_thumb = args
    from PIL import ImageDraw

    from e14detector.layout import field_layouts_for_page
    from e14detector.pdf_render import render_pdf_pages
    from e14detector.rectify import rectify_image

    rec = {"document_id": document_id, "ok": False, "score": 0.0, "method": "render-fail"}
    try:
        page = render_pdf_pages(Path(path), pages=(1,), dpi=200)[0]
    except Exception as exc:  # noqa: BLE001
        rec["method"] = f"render-fail:{type(exc).__name__}"
        return rec
    res = rectify_image(page.image)
    rec.update(ok=res.ok, score=round(res.score, 3), method=res.method)

    if make_thumb:
        if res.image is not None:
            img = res.image.convert("RGB")
            d = ImageDraw.Draw(img)
            for fl in field_layouts_for_page(1, img.width, img.height):
                b = fl.field_box
                d.rectangle([b.x0, b.y0, b.x1, b.y1], outline=(255, 40, 40), width=3)
        else:
            img = page.image.convert("RGB")
        img.thumbnail((300, 950))
        thumb = OUT_DIR / "rectify_samples" / f"{document_id}.png"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        img.save(thumb)
        rec["thumb"] = f"rectify_samples/{document_id}.png"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--per-size", type=int, default=12, help="sample N per (format,w,h) bucket")
    ap.add_argument("--all", action="store_true", help="evaluate every non-normal acta (no thumbs)")
    args = ap.parse_args()

    recs = json.loads(args.census.read_text())
    nonnormal = [r for r in recs if r["format"] in ("wide", "other")]

    if args.all:
        sample = nonnormal
        make_thumb = False
    else:
        buckets: dict[tuple, list[dict]] = defaultdict(list)
        for r in nonnormal:
            buckets[(r["format"], r["w"], r["h"])].append(r)
        sample = []
        for rs in buckets.values():
            sample.extend(rs[: args.per_size])
        make_thumb = True

    print(f"evaluating {len(sample)} / {len(nonnormal)} non-normal actas ({args.workers} workers) ...")
    tasks = [(r["path"], r["document_id"], make_thumb) for r in sample]
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(_eval_one, tasks, chunksize=8):
            results.append(rec)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(sample)}", flush=True)

    npass = sum(1 for r in results if r["ok"])
    nfail = len(results) - npass
    print(f"\nPASS (rectifiable): {npass}  ({100*npass/len(results):.1f}%)")
    print(f"FAIL (quarantine):  {nfail}  ({100*nfail/len(results):.1f}%)")
    print("\nfail methods:")
    for m, c in Counter(r["method"] for r in results if not r["ok"]).most_common(10):
        print(f"  {m:24} {c}")

    # persist per-acta verdicts (drives Phase 3 reprocess vs quarantine)
    verdicts = OUT_DIR / "rectify_verdicts.json"
    verdicts.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"\nverdicts -> {verdicts}")

    if make_thumb:
        html = ["<!doctype html><meta charset=utf-8><title>rectify validation</title>",
                "<style>body{font-family:sans-serif;background:#111;color:#eee}"
                "figure{display:inline-block;margin:6px;vertical-align:top}"
                "img{border:2px solid #444;max-height:520px}"
                ".pass img{border-color:#2a2}.fail img{border-color:#a22}"
                "figcaption{font-size:11px;max-width:200px}</style>",
                f"<h1>Rectify validation — {npass} pass / {nfail} fail</h1>"]
        for label in ("fail", "pass"):
            html.append(f"<h2>{label.upper()}</h2>")
            for r in sorted(results, key=lambda r: -r["score"]):
                if (r["ok"] and label == "pass") or (not r["ok"] and label == "fail"):
                    if "thumb" not in r:
                        continue
                    cls = "pass" if r["ok"] else "fail"
                    html.append(
                        f"<figure class={cls}><img src='{r['thumb']}'>"
                        f"<figcaption>{r['document_id']}<br>{r['method']} score={r['score']}</figcaption></figure>")
        (OUT_DIR / "rectify_gallery.html").write_text("\n".join(html), encoding="utf-8")
        print(f"gallery  -> {OUT_DIR / 'rectify_gallery.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
