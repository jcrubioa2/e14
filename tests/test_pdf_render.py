from pathlib import Path
from unittest import TestCase

from e14detector.pdf_render import PdfRenderError, render_pdf_pages


class PdfRenderTests(TestCase):
    def test_missing_dependency_or_missing_pdf_reports_render_error(self) -> None:
        with self.assertRaises(PdfRenderError):
            render_pdf_pages(Path("/tmp/does-not-exist.pdf"), pages=(1,), dpi=72)
