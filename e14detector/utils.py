"""Small utility helpers shared by detector modules."""
from __future__ import annotations

import hashlib
import re
import csv
from dataclasses import replace
from pathlib import Path

from .schemas import DocumentMetadata

FILENAME_RE = re.compile(
    r"^E14_(?P<corp>[A-Z]+)_(?P<dep>\d{2})_(?P<muni>\d{3})_"
    r"(?P<zona>\d{3})_(?P<puesto>\d{2})_(?P<mesa>\d{3})_"
    r"(?P<variant>[a-z]+)\.pdf$"
)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_document_metadata(path: Path) -> DocumentMetadata:
    """Parse known scraper filename codes, allowing unknowns for other PDFs."""
    path = Path(path)
    match = FILENAME_RE.match(path.name)
    if not match:
        return DocumentMetadata(
            document_id=path.stem,
            source_path=str(path),
            filename=path.name,
            metadata_confidence=0.2,
            metadata_source="filename_unrecognized",
        )
    parts = match.groupdict()
    document_id = (
        f"E14_{parts['corp']}_{parts['dep']}_{parts['muni']}_"
        f"{parts['zona']}_{parts['puesto']}_{parts['mesa']}_{parts['variant']}"
    )
    return DocumentMetadata(
        document_id=document_id,
        source_path=str(path),
        filename=path.name,
        department_code=parts["dep"],
        municipality_code=parts["muni"],
        zone=parts["zona"],
        puesto=parts["puesto"],
        mesa=parts["mesa"],
        metadata_confidence=0.9,
        metadata_source="filename",
    )


def enrich_metadata_from_index(meta: DocumentMetadata, index_csv: Path = Path("data") / "index.csv") -> DocumentMetadata:
    """Add human-readable names and official URL from scraper index.csv."""
    if not meta.department_code or not meta.municipality_code or not meta.zone or not meta.puesto or not meta.mesa:
        return meta
    if not Path(index_csv).exists():
        return meta

    with Path(index_csv).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (
                row.get("cod_departamento") == meta.department_code
                and row.get("cod_municipio") == meta.municipality_code
                and row.get("cod_zona") == meta.zone
                and row.get("cod_puesto") == meta.puesto
                and row.get("mesa") == meta.mesa
            ):
                return replace(
                    meta,
                    department_name=row.get("departamento") or None,
                    municipality_name=row.get("municipio") or None,
                    place_name=row.get("lugar_votacion") or None,
                    official_lookup_url=row.get("enlace_oficial") or None,
                    metadata_confidence=max(meta.metadata_confidence, 0.95),
                    metadata_source="filename+index",
                )
    return meta
