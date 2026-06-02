from pathlib import Path
from unittest import TestCase

from e14detector.crop_audit import export_crop_audit
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore


class CropAuditTests(TestCase):
    def test_export_crop_audit_html(self) -> None:
        root = Path("/tmp/e14detector-crop-audit-test")
        db = root / "results.sqlite"
        jsonl = root / "results.jsonl"
        if db.exists():
            db.unlink()
        if jsonl.exists():
            jsonl.unlink()
        store = DetectorStore(db, jsonl)
        store.upsert_document(DocumentMetadata(
            document_id="doc1",
            source_path="data/actas/doc1.pdf",
            source_sha256="abc",
            department_code="09",
            municipality_code="079",
            zone="099",
            puesto="05",
            mesa="003",
            place_name="SAN BARTOLOME",
            official_lookup_url="https://official.example/doc1.pdf",
        ))
        store.insert_vote_field(VoteField(
            document_id="doc1",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_number=1,
            raw_crop_path="data/detector/crops/raw.png",
            slot_1_crop_path="data/detector/slots/s1.png",
            slot_2_crop_path="data/detector/slots/s2.png",
            slot_3_crop_path="data/detector/slots/s3.png",
        ))
        store.commit()
        store.close()

        out = root / "crop_audit.html"
        n = export_crop_audit(db, out)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(n, 1)
        self.assertIn("E-14 Crop Audit", text)
        self.assertIn("doc1", text)
        self.assertIn("Slot 1", text)
