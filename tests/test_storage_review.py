import sqlite3
from pathlib import Path
from unittest import TestCase

from e14detector.review_export import export_review_cases
from e14detector.schemas import DocumentMetadata, FieldClassification, VoteField
from e14detector.storage import DetectorStore


class StorageReviewTests(TestCase):
    def test_insert_read_and_review_export(self) -> None:
        root = Path("/tmp/e14detector-storage-test")
        db = root / "results.sqlite"
        jsonl = root / "results.jsonl"
        if db.exists():
            db.unlink()
        if jsonl.exists():
            jsonl.unlink()
        store = DetectorStore(db, jsonl)
        meta = DocumentMetadata(
            document_id="doc1",
            source_path="doc1.pdf",
            source_sha256="abc",
            department_code="09",
            municipality_code="079",
            zone="099",
            puesto="05",
            mesa="003",
        )
        store.upsert_document(meta)
        field = VoteField(
            document_id="doc1",
            page_number=1,
            row_type="candidate",
            row_number=1,
            final_classification=FieldClassification.UNCLEAR,
            final_reason="unclear mark; needs human review",
        )
        store.insert_vote_field(field, features={"slots": []})
        store.commit()
        self.assertTrue(store.already_processed("doc1", "abc"))
        store.clear_document_results("doc1")
        store.commit()
        conn = sqlite3.connect(db)
        cleared = conn.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0]
        conn.close()
        self.assertEqual(cleared, 0)
        store.insert_vote_field(field, features={"slots": []})
        store.commit()
        store.close()

        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

        out = root / "review.csv"
        exported = export_review_cases(db, out)
        self.assertEqual(exported, 1)
        self.assertTrue(out.exists())
