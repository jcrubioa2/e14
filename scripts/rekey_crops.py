#!/usr/bin/env python3
"""One-time migration: re-key crop objects to their OPAQUE name.

Historically a crop's object key was the readable path
``crops/E14_PRE_<dept>_<mun>_<zone>_<puesto>_<mesa>_..._candidate_field.png`` — which is also
the public CDN URL, so opening a swipe-feed crop in a new tab de-anonymized the acta.
``webapp.crop_key`` now derives an OPAQUE key (``crops/<hmac>.png``); this script copies every
existing object from its old readable key to the new opaque key so the bucket matches the code.

Forward-only: the old readable key still contains ``crop_rel``, so we recompute the new key with
the exact same ``crop_key`` the uploader/feed use. Run with the SAME ``E14_CROP_KEY_SECRET``
(or ``E14_FORM_TOKEN_SECRET``/``E14_VOTER_SALT`` fallback) set on the webapp — otherwise the
copied keys won't match the URLs the app serves.

Two phases, both idempotent:
  1. copy   (default): old readable key -> new opaque key. Both keys coexist; nothing breaks
             mid-cutover. Already-opaque keys are skipped.
  2. delete (--delete-old): remove the old readable keys, but only after confirming the opaque
             copy exists. Run this only AFTER the app is verified serving opaque URLs.

Creds/bucket come from the same env as publishing (AWS_* / BUCKET_NAME) via
``publish._default_client``.

Examples:
  python -m scripts.rekey_crops --dry-run
  python -m scripts.rekey_crops --workers 32
  python -m scripts.rekey_crops --delete-old
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from e14detector import config
from e14detector.publish import _default_client, list_bucket_crop_keys
from e14detector.webapp import crop_key, crop_prefix, crop_rel

# An already-migrated key's file part is exactly <24 hex>.png (see webapp.crop_obj_name); anything
# else is a readable legacy key still to migrate. Guards against double-hashing opaque keys.
_OPAQUE_RE = re.compile(r"^[0-9a-f]{24}\.png$")


def _is_opaque(key: str, prefix: str) -> bool:
    name = key[len(prefix):] if key.startswith(prefix) else key
    return bool(_OPAQUE_RE.match(name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=None, help="override BUCKET_NAME / E14_TIGRIS_BUCKET")
    ap.add_argument("--round", default=None, help="election round (default: config.ELECTION_ROUND)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None, help="cap objects processed (smoke test)")
    ap.add_argument("--delete-old", action="store_true",
                    help="delete the old readable keys (only after the app serves opaque URLs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not config.CROP_KEY_SECRET:
        print("refusing to run: CROP_KEY_SECRET is empty (set E14_CROP_KEY_SECRET / "
              "E14_FORM_TOKEN_SECRET / E14_VOTER_SALT)", file=sys.stderr)
        return 2

    bucket = args.bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        print("refusing to run: no bucket (set BUCKET_NAME or pass --bucket)", file=sys.stderr)
        return 2

    prefix = crop_prefix(args.round)
    client = _default_client(args.workers)
    keys = sorted(list_bucket_crop_keys(bucket=bucket, client=client, round=args.round,
                                        verbose=not args.quiet))
    if args.limit is not None:
        keys = keys[: args.limit]

    todo = [k for k in keys if not _is_opaque(k, prefix)]
    skipped = len(keys) - len(todo)
    if not args.quiet:
        verb = "delete" if args.delete_old else "copy"
        print(f"{len(keys)} key(s) in {prefix!r}; {skipped} already opaque; {len(todo)} to {verb}",
              flush=True)

    counts = {"ok": 0, "missing_copy": 0, "err": 0}
    lock = threading.Lock()

    def _copy(old: str) -> tuple[str, str | None]:
        new = crop_key(old, args.round)  # old key still carries crop_rel -> same key the app uses
        if new == old:
            return ("ok", None)  # belt-and-suspenders; _is_opaque already filtered these
        if args.dry_run:
            return ("ok", None)
        # MetadataDirective=COPY preserves content-type so the served image still renders.
        client.copy_object(Bucket=bucket, Key=new, CopySource={"Bucket": bucket, "Key": old},
                           MetadataDirective="COPY")
        return ("ok", None)

    def _delete(old: str) -> tuple[str, str | None]:
        new = crop_key(old, args.round)
        try:
            client.head_object(Bucket=bucket, Key=new)  # never delete an un-migrated original
        except Exception:  # noqa: BLE001
            return ("missing_copy", old)
        if not args.dry_run:
            client.delete_object(Bucket=bucket, Key=old)
        return ("ok", None)

    work = _delete if args.delete_old else _copy
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, k): k for k in todo}
        for fut in as_completed(futs):
            old = futs[fut]
            try:
                status, _ = fut.result()
            except Exception as exc:  # noqa: BLE001 — one object's failure shouldn't abort the run
                status = "err"
                print(f"\nERR {old}: {exc}", file=sys.stderr, flush=True)
            with lock:
                counts[status] = counts.get(status, 0) + 1
                done += 1
            if not args.quiet and done % 1000 == 0:
                print(f"  {done}/{len(todo)}…", end="\r", flush=True)

    if not args.quiet:
        print(f"\ndone: ok={counts['ok']} missing_copy={counts['missing_copy']} err={counts['err']}"
              f"{' (dry-run)' if args.dry_run else ''}", flush=True)
    return 1 if counts["err"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
