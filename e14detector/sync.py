"""Unified, round-aware sync orchestration — ONE entry point with the count-model consistency
rules baked into the code path, so an operator can't forget them.

Before this, the incremental upload pipeline was scattered across ~10 shell scripts and a dozen
CLI verbs, with the consistency rules ("publish only the uploaded frontier", "never shrink the
live DB", "refresh the universe so the denominator stays honest", "verify before you lock") living
in operators' heads. That is exactly how the served/published counts drifted. This module folds
them into a single tool whose every path enforces the rules:

- **lock-aware**     — never overwrites a locked round unless explicitly allowed
- **frontier-only**  — publishes only actas whose crops are all uploaded (``only_uploaded``)
- **shrink-guard**   — refuses a DB that lost actas (inherited from ``publish_db``)
- **chain-stamp**    — every publish stamps the count-model reconciliation into the pointer
- **verify-first**   — ``run`` re-checks the invariant chain before returning

It is a thin orchestrator over the existing primitives (``publish_crops``, ``publish_db``,
``pull_db``, ``reconcile_manifest``, ``fetch_universe_counts``) — it does not reimplement them.
CV-only: no VLM stage is invoked anywhere here.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config  # noqa: F401 — importing triggers config._load_dotenv() so boto3 sees the
#                       Tigris creds from .env when sync hits the bucket (stamp/backup/restore/run)

# The canonical, non-increasing order of the count-model chain. Kept here (not imported from
# webapp, which pulls FastAPI) so the CLI stays light.
CHAIN_ORDER = [
    ("total_global", "Total nacional de mesas"),
    ("mesas_escrutadas", "Mesas escrutadas (resultados)"),
    ("mesas_informadas", "Actas con imagen (divulgador)"),
    ("downloaded", "Actas descargadas"),
    ("crops_uploaded", "Recortes subidos (frontera)"),
    ("sqlite_served", "En la DB servida"),
]


def _served_db(output_dir: Path) -> Path:
    return Path(output_dir) / "results" / "results.sqlite"


def _served_count(output_dir: Path) -> int | None:
    """COUNT(documents) in the local served DB, or None if there is no DB yet."""
    db = _served_db(output_dir)
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=30.0)
        try:
            return int(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None


def gather_reconciliation(output_dir: Path, *, cdn_base: str | None = None,
                          round: str | None = None) -> tuple[dict, str]:
    """Return (reconciliation, source). Prefer the live published pointer (the single
    reconciliation record); fall back to computing it locally from the served DB + the
    universe snapshot when there is no pointer (fresh machine / offline)."""
    from .dbsync import compute_reconciliation, pointer_key, pointer_status

    if cdn_base:
        ptr = pointer_status(cdn_base, round=round)
        recon = (ptr or {}).get("reconciliation")
        if recon:
            return recon, f"puntero publicado ({pointer_key(round)})"
    db = _served_db(output_dir)
    if db.exists():
        n = _served_count(output_dir) or 0
        recon = compute_reconciliation(db, n_docs=n, output_dir=Path(output_dir), round=round)
        return recon, "cálculo local (DB servida + universe_snapshot)"
    return {}, "sin datos"


# --- verify ----------------------------------------------------------------

@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_chain(recon: dict, *, served_count: int | None = None,
                 published: int | None = None) -> VerifyReport:
    """Assert the count-model invariants over a reconciliation block.

    1. The chain is non-increasing over its known counts (any inversion is impossible and an
       alarm). 2. The local served count equals what the pointer claims it published
       (``served == published`` — the hard invariant). Unknown counts are skipped, not failed.
    """
    rep = VerifyReport()
    prev_key = None
    prev_val = None
    for key, label in CHAIN_ORDER:
        val = recon.get(key)
        if not isinstance(val, int):
            continue
        if prev_val is not None and val > prev_val:
            rep.problems.append(
                f"inversión de la cadena: {label} ({val}) > {prev_key} ({prev_val})")
        prev_key, prev_val = label, val
    # served == published
    if served_count is not None and published is not None and served_count != published:
        rep.problems.append(
            f"servida ≠ publicada: la DB local tiene {served_count} actas pero el puntero "
            f"declara {published}")
    elif served_count is not None and published is not None:
        rep.notes.append(f"servida = publicada = {served_count} ✓")
    bi = recon.get("backlog_ingesta")
    if isinstance(bi, int):
        rep.notes.append(f"backlog de ingesta = {bi}")
    return rep


# --- printable status ------------------------------------------------------

def format_chain(recon: dict, source: str) -> str:
    """A terminal rendering of the count chain + cobertura + backlogs (matches the admin board)."""
    lines = [f"Cadena de conteos  (fuente: {source})"]
    if not recon:
        return lines[0] + "\n  (sin datos: no hay puntero ni DB servida local)"
    prev = None
    for key, label in CHAIN_ORDER:
        val = recon.get(key)
        if isinstance(val, int):
            mark = "✓"
            if prev is not None and val > prev:
                mark = "⚠ INVERSIÓN"
            prev = val
            shown = f"{val:,}".replace(",", ".")
        else:
            mark = "—"
            shown = "—"
        lines.append(f"  {label:<32} {shown:>12}  {mark}")
    inf = recon.get("mesas_informadas")
    served = recon.get("sqlite_served")
    if isinstance(inf, int) and inf and isinstance(served, int):
        cob = f"{served * 100 / inf:.2f}".replace(".", ",")
        lines.append(f"  cobertura = {cob}%   backlog ingesta = {recon.get('backlog_ingesta', '—')}"
                     f"   backlog reporte = {recon.get('backlog_reporte', '—')}")
    if recon.get("universe_fetched_at"):
        lines.append(f"  universo actualizado: {recon['universe_fetched_at']}")
    return "\n".join(lines)


# --- orchestrated actions (called by the CLI) ------------------------------

def do_status(output_dir: Path, *, cdn_base: str | None, round: str | None = None) -> int:
    recon, source = gather_reconciliation(output_dir, cdn_base=cdn_base, round=round)
    print(format_chain(recon, source))
    return 0


def do_verify(output_dir: Path, *, bucket: str | None, cdn_base: str | None,
              check_crops: bool = False, check_content: bool = False,
              round: str | None = None) -> int:
    """Invariant-chain assertions (plus optional crop-existence / content-integrity, wired in
    P1.C/P1.D). Returns a nonzero exit code on any violation so it can gate a cron / pre-lock."""
    from .dbsync import pointer_status

    recon, source = gather_reconciliation(output_dir, cdn_base=cdn_base, round=round)
    served = _served_count(output_dir)
    published = None
    if cdn_base:
        ptr = pointer_status(cdn_base, round=round)
        published = (ptr or {}).get("n_docs")
    rep = verify_chain(recon, served_count=served, published=published)

    if check_crops:
        try:
            from .cropaudit import audit_served_crops  # P1.D
        except ImportError:
            rep.notes.append("verificación de recortes: módulo aún no disponible (P1.D)")
        else:
            rep.problems.extend(audit_served_crops(output_dir, bucket=bucket, round=round))
    if check_content:
        from .contentcheck import content_note
        note = content_note()
        rep.notes.append(note or "integridad de contenido: sin informe aún "
                                 "(genera uno con scripts/verify_acta_content.py --sample N)")

    print(format_chain(recon, source))
    for n in rep.notes:
        print(f"  · {n}")
    if rep.ok:
        print("verify: OK — la cadena de conteos es consistente.")
        return 0
    print("verify: PROBLEMAS:")
    for p in rep.problems:
        print(f"  ✗ {p}")
    return 1


def do_restore(output_dir: Path, *, bucket: str | None, cdn_base: str | None,
               prefix: str | None = None, round: str | None = None) -> int:
    """Resume on a fresh/crashed machine: rebuild the upload manifest from the bucket, then pull
    and merge the published DB. Wraps ``reconcile_manifest`` + ``pull_db``. Round-scoped: the
    manifest is rebuilt from the round's crop prefix and the round's published pointer."""
    from .dbsync import pull_db
    from .publish import reconcile_manifest

    info = reconcile_manifest(output_dir=Path(output_dir), bucket=bucket, prefix=prefix, round=round)
    print(f"restore: bucket tenía {info['listed']} recorte(s); manifest {info['before']} -> {info['after']}")
    pulled = pull_db(Path(output_dir), cdn_base=cdn_base, bucket=bucket, round=round)
    if pulled is None:
        print("restore: no hay puntero publicado aún (nada que fusionar)")
    else:
        print(f"restore: DB fusionada (sha {pulled.get('sha256', '?')})")
    return 0


def do_stamp_pointer(output_dir: Path, *, bucket: str | None, cdn_base: str | None,
                     round: str | None = None) -> int:
    """Safely add/refresh the reconciliation block on the LIVE pointer WITHOUT rebuilding the DB.

    For a frozen/locked round whose snapshot is unchanged: ``publish-db --force-pointer`` would
    rebuild from the local DB (dangerous if it's stale), so instead this downloads the *live*
    snapshot read-only to compute the served-key diff, then merges the reconciliation block into
    the existing ``db/latest.json`` and re-uploads it — preserving key/sha/size/n_docs exactly, so
    the served count can never regress. The only mutation is adding the reconciliation block.
    """
    import json
    import tempfile

    from .dbsync import (
        _download_snapshot_file, _resolve_bucket, _s3_client,
        compute_reconciliation, fetch_published_pointer, pointer_key,
    )

    pk = pointer_key(round)
    bucket = _resolve_bucket(bucket)
    if not bucket:
        print("stamp-pointer: no bucket (set BUCKET_NAME or pass --bucket)")
        return 1
    client = _s3_client()
    pointer = fetch_published_pointer(bucket=bucket, client=client, round=round)
    if pointer is None:
        print("stamp-pointer: no hay puntero publicado")
        return 1
    n_docs = pointer.get("n_docs")
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "live.sqlite"
        _download_snapshot_file(pointer, snap, cdn_base=cdn_base or None, bucket=bucket,
                                client=client, timeout=600)
        recon = compute_reconciliation(snap, n_docs=int(n_docs or 0), output_dir=Path(output_dir),
                                       round=round)
    pointer["reconciliation"] = recon
    pointer["ts"] = int(time.time())
    client.put_object(Bucket=bucket, Key=pk,
                      Body=json.dumps(pointer).encode(),
                      ContentType="application/json", CacheControl="no-store, max-age=0")
    print(f"stamp-pointer: reconciliación estampada en {pk} "
          f"(n_docs preservado={n_docs}, total_global={recon.get('total_global', '—')}, "
          f"informadas={recon.get('mesas_informadas', '—')}, "
          f"ingesta={recon.get('backlog_ingesta', '—')}, reporte={recon.get('backlog_reporte', '—')})")
    return 0


def do_backup(output_dir: Path, *, dest: Path, bucket: str | None, cdn_base: str | None,
              round: str | None = None) -> int:
    """Write one off-Tigris DR copy of the live published snapshot (R1 is the permanent record)."""
    from .dbsync import backup_published_db

    info = backup_published_db(Path(dest), cdn_base=cdn_base, bucket=bucket, round=round)
    if info is None:
        print("backup: no hay puntero publicado (nada que respaldar)")
        return 1
    print(f"backup: {info.get('n_docs', '?')} actas -> {info['path']} (sha {info['sha256'][:12]})")
    return 0


def do_run(output_dir: Path, *, bucket: str | None, cdn_base: str | None,
           refresh_universe: bool = True, workers: int = 32, upload_limit: int | None = 12000,
           interval: int = 60, db_interval: int = 300, once: bool = False,
           department: str | None = None, allow_locked: bool = False,
           allow_shrink: bool = False, round: str | None = None) -> int:
    """The one safe publisher loop, with the consistency rules baked in.

    Each cycle: (optionally) refresh the universe snapshot so the chain denominator stays honest,
    upload the new crop delta, then publish the fully-uploaded *frontier* DB (which stamps the
    reconciliation chain). Lock-aware: a locked round is skipped unless ``allow_locked``. Verifies
    the chain at the end of a one-shot run. Idempotent and resumable — safe to kill and restart.
    """
    from .dbsync import publish_db, read_db_lock
    from .publish import publish_crops

    output_dir = Path(output_dir)

    def _refresh_universe() -> None:
        try:
            import os as _os

            from e14.universe import (
                fetch_universe_counts, load_universe_snapshot, snapshot_path,
                write_universe_snapshot,
            )
            uni_path = snapshot_path(round)
            recs, _nodes = fetch_universe_counts()
            # total_global (installed mesas) is from the bot-protected results portal, so it can't
            # be scraped here: take $E14_TOTAL_GLOBAL, else carry forward the last snapshot's value.
            tg = None
            src = None
            if _os.environ.get("E14_TOTAL_GLOBAL", "").isdigit():
                tg, src = int(_os.environ["E14_TOTAL_GLOBAL"]), "$E14_TOTAL_GLOBAL"
            else:
                prev = load_universe_snapshot(uni_path)
                if prev and prev.get("total_global"):
                    tg, src = int(prev["total_global"]), (prev.get("total_global_source") or "heredado")
            snap = write_universe_snapshot(recs, uni_path, total_global=tg, total_global_source=src)
            tg_txt = f"{snap['total_global']:,}" if snap["total_global"] is not None else "—"
            print(f"[sync] universo: total_global={tg_txt} informadas={snap['mesas_informadas']:,}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 — a failed refresh must never stop publishing
            print(f"[sync] universo: refresh omitido ({type(exc).__name__}: {exc})", flush=True)

    last_db = 0.0
    last_universe = 0.0
    while True:
        started = time.time()
        try:
            # Refresh the universe at most every db_interval (it changes slowly; the scrape is ~6MB).
            if refresh_universe and (once or (time.time() - last_universe) >= db_interval):
                _refresh_universe()
                last_universe = time.time()

            # Lock-aware: don't even upload over a locked round unless told to.
            if not allow_locked:
                lock = read_db_lock(bucket=bucket, round=round)
                if lock.get("locked"):
                    print(f"[sync] ronda BLOQUEADA{(' (' + lock['reason'] + ')') if lock.get('reason') else ''}"
                          f" — sin publicar. Usa --allow-locked para forzar.", flush=True)
                    if once:
                        return 0
                    time.sleep(interval)
                    continue

            crops = publish_crops(output_dir, bucket=bucket, workers=workers,
                                  limit=upload_limit, department=department, round=round, verbose=False)
            db_note = "db no toca"
            if once or (time.time() - last_db) >= db_interval:
                info = publish_db(output_dir, bucket=bucket, only_uploaded=True,
                                  allow_locked=allow_locked, allow_shrink=allow_shrink,
                                  round=round, verbose=False)
                if info is None:
                    db_note = "frontera vacía"
                elif info.get("locked"):
                    db_note = "BLOQUEADA"
                elif info.get("guarded"):
                    db_note = "GUARD (shrink)"
                else:
                    last_db = time.time()
                    recon = info.get("reconciliation") or {}
                    db_note = (f"frontera {info.get('kept', info.get('n_docs'))} actas "
                               f"(sha {info['sha256'][:8]}, ingesta {recon.get('backlog_ingesta', '—')})")
            print(f"[sync] +{crops['uploaded']} recortes (fallo {crops['failed']}) · {db_note} · "
                  f"{time.time()-started:.0f}s", flush=True)
        except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the loop
            print(f"[sync] error de ciclo ({type(exc).__name__}): {exc} · "
                  f"{time.time()-started:.0f}s — continúo", flush=True)
        if once:
            break
        time.sleep(interval)

    # verify-before-lock: a one-shot run ends by re-checking the invariant chain.
    return do_verify(output_dir, bucket=bucket, cdn_base=cdn_base, round=round)
