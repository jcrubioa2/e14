"""Static web report of detector results.

Produces a small browsable site under ``<output_dir>/review/web``:

* ``index.html`` — summary stats plus a table of every PDF that has at least one
  non-clean detection, each linking to its own page;
* ``docs/<document_id>.html`` — for one acta, the number-crop of every candidate
  (and the summary rows), with the CV verdict and any VLM second opinion, with
  flagged rows highlighted.

Everything is static HTML referencing the crop PNGs by relative path, so it can
be served with any static file server (e.g. ``python -m http.server``).
"""
from __future__ import annotations

import html
import os
import sqlite3
from pathlib import Path

NON_CLEAN = ("UNCLEAR", "SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY", "CROP_FAILED")

STYLE = """
<style>
body { font-family: system-ui, sans-serif; margin: 24px; color: #202124; }
h1 { margin-bottom: 4px; }
a { color: #0b66c3; text-decoration: none; }
a:hover { text-decoration: underline; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 24px; }
.card { border: 1px solid #d0d7de; border-radius: 8px; padding: 10px 16px; min-width: 120px; }
.card .n { font-size: 26px; font-weight: 700; }
.card .l { font-size: 12px; color: #57606a; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 7px 9px; font-size: 13px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; position: sticky; top: 0; }
tr.flagged { background: #fff8e6; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
.slot { border: 1px solid #d0d7de; border-radius: 8px; padding: 10px; }
.slot.bad { border-color: #d1242f; box-shadow: 0 0 0 2px #ffd7d5 inset; }
.slot img { width: 100%; max-height: 90px; object-fit: contain; background: #fff; }
.cand { font-weight: 600; margin-bottom: 4px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 10px; background: #eaeef2; }
.tag.bad { background: #ffd7d5; color: #82071e; }
.tag.clean { background: #dafbe1; color: #0a5a2a; }
.muted { color: #57606a; font-size: 12px; }
.read { font-family: ui-monospace, monospace; font-size: 18px; letter-spacing: 2px; }
</style>
"""


def _rel(path: str | None, base: Path) -> str | None:
    if not path:
        return None
    try:
        return os.path.relpath(Path(path), base)
    except OSError:
        return path


def _tag(classification: str | None) -> str:
    c = classification or ""
    cls = "clean" if c == "CLEAN" else ("bad" if c in NON_CLEAN else "")
    return f"<span class='tag {cls}'>{html.escape(c)}</span>"


def _doc_page(conn: sqlite3.Connection, doc: sqlite3.Row, page_path: Path, crop_base: Path) -> int:
    fields = conn.execute(
        """
        SELECT page_number,row_number,row_type,section,candidate_name,raw_crop_path,
               final_classification,final_reason,read_value,vlm_classification,vlm_confidence
        FROM vote_fields WHERE document_id=?
        ORDER BY page_number,row_number
        """,
        (doc["document_id"],),
    ).fetchall()

    cells = []
    flagged = 0
    for f in fields:
        bad = f["final_classification"] in NON_CLEAN
        flagged += 1 if bad else 0
        label = f["candidate_name"] or f["section"] or f["row_type"]
        img_rel = _rel(f["raw_crop_path"], page_path.parent)
        img = (
            f"<a href='{html.escape(img_rel)}'><img src='{html.escape(img_rel)}' loading='lazy'></a>"
            if img_rel else "<span class='muted'>missing crop</span>"
        )
        vlm = ""
        if f["vlm_classification"]:
            vlm = (
                f"<div class='muted'>VLM: {_tag(f['vlm_classification'])} "
                f"({f['vlm_confidence']})</div>"
            )
        read = f["read_value"]
        read_html = f"<span class='read'>{html.escape(read)}</span>" if read else "<span class='muted'>—</span>"
        cells.append(
            f"<div class='slot {'bad' if bad else ''}'>"
            f"<div class='cand'>{html.escape(str(label))}</div>"
            f"{img}"
            f"<div>read: {read_html}</div>"
            f"<div>{_tag(f['final_classification'])}</div>"
            f"{vlm}"
            f"<div class='muted'>{html.escape(f['final_reason'] or '')}</div>"
            "</div>"
        )

    pdf_rel = _rel(doc["source_path"], page_path.parent) or html.escape(doc["source_path"] or "")
    codes = f"{doc['department_code']}/{doc['municipality_code']}/{doc['zone']}/{doc['puesto']} mesa {doc['mesa']}"
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(doc['document_id'])}</title>", STYLE, "</head><body>",
        "<p><a href='../index.html'>&larr; back to summary</a></p>",
        f"<h1>{html.escape(doc['document_id'])}</h1>",
        f"<div class='muted'>{html.escape(codes)} &middot; {html.escape(doc['place_name'] or '')}</div>",
        f"<div class='muted'>Source PDF: <a href='{html.escape(pdf_rel)}'>{html.escape(doc['source_path'] or '')}</a>"
        + (f" &middot; <a href='{html.escape(doc['official_lookup_url'])}'>official lookup</a>" if doc["official_lookup_url"] else "")
        + "</div>",
        f"<p><b>{flagged}</b> non-clean field(s) of {len(fields)}.</p>",
        "<div class='grid'>", *cells, "</div>",
        "</body></html>",
    ]
    page_path.write_text("\n".join(parts), encoding="utf-8")
    return flagged


def export_web_report(results_db: Path, output_dir: Path) -> dict[str, int]:
    results_db = Path(results_db)
    web_dir = Path(output_dir) / "review" / "web"
    docs_dir = web_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    crop_base = Path(output_dir)

    conn = sqlite3.connect(results_db)
    conn.row_factory = sqlite3.Row

    totals = {row["final_classification"]: row["c"] for row in conn.execute(
        "SELECT final_classification, COUNT(*) c FROM vote_fields GROUP BY final_classification"
    )}
    total_fields = sum(totals.values())
    total_docs = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]

    flagged_docs = conn.execute(
        f"""
        SELECT d.document_id,d.source_path,d.department_code,d.municipality_code,d.zone,
               d.puesto,d.mesa,d.place_name,d.official_lookup_url,
               COUNT(*) FILTER (WHERE vf.final_classification IN ({','.join('?'*len(NON_CLEAN))})) AS n_flag,
               COUNT(*) AS n_fields
        FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
        GROUP BY d.document_id
        HAVING n_flag > 0
        ORDER BY n_flag DESC, d.document_id
        """,
        NON_CLEAN,
    ).fetchall()

    rows_html = []
    for doc in flagged_docs:
        page_path = docs_dir / f"{doc['document_id']}.html"
        _doc_page(conn, doc, page_path, crop_base)
        codes = f"{doc['department_code']}/{doc['municipality_code']}/{doc['zone']}/{doc['puesto']}"
        rows_html.append(
            "<tr>"
            f"<td><a href='docs/{html.escape(doc['document_id'])}.html'>{html.escape(doc['document_id'])}</a></td>"
            f"<td>{html.escape(codes)}</td>"
            f"<td>{html.escape(doc['place_name'] or '')}</td>"
            f"<td><b>{doc['n_flag']}</b> / {doc['n_fields']}</td>"
            "</tr>"
        )
    conn.close()

    def card(n, label):
        return f"<div class='card'><div class='n'>{n}</div><div class='l'>{label}</div></div>"

    non_clean_fields = sum(totals.get(k, 0) for k in NON_CLEAN)
    cards = [
        card(total_docs, "documents"),
        card(len(flagged_docs), "with non-clean"),
        card(total_fields, "vote fields"),
        card(non_clean_fields, "non-clean fields"),
        card(totals.get("UNCLEAR", 0), "UNCLEAR"),
        card(totals.get("SUSPICIOUS_OVERLAP", 0), "SUSPICIOUS_OVERLAP"),
        card(totals.get("DIGIT_SHAPE_ANOMALY", 0), "DIGIT_SHAPE_ANOMALY"),
        card(totals.get("CLEAN", 0), "CLEAN"),
    ]
    index = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>E-14 detector report</title>", STYLE, "</head><body>",
        "<h1>E-14 Detection Report</h1>",
        "<div class='muted'>Every PDF below has at least one non-clean vote field. Open one to see the number crop for each candidate.</div>",
        "<div class='cards'>", *cards, "</div>",
        f"<h2>Flagged documents ({len(flagged_docs)})</h2>",
        "<table><thead><tr><th>Document</th><th>Dept/Mun/Zone/Puesto</th><th>Place</th><th>Non-clean</th></tr></thead><tbody>",
        *rows_html,
        "</tbody></table></body></html>",
    ]
    (web_dir / "index.html").write_text("\n".join(index), encoding="utf-8")
    return {"documents": total_docs, "flagged_documents": len(flagged_docs), "fields": total_fields}
