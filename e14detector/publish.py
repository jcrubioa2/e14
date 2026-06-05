"""Upload public candidate crops to the object store (Fly Tigris / S3).

Loop #1 of the rollout, crop half: push the candidate crops the public page references
to the bucket the page serves from (``E14_CDN_BASE_URL``). Incremental — a local manifest
records uploaded keys so re-running only sends new crops ("publish the next batch").

The DB half (shipping new rows to the served volume) is separate; see the rollout plan.

Credentials come from the standard S3 env (set on your machine from `fly storage create`):
``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_ENDPOINT_URL_S3``, ``BUCKET_NAME``.
"""
from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .storage import DetectorStore
from .webapp import crop_key, crop_prefix, crop_rel


def _results_db(output_dir: Path) -> Path:
    return Path(output_dir) / "results" / "results.sqlite"


def crop_upload_plan(
    output_dir: Path,
    skip_keys: set[str] | None = None,
    *,
    department: str | None = None,
    round: str | None = None,
) -> list[tuple[Path, str]]:
    """(local_path, object_key) for candidate crops that need uploading.

    ``skip_keys`` (already-uploaded keys) are skipped BEFORE touching the filesystem, so
    enumeration is O(new crops) not O(all crops) — critical once the manifest is large.
    With ``department``, only crops for that department code or name are considered.

    The object key is round-scoped (``crops/<file>`` for r1, ``crops/<round>/<file>`` otherwise),
    but the local source file always lives under ``<output_dir>/crops/<rel>`` regardless of round
    (one round per machine), so local-file resolution uses the round-agnostic ``crop_rel``.
    """
    skip_keys = skip_keys or set()
    store = DetectorStore(_results_db(output_dir))
    try:
        paths = store.candidate_crop_paths(department)
    finally:
        store.close()
    plan: list[tuple[Path, str]] = []
    out = Path(output_dir)
    for p in paths:
        key = crop_key(p, round)
        if key in skip_keys:
            continue  # already uploaded — don't even stat it
        local = Path(p)
        if not local.is_absolute():
            local = local if local.exists() else (out / Path(p).name)
        if not local.exists():
            # Round-agnostic on-disk path: <output_dir>/crops/<rel> (NOT the round-prefixed key).
            local = out / "crops" / crop_rel(p)
        if local.exists():
            plan.append((local, key))
    return plan


def _load_manifest(manifest: Path) -> set[str]:
    if manifest and manifest.exists():
        return set(manifest.read_text(encoding="utf-8").split())
    return set()


def list_bucket_crop_keys(
    *,
    bucket: str | None = None,
    client=None,
    prefix: str | None = None,
    round: str | None = None,
    verbose: bool = False,
) -> set[str]:
    """All crop object keys for ``round`` in the bucket (Tigris source of truth for uploads).

    The default prefix is the round's crop prefix (``crops/`` for r1, ``crops/<round>/`` else),
    so an r2 listing is properly isolated. NB: an r1 (``crops/``) listing also matches nested
    ``crops/r2/`` keys — harmless because R1 is frozen before R2 starts publishing, but pass an
    explicit ``prefix`` if you ever need a strict r1-only listing.
    """
    prefix = prefix or crop_prefix(round)
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    if client is None:
        client = _default_client()
    keys: set[str] = set()
    listed = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
            listed += 1
        if verbose:
            print(f"list-bucket: {listed} object(s)…", end="\r", flush=True)
    if verbose:
        print(f"\nlist-bucket: {len(keys)} crop key(s) in {bucket}", flush=True)
    return keys


def refresh_bucket_upload_cache(
    cache_path: Path,
    *,
    bucket: str | None = None,
    client=None,
    round: str | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Write every bucket crop key to *cache_path* (for status / cross-host visibility)."""
    keys = list_bucket_crop_keys(bucket=bucket, client=client, round=round, verbose=verbose)
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")
    tmp.replace(cache_path)
    return {"listed": len(keys)}


def reconcile_manifest(
    output_dir: Path,
    *,
    bucket: str | None = None,
    client=None,
    prefix: str | None = None,
    round: str | None = None,
    manifest: Path | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    """Rebuild the upload manifest from the *bucket* (the source of truth) for ``round``.

    Lets any machine resume incremental publishing: it lists every crop object already in the
    store (under the round's crop prefix) and writes their keys to the manifest, so a later
    ``publish-crops`` / ``publish-loop`` only sends the delta instead of re-uploading everything.
    The object key is exactly the manifest key (``crops/<file>`` for r1 — see ``webapp.crop_key``),
    so no mapping is needed. Unions with any existing manifest, so a just-uploaded key is never
    forgotten.
    """
    output_dir = Path(output_dir)
    prefix = prefix or crop_prefix(round)
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    manifest = manifest or (output_dir / "review" / "uploaded_crops.txt")
    if client is None:
        client = _default_client()

    keys = _load_manifest(manifest)
    before = len(keys)
    listed = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
            listed += 1
        if verbose:
            print(f"reconcile: listed {listed} object(s)…", end="\r", flush=True)

    manifest.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: build beside the file and replace, so an interrupted run never leaves a
    # half-written manifest that would cause re-uploads.
    tmp = manifest.with_suffix(manifest.suffix + ".tmp")
    tmp.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")
    tmp.replace(manifest)
    if verbose:
        print(f"\nreconcile: {listed} crop object(s) in bucket; manifest {before} -> {len(keys)} key(s)",
              flush=True)
    return {"listed": listed, "before": before, "after": len(keys)}


def _default_client(workers: int = 16):
    import boto3  # local-only dep; imported lazily so the lean serve image never needs it
    from botocore.config import Config

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    # Size the connection pool to the worker count (default pool is 10, which throttles
    # many small concurrent PUTs), and retry transient errors.
    cfg = Config(
        max_pool_connections=max(workers * 2, 16),
        retries={"max_attempts": 5, "mode": "adaptive"},
    )
    return boto3.client("s3", endpoint_url=endpoint, config=cfg)


def publish_crops(
    output_dir: Path,
    *,
    bucket: str | None = None,
    client=None,
    manifest: Path | None = None,
    limit: int | None = None,
    workers: int = 16,
    department: str | None = None,
    round: str | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, int]:
    """Upload new candidate crops to ``bucket`` under the round's crop prefix. Returns counts."""
    output_dir = Path(output_dir)
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket and not dry_run:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    manifest = manifest or (output_dir / "review" / "uploaded_crops.txt")

    done = _load_manifest(manifest)
    pending = crop_upload_plan(output_dir, skip_keys=done, department=department, round=round)
    if limit is not None:
        pending = pending[:limit]
    totals = {"uploaded": 0, "skipped": len(done), "failed": 0}

    if verbose:
        dept_note = f" dept={department}" if department else ""
        print(
            f"publish-crops: {len(pending)} new crop(s){dept_note} "
            f"-> bucket={bucket or '(dry-run)'}{' [dry-run]' if dry_run else ''}",
            flush=True,
        )
    if dry_run or not pending:
        return totals

    if client is None:
        client = _default_client(workers)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    def _upload(local: Path, key: str) -> None:
        client.upload_file(str(local), bucket, key, ExtraArgs={"ContentType": "image/png"})

    # Bounded in-flight window: submit a few per worker, then submit one more for each
    # completion. Keeps memory flat at any scale and lets the manifest track uploads
    # closely (so a resume after interruption re-does almost nothing).
    window = max(workers * 4, 32)
    items = iter(pending)
    total = len(pending)
    with manifest.open("a", encoding="utf-8") as mf, ThreadPoolExecutor(max_workers=workers) as ex:
        inflight: dict = {}

        def _submit_next() -> bool:
            item = next(items, None)
            if item is None:
                return False
            local, key = item
            inflight[ex.submit(_upload, local, key)] = key
            return True

        for _ in range(window):
            if not _submit_next():
                break
        while inflight:
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                key = inflight.pop(fut)
                try:
                    fut.result()
                except Exception as exc:
                    totals["failed"] += 1
                    if verbose:
                        print(f"publish-crops: FAILED {key}: {exc}", flush=True)
                else:
                    mf.write(key + "\n")
                    mf.flush()
                    totals["uploaded"] += 1
                    if verbose and totals["uploaded"] % 500 == 0:
                        print(f"publish-crops: {totals['uploaded']}/{total} uploaded", flush=True)
                _submit_next()
    return totals
