"""Publish and pull the fleet department queue through Tigris/S3."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from .fleet import QUEUE_NAME, default_queue_path, load_queue, merge_queues, save_queue

FLEET_DIR = "fleet"
POINTER_KEY = f"{FLEET_DIR}/latest.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _s3_client():
    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client("s3", endpoint_url=endpoint)


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_fleet_pointer(
    *,
    cdn_base: str | None = None,
    bucket: str | None = None,
    client=None,
    timeout: float = 30.0,
) -> dict | None:
    if cdn_base:
        url = f"{cdn_base.rstrip('/')}/{POINTER_KEY}"
        if url.startswith("http"):
            url += f"?t={int(time.time())}"
        try:
            return json.loads(_fetch(url, timeout))
        except Exception:
            return None
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        return None
    if client is None:
        client = _s3_client()
    try:
        return json.loads(client.get_object(Bucket=bucket, Key=POINTER_KEY)["Body"].read())
    except Exception:
        return None


def _download_queue(pointer: dict, dest: Path, *, cdn_base: str | None, bucket: str | None, client, timeout: float) -> None:
    key = pointer["key"]
    want = pointer["sha256"]
    cdn_base = (cdn_base or "").rstrip("/")
    if cdn_base.startswith("http"):
        data = _fetch(f"{cdn_base}/{key}", timeout)
        dest.write_bytes(data)
    else:
        bucket = bucket or os.environ.get("BUCKET_NAME")
        if client is None:
            client = _s3_client()
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        dest.write_bytes(body)
    got = _sha256_file(dest)
    if got != want:
        raise ValueError(f"fleet queue sha mismatch: got {got[:12]} want {want[:12]}")


def pull_fleet(
    output_dir: Path,
    *,
    cdn_base: str | None = None,
    bucket: str | None = None,
    client=None,
    timeout: float = 60.0,
    verbose: bool = True,
) -> dict | None:
    """Download remote fleet queue and merge into local ``fleet/queue.json``."""
    local_path = default_queue_path(output_dir)
    pointer = fetch_fleet_pointer(cdn_base=cdn_base, bucket=bucket, client=client, timeout=timeout)
    if pointer is None:
        if verbose:
            print("pull-fleet: no published pointer (nothing to merge yet)", flush=True)
        return None
    with tempfile.TemporaryDirectory() as td:
        remote_path = Path(td) / QUEUE_NAME
        _download_queue(
            pointer, remote_path, cdn_base=cdn_base or os.environ.get("E14_CDN_BASE_URL") or "",
            bucket=bucket, client=client, timeout=timeout,
        )
        remote_q = load_queue(remote_path)
        if local_path.is_file():
            local_q = load_queue(local_path)
            merged = merge_queues(local_q, remote_q)
        else:
            merged = remote_q
        save_queue(local_path, merged)
    if verbose:
        print(f"pull-fleet: merged remote@{pointer.get('sha256', '')[:12]}", flush=True)
    return {"sha256": pointer.get("sha256", "")[:12], "path": str(local_path)}


def publish_fleet(
    output_dir: Path,
    *,
    bucket: str | None = None,
    client=None,
    verbose: bool = True,
) -> dict | None:
    """Upload local fleet queue and flip ``fleet/latest.json``."""
    local_path = default_queue_path(output_dir)
    if not local_path.is_file():
        if verbose:
            print("publish-fleet: no local queue (run fleet-init first)", flush=True)
        return None
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        raise ValueError("no bucket for publish-fleet")
    if client is None:
        client = _s3_client()
    raw = local_path.read_bytes()
    digest = _sha256_bytes(raw)
    key = f"{FLEET_DIR}/queue-{digest[:16]}.json"
    pointer = fetch_fleet_pointer(bucket=bucket, client=client)
    if pointer and pointer.get("sha256") == digest:
        if verbose:
            print("publish-fleet: unchanged since last publish; skipping upload", flush=True)
        return {"sha256": digest, "key": pointer.get("key", key), "unchanged": True}
    client.put_object(Bucket=bucket, Key=key, Body=raw, ContentType="application/json")
    meta = {
        "key": key,
        "sha256": digest,
        "size": len(raw),
        "ts": int(time.time()),
    }
    client.put_object(
        Bucket=bucket,
        Key=POINTER_KEY,
        Body=json.dumps(meta, separators=(",", ":")).encode(),
        ContentType="application/json",
        CacheControl="no-store, max-age=0",
    )
    if verbose:
        print(f"publish-fleet: {key} ({len(raw)/1024:.1f} KB, sha={digest[:12]})", flush=True)
    return meta
