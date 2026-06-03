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
