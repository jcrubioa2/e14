"""Tests for the community-flag poll: dedup, trigger, re-eligibility, rate-limit, privacy."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from PIL import Image

from e14detector.community import CommunityStore, PollConfig, field_key_of, voter_token
from e14detector.schemas import DocumentMetadata, FieldClassification, VoteField
from e14detector.storage import DetectorStore
from e14detector.vlm.base import VLMReviewResult
from e14detector.webapp import create_app


# --- CommunityStore unit tests --------------------------------------------

def test_dedup_one_vote_per_identity(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.record_flag("k", "voter-a") is True
    assert store.record_flag("k", "voter-a") is False  # same identity, ignored
    assert store.record_flag("k", "voter-b") is True
    assert store.distinct_votes("k") == 2
    store.close()


def test_trigger_only_at_threshold_and_claims_once(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    for i in range(2):
        store.record_flag("k", f"v{i}")
    assert store.try_claim_adjudication("k", threshold=3, rescale_step=2) is None  # 2 < 3
    store.record_flag("k", "v2")
    assert store.try_claim_adjudication("k", threshold=3, rescale_step=2) == 3  # fires
    # Now PENDING: a concurrent flag must not double-fire.
    store.record_flag("k", "v3")
    assert store.try_claim_adjudication("k", threshold=3, rescale_step=2) is None
    store.close()


def test_clean_is_re_eligible_not_terminal(tmp_path: Path) -> None:
    """The security property: one 'clean' verdict cannot bury a crop forever."""
    store = CommunityStore(tmp_path / "c.sqlite")
    for i in range(3):
        store.record_flag("k", f"v{i}")
    votes = store.try_claim_adjudication("k", 3, 2)
    store.record_verdict("k", strange=False, votes_at_call=votes)  # CLEAN at 3 votes
    assert store.state_of("k")["published"] == 0
    # One more vote: not enough to re-open (needs last+step = 3+2 = 5).
    store.record_flag("k", "v3")
    assert store.try_claim_adjudication("k", 3, 2) is None
    # Climb to 5 distinct votes -> re-adjudicate.
    store.record_flag("k", "v4")
    votes2 = store.try_claim_adjudication("k", 3, 2)
    assert votes2 == 5
    store.record_verdict("k", strange=True, votes_at_call=votes2)  # now STRANGE
    assert store.state_of("k")["published"] == 1
    # STRANGE is terminal: further votes never re-trigger.
    store.record_flag("k", "v5")
    assert store.try_claim_adjudication("k", 3, 2) is None
    store.close()


def test_rate_limit_token_bucket(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    # Bucket of 2, negligible refill: third immediate call is denied.
    assert store.allow("t", refill_per_min=0.0, bucket=2) is True
    assert store.allow("t", refill_per_min=0.0, bucket=2) is True
    assert store.allow("t", refill_per_min=0.0, bucket=2) is False
    store.close()


def test_release_pending_allows_retry(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    for i in range(3):
        store.record_flag("k", f"v{i}")
    assert store.try_claim_adjudication("k", 3, 2) == 3
    store.release_pending("k")  # simulate a failed VLM call
    assert store.try_claim_adjudication("k", 3, 2) == 3  # claimable again
    store.close()


# --- Webapp flag-flow integration -----------------------------------------

class _FakeReviewer:
    """Deterministic stand-in for the VLM with a switchable verdict."""

    def __init__(self, classification: FieldClassification) -> None:
        self.classification = classification

    def review_vote_field(self, image_paths, metadata, thinking_budget=None) -> VLMReviewResult:
        return VLMReviewResult(self.classification, 0.9, None, {}, "stub")


def _build_app(tmp_path: Path, reviewer: _FakeReviewer):
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)

    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(
        VoteField(
            document_id="doc1", page_number=1, row_type="candidate", row_number=1,
            candidate_name="Candidate A", raw_crop_path=str(crop),
        )
    )
    store.commit()
    store.close()

    poll = PollConfig(threshold=3, rescale_step=2, rate_refill_per_min=10_000,
                      rate_bucket=10_000, turnstile_secret="", voter_salt="t")
    app = create_app(results_db=db, output_dir=output_dir,
                     community_db=tmp_path / "community.sqlite", reviewer=reviewer, poll=poll)
    return app


async def _flag(client: httpx.AsyncClient, field_key: str, ip: str) -> httpx.Response:
    return await client.post("/api/flag", json={"field_key": field_key},
                             headers={"x-forwarded-for": ip})


async def _drain(app) -> None:
    while app.state._bg_tasks:
        await asyncio.gather(*list(app.state._bg_tasks))


def test_flag_flow_clean_then_strange_via_re_eligibility(tmp_path: Path) -> None:
    """End-to-end: CLEAN keeps it unpublished; rising votes re-open it; STRANGE publishes."""
    reviewer = _FakeReviewer(FieldClassification.CLEAN)
    app = _build_app(tmp_path, reviewer)
    fkey = field_key_of("doc1", 1, 1, None)
    community = app.state.community

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # 3 distinct voters -> triggers a CLEAN adjudication.
            for i in range(3):
                assert (await _flag(client, fkey, f"10.0.0.{i}")).json() == {"ok": True}
            await _drain(app)
            assert community.state_of(fkey)["vlm_state"] == "CLEAN"
            assert community.state_of(fkey)["published"] == 0

            # 4th vote: below re-eligibility threshold (3+2=5), no new adjudication.
            await _flag(client, fkey, "10.0.0.3")
            await _drain(app)
            assert community.state_of(fkey)["vlm_state"] == "CLEAN"

            # Switch the verdict, climb to 5 distinct votes -> re-adjudicate -> STRANGE.
            reviewer.classification = FieldClassification.SUSPICIOUS_OVERLAP
            await _flag(client, fkey, "10.0.0.4")
            await _drain(app)
            assert community.state_of(fkey)["vlm_state"] == "STRANGE"
            assert community.state_of(fkey)["published"] == 1

            # The published badge now shows on the public browse page.
            html = (await client.get("/browse")).text
            assert "marcada como sospechosa" in html

    asyncio.run(run())


def test_unknown_field_rejected_and_counter_never_leaked(tmp_path: Path) -> None:
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.CLEAN))

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            bad = await _flag(client, "doc1:1:999:", "10.0.0.1")
            assert bad.status_code == 404
            ok = await _flag(client, field_key_of("doc1", 1, 1, None), "10.0.0.1")
            # The response body exposes no vote count — only an acknowledgement.
            assert set(ok.json().keys()) == {"ok"}

    asyncio.run(run())


def test_voter_token_distinct_by_ip_and_session() -> None:
    a = voter_token("salt", "1.1.1.1", "sidA", day="2026-06-01")
    b = voter_token("salt", "2.2.2.2", "sidA", day="2026-06-01")
    c = voter_token("salt", "1.1.1.1", "sidB", day="2026-06-01")
    assert a != b and a != c and len({a, b, c}) == 3
