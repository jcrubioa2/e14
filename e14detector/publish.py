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
from .webapp import crop_key


def _results_db(output_dir: Path) -> Path:
    return Path(output_dir) / "results" / "results.sqlite"


def crop_upload_plan(output_dir: Path) -> list[tuple[Path, str]]:
    """(local_path, object_key) for every candidate crop that exists on disk."""
    store = DetectorStore(_results_db(output_dir))
    try:
        paths = store.candidate_crop_paths()
    finally:
        store.close()
    plan: list[tuple[Path, str]] = []
    out = Path(output_dir)
    for p in paths:
        local = Path(p)
        if not local.is_absolute():
            # Stored relative (e.g. "data/detector_national/crops/x.png"); try as-is and
            # under the output dir's parent layout.
            local = local if local.exists() else (out / Path(p).name)
        if not local.exists():
            # Last resort: <output_dir>/<crops/...> from the key suffix.
            local = out / crop_key(p)
        if local.exists():
            plan.append((local, crop_key(p)))
    return plan


def _load_manifest(manifest: Path) -> set[str]:
    if manifest and manifest.exists():
        return set(manifest.read_text(encoding="utf-8").split())
    return set()


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
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, int]:
    """Upload new candidate crops to ``bucket``. Returns counts."""
    output_dir = Path(output_dir)
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket and not dry_run:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    manifest = manifest or (output_dir / "review" / "uploaded_crops.txt")

    plan = crop_upload_plan(output_dir)
    done = _load_manifest(manifest)
    pending = [(p, k) for (p, k) in plan if k not in done]
    if limit is not None:
        pending = pending[:limit]
    totals = {"uploaded": 0, "skipped": len(plan) - len(pending), "failed": 0}

    if verbose:
        print(
            f"publish-crops: {len(plan)} candidate crop(s), {len(pending)} new "
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
