"""Quarantine of non-standard-geometry actas: marked, hidden from voting, shown with a notice."""
from __future__ import annotations

from pathlib import Path

import httpx
from PIL import Image

from e14detector.community import CommunityStore, PollConfig, field_key_of
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore
from e14detector.webapp import create_app


def _crop(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color=(255, 255, 255)).save(path)
    return path


def _seed(output_dir: Path, quarantine: bool) -> Path:
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id="doc-a", source_path="doc-a.pdf", department_code="01",
        department_name="ANTIOQUIA", municipality_code="001", municipality_name="MEDELLIN",
        zone="001", puesto="01", mesa="001",
    ))
    store.insert_vote_field(VoteField(
        document_id="doc-a", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    if quarantine:
        assert store.set_quarantined(["doc-a"]) == 1
    store.commit()
    store.close()
    return db


def test_set_quarantined_round_trips(tmp_path: Path) -> None:
    db = _seed(tmp_path / "out", quarantine=True)
    import sqlite3

    con = sqlite3.connect(db)
    val = con.execute("SELECT quarantined FROM documents WHERE document_id='doc-a'").fetchone()[0]
    con.close()
    assert val == 1


def test_quarantined_acta_is_excluded_from_voting_feed(tmp_path: Path) -> None:
    """The only acta is quarantined -> the swipe feed and the per-acta deck serve nothing."""
    output_dir = tmp_path / "out"
    db = _seed(output_dir, quarantine=True)

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            feed = (await client.get("/api/feed?n=12")).json()
            deck = (await client.get("/api/acta-deck")).json()
        assert feed["items"] == []
        assert deck["items"] == []

    import asyncio

    asyncio.run(run())


def test_non_quarantined_acta_is_served_for_voting(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    db = _seed(output_dir, quarantine=False)

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            feed = (await client.get("/api/feed?n=12")).json()
        assert len(feed["items"]) == 1

    import asyncio

    asyncio.run(run())


def test_acta_page_shows_quarantine_notice(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    db = _seed(output_dir, quarantine=True)

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/acta/doc-a")).text
        assert "No fue posible escanear esta acta autom" in html
        # crops are still shown (transparency), so the casilla image is present
        assert "<img" in html

    import asyncio

    asyncio.run(run())


def test_vote_on_quarantined_crop_is_dropped(tmp_path: Path) -> None:
    """A stale cid for a quarantined acta is accepted-and-dropped (no flag recorded)."""
    output_dir = tmp_path / "out"
    db = _seed(output_dir, quarantine=True)
    community_db = output_dir / "community.sqlite"

    # Pre-register a cid pointing at the quarantined acta, as the feed would have before quarantine.
    # The endpoint resolves the cid via cid_index, so its exact value is arbitrary.
    fkey = field_key_of("doc-a", 1, 1, "votacion")
    cid = "stale-cid-123"
    store = CommunityStore(community_db)
    store.register_cid(cid, fkey, "crops/c.png", "doc-a")
    store.close()

    async def run() -> None:
        # Permissive poll config so the anti-bot form-token gate doesn't block the test vote.
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db,
                         poll=PollConfig(form_token_secret="", voter_salt="t"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/vote", json={"cid": cid, "value": "strange"},
                headers={"origin": "http://t"},
            )
        assert r.status_code == 200
        # No flag should have been recorded for the quarantined crop.
        check = CommunityStore(community_db)
        assert check.distinct_votes(fkey) == 0
        check.close()

    import asyncio

    asyncio.run(run())
