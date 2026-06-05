"""PDF rendering helpers for local E-14 actas."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# A few actas declare huge page boxes (e.g. 9000x16000 px @ 300 DPI = 144 MP, ~432 MB
# raw + multi-GB downstream when several workers hit them at once). That spike is what
# OOM-kills crop workers and destabilizes the WSL VM. Cap the output pixel budget so a
# single page can never balloon a worker; tune with E14_MAX_RENDER_MP (0 disables).
DEFAULT_MAX_RENDER_MP = 50.0


def _max_render_megapixels() -> float:
    try:
        return float(os.environ.get("E14_MAX_RENDER_MP", DEFAULT_MAX_RENDER_MP))
    except ValueError:
        return DEFAULT_MAX_RENDER_MP


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
        max_mp = _max_render_megapixels()
        for page_number in pages:
            index = page_number - 1
            if index < 0 or index >= len(doc):
                raise PdfRenderError(f"PAGE_MISSING: {pdf_path}: page {page_number}")
            page = doc.load_page(index)
            # Clamp zoom for pathologically large page boxes so the rendered bitmap
            # (and every array derived from it) stays within a safe memory budget.
            page_zoom = zoom
            rect = page.rect
            projected_mp = (rect.width * zoom) * (rect.height * zoom) / 1e6
            if max_mp > 0 and projected_mp > max_mp:
                page_zoom = zoom * (max_mp / projected_mp) ** 0.5
            matrix = fitz.Matrix(page_zoom, page_zoom)
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
