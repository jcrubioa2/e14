"""HTML contact sheet for verifying source PDFs, field crops, and slots."""
from __future__ import annotations

import html
import os
import sqlite3
from pathlib import Path


def _href(path: str | None, output_html: Path) -> str:
    if not path:
        return ""
    p = Path(path)
    try:
        if p.exists():
            return html.escape(os.path.relpath(p, output_html.parent))
    except OSError:
        pass
    return html.escape(path)


def _img(path: str | None, output_html: Path, css_class: str = "crop") -> str:
    src = _href(path, output_html)
    if not src:
        return "<span class='missing'>missing</span>"
    return f"<a href='{src}'><img class='{css_class}' src='{src}' loading='lazy'></a>"


def _source_link(path: str | None, output_html: Path) -> str:
    href = _href(path, output_html)
    label = html.escape(path or "")
    if not href:
        return ""
    return f"<a href='{href}'>{label}</a>"


def export_crop_audit(
    results_db: Path,
    output_html: Path,
    limit: int | None = None,
    document_id: str | None = None,
) -> int:
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(results_db)
    conn.row_factory = sqlite3.Row
    where = []
    args: list[object] = []
    if document_id:
        where.append("vf.document_id = ?")
        args.append(document_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT ?" if limit else ""
    if limit:
        args.append(limit)
    rows = conn.execute(
        f"""
        SELECT d.source_path,d.department_code,d.department_name,d.municipality_code,
               d.municipality_name,d.zone,d.puesto,d.mesa,d.place_name,d.official_lookup_url,
               vf.document_id,vf.page_number,vf.row_type,vf.row_number,vf.candidate_number,
               vf.candidate_name,vf.raw_crop_path,vf.enhanced_crop_path,vf.debug_crop_path,
               vf.slot_1_crop_path,vf.slot_2_crop_path,vf.slot_3_crop_path,
               vf.slot_1_class,vf.slot_2_class,vf.slot_3_class,
               vf.final_classification,vf.final_reason,vf.placeholder_overlap_score,
               vf.digit_shape_score,vf.read_value,vf.vlm_classification,vf.vlm_confidence
        FROM vote_fields vf
        JOIN documents d ON d.document_id = vf.document_id
        {where_sql}
        ORDER BY vf.document_id,vf.page_number,vf.row_number
        {limit_sql}
        """,
        args,
    ).fetchall()
    conn.close()

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>E-14 crop audit</title>",
        """
        <style>
        body { font-family: system-ui, sans-serif; margin: 20px; color: #202124; }
        table { border-collapse: collapse; width: 100%; table-layout: fixed; }
        th, td { border: 1px solid #d0d7de; padding: 6px; vertical-align: top; font-size: 12px; }
        th { position: sticky; top: 0; background: #f6f8fa; z-index: 1; }
        img.crop { max-width: 190px; max-height: 120px; object-fit: contain; background: white; }
        img.slot { max-width: 90px; max-height: 120px; object-fit: contain; background: white; }
        .meta { font-size: 11px; line-height: 1.35; word-break: break-word; }
        .reason { max-width: 220px; }
        .missing { color: #b42318; }
        </style>
        """,
        "</head><body>",
        "<h1>E-14 Crop Audit</h1>",
        "<p>Use this contact sheet to verify that each row maps from source PDF to field crop to slot crops. Click any image to open it directly.</p>",
        f"<p>Rows: {len(rows)}</p>",
        "<table><thead><tr>",
        "<th>Source / row</th><th>Raw field</th><th>Enhanced</th><th>Debug</th>",
        "<th>Slot 1</th><th>Slot 2</th><th>Slot 3</th><th>Classification</th>",
        "</tr></thead><tbody>",
    ]
    for row in rows:
        row_label = (
            f"<div class='meta'><b>{html.escape(row['document_id'])}</b><br>"
            f"PDF: {_source_link(row['source_path'], output_html)}<br>"
            f"Official: {_source_link(row['official_lookup_url'], output_html)}<br>"
            f"Codes: {row['department_code']}/{row['municipality_code']}/{row['zone']}/{row['puesto']} mesa {row['mesa']}<br>"
            f"Place: {html.escape(row['place_name'] or '')}<br>"
            f"Page {row['page_number']} row {row['row_number']} "
            f"({html.escape(row['row_type'] or '')}, candidate {row['candidate_number'] or ''})<br>"
            f"<b>{html.escape(row['candidate_name'] or '')}</b>"
            "</div>"
        )
        parts.extend([
            "<tr>",
            f"<td>{row_label}</td>",
            f"<td>{_img(row['raw_crop_path'], output_html)}</td>",
            f"<td>{_img(row['enhanced_crop_path'], output_html)}</td>",
            f"<td>{_img(row['debug_crop_path'], output_html)}</td>",
            f"<td>{_img(row['slot_1_crop_path'], output_html, 'slot')}<br>{html.escape(row['slot_1_class'] or '')}</td>",
            f"<td>{_img(row['slot_2_crop_path'], output_html, 'slot')}<br>{html.escape(row['slot_2_class'] or '')}</td>",
            f"<td>{_img(row['slot_3_crop_path'], output_html, 'slot')}<br>{html.escape(row['slot_3_class'] or '')}</td>",
            "<td class='reason'>"
            f"<b>{html.escape(row['final_classification'] or '')}</b><br>"
            f"{html.escape(row['final_reason'] or '')}<br>"
            f"overlap={row['placeholder_overlap_score']} shape={row['digit_shape_score']}"
            + (
                f"<br>VLM: <b>{html.escape(row['vlm_classification'])}</b>"
                f" ({row['vlm_confidence']}) read={html.escape(str(row['read_value'] or ''))}"
                if row['vlm_classification'] else ""
            )
            + "</td>",
            "</tr>",
        ])
    parts.extend(["</tbody></table>", "</body></html>"])
    output_html.write_text("\n".join(parts), encoding="utf-8")
    return len(rows)
