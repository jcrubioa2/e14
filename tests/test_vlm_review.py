from pathlib import Path
from unittest import TestCase

from PIL import Image

from e14detector.schemas import DocumentMetadata, FieldClassification, VoteField
from e14detector.storage import DetectorStore
from e14detector.vlm.base import VLMReviewResult
from e14detector.vlm_review import _normalize_placeholder_result, run_vlm_review


class VlmReviewPassTests(TestCase):
    def _make_field(self, crop_path: Path, **over) -> VoteField:
        base = dict(
            document_id="doc1",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_name="Ivan Cepeda",
            raw_crop_path=str(crop_path),
            final_classification=FieldClassification.UNCLEAR,
            needs_human_review=True,
        )
        base.update(over)
        return VoteField(**base)

    def test_mock_review_persists_and_caches(self) -> None:
        root = Path("/tmp/e14detector-vlm-review-test")
        root.mkdir(parents=True, exist_ok=True)
        db = root / "results" / "results.sqlite"
        if db.exists():
            db.unlink()
        crop = root / "crop.png"
        Image.new("L", (30, 20), color=255).save(crop)

        store = DetectorStore(db)
        store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
        # Two flagged fields sharing the same crop (so the second is a cache hit),
        # plus one CLEAN field that must be skipped.
        store.insert_vote_field(self._make_field(crop, row_number=1))
        store.insert_vote_field(self._make_field(crop, row_number=2))
        store.insert_vote_field(
            self._make_field(crop, row_number=3, final_classification=FieldClassification.CLEAN, needs_human_review=False)
        )
        store.commit()
        store.close()

        totals = run_vlm_review(output_dir=root, provider="mock", concurrency=2, verbose=False)

        # One real review + one cache hit; the CLEAN field is untouched.
        self.assertEqual(totals["reviewed"] + totals["cached"], 2)
        self.assertEqual(totals["failed"], 0)

        store = DetectorStore(db)
        reviewed = store.conn.execute(
            "SELECT COUNT(*) c FROM vote_fields WHERE vlm_classification IS NOT NULL"
        ).fetchone()["c"]
        clean_untouched = store.conn.execute(
            "SELECT vlm_classification FROM vote_fields WHERE row_number=3"
        ).fetchone()["vlm_classification"]
        # Re-running is idempotent: nothing left pending.
        remaining = len(store.fields_needing_vlm())
        store.close()

        self.assertEqual(reviewed, 2)
        self.assertIsNone(clean_untouched)
        self.assertEqual(remaining, 0)

    def test_review_can_be_restricted_to_document(self) -> None:
        root = Path("/tmp/e14detector-vlm-review-document-test")
        root.mkdir(parents=True, exist_ok=True)
        db = root / "results" / "results.sqlite"
        if db.exists():
            db.unlink()
        crop = root / "crop-document.png"
        Image.new("L", (30, 20), color=255).save(crop)

        store = DetectorStore(db)
        store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
        store.upsert_document(DocumentMetadata(document_id="doc2", source_path="doc2.pdf"))
        store.insert_vote_field(self._make_field(crop, document_id="doc1", row_number=1))
        store.insert_vote_field(self._make_field(crop, document_id="doc2", row_number=1))
        store.commit()
        store.close()

        totals = run_vlm_review(output_dir=root, provider="mock", concurrency=1, document_id="doc2", verbose=False)

        self.assertEqual(totals["reviewed"], 1)
        store = DetectorStore(db)
        rows = store.conn.execute(
            "SELECT document_id, vlm_classification FROM vote_fields ORDER BY document_id"
        ).fetchall()
        store.close()

        self.assertIsNone(rows[0]["vlm_classification"])
        self.assertIsNotNone(rows[1]["vlm_classification"])

    def test_placeholder_only_unclear_result_is_downgraded_to_clean(self) -> None:
        row = {
            "slot_1_class": "PLACEHOLDER",
            "slot_2_class": "PLACEHOLDER",
            "slot_3_class": "PLACEHOLDER",
        }
        result = VLMReviewResult(
            classification=FieldClassification.UNCLEAR,
            confidence=0.95,
            read_value="***",
            raw_json={"classification": "UNCLEAR"},
            reason="placeholder marks",
        )

        normalized = _normalize_placeholder_result(row, result)

        self.assertEqual(normalized.classification, FieldClassification.CLEAN)
        self.assertIsNone(normalized.read_value)

    def test_leading_placeholder_with_digits_is_downgraded_to_clean(self) -> None:
        row = {
            "slot_1_class": "UNCLEAR",
            "slot_2_class": "DIGIT",
            "slot_3_class": "DIGIT",
        }
        result = VLMReviewResult(
            classification=FieldClassification.UNCLEAR,
            confidence=0.60,
            read_value="56",
            raw_json={"classification": "UNCLEAR"},
            reason="normal filler and digits",
        )

        normalized = _normalize_placeholder_result(row, result)

        self.assertEqual(normalized.classification, FieldClassification.CLEAN)
        self.assertEqual(normalized.read_value, "56")
