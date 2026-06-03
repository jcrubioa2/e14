"""Tests for the community-flag poll: dedup, trigger, re-eligibility, rate-limit, privacy."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
from PIL import Image

from e14detector.community import (
    CommunityStore,
    PollConfig,
    field_key_of,
    issue_form_token,
    verify_form_token,
    voter_token,
)
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


def test_appeal_clears_then_is_terminal(tmp_path: Path) -> None:
    """A neutral re-read that comes back CLEAN suppresses the strange mark."""
    store = CommunityStore(tmp_path / "c.sqlite")
    for i in range(2):
        store.record_appeal("k", f"v{i}")
    assert store.try_claim_appeal("k", threshold=3, rescale_step=2) is None  # 2 < 3
    store.record_appeal("k", "v2")
    votes = store.try_claim_appeal("k", threshold=3, rescale_step=2)
    assert votes == 3
    # Concurrent appeal must not double-fire while PENDING.
    store.record_appeal("k", "v3")
    assert store.try_claim_appeal("k", 3, 2) is None
    store.record_appeal_verdict("k", cleared=True, votes_at_call=votes)
    assert store.cleared_among(["k"]) == {"k"}
    assert store.cleared_keys() == ["k"]
    # Cleared is terminal for the appeal: more normal-votes never re-fire it.
    store.record_appeal("k", "v4")
    store.record_appeal("k", "v5")
    assert store.try_claim_appeal("k", 3, 2) is None
    store.close()


def test_appeal_still_strange_is_re_appealable(tmp_path: Path) -> None:
    """If the re-read stays strange, the crop is re-appealable after another step."""
    store = CommunityStore(tmp_path / "c.sqlite")
    for i in range(3):
        store.record_appeal("k", f"v{i}")
    votes = store.try_claim_appeal("k", 3, 2)
    store.record_appeal_verdict("k", cleared=False, votes_at_call=votes)  # still strange
    assert store.cleared_among(["k"]) == set()
    # One more normal-vote: below last+step (3+2=5), no re-fire.
    store.record_appeal("k", "v3")
    assert store.try_claim_appeal("k", 3, 2) is None
    # Climb to 5 -> re-read again; this time CLEAN -> cleared.
    store.record_appeal("k", "v4")
    votes2 = store.try_claim_appeal("k", 3, 2)
    assert votes2 == 5
    store.record_appeal_verdict("k", cleared=True, votes_at_call=votes2)
    assert store.cleared_among(["k"]) == {"k"}
    store.close()


def test_appeal_and_suspicious_tallies_are_independent(tmp_path: Path) -> None:
    """Suspicious votes and normal-votes never share a counter."""
    store = CommunityStore(tmp_path / "c.sqlite")
    store.record_flag("k", "a")
    store.record_appeal("k", "a")  # same identity, opposite direction: both count
    assert store.distinct_votes("k") == 1
    assert store.distinct_appeals("k") == 1
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

    def review_vote_field(self, image_paths, metadata, thinking_budget=None, prompt_text=None,
                          temperature=None) -> VLMReviewResult:
        self.last_prompt_text = prompt_text  # so a test can assert the neutral prompt was used
        return VLMReviewResult(self.classification, 0.9, None, {}, "stub")


class _SplitReviewer:
    """Returns STRANGE for exactly ``strange_count`` of the K reads, CLEAN after — models
    the self-consistency wobble of the undetectable fused-dot class across repeated calls."""

    def __init__(self, strange_count: int) -> None:
        self.strange_count = strange_count
        self._n = 0
        self._lock = threading.Lock()

    def review_vote_field(self, image_paths, metadata, thinking_budget=None, prompt_text=None,
                          temperature=None) -> VLMReviewResult:
        with self._lock:
            i = self._n
            self._n += 1
        cls = (FieldClassification.SUSPICIOUS_OVERLAP if i < self.strange_count
               else FieldClassification.CLEAN)
        return VLMReviewResult(cls, 0.9, None, {}, "stub")


def _build_app(tmp_path: Path, reviewer: _FakeReviewer, seed_strange: bool = False, form_secret: str = ""):
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
    if seed_strange:
        # Mark the field as a Gemma seed so it is shown strange and is appealable.
        import sqlite3 as _sq
        c = _sq.connect(db)
        c.execute("UPDATE vote_fields SET vlm_classification='SUSPICIOUS_OVERLAP' WHERE document_id='doc1'")
        c.commit()
        c.close()

    poll = PollConfig(threshold=3, rescale_step=2, appeal_threshold=3, appeal_rescale_step=2,
                      rate_refill_per_min=10_000, rate_bucket=10_000,
                      turnstile_secret="", voter_salt="t",
                      form_token_secret=form_secret, form_min_seconds=0.0)
    app = create_app(results_db=db, output_dir=output_dir,
                     community_db=tmp_path / "community.sqlite", reviewer=reviewer, poll=poll)
    return app


async def _flag(client: httpx.AsyncClient, field_key: str, ip: str) -> httpx.Response:
    return await client.post("/api/flag", json={"field_key": field_key},
                             headers={"x-forwarded-for": ip})


async def _appeal(client: httpx.AsyncClient, field_key: str, ip: str) -> httpx.Response:
    return await client.post("/api/appeal", json={"field_key": field_key},
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

            # The published badge now shows on the acta detail page; the summary
            # list reflects it as a "marcada por la gente" count.
            detail = (await client.get("/acta/doc1")).text
            assert "marcada como sospechosa" in detail
            summary = (await client.get("/browse")).text
            assert "marcada por la gente" in summary

    asyncio.run(run())


def test_flag_flow_no_consensus_stays_re_eligible(tmp_path: Path, monkeypatch) -> None:
    """A crop the model can't read stably (1 of 3 reads STRANGE) gets NO majority, so it is
    NOT terminally published — it stays re-eligible. This is how the information-undetectable
    fused-dot class is handled honestly: never a false public accusation on a split verdict."""
    from e14detector import config as _config
    monkeypatch.setattr(_config, "POLL_CONSENSUS_K", 3)
    reviewer = _SplitReviewer(strange_count=1)  # 1/3 STRANGE -> 2*1 > 3 is False -> CLEAN
    app = _build_app(tmp_path, reviewer)
    fkey = field_key_of("doc1", 1, 1, None)
    community = app.state.community

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            for i in range(3):
                assert (await _flag(client, fkey, f"10.0.0.{i}")).json() == {"ok": True}
            await _drain(app)
            state = community.state_of(fkey)
            assert state["vlm_state"] == "CLEAN"      # no majority -> not strange
            assert state["published"] == 0            # never falsely published

    asyncio.run(run())


def test_appeal_flow_clears_a_gemma_false_positive(tmp_path: Path) -> None:
    """End-to-end: a Gemma-seeded strange crop, appealed past threshold, is cleared
    by a neutral re-read and disappears from the public 'para revisar' basis."""
    reviewer = _FakeReviewer(FieldClassification.CLEAN)  # neutral re-read says CLEAN
    app = _build_app(tmp_path, reviewer, seed_strange=True)
    fkey = field_key_of("doc1", 1, 1, None)
    community = app.state.community

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # Initially the seed is shown strange and exposes the "Se ve normal" button.
            before = (await client.get("/acta/doc1")).text
            assert 'class="chip seed">para revisar' in before
            assert '<button class="normal"' in before

            # 3 distinct "Se ve normal" votes -> neutral re-read -> CLEAN -> cleared.
            for i in range(3):
                assert (await _appeal(client, fkey, f"10.0.0.{i}")).json() == {"ok": True}
            await _drain(app)
            assert community.cleared_among([fkey]) == {fkey}
            # The neutral appeal prompt (not the fraud-priming one) was used.
            assert reviewer.last_prompt_text and "dirty game" not in reviewer.last_prompt_text

            # The crop is no longer shown strange; the appeal button is gone.
            after = (await client.get("/acta/doc1")).text
            assert 'class="chip seed">para revisar' not in after
            assert '<button class="normal"' not in after

    asyncio.run(run())


def test_appeal_rejected_on_non_strange_field(tmp_path: Path) -> None:
    """The crowd cannot open an appeal on an ordinary (non-strange) crop."""
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.CLEAN), seed_strange=False)
    fkey = field_key_of("doc1", 1, 1, None)

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            res = await _appeal(client, fkey, "10.0.0.1")
            assert res.status_code == 409  # not marked strange

    asyncio.run(run())


def test_flag_rejected_on_already_strange_field(tmp_path: Path) -> None:
    """A crop already shown as strange can't be re-flagged; the appeal path applies."""
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.CLEAN), seed_strange=True)
    fkey = field_key_of("doc1", 1, 1, None)

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            res = await _flag(client, fkey, "10.0.0.1")
            assert res.status_code == 409  # already marked strange

            # The acta page hides the flag button on the strange row (the appeal
            # button is shown instead).
            detail = (await client.get("/acta/doc1")).text
            assert '<button class="flag"' not in detail
            assert '<button class="normal"' in detail

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


def test_browse_floats_most_voted_to_top_silently(tmp_path: Path) -> None:
    """Most-voted actas appear first on /browse (silently), above unvoted ones."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)

    store = DetectorStore(db)
    for doc_id in ("doc-a", "doc-b", "doc-c"):  # none flagged -> region (id) order by default
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    community = CommunityStore(tmp_path / "community.sqlite")
    # doc-c: 2 distinct voters; doc-b: 1; doc-a: 0.
    community.record_flag(field_key_of("doc-c", 1, 1, None), "v1")
    community.record_flag(field_key_of("doc-c", 1, 1, None), "v2")
    community.record_flag(field_key_of("doc-b", 1, 1, None), "v1")
    community.close()

    app = create_app(results_db=db, output_dir=output_dir,
                     community_db=tmp_path / "community.sqlite")

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/browse")).text
            pos = {d: html.index(f"/acta/{d}") for d in ("doc-a", "doc-b", "doc-c")}
            # Vote order wins over the default id order (which would be a < b < c).
            assert pos["doc-c"] < pos["doc-b"] < pos["doc-a"]
            # Silent: no raw vote counts are rendered for the floated actas.
            assert "2 votos" not in html and "votantes" not in html

    asyncio.run(run())


def test_review_page_lists_only_flagged_or_voted_and_hotlist_always_shows(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)

    store = DetectorStore(db)
    for doc_id, klass in (("doc-seed", FieldClassification.SUSPICIOUS_OVERLAP), ("doc-voted", None), ("doc-plain", None)):
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf",
                                               department_code="01", department_name="ANTIOQUIA"))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop), vlm_classification=klass,
        ))
    store.commit()
    store.close()

    community = CommunityStore(tmp_path / "community.sqlite")
    community.record_flag(field_key_of("doc-voted", 1, 1, None), "v1")
    community.close()
    app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "community.sqlite")

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            review = (await client.get("/browse?review=1")).text
            assert "/acta/doc-seed" in review and "/acta/doc-voted" in review
            assert "/acta/doc-plain" not in review  # plain acta is not "to review"

            # Hotlist shows even when a filter is applied.
            filtered = (await client.get("/browse?department=01")).text
            assert "Actas para revisar ahora" in filtered
            assert 'href="/browse?review=1"' in filtered  # "Ver todas" link present

    asyncio.run(run())


def test_high_vote_label_shows_even_when_model_clean(tmp_path: Path, monkeypatch) -> None:
    from e14detector import config
    monkeypatch.setattr(config, "HIGH_VOTE_THRESHOLD", 3)

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(VoteField(  # model says nothing (clean / unseeded)
        document_id="doc1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    community = CommunityStore(tmp_path / "community.sqlite")
    fkey = field_key_of("doc1", 1, 1, None)
    for i in range(3):  # 3 distinct voters >= threshold
        community.record_flag(fkey, f"voter{i}")
    community.close()
    app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "community.sqlite")

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            acta = (await client.get("/acta/doc1")).text
            assert "muy reportada por la comunidad" in acta
            assert 'chip seed">para revisar' not in acta  # not a seed; crowd label stands alone
            browse = (await client.get("/browse")).text
            assert "muy reportada por la comunidad" in browse

    asyncio.run(run())


def test_flag_records_when_crop_is_not_on_local_disk(tmp_path: Path) -> None:
    """National deploy: crops live on the CDN, not the volume. A flag must still record
    (the crop is only fetched when adjudication fires) — regression for the 404 bug."""
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.CLEAN))
    # Remove the local crop so resolve_crop_path would fail (as on the Fly volume).
    (tmp_path / "out" / "crops" / "c.png").unlink()
    fkey = field_key_of("doc1", 1, 1, None)
    community = app.state.community

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            res = await _flag(client, fkey, "10.0.0.1")
            assert res.status_code == 200 and res.json() == {"ok": True}
            assert community.distinct_votes(fkey) == 1  # vote recorded despite no local crop

    asyncio.run(run())


def test_admin_poll_is_token_gated_and_shows_activity(tmp_path: Path, monkeypatch) -> None:
    from e14detector import config as _config
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf",
                                           department_name="ANTIOQUIA", municipality_name="MEDELLIN", mesa="003"))
    store.insert_vote_field(VoteField(document_id="doc1", page_number=1, row_type="candidate",
                                      row_number=1, candidate_name="Cand A", raw_crop_path=str(crop)))
    store.commit(); store.close()
    community = CommunityStore(tmp_path / "community.sqlite")
    fk = field_key_of("doc1", 1, 1, None)
    community.record_flag(fk, "v1"); community.record_flag(fk, "v2")
    community.close()
    app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "community.sqlite")

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            monkeypatch.setattr(_config, "ADMIN_TOKEN", "")
            assert (await client.get("/admin/poll")).status_code == 404          # disabled
            monkeypatch.setattr(_config, "ADMIN_TOKEN", "s3cret")
            assert (await client.get("/admin/poll")).status_code == 403          # no key
            assert (await client.get("/admin/poll?key=wrong")).status_code == 403
            ok = await client.get("/admin/poll?key=s3cret")
            assert ok.status_code == 200
            assert "Cand A" in ok.text and "MEDELLIN" in ok.text  # private data shown

    asyncio.run(run())


def test_admin_review_runs_on_demand_and_can_record(tmp_path: Path, monkeypatch) -> None:
    from e14detector import config as _config
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.SUSPICIOUS_OVERLAP))
    fk = field_key_of("doc1", 1, 1, None)

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            monkeypatch.setattr(_config, "ADMIN_TOKEN", "")
            assert (await client.get(f"/admin/review?field_key={fk}")).status_code == 404
            monkeypatch.setattr(_config, "ADMIN_TOKEN", "k")
            assert (await client.get(f"/admin/review?key=bad&field_key={fk}")).status_code == 403
            # Debug run: returns the verdict, does NOT record.
            j = (await client.get(f"/admin/review?key=k&field_key={fk}")).json()
            assert j["classification"] == "SUSPICIOUS_OVERLAP" and j["strange"] is True
            assert j["recorded"] is False
            assert app.state.community.state_of(fk) is None
            # record=1 applies the verdict to the poll.
            j2 = (await client.get(f"/admin/review?key=k&field_key={fk}&record=1")).json()
            assert j2["recorded"] is True
            assert app.state.community.state_of(fk)["vlm_state"] == "STRANGE"

    asyncio.run(run())


def test_pending_among_reflects_in_flight_review(tmp_path: Path) -> None:
    """A casilla shows 'en revisión' (PENDING) only while its VLM check is in flight."""
    c = CommunityStore(tmp_path / "c.sqlite")
    fk = field_key_of("doc1", 1, 1, None)
    for i in range(3):
        c.record_flag(fk, f"v{i}")
    assert c.pending_among([fk]) == set()           # nothing claimed yet
    assert c.try_claim_adjudication(fk, 3, 2) == 3   # threshold crossed -> PENDING
    assert c.pending_among([fk]) == {fk}
    c.record_verdict(fk, strange=False, votes_at_call=3, image_hash=None)
    assert c.pending_among([fk]) == set()            # resolved -> no longer pending
    c.close()


def test_turnstile_enabled_rejects_missing_token(tmp_path: Path) -> None:
    """When Turnstile is enabled, a flag without a token is rejected; disabled => ignored."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(VoteField(document_id="doc1", page_number=1, row_type="candidate",
                                      row_number=1, candidate_name="A", raw_crop_path=str(crop)))
    store.commit(); store.close()
    poll = PollConfig(threshold=3, rescale_step=2, appeal_threshold=3, appeal_rescale_step=2,
                      rate_refill_per_min=10_000, rate_bucket=10_000, voter_salt="t",
                      turnstile_secret="x", turnstile_enabled=True)
    app = create_app(results_db=db, output_dir=output_dir,
                     community_db=tmp_path / "community.sqlite", poll=poll)
    fkey = field_key_of("doc1", 1, 1, None)

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await _flag(client, fkey, "10.0.0.1")  # no turnstile_token
            assert r.status_code == 403
            assert app.state.community.distinct_votes(fkey) == 0

    asyncio.run(run())


def test_form_token_roundtrip_and_timing() -> None:
    tok = issue_form_token("s", "sid1", now=1000.0)
    # Too fast (age 0.5s < 2s min) is rejected; aged enough passes.
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=3600, now=1000.5) is False
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=3600, now=1005.0) is True
    # Too old is rejected.
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=10, now=1100.0) is False


def test_form_token_rejects_forgery_and_wrong_session() -> None:
    tok = issue_form_token("realsecret", "sid1", now=1000.0)
    assert verify_form_token("realsecret", "sid1", "1000.deadbeefdeadbeef", 0, 3600, now=1005.0) is False
    assert verify_form_token("wrongsecret", "sid1", tok, 0, 3600, now=1005.0) is False
    assert verify_form_token("realsecret", "sid2", tok, 0, 3600, now=1005.0) is False


def test_flag_requires_valid_form_token_and_honeypot_drops_bots(tmp_path: Path) -> None:
    app = _build_app(tmp_path, _FakeReviewer(FieldClassification.CLEAN), form_secret="botsecret")
    fkey = field_key_of("doc1", 1, 1, None)
    community = app.state.community

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # No token -> rejected, nothing recorded.
            r = await client.post("/api/flag", json={"field_key": fkey}, headers={"x-forwarded-for": "10.0.0.1"})
            assert r.status_code == 403 and r.json()["error"] == "invalid_request"
            assert community.distinct_votes(fkey) == 0

            # Real page load issues a valid token; flagging with it works.
            page = (await client.get("/acta/doc1")).text
            import re
            tok = re.search(r'__formToken = "([^"]+)"', page).group(1)
            sid = client.cookies.get("sid")
            assert tok and sid
            ok = await client.post("/api/flag", json={"field_key": fkey, "form_token": tok},
                                   headers={"x-forwarded-for": "10.0.0.1"})
            assert ok.json() == {"ok": True}
            assert community.distinct_votes(fkey) == 1

            # Honeypot filled -> looks like success to the bot, but no vote is recorded.
            hp = await client.post("/api/flag", json={"field_key": fkey, "form_token": tok, "website": "x"},
                                   headers={"x-forwarded-for": "10.0.0.2"})
            assert hp.json() == {"ok": True}
            assert community.distinct_votes(fkey) == 1  # unchanged

    asyncio.run(run())


def test_voter_token_is_per_ip_per_day_not_per_cookie() -> None:
    a = voter_token("salt", "1.1.1.1", day="2026-06-01")
    assert voter_token("salt", "2.2.2.2", day="2026-06-01") != a   # different IP differs
    assert voter_token("salt", "1.1.1.1", day="2026-06-02") != a   # rotates daily
    # Same IP+day is stable — no session/cookie input, so clearing cookies can't re-vote.
    assert voter_token("salt", "1.1.1.1", day="2026-06-01") == a
