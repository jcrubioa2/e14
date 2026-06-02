"""Export suspicious and unclear detector rows for human review."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


REVIEW_CLASSES = ("SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY", "UNCLEAR")


def export_review_cases(results_db: Path, output_csv: Path) -> int:
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(results_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT d.department_code,d.department_name,d.municipality_code,d.municipality_name,
               d.zone,d.puesto,d.mesa,d.official_lookup_url,
               vf.document_id,vf.page_number,vf.row_type,vf.row_number,vf.candidate_name,
               vf.read_value,vf.final_classification,vf.final_reason,vf.anomaly_tags,
               vf.placeholder_overlap_score,vf.digit_shape_score,vf.shape_anomaly_slot,
               vf.shape_anomaly_digit,vf.cv_score,vf.vlm_classification,vf.vlm_confidence,
               vf.comparison_notes,vf.raw_crop_path,vf.enhanced_crop_path,vf.debug_crop_path,
               vf.comparison_crop_path
        FROM vote_fields vf
        JOIN documents d ON d.document_id = vf.document_id
        WHERE vf.final_classification IN (?,?,?)
        ORDER BY vf.document_id,vf.page_number,vf.row_number
        """,
        REVIEW_CLASSES,
    ).fetchall()
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        if rows:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) + ["official_lookup_instructions"])
            writer.writeheader()
            for row in rows:
                data = dict(row)
                data["official_lookup_instructions"] = "Open the official government E-14 lookup page and search using the listed codes."
                writer.writerow(data)
        else:
            writer = csv.writer(fh)
            writer.writerow(["document_id", "final_classification", "final_reason"])
    conn.close()
    return len(rows)
