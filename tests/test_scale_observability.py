"""Tests for the scale-readiness + observability additions:

- ``counts_among_cached`` collapses repeated per-crop tally reads (the Aurora-decoupling change)
- ``GET /health`` is a real readiness probe (200 only with a non-stub DB) for an external monitor
- ``alerts.notify`` dedups so a sustained fault doesn't flood the channel, and is a safe no-op
  when no channel is configured.
"""
import asyncio
from pathlib import Path

import httpx
from PIL import Image

from e14detector import alerts
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore
from e14detector.webapp import create_app


def _crop(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color=(255, 255, 255)).save(path)
    return path


def _seed_acta(output_dir: Path) -> Path:
    """One acta with three candidate casillas (crops) — enough for the /acta deck."""
    db = output_dir / "results" / "results.sqlite"
    crop = _crop(output_dir / "crops" / "c.png")
    store = DetectorStore(db)
    store.upsert_document(DocumentMetadata(
        document_id="doc-a", source_path="doc-a.pdf", department_code="01",
        department_name="ANTIOQUIA", municipality_code="001", municipality_name="MEDELLIN",
        zone="001", puesto="01",
    ))
    for row in (1, 2, 3):
        store.insert_vote_field(VoteField(
            document_id="doc-a", page_number=1, row_type="candidate", row_number=row,
            candidate_name=f"C{row}", raw_crop_path=str(crop),
        ))
    store.commit()
    store.close()
    return db


def test_counts_among_cached_collapses_repeat_reads(tmp_path: Path) -> None:
    """Two views of the same acta within the TTL make only ONE counts_among round-trip."""
    output_dir = tmp_path / "out"
    db = _seed_acta(output_dir)
    app = create_app(results_db=db, output_dir=output_dir)

    # Count how often the underlying store is actually queried (app.state.community IS the
    # object the cache closure calls — see webapp.create_app).
    calls: list[list[str]] = []
    real = app.state.community.counts_among

    def counting(keys):
        calls.append(list(keys))
        return real(keys)

    app.state.community.counts_among = counting

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r1 = await client.get("/acta/doc-a")
            r2 = await client.get("/acta/doc-a")
        assert r1.status_code == 200 and r2.status_code == 200
        # First render queried the three casillas; the second was fully served from cache.
        assert len(calls) == 1, f"expected 1 store query, got {len(calls)}: {calls}"
        assert len(calls[0]) == 3

    asyncio.run(run())


def test_counts_ttl_zero_disables_cache(tmp_path: Path, monkeypatch) -> None:
    """E14_COUNTS_TTL=0 falls straight through to the store on every view (escape hatch)."""
    from e14detector import config

    monkeypatch.setattr(config, "COUNTS_TTL", 0.0)
    output_dir = tmp_path / "out"
    db = _seed_acta(output_dir)
    app = create_app(results_db=db, output_dir=output_dir)
    calls: list[list[str]] = []
    real = app.state.community.counts_among
    app.state.community.counts_among = lambda keys: (calls.append(list(keys)), real(keys))[1]

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.get("/acta/doc-a")
            await client.get("/acta/doc-a")
        assert len(calls) == 2, "TTL=0 must not cache"

    asyncio.run(run())


def test_health_ready_and_stub(tmp_path: Path) -> None:
    """/health: 200 with a populated DB, 503 with a stub (schema but no documents)."""
    ready_dir = tmp_path / "ready"
    ready_db = _seed_acta(ready_dir)

    stub_dir = tmp_path / "stub"
    stub_db = stub_dir / "results" / "results.sqlite"
    DetectorStore(stub_db).close()  # creates the schema, inserts nothing

    async def status(db: Path, out: Path) -> tuple[int, bool]:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=out))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/health")
            return r.status_code, r.json()["db"]

    async def run() -> None:
        code, ok = await status(ready_db, ready_dir)
        assert code == 200 and ok is True
        code, ok = await status(stub_db, stub_dir)
        assert code == 503 and ok is False

    asyncio.run(run())


def test_alert_dedup_and_unconfigured_noop(monkeypatch) -> None:
    """Same key is deduped within the interval; notify never raises and is a no-op offline."""
    monkeypatch.setenv("E14_ALERT_MIN_INTERVAL", "600")
    monkeypatch.delenv("E14_ALERT_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("E14_ALERT_TELEGRAM_CHAT_ID", raising=False)
    alerts._last_sent.clear()

    assert alerts.configured() is False
    assert alerts._should_send("db-sync") is True
    assert alerts._should_send("db-sync") is False      # deduped
    assert alerts._should_send("vote-publish") is True  # different key, independent

    # No channel configured -> safe no-op (must not raise even though it spawns a thread).
    alerts._last_sent.clear()
    alerts.notify("db-sync", "loop error: boom")


def test_admin_health_board_renders(tmp_path: Path, monkeypatch) -> None:
    """The operator board renders the new health tiles (token-gated, votes-backend up)."""
    from e14detector import config

    monkeypatch.setattr(config, "ADMIN_TOKEN", "secret-token")
    output_dir = tmp_path / "out"
    db = _seed_acta(output_dir)

    async def run() -> None:
        transport = httpx.ASGITransport(app=create_app(results_db=db, output_dir=output_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/admin/poll")).status_code == 403  # no key
            r = await client.get("/admin/poll?key=secret-token")
        assert r.status_code == 200
        body = r.text
        assert "errores 5xx" in body and "backend de votos" in body and "conectado" in body

    asyncio.run(run())
