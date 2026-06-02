from pathlib import Path
from unittest import TestCase

from e14detector.utils import enrich_metadata_from_index, parse_document_metadata


class UtilsTests(TestCase):
    def test_parse_known_scraper_filename(self) -> None:
        meta = parse_document_metadata(Path("data/actas/09/079/099/05/E14_PRE_09_079_099_05_003_delegados.pdf"))
        self.assertEqual(meta.document_id, "E14_PRE_09_079_099_05_003_delegados")
        self.assertEqual(meta.department_code, "09")
        self.assertEqual(meta.municipality_code, "079")
        self.assertEqual(meta.zone, "099")
        self.assertEqual(meta.puesto, "05")
        self.assertEqual(meta.mesa, "003")
        self.assertEqual(meta.metadata_source, "filename")

    def test_parse_alphanumeric_puesto(self) -> None:
        # Special "zona 099 / puesto A1" tables have an alphanumeric puesto.
        meta = parse_document_metadata(Path("E14_PRE_01_001_099_A1_002_delegados.pdf"))
        self.assertEqual(meta.department_code, "01")
        self.assertEqual(meta.zone, "099")
        self.assertEqual(meta.puesto, "A1")
        self.assertEqual(meta.mesa, "002")
        self.assertEqual(meta.metadata_source, "filename")

    def test_parse_unknown_filename_degrades_cleanly(self) -> None:
        meta = parse_document_metadata(Path("example.pdf"))
        self.assertEqual(meta.document_id, "example")
        self.assertIsNone(meta.department_code)
        self.assertEqual(meta.metadata_source, "filename_unrecognized")

    def test_enrich_metadata_from_index(self) -> None:
        index = Path("/tmp/e14detector-index-test.csv")
        index.write_text(
            "cod_departamento,departamento,cod_municipio,municipio,cod_zona,zona,cod_puesto,lugar_votacion,mesa,archivo,enlace_oficial\n"
            "09,CALDAS,079,PACORA,099,ZONA 99,05,SAN BARTOLOME,003,path.pdf,https://official.example/doc.pdf\n",
            encoding="utf-8",
        )
        meta = parse_document_metadata(Path("E14_PRE_09_079_099_05_003_delegados.pdf"))
        enriched = enrich_metadata_from_index(meta, index)
        self.assertEqual(enriched.department_name, "CALDAS")
        self.assertEqual(enriched.municipality_name, "PACORA")
        self.assertEqual(enriched.place_name, "SAN BARTOLOME")
        self.assertEqual(enriched.official_lookup_url, "https://official.example/doc.pdf")
        self.assertEqual(enriched.metadata_source, "filename+index")
