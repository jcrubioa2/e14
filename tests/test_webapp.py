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
from e14detector.webapp import (
    create_app,
    doc_muni_index,
    municipio_report_stats,
    resolve_crop_path,
)
from e14detector.community import CommunityStore, field_key_of
from e14detector.webapp import load_geo_names


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
        html = await get("/buscar")
        assert 'name="municipality" data-level="1" disabled' in html
        assert "001 MEDELLIN" not in html

        # Pick ANTIOQUIA: its municipio options appear; VALLE's CALI is gone; zona disabled.
        html = await get("/buscar?department=01")
        assert "001 MEDELLIN" in html and "088 BELLO" in html and "CALI" not in html
        assert 'name="zone" data-level="2" disabled' in html

        # Pick Medellin: zona drop-down is now enabled (populated).
        html = await get("/buscar?department=01&municipality=001")
        assert 'name="zone" data-level="2" disabled' not in html

        # Full drill-down narrows to the one acta.
        html = await get("/buscar?department=01&municipality=001&zone=001&puesto=01")
        assert "/acta/doc-a" in html and "/acta/doc-b" not in html

        # A child filter without its parent is ignored (not 500).
        html = await get("/buscar?municipality=001")
        assert "/acta/doc-a" in html and "/acta/doc-c" in html

        # The AJAX places endpoint returns the next level's options (no full reload).
        places = json.loads(await get("/api/places?department=01"))
        labels = {o["label"] for o in places["options"]}
        assert "001 MEDELLIN" in labels and "088 BELLO" in labels
        zonas = json.loads(await get("/api/places?department=01&municipality=001"))
        assert any(o["value"] == "001" for o in zonas["options"])

    asyncio.run(run())


def test_browse_shows_national_sync_progress(tmp_path: Path, monkeypatch) -> None:
    """The shared app bar shows national-load progress (pct) while the rollout is incomplete.
    (The old /browse synced/total/ETA panel was retired when /browse split into /buscar+/reportes;
    the compact app-bar label is what remains, and it renders on every page incl. /buscar.)"""
    # The denominator now comes from the count-model reconciliation; with no published pointer
    # in tests, the E14_NATIONAL_TOTAL env override supplies it (replaces the old constant).
    monkeypatch.setenv("E14_NATIONAL_TOTAL", "100")
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
            html = (await client.get("/buscar")).text
            assert "Cargando las actas a nivel nacional" in html
            assert "2.0%" in html  # 2 of 100 actas synced -> pct rounded to one decimal

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

    # Configured: the CDN URL, keyed by the OPAQUE crop key (no readable path leaks).
    from e14detector.webapp import crop_key
    monkeypatch.setattr(config, "CDN_BASE_URL", "https://cdn.example.com")
    html = asyncio.run(fetch())
    assert f'src="https://cdn.example.com/{crop_key(str(crop))}"' in html
    assert "/crop?path=" not in html
    assert "abc.png" not in html  # the readable mesa-identifying name never reaches the page


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
            # / still redirects to the public crowd-voting feed (307 temporary, not 308 —
            # the landing default moved to /votar and must stay re-checkable).
            assert (await client.get("/")).status_code == 307

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


def test_crop_key_is_opaque_and_keyed(monkeypatch) -> None:
    """The crop object key == the public CDN URL path, so it must reveal nothing about the acta:
    an HMAC of the path, not the readable, mesa-identifying filename."""
    from e14detector import config
    from e14detector.webapp import crop_key

    raw = "data/out/crops/E14_PRE_01_001_001_01_001_delegados_p1_row1_candidate_field.png"
    key = crop_key(raw)
    # Opaque: crops/<24 hex>.png, with no readable fragment of the original path.
    assert key.startswith("crops/") and key.endswith(".png")
    for leak in ("E14_PRE", "delegados", "row1", "candidate"):
        assert leak not in key
    name = key[len("crops/"):-len(".png")]
    assert len(name) == 24 and all(c in "0123456789abcdef" for c in name)
    # Deterministic, distinct per path, and secret-keyed (rotating the secret rotates the key).
    assert crop_key(raw) == key
    other = "data/out/crops/E14_PRE_01_001_001_01_002_delegados_p1_row1_candidate_field.png"
    assert crop_key(other) != key
    monkeypatch.setattr(config, "CROP_KEY_SECRET", config.CROP_KEY_SECRET + "rotated")
    assert crop_key(raw) != key


def test_feed_serves_opaque_cdn_url_without_leaking_path(tmp_path: Path, monkeypatch) -> None:
    """With a CDN configured the swipe feed serves the crop straight from the CDN at its OPAQUE
    key — no /c redirect hop, and no acta id / location / readable path anywhere in the payload."""
    from e14detector import config
    from e14detector.webapp import crop_cdn_url

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" /
                 "E14_PRE_01_001_001_01_001_delegados_p1_row1_candidate_field.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id="E14_PRE_01_001_001_01_001", source_path="x.pdf",
        department_name="ANTIOQUIA", municipality_name="MEDELLIN", mesa="01"))
    store.insert_vote_field(VoteField(
        document_id="E14_PRE_01_001_001_01_001", page_number=1, row_type="candidate",
        row_number=1, candidate_name="A", raw_crop_path=str(crop)))
    store.commit()
    store.close()

    monkeypatch.setattr(config, "CDN_BASE_URL", "https://cdn.example.com")

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/feed?n=5")
            items = r.json()["items"]
            assert items
            it = items[0]
            # Direct opaque CDN URL (the fix), not a /c/{cid} redirect.
            assert it["img_url"] == crop_cdn_url(str(crop), config.CDN_BASE_URL)
            assert it["img_url"].startswith("https://cdn.example.com/crops/")
            assert not it["img_url"].startswith("/c/")
            # Nothing in the payload identifies the mesa / location / readable crop path.
            body = r.text
            for leak in ("E14_PRE", "delegados", "row1", "ANTIOQUIA", "MEDELLIN"):
                assert leak not in body

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


def _chi_square(counts: dict[str, int], expected: dict[str, float]) -> float:
    """Pearson goodness-of-fit statistic ``Σ (O-E)²/E`` of the observed acta-draw
    counts against the expected (uniform) counts. Near ``df`` for a uniform sampler;
    blows far past the critical value under any real skew."""
    return sum((counts.get(k, 0) - e) ** 2 / e for k, e in expected.items())


def _acta_deck_doc_counts(db: Path, output_dir: Path, runs: int, seed: int) -> dict[str, int]:
    """Hit /api/acta-deck ``runs`` times and tally which acta each draw came from.

    Maps the returned (anonymized) cids back to their document via crop_id/field_key_of,
    the same way the anonymization test does, since the payload never names the acta.
    ``random`` is seeded so the whole run is DETERMINISTIC: the uniformity assertions are
    reproducible and never flake, yet still measure the real selection distribution.
    """
    import random
    from collections import Counter
    from e14detector.community import crop_id, field_key_of

    cid_to_doc: dict[str, str] = {}
    counts: Counter[str] = Counter()

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            random.seed(seed)  # fix the draw sequence after app construction
            for _ in range(runs):
                items = (await client.get("/api/acta-deck")).json()["items"]
                # Every crop in one response is from a single acta; identify it via any cid.
                doc = cid_to_doc[items[0]["cid"]]
                counts[doc] += 1

    # The deck endpoint is per-IP rate limited; lift the bucket so the sampling loop runs.
    orig_bucket, orig_refill = config.FEED_RATE_BUCKET, config.FEED_RATE_REFILL_PER_MIN
    config.FEED_RATE_BUCKET = float(runs + 100)
    config.FEED_RATE_REFILL_PER_MIN = 1.0e6

    # Pre-build the cid -> document map from the seeded DB so we can deanonymize the draws.
    store = DetectorStore(db)
    for row in store.conn.execute(
        "SELECT document_id, page_number, row_number, section FROM vote_fields "
        "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL"
    ):
        fkey = field_key_of(row["document_id"], row["page_number"], row["row_number"],
                            row["section"])
        cid_to_doc[crop_id(config.FORM_TOKEN_SECRET, fkey)] = row["document_id"]
    store.close()

    try:
        asyncio.run(run())
    finally:
        config.FEED_RATE_BUCKET, config.FEED_RATE_REFILL_PER_MIN = orig_bucket, orig_refill
    return dict(counts)


def test_acta_deck_uniform_over_actas(tmp_path: Path) -> None:
    """The acta the grid serves is uniform over actas — not skewed toward the
    earliest-inserted (lowest-rowid) ones. See _acta_deck_payload."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    n_actas = 20
    # Insert in order so doc 0 gets the lowest rowids and doc 19 the highest; the old
    # "take the first IN-match" pick would over-serve the low-rowid actas.
    for d in range(n_actas):
        doc_id = f"doc-{d:02d}"
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(13):  # every acta has 13 casillas
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    runs = 1200
    counts = _acta_deck_doc_counts(db, output_dir, runs, seed=20250608)

    # Deterministic (seeded) chi-square goodness-of-fit against a uniform draw over the
    # 20 actas. A uniform sampler sits near df=19; the old low-rowid pick is wildly skewed.
    # 43.82 is the upper-tail critical value at alpha=0.001 (df=19): the seeded uniform
    # sampler passes, any real bias fails. Every acta must also appear at least once.
    assert len(counts) == n_actas, f"only {len(counts)}/{n_actas} actas ever served"
    expected = {f"doc-{d:02d}": runs / n_actas for d in range(n_actas)}
    chi2 = _chi_square(counts, expected)
    assert chi2 < 43.82, f"acta selection not uniform: chi2={chi2:.1f} over df=19 (counts={counts})"


def test_acta_deck_uniform_with_uneven_crop_counts(tmp_path: Path) -> None:
    """Selection is per-acta, not per-crop: an acta with many casillas is no likelier
    to be served than one with few. See _acta_deck_payload."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    specs = {"doc-big": 13, "doc-small": 3}  # ~4.3x more crops in doc-big
    for doc_id, n in specs.items():
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(n):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    runs = 800
    counts = _acta_deck_doc_counts(db, output_dir, runs, seed=20250608)

    # Deterministic (seeded) chi-square against a uniform 50/50 split — NOT the ~13:3 a
    # crop-weighted sampler would give. 10.83 is the upper-tail critical value at
    # alpha=0.001 (df=1); a uniform sampler sits near 1, the crop-weighted one near 800.
    assert set(counts) == set(specs)
    expected = {k: runs / 2 for k in specs}
    chi2 = _chi_square(counts, expected)
    assert chi2 < 10.83, f"selection weighted by crop count: chi2={chi2:.1f} (counts={counts})"


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
    """A reported acta shows on the community billboard (one card per mesa), linking to it.
    The billboard moved with the /browse split: the global "most reported" board is /reportes,
    and the "Ver todas" (review=1) list is /buscar?review=1 — both render the same tile."""
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
            html = (await client.get("/reportes")).text
            assert "reportadas" in html  # billboard heading "Las N más reportadas"
            assert "reportaron" in html  # the per-acta tally line
            assert "/acta/doc-hot" in html
            assert "VALLE" in html and "CALI" in html

            # "Ver todas" (review=1) renders the SAME billboard tile: thumb + loc + tally.
            review_html = (await client.get("/buscar?review=1")).text
            assert '"board-list"' in review_html and "board-thumb" in review_html
            assert "reportaron" in review_html
            assert "/acta/doc-hot" in review_html
            assert '<div class="list">' not in review_html  # not the old text-only card grid

    asyncio.run(run())


def test_buscar_crowd_filters(tmp_path: Path) -> None:
    """/buscar?filter= narrows by crowd signal: reportadas (flagged >=1), revisadas (any vote),
    sin_revisar (never voted). review=1 stays a back-compat alias for reportadas."""
    from e14detector.community import CommunityStore, field_key_of

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    for doc_id in ("doc-rep", "doc-good", "doc-clean"):
        store.upsert_document(DocumentMetadata(
            document_id=doc_id, source_path=f"{doc_id}.pdf",
            department_code="11", municipality_code="001",
        ))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    cs = CommunityStore(community_db)
    cs.record_flag(field_key_of("doc-rep", 1, 1, None), "v1")     # reportada (-> also revisada)
    cs.record_appeal(field_key_of("doc-good", 1, 1, None), "v2")  # revisada, "se ve bien" only
    cs.close()                                                    # doc-clean: never touched

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            rep = (await client.get("/buscar?filter=reportadas")).text
            assert "/acta/doc-rep" in rep
            assert "/acta/doc-good" not in rep and "/acta/doc-clean" not in rep

            rev = (await client.get("/buscar?filter=revisadas")).text
            assert "/acta/doc-rep" in rev and "/acta/doc-good" in rev
            assert "/acta/doc-clean" not in rev

            sin = (await client.get("/buscar?filter=sin_revisar")).text
            assert "/acta/doc-clean" in sin
            assert "/acta/doc-rep" not in sin and "/acta/doc-good" not in sin

            # HIGH_VOTE_THRESHOLD (100) unmet -> muy_reportadas is empty but still 200.
            muy = await client.get("/buscar?filter=muy_reportadas")
            assert muy.status_code == 200 and "/acta/doc-rep" not in muy.text

            # Back-compat: review=1 behaves exactly like filter=reportadas.
            alias = (await client.get("/buscar?review=1")).text
            assert "/acta/doc-rep" in alias and "/acta/doc-clean" not in alias

    asyncio.run(run())


def test_compute_sync_progress_separates_browsable_from_total(tmp_path: Path) -> None:
    """The served headline counts browsable actas only; docs with no candidate crop widen the
    served_total gap, never the served count. This is the reconciliation the admin board renders,
    and the reason 'servidas' and 'publicadas' must never be shown as one undifferentiated number."""
    import sqlite3

    from e14detector.webapp import compute_sync_progress

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-has", source_path="doc-has.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-has", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop)))
    store.upsert_document(DocumentMetadata(document_id="doc-none", source_path="doc-none.pdf"))
    store.commit()
    store.close()

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        prog = compute_sync_progress(con)
    finally:
        con.close()
    assert prog["served_browsable"] == 1  # only the doc with a candidate crop
    assert prog["served_total"] == 2  # both docs are shipped in the snapshot
    assert prog["served_gap"] == 1  # the 'sin recortes' gap = total - browsable
    assert prog["served_total"] == prog["served_browsable"] + prog["served_gap"]  # triplet reconciles
    assert prog["synced"] == 1  # headline derives from browsable, not the row total


def test_transparencia_renders_without_pointer(tmp_path: Path) -> None:
    """The public page renders even with no reconciliation pointer (shows the 'preparando' note)."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(VoteField(document_id="doc1", page_number=1, row_type="candidate",
                                      row_number=1, candidate_name="A", raw_crop_path=str(crop)))
    store.commit(); store.close()

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/transparencia")
            assert r.status_code == 200
            assert "Cómo cuadran" in r.text  # header renders

    asyncio.run(run())


def test_transparencia_renders_chain_from_pointer(tmp_path: Path, monkeypatch) -> None:
    """With a reconciliation pointer, the public page renders the chain + cobertura + backlog."""
    from e14detector import config, dbsync

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc1", source_path="doc1.pdf"))
    store.insert_vote_field(VoteField(document_id="doc1", page_number=1, row_type="candidate",
                                      row_number=1, candidate_name="A", raw_crop_path=str(crop)))
    store.commit(); store.close()

    monkeypatch.setattr(config, "CDN_BASE_URL", "http://cdn.example")
    recon = {"total_global": 100, "mesas_informadas": 100, "sqlite_served": 1,
             "backlog_ingesta": 99, "backlog_reporte": 0, "missing_count": 99,
             "missing_keys_sample": ["88_001_001_01_001"]}
    monkeypatch.setattr(dbsync, "pointer_status",
                        lambda *a, **k: {"reconciliation": recon, "n_docs": 1})

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/transparencia")
            assert r.status_code == 200
            # Public funnel labels (plain language), not the technical chain.
            assert "Mesas escaneadas" in r.text
            assert "Mesas disponibles en nuestro sistema" in r.text
            assert "Mesas instaladas en el país" in r.text
            assert "88_001_001_01_001" in r.text  # the pending mesa is listed (pendientes > 0)

    asyncio.run(run())


def test_build_public_counts_is_a_friendly_funnel() -> None:
    """The public projection is a 4-step funnel with plain labels (no internal frontier/jargon),
    surfaces escrutadas as 'escaneadas', and highlights the served step as the final one."""
    from e14detector.webapp import build_public_counts

    recon = {"total_global": 122020, "mesas_escrutadas": 122020, "mesas_informadas": 122016}
    pub = build_public_counts(recon, served_total=122016)
    keys = [s["key"] for s in pub["funnel"]]
    assert keys == ["pais", "escaneadas", "acta", "sistema"]
    assert pub["funnel"][-1]["highlight"] is True  # "disponibles en nuestro sistema" is final
    assert pub["funnel"][1]["label"] == "Mesas escaneadas"  # not "escrutadas"
    assert pub["served_label"] == "122.016"
    assert pub["cobertura_label"] == "100,00"  # served / informadas
    assert pub["sin_acta"] == 4       # escaneadas − acta publicada (registraduría's)
    assert pub["pendientes"] == 0     # acta publicada − served (ours; caught up)
    # No technical fields leak into the public projection.
    assert "rows" not in pub and "served_eq_published" not in pub


def test_build_public_counts_handles_missing_data() -> None:
    from e14detector.webapp import build_public_counts
    pub = build_public_counts(None, served_total=5)
    assert pub["has_data"] is False
    assert pub["funnel"][0]["value_label"] == "—"


def test_build_count_chain_orders_and_derives() -> None:
    """The chain renders non-increasing top→bottom, computes cobertura + both backlogs, and
    confirms published==served — the single reconciliation the admin/public pages render."""
    from e14detector.webapp import build_count_chain

    recon = {
        "total_global": 122020, "mesas_escrutadas": 122020, "mesas_informadas": 122016,
        "downloaded": 122010, "crops_uploaded": 122007,
        "sqlite_served": 122007, "missing_count": 9,
    }
    chain = build_count_chain(recon, served_total=122007)
    by_key = {r["key"]: r for r in chain["rows"]}
    assert [r["status"] for r in chain["rows"]] == ["ok"] * 7  # monotone, nothing inverted
    assert by_key["published"]["count"] == 122007
    assert chain["served_eq_published"] is True
    # cobertura = served / informadas (acta images), not / total_global.
    assert chain["cobertura"] == round(122007 * 100 / 122016, 2)
    assert chain["backlog_ingesta"] == 9    # informadas − served (ours)
    assert chain["backlog_reporte"] == 4    # total_global − informadas (registraduría's)


def test_build_count_chain_flags_inversion_and_divergence() -> None:
    """An impossible inversion (a lower count exceeds a higher one) is flagged 'bad', and a
    published count that disagrees with the app's own served count breaks served_eq_published."""
    from e14detector.webapp import build_count_chain

    # sqlite_served (120) > crops_uploaded (100): an inversion that must alarm.
    recon = {"total_global": 200, "mesas_informadas": 150, "downloaded": 130,
             "crops_uploaded": 100, "sqlite_served": 99}
    chain = build_count_chain(recon, served_total=120)
    statuses = {r["key"]: r["status"] for r in chain["rows"]}
    assert statuses["sqlite_served"] == "bad"  # 120 > 100 above it
    # The app's own served (120) disagrees with the pointer's published (99).
    assert chain["served_eq_published"] is False


def test_build_count_chain_tolerates_unknown_rows() -> None:
    """Counts a publishing machine can't see (downloaded/crops_uploaded) render 'na' and don't
    break the monotone check — only the external anchors + served are guaranteed present."""
    from e14detector.webapp import build_count_chain

    recon = {"total_global": 100, "mesas_informadas": 100, "sqlite_served": 90}
    chain = build_count_chain(recon, served_total=90)
    statuses = {r["key"]: r["status"] for r in chain["rows"]}
    assert statuses["downloaded"] == "na" and statuses["crops_uploaded"] != "bad"
    assert chain["backlog_ingesta"] == 10
    assert chain["served_eq_published"] is True


def test_build_count_chain_without_reconciliation_is_empty() -> None:
    """A legacy pointer (no reconciliation block) yields has_reconciliation=False so the admin
    page shows the 'run publish-db --force-pointer' hint instead of a half-blank table."""
    from e14detector.webapp import build_count_chain

    chain = build_count_chain(None, served_total=122007)
    assert chain["has_reconciliation"] is False
    assert chain["cobertura"] is None


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
    (ensure_n_candidates) so /buscar stays on the fast path and never 500s (prod incident)."""
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
        # create_app calls ensure_n_candidates itself; /buscar uses the fast n_candidates column.
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/buscar")
            assert r.status_code == 200
            html = r.text
            assert "/acta/doc-has" in html and "/acta/doc-none" not in html
            assert "2 candidatos" in html

    asyncio.run(run())


def test_ensure_n_candidates_boot_recovers_missing_column(tmp_path: Path) -> None:
    """Even without calling ensure_n_candidates by hand, building the app on a column-less DB
    recovers it (the create_app boot hook) — /buscar responds 200, not 500."""
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
            assert (await client.get("/buscar")).status_code == 200

    asyncio.run(run())


def test_geo_names_resolved_at_render_without_touching_db(tmp_path: Path) -> None:
    """A snapshot carrying only DIVIPOLA codes (names NULL) renders human names on /buscar,
    resolved from the in-memory dictionary at render time — the DB is NOT mutated (stays
    codes-only, no per-row name duplication)."""
    import sqlite3

    from e14detector.webapp import load_geo_names

    # The lookup itself resolves the hierarchy from a small CSV.
    dict_path = tmp_path / "divipol.csv"
    dict_path.write_text(
        "cod_departamento,departamento,cod_municipio,municipio,cod_zona,zona,cod_puesto,lugar_votacion,num_mesas\n"
        "01,ANTIOQUIA,001,MEDELLIN,001,Zona 01,01,COLEGIO LA ESPERANZA,10\n",
        encoding="utf-8",
    )
    geo = load_geo_names(dict_path)
    assert geo.dept("01") == "ANTIOQUIA"
    assert geo.muni("01", "001") == "MEDELLIN"
    assert geo.place("01", "001", "001", "01") == "COLEGIO LA ESPERANZA"
    assert geo.dept("99") is None  # miss -> caller falls back to the code

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    # Codes present, names absent — exactly the degraded national snapshot shape.
    store.upsert_document(DocumentMetadata(
        document_id="d1", source_path="d1.pdf", department_code="01", municipality_code="001",
    ))
    store.insert_vote_field(VoteField(
        document_id="d1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    async def run() -> None:
        # create_app uses the bundled real dictionary, which maps 01->ANTIOQUIA, 001->MEDELLIN.
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/buscar")).text
            assert "ANTIOQUIA" in html and "MEDELLIN" in html

    asyncio.run(run())

    # The served DB was never written to — names stay NULL (no denormalized per-row copies).
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT COUNT(*) FROM documents WHERE department_name IS NOT NULL"
    ).fetchone()[0] == 0
    con.close()


def test_official_pdf_link_rebuilt_from_hash(tmp_path: Path) -> None:
    """A codes-only snapshot ships official_lookup_url NULL; /acta still links to the
    Registraduría PDF by rebuilding the URL from the bundled per-acta hash map + the codes
    encoded in the document_id. Uses a real id present in e14detector/acta_hashes.sqlite."""
    import sqlite3 as _sql
    from e14detector.webapp import ACTA_HASHES_PATH

    hc = _sql.connect(ACTA_HASHES_PATH)
    did, hash_hex = hc.execute(
        "SELECT document_id, hex(hash) FROM acta_hash WHERE document_id LIKE 'E14_PRE_01_001%' LIMIT 1"
    ).fetchone()
    hc.close()
    # document_id -> codes for the URL path (E14_PRE_{dep}_{muni}_{zona}_{puesto}_{mesa}_...).
    _, _, dep, muni, zona, puesto, mesa = did.split("_")[:7]

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    # No official_lookup_url, no names — the degraded snapshot shape.
    store.upsert_document(DocumentMetadata(
        document_id=did, source_path=f"{did}.pdf", department_code=dep,
        municipality_code=muni, zone=zona, puesto=puesto, mesa=mesa,
    ))
    store.insert_vote_field(VoteField(
        document_id=did, page_number=1, row_type="candidate", row_number=1,
        candidate_name="A", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    expected = (
        f"https://divulgacione14presidente.registraduria.gov.co/assets/temis/pdf/"
        f"{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{hash_hex.lower()}.pdf"
    )

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get(f"/acta/{did}")).text
            assert "Ver el acta oficial" in html
            assert expected in html

    asyncio.run(run())

    # The link was rebuilt at render — the served DB still carries no official_lookup_url.
    con = _sql.connect(db)
    assert con.execute(
        "SELECT COUNT(*) FROM documents WHERE official_lookup_url IS NOT NULL"
    ).fetchone()[0] == 0
    con.close()


def test_municipio_report_stats_aggregation(tmp_path: Path) -> None:
    """Mesa-level reported counts roll up per municipio and department."""
    import sqlite3

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    for doc_id, dep, muni in (
        ("doc-a", "01", "001"),
        ("doc-b", "01", "001"),
        ("doc-c", "05", "001"),
    ):
        store.upsert_document(DocumentMetadata(
            document_id=doc_id, source_path=f"{doc_id}.pdf",
            department_code=dep, municipality_code=muni,
        ))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="X", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    idx = doc_muni_index(con)
    con.close()
    stats = municipio_report_stats(idx, {"doc-a"}, load_geo_names())
    m01 = next(m for m in stats["municipios"] if m["dep"] == "01" and m["muni"] == "001")
    m05 = next(m for m in stats["municipios"] if m["dep"] == "05" and m["muni"] == "001")
    assert m01["total"] == 2 and m01["reported"] == 1 and m01["pct"] == 50.0
    assert m05["total"] == 1 and m05["reported"] == 0 and m05["pct"] == 0.0
    d01 = next(d for d in stats["departments"] if d["dep"] == "01")
    assert d01["total"] == 2 and d01["reported"] == 1
    # Backward-compat: callers that omit reviewed_docs get reviewed counts of 0.
    assert m01["reviewed"] == 0 and m01["pct_reviewed"] == 0.0
    assert stats["summary"]["reviewed_mesas"] == 0

    # Reviewed denominator: pct is reported/reviewed instead of reported/total.
    rev = municipio_report_stats(idx, {"doc-a"}, load_geo_names(), reviewed_docs={"doc-a", "doc-b"})
    r01 = next(m for m in rev["municipios"] if m["dep"] == "01" and m["muni"] == "001")
    assert r01["reviewed"] == 2 and r01["pct_reviewed"] == 50.0  # 1 reported of 2 reviewed
    assert rev["summary"]["reviewed_mesas"] == 2
    rd01 = next(d for d in rev["departments"] if d["dep"] == "01")
    assert rd01["reviewed"] == 2 and rd01["pct_reviewed"] == 50.0
    # New fields ride alongside the literal ratios: coverage = reviewed/total, an adjusted score,
    # a confidence in 0..1, and a national baseline. Here baseline = 1 reported / 2 reviewed = 50%.
    assert r01["coverage"] == 100.0 and 0.0 < r01["conf"] < 1.0
    assert r01["score"] == 50.0  # (1 + 25*0.5) / (2 + 25) = 50%
    assert rev["summary"]["baseline_pct"] == 50.0 and rev["summary"]["prior_k"] == 25.0
    assert rev["summary"]["coverage_pct"] == 66.7  # 2 reviewed of 3 total mesas
    # One mesa reviewed and that same mesa reported -> 100%.
    one = municipio_report_stats(idx, {"doc-a"}, load_geo_names(), reviewed_docs={"doc-a"})
    o01 = next(m for m in one["municipios"] if m["dep"] == "01" and m["muni"] == "001")
    assert o01["reviewed"] == 1 and o01["pct_reviewed"] == 100.0


def test_municipio_report_stats_shrinkage() -> None:
    """Small-sample shrinkage: a 1-of-1 (raw 100%) municipio must NOT outrank a well-sampled one.

    Reproduces the user's concern — a tiny zone with a single report shouldn't paint as a wave.
    With many clean reviewed mesas pulling the national baseline low, the 1/1 zone shrinks toward
    that baseline while a 30-of-100 zone keeps most of its rate, so the well-sampled zone scores
    higher and sorts first."""
    idx: dict[str, tuple[str, str]] = {}
    reviewed: set[str] = set()
    reported: set[str] = set()
    # Zone A (01/001): 1 reviewed, 1 reported -> raw 100%, but n = 1.
    idx["a0"] = ("01", "001"); reviewed.add("a0"); reported.add("a0")
    # Zone B (02/001): 100 reviewed, 30 reported -> raw 30%, n = 100 (the real signal).
    for i in range(100):
        d = f"b{i}"; idx[d] = ("02", "001"); reviewed.add(d)
        if i < 30:
            reported.add(d)
    # Zone C (03/001): 100 reviewed, 0 reported -> drags the national baseline down.
    for i in range(100):
        d = f"c{i}"; idx[d] = ("03", "001"); reviewed.add(d)

    stats = municipio_report_stats(idx, reported, load_geo_names(), reviewed_docs=reviewed)
    a = next(m for m in stats["municipios"] if m["dep"] == "01")
    b = next(m for m in stats["municipios"] if m["dep"] == "02")
    assert a["pct_reviewed"] == 100.0 and b["pct_reviewed"] == 30.0  # raw ratios unchanged
    assert a["score"] < a["pct_reviewed"]      # the 1/1 fluke is shrunk hard, not left at 100%
    assert a["score"] < b["score"]             # ...below the well-sampled 30% zone
    assert a["conf"] < b["conf"]               # and carries far less confidence (faint on the map)
    # Sort is by adjusted score: the well-sampled zone comes before the 1/1 fluke.
    order = [m["dep"] for m in stats["municipios"]]
    assert order.index("02") < order.index("01")


def test_api_reportes_map_national_and_drilldown(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id="doc-x", source_path="doc-x.pdf",
        department_code="11", municipality_code="001",
    ))
    store.insert_vote_field(VoteField(
        document_id="doc-x", page_number=1, row_type="candidate", row_number=1,
        candidate_name="Y", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    cs = CommunityStore(community_db)
    cs.record_flag(field_key_of("doc-x", 1, 1, None), "v1")
    cs.record_appeal(field_key_of("doc-x", 1, 1, None), "v2")  # also reviewed via "se ve bien"
    cs.close()

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            nat = (await client.get("/api/reportes/map")).json()
            assert nat["view"] == "departments"
            assert any(d["dep"] == "11" for d in nat["departments"])
            assert nat["summary"]["reviewed_mesas"] == 1
            assert nat["summary"]["baseline_pct"] == 100.0  # 1 reported of 1 reviewed
            drill = (await client.get("/api/reportes/map?department=11")).json()
            assert drill["view"] == "municipios"
            assert drill["department"] == "11"
            m = drill["municipios"][0]
            assert m["reported"] == 1 and m["reviewed"] == 1 and m["pct_reviewed"] == 100.0
            # New fields ride through the API for the frontend's two-channel encoding.
            assert m["coverage"] == 100.0 and "score" in m and 0.0 < m["conf"] < 1.0
            geo = await client.get("/geo/colombia_departamentos.geojson")
            assert geo.status_code == 200
            assert geo.headers["content-type"].startswith("application/")

    asyncio.run(run())


def test_reportes_includes_map_assets(tmp_path: Path) -> None:
    db = tmp_path / "results.sqlite"
    DetectorStore(db).close()

    async def run() -> None:
        app = create_app(results_db=db, output_dir=tmp_path / "out")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/reportes")).text
            assert "reportes-map" in html
            assert "leaflet" in html
            assert 'data-panel="panel-map"' in html  # the map viz tab

    asyncio.run(run())


def test_reportes_billboard_caps_at_top_n(tmp_path: Path) -> None:
    """/reportes is a quick top-N view (no pager); when more reported actas exist it links to the
    full list at /buscar?filter=reportadas instead of paginating."""
    from e14detector.community import CommunityStore, field_key_of
    from e14detector.webapp import REPORTES_TOP_N

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    cs = CommunityStore(community_db)
    n_docs = REPORTES_TOP_N + 2  # more reported actas than the board shows
    for i in range(n_docs):
        doc_id = f"doc-{i:02d}"
        store.upsert_document(DocumentMetadata(
            document_id=doc_id, source_path=f"{doc_id}.pdf",
            department_code="11", municipality_code="001",
        ))
        store.insert_vote_field(VoteField(
            document_id=doc_id, page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(crop),
        ))
        cs.record_flag(field_key_of(doc_id, 1, 1, None), f"v{i}")
    store.commit()
    store.close()
    cs.close()

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/reportes")).text
            assert html.count('class="hot-num"') == REPORTES_TOP_N      # one rank numeral per row, capped
            assert html.count('class="hot-bar"') == REPORTES_TOP_N      # one intensity bar per row
            assert "/buscar?filter=reportadas" in html                  # "Ver todas" link to full list
            assert "?page=" not in html                                 # no pager anymore

    asyncio.run(run())


def test_colombia_geojson_bundled() -> None:
    from e14detector.webapp import DEPARTAMENTOS_GEOJSON, MUNICIPIOS_GEOJSON

    assert MUNICIPIOS_GEOJSON.is_file()
    assert DEPARTAMENTOS_GEOJSON.is_file()
    data = json.loads(MUNICIPIOS_GEOJSON.read_text(encoding="utf-8"))
    assert data["features"][0]["properties"]["dep"]
    assert data["features"][0]["properties"]["muni"]


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


# --- Security hardening: trusted client IP, IPv6 bucketing, amplification, promotion floor ----

def _fake_request(headers: dict | None = None, client=("203.0.113.7", 0)):
    from starlette.requests import Request

    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": hdrs, "client": client})


def test_client_ip_trusts_edge_header_not_spoofable_xff() -> None:
    """The voter IP must come from the edge-set header (Fly-Client-IP), never the attacker-
    controlled first X-Forwarded-For hop — that distinction is the whole anti-Sybil fix."""
    from e14detector.webapp import _client_ip

    # Trusted edge header wins even when the client forges an XFF first hop.
    req = _fake_request({"fly-client-ip": "9.9.9.9", "x-forwarded-for": "1.2.3.4, 9.9.9.9"})
    assert _client_ip(req) == "9.9.9.9"
    # No trusted header -> fall back to the LAST xff hop (closest trusted proxy), not the first.
    assert _client_ip(_fake_request({"x-forwarded-for": "1.2.3.4, 8.8.8.8"})) == "8.8.8.8"
    # Nothing -> the socket peer.
    assert _client_ip(_fake_request(client=("203.0.113.7", 0))) == "203.0.113.7"


def test_client_ip_uses_cf_connecting_ip_only_behind_cloudflare() -> None:
    """When Fly-Client-IP is a Cloudflare edge IP, the real visitor comes from cf-connecting-ip;
    when it isn't (direct-to-Fly), cf-connecting-ip is ignored so it can't be forged."""
    from e14detector.webapp import _client_ip

    # Through Cloudflare: Fly saw a CF IP connect -> trust cf-connecting-ip (the real visitor).
    via_cf = _fake_request({"fly-client-ip": "104.16.0.1", "cf-connecting-ip": "5.5.5.5"})
    assert _client_ip(via_cf) == "5.5.5.5"
    # Direct to Fly with a FORGED cf-connecting-ip: Fly-Client-IP isn't a CF IP -> ignore the
    # forgery and use the un-spoofable Fly-Client-IP.
    forged = _fake_request({"fly-client-ip": "9.9.9.9", "cf-connecting-ip": "5.5.5.5"})
    assert _client_ip(forged) == "9.9.9.9"


def test_edge_guard_requires_cloudflare_origin_when_enabled(tmp_path: Path, monkeypatch) -> None:
    """With E14_REQUIRE_CF on, the bare Fly origin must look dead to anyone bypassing Cloudflare:
    ANY request (page or api) whose un-spoofable Fly-Client-IP is not a Cloudflare IP gets a bare
    404. The sole exemption is /health (Fly's own checker reaches it directly, no Cloudflare hop).
    Cloudflare-originated traffic passes through. Fail-open when off (covered by every other test)."""
    from e14detector import config as _config

    monkeypatch.setattr(_config, "REQUIRE_CF_ORIGIN", True)
    output_dir, db = _one_crop_db(tmp_path)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "c.sqlite")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            direct = {"fly-client-ip": "9.9.9.9"}  # connected to Fly from a non-Cloudflare IP
            # Direct-to-Fly page hit -> 404, body indistinguishable from a normal miss.
            page = await client.get("/votar", headers=direct)
            assert page.status_code == 404 and page.json()["detail"] == "Not Found"
            # Direct-to-Fly api hit -> the SAME bare 404 (previously 403 'forbidden').
            api = await client.post("/api/vote", json={"cid": "x", "value": "good"}, headers=direct)
            assert api.status_code == 404 and api.json()["detail"] == "Not Found"
            # /health is exempt even from a non-CF IP (Fly's internal checker must keep it green).
            assert (await client.get("/health", headers=direct)).status_code in (200, 503)
            # Arrived via Cloudflare (Fly-Client-IP is a CF IP) -> guard passes, served normally.
            feed = await client.get("/api/feed", headers={"fly-client-ip": "104.16.0.1"})
            assert feed.status_code == 200

    asyncio.run(run())


def test_voter_ip_collapses_ipv6_to_64() -> None:
    """One IPv6 /64 allocation = one identity (else a single allocation mints billions)."""
    from e14detector.webapp import _voter_ip

    a = _voter_ip(_fake_request({"fly-client-ip": "2001:db8:abcd:1234::1"}))
    b = _voter_ip(_fake_request({"fly-client-ip": "2001:db8:abcd:1234:ffff:ffff:ffff:ffff"}))
    c = _voter_ip(_fake_request({"fly-client-ip": "2001:db8:abcd:9999::1"}))
    assert a == b          # same /64 -> one identity
    assert a != c          # different /64 -> different identity
    assert _voter_ip(_fake_request({"fly-client-ip": "203.0.113.7"})) == "203.0.113.7"  # IPv4 as-is


def _one_crop_db(tmp_path: Path, doc="doc-x", n=1):
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id=doc, source_path=f"{doc}.pdf",
        department_name="VALLE", municipality_name="CALI", mesa="07"))
    for i in range(n):
        store.insert_vote_field(VoteField(
            document_id=doc, page_number=1, row_type="candidate", row_number=i + 1,
            candidate_name=f"C{i}", raw_crop_path=str(crop)))
    store.commit()
    store.close()
    return output_dir, db


def test_vote_identity_keys_on_trusted_ip_not_spoofed_xff(tmp_path: Path) -> None:
    """Spoofing X-Forwarded-For can no longer mint new identities: many forged XFF values behind
    one Fly-Client-IP collapse to ONE vote; a genuinely different Fly-Client-IP is a second."""
    import dataclasses
    from e14detector.community import CommunityStore, PollConfig, field_key_of

    output_dir, db = _one_crop_db(tmp_path)
    community_db = tmp_path / "community.sqlite"
    cfg = dataclasses.replace(PollConfig.from_config(), form_token_secret="")
    fk = field_key_of("doc-x", 1, 1, None)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db, poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            cid = (await client.get("/api/acta-deck", headers={"fly-client-ip": "10.0.0.1"})
                   ).json()["items"][0]["cid"]
            for xff in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):  # same edge IP, forged XFF each time
                r = await client.post("/api/vote", json={"cid": cid, "value": "strange"},
                                      headers={"fly-client-ip": "10.0.0.1", "x-forwarded-for": xff})
                assert r.status_code == 200
            r = await client.post("/api/vote", json={"cid": cid, "value": "strange"},
                                  headers={"fly-client-ip": "10.0.0.2"})  # a real second identity
            assert r.status_code == 200

    asyncio.run(run())
    cs = CommunityStore(community_db)
    strange = cs.counts_among([fk])[fk]["strange"]
    cs.close()
    assert strange == 2  # 3 spoofed-XFF votes from 10.0.0.1 dedup to 1; +1 from 10.0.0.2


def test_feed_endpoint_rate_limited_per_ip(tmp_path: Path, monkeypatch) -> None:
    """/api/feed is throttled per IP so a script can't drive unbounded cid_index writes."""
    from e14detector import config as _config, webapp as _wa

    monkeypatch.setattr(_config, "FEED_RATE_BUCKET", 3.0)
    monkeypatch.setattr(_config, "FEED_RATE_REFILL_PER_MIN", 1.0)  # ~0 refill during the test
    _wa._feed_buckets.clear()
    output_dir, db = _one_crop_db(tmp_path)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "c.sqlite")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            h = {"fly-client-ip": "198.51.100.5"}
            codes = [(await client.get("/api/feed?n=1", headers=h)).status_code for _ in range(5)]
            assert codes[:3] == [200, 200, 200] and 429 in codes[3:]

    asyncio.run(run())


def test_vote_batch_charges_rate_proportionally(tmp_path: Path, monkeypatch) -> None:
    """A batch larger than the bucket allows is rejected (charged ceil(n/BATCH_VOTES_PER_TOKEN))."""
    import dataclasses
    from e14detector import config as _config
    from e14detector.community import PollConfig

    monkeypatch.setattr(_config, "BATCH_VOTES_PER_TOKEN", 1)
    output_dir, db = _one_crop_db(tmp_path, n=5)
    cfg = dataclasses.replace(PollConfig.from_config(), form_token_secret="",
                              rate_bucket=2.0, rate_refill_per_min=0.0)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "c.sqlite", poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            cids = [it["cid"] for it in (await client.get(
                "/api/acta-deck", headers={"fly-client-ip": "203.0.113.20"})).json()["items"]]
            assert len(cids) == 5
            r = await client.post("/api/vote-batch", json={"strange": [], "good": cids},
                                  headers={"fly-client-ip": "203.0.113.20"})
            assert r.status_code == 429 and r.json()["error"] == "rate_limited"

    asyncio.run(run())


def test_promotion_floor_hides_single_voter_actas(tmp_path: Path, monkeypatch) -> None:
    """With MIN_PROMOTE_VOTERS=2 a single-reporter acta stays off the public billboard/reportes
    until a second distinct voter flags it."""
    from e14detector import config as _config
    from e14detector.community import CommunityStore, field_key_of

    monkeypatch.setattr(_config, "MIN_PROMOTE_VOTERS", 2)
    output_dir, db = _one_crop_db(tmp_path, doc="doc-hot")
    community_db = tmp_path / "community.sqlite"
    fk = field_key_of("doc-hot", 1, 1, None)
    cs = CommunityStore(community_db)
    cs.record_flag(fk, "voter-1")  # only ONE distinct voter -> below the floor
    cs.close()

    async def reportes_html() -> tuple[str, list]:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            html = (await client.get("/reportes")).text
            bb = (await client.get("/api/billboard")).json()["items"]
            return html, bb

    html, bb = asyncio.run(reportes_html())
    assert "/acta/doc-hot" not in html and bb == []

    cs = CommunityStore(community_db)
    cs.record_flag(fk, "voter-2")  # second distinct voter -> meets the floor
    cs.close()
    html2, _ = asyncio.run(reportes_html())
    assert "/acta/doc-hot" in html2


def test_vote_succeeds_with_session_token_when_turnstile_enabled(tmp_path: Path, monkeypatch) -> None:
    """With Turnstile ON, a vote carrying the /api/session-minted form token must be ACCEPTED and
    recorded. Turnstile gates the session (one solve -> form token); votes do NOT carry a per-vote
    Turnstile token, so the handler must not re-verify one (doing so 403'd every vote)."""
    import dataclasses
    from e14detector import webapp as wa
    from e14detector.community import CommunityStore, PollConfig, field_key_of

    output_dir, db = _one_crop_db(tmp_path)
    community_db = tmp_path / "community.sqlite"
    cfg = dataclasses.replace(
        PollConfig.from_config(), turnstile_enabled=True,
        turnstile_sitekey="0xSITE", turnstile_secret="sekret", form_min_seconds=0.0)
    # Realistic stub: a Turnstile token verifies only when one is actually present (so a stray
    # per-vote check on a token-less vote would fail — exactly the bug this guards against).
    monkeypatch.setattr(wa, "verify_turnstile", lambda secret, token, ip=None: bool(token))
    fk = field_key_of("doc-x", 1, 1, None)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db, poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            h = {"fly-client-ip": "10.1.1.1"}
            cid = (await client.get("/api/acta-deck", headers=h)).json()["items"][0]["cid"]
            ft = (await client.post("/api/session", json={"turnstile_token": "x"}, headers=h)
                  ).json()["form_token"]
            assert ft
            r = await client.post("/api/vote", json={"cid": cid, "value": "strange", "form_token": ft},
                                  headers=h)  # carries the FORM token, no turnstile_token
            assert r.status_code == 200 and r.json()["ok"] is True

    asyncio.run(run())
    cs = CommunityStore(community_db)
    n = cs.counts_among([fk])[fk]["strange"]
    cs.close()
    assert n == 1  # the vote was actually recorded, not silently 403'd


def test_form_token_is_bound_to_client_ip(tmp_path: Path, monkeypatch) -> None:
    """A session form token minted for one IP must be rejected from another — so a solved token
    can't be replayed across a proxy pool (each Sybil identity needs its own Turnstile solve)."""
    import dataclasses
    from e14detector import webapp as wa
    from e14detector.community import CommunityStore, PollConfig, field_key_of

    output_dir, db = _one_crop_db(tmp_path)
    community_db = tmp_path / "community.sqlite"
    cfg = dataclasses.replace(
        PollConfig.from_config(), turnstile_enabled=True,
        turnstile_sitekey="0xSITE", turnstile_secret="sekret", form_min_seconds=0.0)
    monkeypatch.setattr(wa, "verify_turnstile", lambda secret, token, ip=None: bool(token))
    fk = field_key_of("doc-x", 1, 1, None)

    async def run() -> None:
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db, poll=cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            cid = (await client.get("/api/acta-deck", headers={"fly-client-ip": "10.0.0.1"})
                   ).json()["items"][0]["cid"]
            ft = (await client.post("/api/session", json={"turnstile_token": "x"},
                  headers={"fly-client-ip": "10.0.0.1"})).json()["form_token"]
            # Same token, DIFFERENT IP -> rejected (the replay we want to stop).
            r_other = await client.post(
                "/api/vote", json={"cid": cid, "value": "strange", "form_token": ft},
                headers={"fly-client-ip": "203.0.113.99"})
            # Same token, SAME IP -> accepted.
            r_same = await client.post(
                "/api/vote", json={"cid": cid, "value": "strange", "form_token": ft},
                headers={"fly-client-ip": "10.0.0.1"})
            return r_other.status_code, r_same.status_code

    other, same = asyncio.run(run())
    assert other == 403 and same == 200
    cs = CommunityStore(community_db)
    n = cs.counts_among([fk])[fk]["strange"]
    cs.close()
    assert n == 1  # only the same-IP vote landed


def test_admin_fairness_probe_is_gated_and_uniform(tmp_path: Path) -> None:
    """/admin/fairness is operator-gated (404 off, 403 on wrong key) and, with a valid
    key, reports the LIVE acta-deck selector as uniform — the production proof behind the
    selection-skew fix. See _fairness_probe / _pick_review_doc."""
    import random as _random

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    for d in range(20):  # 20 actas in rowid order -> 20 single-rowid bands, df=19
        doc_id = f"doc-{d:02d}"
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(13):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    orig_token = config.ADMIN_TOKEN

    async def run():
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            config.ADMIN_TOKEN = ""                                  # feature off
            off = await client.get("/admin/fairness")
            config.ADMIN_TOKEN = "s3cret"                            # feature on
            bad = await client.get("/admin/fairness?key=nope")       # wrong key
            _random.seed(20250608)                                   # deterministic probe
            ok = await client.get("/admin/fairness?key=s3cret&n=4000")
            return off.status_code, bad.status_code, ok.status_code, ok.text

    try:
        off, bad, ok, body = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert off == 404, "no token configured -> route hidden"
    assert bad == 403, "token configured, wrong key -> forbidden"
    assert ok == 200
    # No review history -> nothing to steer -> healthy/uniform baseline (flat serving).
    assert "SANO" in body and "FALLA" not in body


def test_reportes_renders_public_fixes_log(tmp_path: Path) -> None:
    """The public reports page renders the version-controlled fairness/fixes log so the
    veeduría shows its work. See e14detector/transparency_log.py."""
    from e14detector.transparency_log import TRANSPARENCY_LOG

    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"

    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-1", source_path="doc-1.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="C", raw_crop_path="",
    ))
    store.commit()
    store.close()

    async def run():
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/reportes")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert "hemos corregido" in resp.text  # the "Qué hemos corregido" heading
    assert TRANSPARENCY_LOG[0]["title"] in resp.text
    # The status (fixed/ongoing) is shown in plain language, not left ambiguous.
    assert "Corregido" in resp.text


def test_admin_coverage_joins_votes_and_index(tmp_path: Path) -> None:
    """/admin/coverage is operator-gated and reports historical review coverage, joining
    per-acta review counts (community/vote backend) with the SQLite acta index (rowid +
    n_candidates). See _selector_board / CommunityStore.review_counts."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    n_actas = 10
    for d in range(n_actas):
        doc_id = f"doc-{d:02d}"
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(5):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    # Simulate the historical skew: only the low-rowid half got reviewed, some heavily.
    cs = CommunityStore(community_db)
    for d in range(5):                       # doc-00..doc-04 reviewed; doc-05..09 untouched
        fk = field_key_of(f"doc-{d:02d}", 1, 1, "")
        for v in range(5 - d):               # doc-00 by 5 voters ... doc-04 by 1
            cs.record_flag(fk, f"voter-{d}-{v}")
    counts = cs.review_counts()
    cs.close()

    # review_counts maps doc -> distinct reviewers; unreviewed actas are absent.
    assert counts["doc-00"] == 5 and counts["doc-04"] == 1 and "doc-05" not in counts

    orig_token = config.ADMIN_TOKEN

    async def run():
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            config.ADMIN_TOKEN = ""                                # feature off
            off = await client.get("/admin/coverage")
            config.ADMIN_TOKEN = "s3cret"                          # feature on
            bad = await client.get("/admin/coverage?key=nope")     # wrong key
            ok = await client.get("/admin/coverage?key=s3cret")
            return off.status_code, bad.status_code, ok.status_code, ok.text

    try:
        off, bad, ok, body = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert off == 404, "no token configured -> route hidden"
    assert bad == 403, "token configured, wrong key -> forbidden"
    assert ok == 200
    assert "Cobertura y reparto" in body            # the merged board
    assert "50.0%" in body  # 5 of 10 reviewable actas have >= 1 review (global coverage)


def test_acta_deck_steers_to_least_reviewed(tmp_path: Path) -> None:
    """With coverage weighting on (default), the selector biases toward the LEAST-reviewed
    actas, and /admin/fairness reports it as steering. See _pick_review_doc / _selector_board."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    n_actas = 20
    for d in range(n_actas):
        doc_id = f"doc-{d:02d}"
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(5):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    # Historical skew: the low-rowid half is heavily reviewed; the high-rowid half untouched.
    cs = CommunityStore(community_db)
    for d in range(10):
        fk = field_key_of(f"doc-{d:02d}", 1, 1, "")
        for v in range(5):
            cs.record_flag(fk, f"voter-{d}-{v}")
    cs.close()

    orig_token = config.ADMIN_TOKEN

    async def run():
        import random as _random
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            config.ADMIN_TOKEN = "s3cret"
            _random.seed(20250608)
            ok = await client.get("/admin/fairness?key=s3cret&n=4000")
            return ok.status_code, ok.text

    try:
        ok, body = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert ok == 200
    # The selector concentrates new serves on the under-covered (high-rowid) bands — and the
    # board reads this as healthy steering, NOT a malfunction pile-up.
    assert "dirigiendo a las menos revisadas" in body
    assert "FALLA" not in body


def test_acta_deck_weighting_off_is_uniform(tmp_path: Path) -> None:
    """With weighting disabled, the selector reverts to plain uniform selection even when a
    review history exists — the rollback switch works. See ACTA_DECK_COVERAGE_WEIGHTED."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    community_db = tmp_path / "community.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")

    store = DetectorStore(db)
    for d in range(2):
        doc_id = f"doc-{d}"
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf"))
        for i in range(5):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=i + 1,
                candidate_name=f"C{i}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()

    cs = CommunityStore(community_db)
    for v in range(8):  # doc-0 heavily reviewed; doc-1 untouched
        cs.record_flag(field_key_of("doc-0", 1, 1, ""), f"voter-{v}")
    cs.close()

    orig_flag = config.ACTA_DECK_COVERAGE_WEIGHTED
    orig_bucket, orig_refill = config.FEED_RATE_BUCKET, config.FEED_RATE_REFILL_PER_MIN

    async def run(weighted: bool):
        import random as _random
        from collections import Counter
        from e14detector.community import crop_id, field_key_of as fk_of
        config.ACTA_DECK_COVERAGE_WEIGHTED = weighted
        config.FEED_RATE_BUCKET = 2000.0
        config.FEED_RATE_REFILL_PER_MIN = 1.0e6
        app = create_app(results_db=db, output_dir=output_dir, community_db=community_db)
        # cid -> doc map to deanonymize the served deck
        store2 = DetectorStore(db)
        cid_to_doc = {}
        for row in store2.conn.execute(
            "SELECT document_id, page_number, row_number, section FROM vote_fields "
            "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL"
        ):
            fkey = fk_of(row["document_id"], row["page_number"], row["row_number"], row["section"])
            cid_to_doc[crop_id(config.FORM_TOKEN_SECRET, fkey)] = row["document_id"]
        store2.close()
        counts = Counter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            _random.seed(99)
            for _ in range(600):
                items = (await client.get("/api/acta-deck")).json()["items"]
                counts[cid_to_doc[items[0]["cid"]]] += 1
        return counts

    try:
        weighted = asyncio.run(run(True))
        uniform = asyncio.run(run(False))
    finally:
        config.ACTA_DECK_COVERAGE_WEIGHTED = orig_flag
        config.FEED_RATE_BUCKET, config.FEED_RATE_REFILL_PER_MIN = orig_bucket, orig_refill

    # Weighted: the untouched doc-1 dominates. Uniform (flag off): roughly 50/50 despite history.
    assert weighted["doc-1"] > 3 * weighted["doc-0"], f"weighting should favour the unreviewed acta: {weighted}"
    assert 0.5 < uniform["doc-0"] / max(uniform["doc-1"], 1) < 2.0, f"flag off should be ~uniform: {uniform}"


def test_admin_cookie_sign_in_and_logout(tmp_path: Path) -> None:
    """Signing in once with ?key= sets an httpOnly token cookie + the non-secret e14_op flag, so
    later admin requests authenticate via cookie alone (no token in the URL); logout clears it.
    See _require_admin / _set_admin_cookies / admin_logout."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-1", source_path="doc-1.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="C", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    orig_token = config.ADMIN_TOKEN

    async def run():
        app = create_app(results_db=db, output_dir=output_dir)
        transport = httpx.ASGITransport(app=app)
        # https base_url so the client returns the Secure admin cookie on later requests.
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as client:
            config.ADMIN_TOKEN = "s3cret"
            no_auth = await client.get("/admin/coverage")             # no key, no cookie
            signin = await client.get("/admin/coverage?key=s3cret")   # signs in, sets cookies
            set_cookie = "; ".join(signin.headers.get_list("set-cookie"))
            cookie_only = await client.get("/admin/coverage")         # cookie in jar -> authed
            await client.get("/admin/logout")                         # clears the cookies
            after = await client.get("/admin/coverage")               # cookie gone -> blocked
            return (no_auth.status_code, signin.status_code, cookie_only.status_code,
                    after.status_code, set_cookie)

    try:
        no_auth, signin, cookie_only, after, set_cookie = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert no_auth == 403           # no key and no cookie -> forbidden
    assert signin == 200            # ?key= sign-in works
    assert "e14_admin=" in set_cookie and "HttpOnly" in set_cookie  # token persisted, httpOnly
    assert "e14_op=1" in set_cookie                                 # non-secret UI flag
    assert cookie_only == 200       # authenticated by the cookie alone, no key in URL
    assert after == 403             # logout cleared the cookie


def test_admin_login_form_sets_cookie(tmp_path: Path) -> None:
    """The /admin/login form authenticates via the X-Admin-Token header (no token in the URL,
    so it works inside the standalone PWA) and sets the operator cookie. See admin_login."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-1", source_path="doc-1.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="C", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    orig_token = config.ADMIN_TOKEN

    async def run():
        app = create_app(results_db=db, output_dir=output_dir)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as client:
            config.ADMIN_TOKEN = ""
            off = await client.get("/admin/login")                 # feature off -> hidden
            config.ADMIN_TOKEN = "s3cret"
            page = await client.get("/admin/login")                # form is viewable
            bad = await client.post("/admin/login", headers={"X-Admin-Token": "nope"})
            ok = await client.post("/admin/login", headers={"X-Admin-Token": "s3cret"})
            set_cookie = "; ".join(ok.headers.get_list("set-cookie"))
            after = await client.get("/admin/coverage")            # authed by the fresh cookie
            return (off.status_code, page.status_code, bad.status_code, ok.status_code,
                    after.status_code, set_cookie)

    try:
        off, page, bad, ok, after, set_cookie = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert off == 404                       # no token configured -> login hidden too
    assert page == 200                      # form is publicly viewable (just a password gate)
    assert bad == 403                       # wrong token rejected
    assert ok == 200                        # correct token accepted
    assert "e14_admin=" in set_cookie and "HttpOnly" in set_cookie
    assert after == 200                     # the cookie now authenticates admin pages


def test_admin_magic_link_signs_in_and_strips_token(tmp_path: Path) -> None:
    """A bookmarkable /admin/login?key=TOKEN validates, sets the cookie, and 303-redirects to a
    token-free /admin/poll; later pages authenticate via the cookie. A wrong key falls to the
    form (no redirect, no cookie). See admin_login_page."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(document_id="doc-1", source_path="doc-1.pdf"))
    store.insert_vote_field(VoteField(
        document_id="doc-1", page_number=1, row_type="candidate", row_number=1,
        candidate_name="C", raw_crop_path=str(crop),
    ))
    store.commit()
    store.close()

    orig_token = config.ADMIN_TOKEN

    async def run():
        app = create_app(results_db=db, output_dir=output_dir)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t",
                                     follow_redirects=False) as client:
            config.ADMIN_TOKEN = "s3cret"
            bad = await client.get("/admin/login?key=nope")     # wrong key -> form, no redirect
            magic = await client.get("/admin/login?key=s3cret")  # right key -> 303 + cookie
            set_cookie = "; ".join(magic.headers.get_list("set-cookie"))
            loc = magic.headers.get("location", "")
            authed = await client.get("/admin/coverage")        # cookie in jar authenticates
            return (bad.status_code, "Token" in bad.text, magic.status_code, loc,
                    set_cookie, authed.status_code)

    try:
        bad_code, bad_is_form, magic_code, loc, set_cookie, authed = asyncio.run(run())
    finally:
        config.ADMIN_TOKEN = orig_token

    assert bad_code == 200 and bad_is_form          # wrong key shows the form, doesn't sign in
    assert magic_code == 303 and loc == "/admin/poll"   # magic link redirects to a token-free URL
    assert "e14_admin=" in set_cookie and "HttpOnly" in set_cookie
    assert authed == 200                             # signed in by the cookie alone
