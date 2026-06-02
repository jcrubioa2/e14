"""Official lookup metadata helpers."""
from __future__ import annotations

from .schemas import DocumentMetadata


def locator_for_field(meta: DocumentMetadata, page_number: int, row_number: int, candidate_name: str | None) -> dict[str, str | int | None]:
    return {
        "department_code": meta.department_code,
        "municipality_code": meta.municipality_code,
        "zone": meta.zone,
        "puesto": meta.puesto,
        "mesa": meta.mesa,
        "page": page_number,
        "row_number": row_number,
        "candidate_name": candidate_name,
        "instructions": "Open the official government E-14 lookup page and search using these values.",
    }
