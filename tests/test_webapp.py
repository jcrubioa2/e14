import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image

from e14detector import config
from e14detector.schemas import DocumentMetadata, FieldClassification, VoteField
from e14detector.storage import DetectorStore
from e14detector.webapp import create_app, resolve_crop_path


def _crop(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color=(255, 255, 255)).save(path)
    return path


def test_browse_cascading_dropdowns(tmp_path: Path) -> None:
    """Department -> Municipio -> Zona -> Puesto populate only once the parent is chosen."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    # Two departments; ANTIOQUIA has Medellin z001/p01 and Bello z002/p03.
    specs = [
        ("doc-a", "01", "ANTIOQUIA", "001", "MEDELLIN", "001", "01"),
        ("doc-b", "01", "ANTIOQUIA", "088", "BELLO", "002", "03"),
        ("doc-c", "76", "VALLE", "001", "CALI", "010", "05"),
    ]
    for did, dc, dn, mc, mn, z, p in specs:
        store.upsert_document(DocumentMetadata(
            document_id=did, source_path=f"{did}.pdf", department_code=dc, department_name=dn,
            municipality_code=mc, municipality_name=mn, zone=z, puesto=p,
        ))
        store.insert_vote_field(VoteField(
            document_id=did, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    async def get(path: str) -> str:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return (await client.get(path)).text

    async def run() -> None:
        # No department: municipality select is disabled, no municipio options (the
        # "code name" label only appears in the dropdown, not the acta cards).
        html = await get("/browse")
        assert 'name="municipality" data-level="1" disabled' in html
        assert "001 MEDELLIN" not in html

        # Pick ANTIOQUIA: its municipio options appear; VALLE's CALI is gone; zona disabled.
        html = await get("/browse?department=01")
        assert "001 MEDELLIN" in html and "088 BELLO" in html and "CALI" not in html
        assert 'name="zone" data-level="2" disabled' in html

        # Pick Medellin: zona drop-down is now enabled (populated).
        html = await get("/browse?department=01&municipality=001")
        assert 'name="zone" data-level="2" disabled' not in html

        # Full drill-down narrows to the one acta.
        html = await get("/browse?department=01&municipality=001&zone=001&puesto=01")
        assert "/acta/doc-a" in html and "/acta/doc-b" not in html

        # A child filter without its parent is ignored (not 500).
        html = await get("/browse?municipality=001")
        assert "/acta/doc-a" in html and "/acta/doc-c" in html

        # The AJAX places endpoint returns the next level's options (no full reload).
        places = json.loads(await get("/api/places?department=01"))
        labels = {o["label"] for o in places["options"]}
        assert "001 MEDELLIN" in labels and "088 BELLO" in labels
        zonas = json.loads(await get("/api/places?department=01&municipality=001"))
        assert any(o["value"] == "001" for o in zonas["options"])

    asyncio.run(run())


def test_browse_shows_national_sync_progress(tmp_path: Path, monkeypatch) -> None:
    """The public /browse page shows rollout progress: synced/total, %, and an ETA."""
    monkeypatch.setattr(config, "NATIONAL_TOTAL_ACTAS", 100)
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    now = datetime.now(timezone.utc)
    # Two browsable actas processed an hour apart -> a measurable rate -> an ETA.
    for i, ts in enumerate([now - timedelta(hours=1), now - timedelta(minutes=1)]):
        doc_id = f"doc-{i}"
        store.upsert_document(DocumentMetadata(
            document_id=doc_id, source_path=f"{doc_id}.pdf",
            processing_timestamp=ts.isoformat(),
        ))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/browse")).text
            assert "2 de 100 actas" in html
            assert "2.0%" in html
            assert "Cargando las actas" in html
            assert "Tiempo restante estimado" in html
            assert "actualizaci" in html  # "Última actualización ..."

    asyncio.run(run())


def test_acta_crop_src_uses_cdn_when_configured(tmp_path: Path, monkeypatch) -> None:
    """With a CDN base set, crop <img> points at the CDN; unset falls back to /crop."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "abc.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    async def fetch() -> str:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return (await client.get("/acta/doc1")).text

    # Default: in-app /crop endpoint.
    assert "/crop?path=" in asyncio.run(fetch())

    # Configured: the CDN URL, keyed by the crops/ suffix.
    monkeypatch.setattr(config, "CDN_BASE_URL", "https://cdn.example.com")
    html = asyncio.run(fetch())
    assert 'src="https://cdn.example.com/crops/abc.png"' in html
    assert "/crop?path=" not in html


def test_retired_verdict_routes_gone_and_crop_traversal_blocked(tmp_path: Path) -> None:
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
            # The verdict/anomaly analyst surface was retired with the move to crowd-only
            # vote counting — these routes no longer exist.
            assert (await client.get("/api/flagged")).status_code == 404
            assert (await client.get("/panel")).status_code == 404
            assert (await client.get("/doc/doc-candidate")).status_code == 404
            # / still redirects to the public crowd-voting browser.
            assert (await client.get("/")).status_code == 308

            crop_path = os.path.relpath(candidate_crop, Path.cwd())
            assert resolve_crop_path(crop_path, output_dir) == candidate_crop.resolve()
            traversal = await client.get(f"/crop?path={outside}")
            assert traversal.status_code == 403

    asyncio.run(run_checks())


def test_feed_random_pk_sampling(tmp_path: Path) -> None:
    """The swipe feed (random-PK sampling) fills to n, dedups, and never serves
    non-candidate or crop-less rows. See _feed_payload."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-feed", source_path="doc-feed.pdf"))
    # 12 valid candidate crops (the only things the feed may return).
    for i in range(12):
        store.insert_vote_field(VoteField(
            document_id="doc-feed", page_number=1, row_type="candidate", row_number=i + 1,
            candidate_name=f"C{i}", raw_crop_path=str(crop),
        ))
    # An invalid pair the feed must skip: a non-candidate row, and a crop-less candidate.
    store.insert_vote_field(VoteField(
        document_id="doc-feed", page_number=1, row_type="summary", row_number=99,
        section="total", raw_crop_path=str(crop),
    ))
    store.insert_vote_field(VoteField(
        document_id="doc-feed", page_number=2, row_type="candidate", row_number=1,
        candidate_name="no-crop", raw_crop_path=None,
    ))
    store.commit()
    store.close()

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # Asking for more than exist returns exactly the 12 valid candidates (the 2
            # invalid rows are filtered out), all distinct.
            big = (await client.get("/api/feed?n=50")).json()["items"]
            cids = [it["cid"] for it in big]
            assert len(cids) == 12
            assert len(set(cids)) == 12
            assert all(it["img_url"] == f"/c/{it['cid']}" for it in big)

            # exclude is honored: excluding 4 cids yields 4 different ones.
            excl = ",".join(cids[:4])
            rest = (await client.get(f"/api/feed?n=4&exclude={excl}")).json()["items"]
            got = {it["cid"] for it in rest}
            assert len(got) == 4
            assert got.isdisjoint(set(cids[:4]))

    asyncio.run(run())


def test_acta_deck_returns_one_actas_crops_anonymized(tmp_path: Path) -> None:
    """/api/acta-deck returns every candidate crop of ONE acta, shuffled, with no acta id /
    location leaking into the payload."""
    from e14detector.community import crop_id, field_key_of

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    # Two actas so the endpoint must pick exactly one and never mix them.
    doc_specs = {"doc-a": 5, "doc-b": 4}
    cid_to_doc: dict[str, str] = {}
    for doc_id, n in doc_specs.items():
        store.upsert_document(DocumentMetadata(
            document_id=doc_id, source_path=f"{doc_id}.pdf",
            department_name="ANTIOQUIA", municipality_name="MEDELLIN", mesa="01",
        ))
        for i in range(n):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
            fkey = field_key_of(doc_id, 1, i + 1, None)
            cid_to_doc[crop_id(config.FORM_TOKEN_SECRET, fkey)] = doc_id
    store.commit()
    store.close()

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/acta-deck")
            items = r.json()["items"]
            cids = [it["cid"] for it in items]
            # All crops come from a single acta, and it's the WHOLE acta (all its crops).
            docs = {cid_to_doc[c] for c in cids}
            assert len(docs) == 1
            assert len(cids) == doc_specs[docs.pop()]
            assert len(set(cids)) == len(cids)               # no dupes
            assert all(it["img_url"] == f"/c/{it['cid']}" for it in items)
            # The payload must not leak the acta id or location.
            body = r.text
            assert "doc-a" not in body and "doc-b" not in body
            assert "ANTIOQUIA" not in body and "MEDELLIN" not in body

    asyncio.run(run())


def test_vote_batch_records_strange_and_good(tmp_path: Path) -> None:
    """A batch submit flags the marked cids ('strange') and appeals the rest ('good')."""
    import dataclasses
    from e14detector.community import CommunityStore, PollConfig, crop_id, field_key_of

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-x", source_path="doc-x.pdf"))
    for i in range(3):
        store.insert_vote_field(VoteField(
            document_id="doc-x", page_number=1, row_type="candidate", row_number=i + 1,
            candidate_name=f"C{i}", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    # Empty form-token secret => skip the bot check (no 2s token-age wait in the test).
    cfg = dataclasses.replace(PollConfig.from_config(), form_token_secret="")
    fkeys = [field_key_of("doc-x", 1, i + 1, None) for i in range(3)]
    fkey_by_cid = {crop_id("", fk): fk for fk in fkeys}
    marked_fkey: dict[str, str] = {}

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db, poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # Surfacing the deck registers the cids the batch will resolve.
            deck = (await client.get("/api/acta-deck")).json()["items"]
            cids = [it["cid"] for it in deck]
            assert len(cids) == 3
            marked_fkey["strange"] = fkey_by_cid[cids[0]]
            marked_fkey["good"] = fkey_by_cid[cids[1]]
            # Mark the first crop strange; the other two go through as 'good'.
            r = await client.post("/api/vote-batch", json={"strange": [cids[0]], "good": cids[1:]})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True and body["strange"] == 1 and body["good"] == 2

    asyncio.run(run())

    cs = CommunityStore(community_db)
    counts = cs.counts_among(fkeys)
    cs.close()
    assert counts[marked_fkey["strange"]]["strange"] == 1 and counts[marked_fkey["strange"]]["good"] == 0
    assert counts[marked_fkey["good"]]["good"] == 1 and counts[marked_fkey["good"]]["strange"] == 0
    # The third (unmarked) crop is a 'good' vote too; total good == 2.
    assert sum(c["good"] for c in counts.values()) == 2
    assert sum(c["strange"] for c in counts.values()) == 1


def test_browse_shows_billboard(tmp_path: Path) -> None:
    """A reported acta shows on /browse's community billboard (one card per mesa), linking to it."""
    from e14detector.community import CommunityStore, field_key_of

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id="doc-hot", source_path="doc-hot.pdf",
        department_name="VALLE", municipality_name="CALI", mesa="07",
    ))
    store.insert_vote_field(VoteField(
        document_id="doc-hot", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    # Seed one community flag so the crop ranks on the billboard.
    cs = CommunityStore(community_db)
    cs.record_flag(field_key_of("doc-hot", 1, 1, None), "voter-1")
    cs.close()

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/browse")).text
            assert "Actas m" in html  # "Actas más reportadas por la comunidad"
            assert "reportaron" in html  # the per-acta tally line
            assert "/acta/doc-hot" in html
            assert "VALLE" in html and "CALI" in html

    asyncio.run(run())


def _drop_n_candidates(db: Path) -> None:
    """Rebuild documents WITHOUT n_candidates, simulating an older/raw serving snapshot.
    (A plain DROP COLUMN trips on the inline comment in the table DDL, so recreate instead.)"""
    import sqlite3

    con = sqlite3.connect(db)
    keep = [r[1] for r in con.execute("PRAGMA table_info(documents)") if r[1] != "n_candidates"]
    cols = ", ".join(keep)
    con.executescript(
        f"CREATE TABLE documents_old AS SELECT {cols} FROM documents;\n"
        "DROP TABLE documents;\n"
        "ALTER TABLE documents_old RENAME TO documents;"
    )
    con.commit()
    assert "n_candidates" not in {r[1] for r in con.execute("PRAGMA table_info(documents)")}
    con.close()


def test_ensure_n_candidates_backfills_and_browse_works(tmp_path: Path) -> None:
    """A served snapshot missing the precomputed n_candidates column is backfilled at load
    (ensure_n_candidates) so /browse stays on the fast path and never 500s (prod incident)."""
    import sqlite3

    from e14detector.webapp import ensure_n_candidates

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    # doc-has: two candidate crops (count must be 2); doc-none: none (filtered out of /browse).
    store.upsert_document(DocumentMetadata(
        document_id="doc-has", source_path="doc-has.pdf",
        department_code="05", department_name="ANTIOQUIA", municipality_name="MEDELLIN",
    ))
    for i in range(2):
        store.insert_vote_field(VoteField(
            document_id="doc-has", page_number=1, row_type="candidate", row_number=i + 1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
    store.upsert_document(DocumentMetadata(document_id="doc-none", source_path="doc-none.pdf"))
    store.commit()
    store.close()

    _drop_n_candidates(db)

    # Backfill: idempotent + correct count, and the column is present afterwards.
    assert ensure_n_candidates(db) is True
    con = sqlite3.connect(db)
    counts = dict(con.execute("SELECT document_id, n_candidates FROM documents"))
    con.close()
    assert counts == {"doc-has": 2, "doc-none": 0}
    assert ensure_n_candidates(db) is True  # second call is a no-op, still True

    async def run() -> None:
        # create_app calls ensure_n_candidates itself; /browse uses the fast n_candidates column.
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/browse")
            assert r.status_code == 200
            html = r.text
            assert "/acta/doc-has" in html and "/acta/doc-none" not in html
            assert "2 candidatos" in html

    asyncio.run(run())


def test_ensure_n_candidates_boot_recovers_missing_column(tmp_path: Path) -> None:
    """Even without calling ensure_n_candidates by hand, building the app on a column-less DB
    recovers it (the create_app boot hook) — /browse responds 200, not 500."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-x", source_path="doc-x.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-x", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()
    _drop_n_candidates(db)

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/browse")).status_code == 200

    asyncio.run(run())


def test_security_headers_and_docs_hidden(tmp_path: Path) -> None:
    """Hardening: docs/openapi are 404 by default, and baseline headers are on every response."""
    db = tmp_path / "results.sqlite"
    output_dir = tmp_path / "out"
    DetectorStore(db).close()

    async def run():
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for p in ("/docs", "/redoc", "/openapi.json"):
                assert (await client.get(p)).status_code == 404
            r = await client.get("/votar")
            assert r.headers.get("x-content-type-options") == "nosniff"
            assert r.headers.get("x-frame-options") == "DENY"
            assert "frame-ancestors" in r.headers.get("content-security-policy", "")

    asyncio.run(run())


def test_origin_allowlist_blocks_cross_site_votes(tmp_path: Path, monkeypatch) -> None:
    """With an allowlist set, a foreign Origin is rejected; same-origin and header-less pass."""
    from e14detector.webapp import _origin_allowed
    from types import SimpleNamespace

    def req(origin=None):
        h = {} if origin is None else {"origin": origin}
        return SimpleNamespace(headers=h)

    # Not configured -> never blocks.
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", [])
    assert _origin_allowed(req("https://evil.example")) is True

    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://veeduria-ciudadana-elecciones-colombia-2026.com"])
    assert _origin_allowed(req("https://evil.example")) is False           # cross-site browser
    assert _origin_allowed(req("https://veeduria-ciudadana-elecciones-colombia-2026.com")) is True
    assert _origin_allowed(req("https://veeduria-ciudadana-elecciones-colombia-2026.com/")) is True  # trailing slash
    assert _origin_allowed(req(None)) is True                              # non-browser client


def test_api_session_mints_token_when_turnstile_off(tmp_path: Path) -> None:
    """With Turnstile off, /api/session returns a usable form token (uniform client flow)."""
    db = tmp_path / "results.sqlite"
    DetectorStore(db).close()

    async def run():
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=tmp_path / "out"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/api/session", json={})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True and body["form_token"]

    asyncio.run(run())


def test_api_session_requires_turnstile_when_enabled(tmp_path: Path, monkeypatch) -> None:
    """With Turnstile on, /api/session gates the form token on a passing challenge."""
    import dataclasses
    from e14detector import webapp as wa
    from e14detector.community import PollConfig

    db = tmp_path / "results.sqlite"
    DetectorStore(db).close()
    cfg = dataclasses.replace(
        PollConfig.from_config(), turnstile_enabled=True,
        turnstile_sitekey="0xSITE", turnstile_secret="sekret",
    )

    async def run(expect_ok: bool):
        app = create_app(results_db=db, output_dir=tmp_path / "out", poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # /votar withholds the inline token and ships the widget when Turnstile is on.
            page = (await client.get("/votar")).text
            assert 'window.__formToken = "";' in page
            assert "challenges.cloudflare.com/turnstile" in page
            r = await client.post("/api/session", json={"turnstile_token": "tok"})
            return r

    monkeypatch.setattr(wa, "verify_turnstile", lambda *a, **k: False)
    r = asyncio.run(run(False))
    assert r.status_code == 403 and r.json()["error"] == "challenge_failed"

    monkeypatch.setattr(wa, "verify_turnstile", lambda *a, **k: True)
    r = asyncio.run(run(True))
    assert r.status_code == 200 and r.json()["form_token"]
