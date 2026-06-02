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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _default_client():
    import boto3  # local-only dep; imported lazily so the lean serve image never needs it

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client("s3", endpoint_url=endpoint)


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
        client = _default_client()
    manifest.parent.mkdir(parents=True, exist_ok=True)

    def _upload(local: Path, key: str) -> str:
        client.upload_file(str(local), bucket, key, ExtraArgs={"ContentType": "image/png"})
        return key

    with manifest.open("a", encoding="utf-8") as mf, ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_upload, p, k): k for (p, k) in pending}
        for n, fut in enumerate(as_completed(futures), start=1):
            key = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                totals["failed"] += 1
                if verbose:
                    print(f"publish-crops: FAILED {key}: {exc}", flush=True)
                continue
            mf.write(key + "\n")
            mf.flush()
            totals["uploaded"] += 1
            if verbose and n % 500 == 0:
                print(f"publish-crops: {n}/{len(pending)} uploaded", flush=True)
    return totals
