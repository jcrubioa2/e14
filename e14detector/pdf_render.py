"""PDF rendering helpers for local E-14 actas."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class PdfRenderError(RuntimeError):
    """Raised when a PDF cannot be rendered."""


@dataclass(frozen=True)
class RenderedPage:
    pdf_path: Path
    page_number: int
    width: int
    height: int
    image: object


def _load_fitz():
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise PdfRenderError(
            "PyMuPDF/fitz is not installed. Install project dependencies before rendering PDFs."
        ) from exc
    return fitz


def render_pdf_pages(pdf_path: Path, pages: Iterable[int] = (1, 2), dpi: int = 300) -> list[RenderedPage]:
    """Render 1-based page numbers to PIL images.

    PIL conversion is local to this function so non-rendering commands can run
    without importing Pillow at module import time.
    """
    fitz = _load_fitz()
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:
        raise PdfRenderError("Pillow is not installed. Install project dependencies before rendering PDFs.") from exc

    pdf_path = Path(pdf_path)
    rendered: list[RenderedPage] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise PdfRenderError(f"PDF_RENDER_FAILED: {pdf_path}: {exc}") from exc

    try:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page_number in pages:
            index = page_number - 1
            if index < 0 or index >= len(doc):
                raise PdfRenderError(f"PAGE_MISSING: {pdf_path}: page {page_number}")
            page = doc.load_page(index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            rendered.append(RenderedPage(
                pdf_path=pdf_path,
                page_number=page_number,
                width=pix.width,
                height=pix.height,
                image=image,
            ))
    finally:
        doc.close()
    return rendered
