import asyncio
import os
from pathlib import Path

import httpx
from PIL import Image

from e14detector.schemas import DocumentMetadata, FieldClassification, VoteField
from e14detector.storage import DetectorStore
from e14detector.webapp import create_app, resolve_crop_path


def _crop(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color=(255, 255, 255)).save(path)
    return path


def test_flagged_api_excludes_summary_only_confirmations_and_crop_traversal(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    candidate_crop = _crop(output_dir / "crops" / "candidate.png")
    summary_crop = _crop(output_dir / "crops" / "summary.png")

    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-summary", source_path="doc-summary.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc-summary",
            page_number=1,
            row_type="summary",
            row_number=14,
            section="total_votos",
            raw_crop_path=str(summary_crop),
            cv_classification=FieldClassification.SUSPICIOUS_OVERLAP,
            final_classification=FieldClassification.SUSPICIOUS_OVERLAP,
            vlm_classification=FieldClassification.SUSPICIOUS_OVERLAP,
            vlm_confidence=0.99,
        )
    )
    store.upsert_document(DocumentMetadata(document_id="doc-candidate", source_path="doc-candidate.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc-candidate",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_name="Candidate A",
            raw_crop_path=str(candidate_crop),
            cv_classification=FieldClassification.DIGIT_SHAPE_ANOMALY,
            final_classification=FieldClassification.DIGIT_SHAPE_ANOMALY,
            vlm_classification=FieldClassification.DIGIT_SHAPE_ANOMALY,
            vlm_confidence=0.88,
        )
    )
    store.upsert_document(DocumentMetadata(document_id="doc-unclear", source_path="doc-unclear.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc-unclear",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_name="Candidate B",
            raw_crop_path=str(candidate_crop),
            cv_classification=FieldClassification.UNCLEAR,
            final_classification=FieldClassification.UNCLEAR,
            vlm_classification=FieldClassification.UNCLEAR,
            vlm_confidence=0.6,
        )
    )
    # Strong CV catch that the (flaky) VLM tried to clear: must stay visible.
    store.upsert_document(DocumentMetadata(document_id="doc-veto", source_path="doc-veto.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc-veto",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_name="Candidate D",
            raw_crop_path=str(candidate_crop),
            cv_classification=FieldClassification.SUSPICIOUS_OVERLAP,
            final_classification=FieldClassification.SUSPICIOUS_OVERLAP,
            vlm_classification=FieldClassification.CLEAN,
            vlm_confidence=0.95,
        )
    )
    store.upsert_document(DocumentMetadata(document_id="doc-clean", source_path="doc-clean.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc-clean",
            page_number=1,
            row_type="candidate",
            row_number=1,
            candidate_name="Candidate C",
            raw_crop_path=str(candidate_crop),
            cv_classification=FieldClassification.UNCLEAR,
            final_classification=FieldClassification.UNCLEAR,
            vlm_classification=FieldClassification.CLEAN,
            vlm_confidence=0.95,
        )
    )
    store.commit()
    store.close()

    outside = tmp_path / "outside.png"
    _crop(outside)

    async def run_checks() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            payload = (await client.get("/api/flagged")).json()
            assert payload["total"] == 3
            # doc-veto (strong CV, VLM CLEAN) is protected; doc-clean (marginal
            # UNCLEAR, VLM CLEAN) is correctly pruned by the VLM.
            assert {item["document_id"] for item in payload["items"]} == {
                "doc-veto",
                "doc-candidate",
                "doc-unclear",
            }

            dashboard = await client.get("/")
            assert dashboard.status_code == 200
            assert "doc-candidate" in dashboard.text
            assert "doc-unclear" in dashboard.text
            assert "doc-veto" in dashboard.text
            assert "doc-summary" not in dashboard.text
            assert "doc-clean" not in dashboard.text

            crop_path = os.path.relpath(candidate_crop, Path.cwd())
            assert resolve_crop_path(crop_path, output_dir) == candidate_crop.resolve()
            traversal = await client.get(f"/crop?path={outside}")
            assert traversal.status_code == 403

    asyncio.run(run_checks())
