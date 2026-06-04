"""Command-line interface for the local E-14 detector."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .env_check import collect_env_report, format_env_report


def cmd_env_check(args: argparse.Namespace) -> int:
    report = collect_env_report(gpu_mode=args.gpu_mode, output_dir=Path(args.output_dir))
    print(format_env_report(report))
    return 0 if report.output_dir_write_test else 1


def cmd_process(args: argparse.Namespace) -> int:
    from .processor import run_process

    totals = run_process(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        limit=args.limit,
        dpi=args.dpi,
        debug=args.debug,
        force=args.force,
        workers=args.workers,
        crop_only=args.crop_only,
        depto=args.depto,
        dept_from=args.dept_from,
        dept_to=args.dept_to,
    )
    print(
        f"done={totals['done']} skipped={totals['skipped']} "
        f"failed={totals['failed']} fields={totals['fields']}"
    )
    return 1 if totals["failed"] else 0


def cmd_process_one(args: argparse.Namespace) -> int:
    from .processor import run_process_one
    from .utils import parse_document_metadata

    pdf_path = Path(args.pdf)
    output_dir = Path(args.output_dir)
    result = run_process_one(
        pdf_path=pdf_path,
        output_dir=output_dir,
        dpi=args.dpi,
        debug=args.debug,
        force=True,
    )
    print(
        f"done={result['done']} skipped={result['skipped']} "
        f"failed={result['failed']} fields={result['fields']}"
    )
    if args.audit:
        from .crop_audit import export_crop_audit

        document_id = parse_document_metadata(pdf_path).document_id
        out_html = Path(args.audit_output)
        n = export_crop_audit(
            results_db=output_dir / "results" / "results.sqlite",
            output_html=out_html,
            document_id=document_id,
        )
        print(f"audit rows: {n} -> {out_html}")
    return 1 if result["failed"] else 0


def cmd_vlm_review(args: argparse.Namespace) -> int:
    from .vlm_review import run_vlm_review

    totals = run_vlm_review(
        output_dir=Path(args.output_dir),
        provider=args.provider,
        limit=args.limit,
        concurrency=args.concurrency,
        candidates_only=not args.include_summary,
        document_id=args.document_id,
        require_flag=not args.all_candidates,
        sample_rate=args.sample_rate,
    )
    print(
        f"reviewed={totals['reviewed']} cached={totals['cached']} failed={totals['failed']}"
    )
    return 1 if totals["failed"] else 0


def cmd_vlm_confirm(args: argparse.Namespace) -> int:
    from .vlm_review import run_seed_confirm

    totals = run_seed_confirm(
        output_dir=Path(args.output_dir),
        confirm_model=args.model,
        concurrency=args.concurrency,
    )
    print(
        f"confirmed={totals['confirmed']} demoted={totals['demoted']} failed={totals['failed']}"
    )
    return 1 if totals["failed"] else 0


def cmd_publish_crops(args: argparse.Namespace) -> int:
    from .publish import publish_crops

    totals = publish_crops(
        output_dir=Path(args.output_dir),
        bucket=args.bucket,
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
    )
    print(
        f"uploaded={totals['uploaded']} skipped={totals['skipped']} failed={totals['failed']}"
    )
    return 1 if totals["failed"] else 0


def cmd_pull_db(args: argparse.Namespace) -> int:
    from .dbsync import pull_db

    info = pull_db(Path(args.output_dir), bucket=args.bucket, verbose=True)
    if info is None:
        print("pull-db: nothing to merge (no live pointer yet)")
        return 0
    print(
        f"pull-db: merged remote@{info.get('sha256', '?')} "
        f"(+{info['docs_added']} actas, +{info['fields_added']} fields, "
        f"{info['docs_total']:,} total)"
    )
    return 0


def cmd_publish_db(args: argparse.Namespace) -> int:
    from .dbsync import publish_db

    info = publish_db(
        output_dir=Path(args.output_dir), bucket=args.bucket, only_uploaded=args.only_uploaded,
        allow_shrink=getattr(args, "allow_shrink", False),
    )
    if info is None:
        print("published db: nothing in the uploaded frontier yet")
        return 0
    if info.get("guarded"):
        print(f"published db: GUARDED — refused to shrink the live DB (use --allow-shrink to override)")
        return 1
    print(f"published db: {info['key']} ({info['size']/1e6:.1f} MB, sha={info['sha256'][:12]})")
    return 0


def _fleet_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    output_dir = Path(args.output_dir)
    universe = Path(args.universe) if getattr(args, "universe", None) else Path("data/mesa_universe.csv")
    results_db = output_dir / "results" / "results.sqlite"
    queue_path = output_dir / "fleet" / "queue.json"
    return output_dir, universe, results_db


def cmd_fleet_init(args: argparse.Namespace) -> int:
    import os

    from .fleet import default_queue_path, new_queue, save_queue

    output_dir, universe, results_db = _fleet_paths(args)
    if not universe.is_file():
        print(f"fleet-init: missing {universe} (run: make universe)", file=sys.stderr)
        return 1
    workers = [w.strip() for w in (args.workers or os.environ.get("E14_FLEET_WORKERS", "")).split(",") if w.strip()]
    queue = new_queue(universe, results_db=results_db if results_db.is_file() else None, workers=workers)
    path = default_queue_path(output_dir)
    save_queue(path, queue)
    print(f"fleet-init: {len(queue['departments'])} departments -> {path}")
    return 0


def cmd_fleet_status(args: argparse.Namespace) -> int:
    from .fleet import default_queue_path, format_status, load_queue, refresh_progress

    output_dir, _, results_db = _fleet_paths(args)
    path = default_queue_path(output_dir)
    if not path.is_file():
        print("fleet-status: no queue (run fleet-init)", file=sys.stderr)
        return 1
    queue = load_queue(path)
    refresh_progress(queue, results_db=results_db if results_db.is_file() else None)
    print(format_status(queue))
    return 0


def cmd_fleet_schedule(args: argparse.Namespace) -> int:
    import os

    from .dbsync import pull_db
    from .fleet import (
        default_queue_path,
        format_status,
        load_queue,
        new_queue,
        refresh_progress,
        save_queue,
        schedule_assignments,
    )
    from .fleetsync import pull_fleet, publish_fleet

    output_dir, universe, results_db = _fleet_paths(args)
    if args.pull_db:
        pull_db(output_dir, bucket=args.bucket, verbose=not args.quiet)
    if args.pull_fleet:
        pull_fleet(output_dir, bucket=args.bucket, verbose=not args.quiet)
    path = default_queue_path(output_dir)
    if not path.is_file():
        if not universe.is_file():
            print("fleet-schedule: run fleet-init first", file=sys.stderr)
            return 1
        workers_pre = [w.strip() for w in (args.workers or os.environ.get("E14_FLEET_WORKERS", "")).split(",") if w.strip()]
        queue = new_queue(
            universe,
            results_db=results_db if results_db.is_file() else None,
            workers=workers_pre,
        )
        save_queue(path, queue)
    else:
        queue = load_queue(path)
    refresh_progress(queue, results_db=results_db if results_db.is_file() else None)
    workers = [w.strip() for w in (args.workers or os.environ.get("E14_FLEET_WORKERS", "")).split(",") if w.strip()]
    if not workers:
        print("fleet-schedule: set --workers or E14_FLEET_WORKERS", file=sys.stderr)
        return 1
    coord = args.coordinator or os.environ.get("E14_FLEET_COORDINATOR")
    assigned = schedule_assignments(queue, workers, coordinator_id=coord)
    save_queue(path, queue)
    if args.publish:
        try:
            publish_fleet(output_dir, bucket=args.bucket, verbose=not args.quiet)
        except ValueError as exc:
            if not args.quiet:
                print(f"fleet-schedule: publish-fleet skipped ({exc})", flush=True)
    if not args.quiet:
        for wid, dep in assigned:
            print(f"fleet-schedule: {wid} -> dept {dep}")
        if not assigned:
            print("fleet-schedule: no new assignments (all workers busy or queue empty)")
        print(format_status(queue))
    return 0


def cmd_fleet_current(args: argparse.Namespace) -> int:
    import os

    from .fleet import current_assignment, default_queue_path, load_queue, refresh_progress

    output_dir, _, results_db = _fleet_paths(args)
    worker = args.worker or os.environ.get("E14_WORKER_ID") or os.environ.get("HOSTNAME", "")
    if not worker:
        print("fleet-current: pass --worker or E14_WORKER_ID", file=sys.stderr)
        return 1
    path = default_queue_path(output_dir)
    if not path.is_file():
        return 0
    queue = load_queue(path)
    refresh_progress(queue, results_db=results_db if results_db.is_file() else None)
    depto = current_assignment(queue, worker)
    if depto:
        print(depto)
    return 0


def cmd_fleet_complete(args: argparse.Namespace) -> int:
    import os

    from .fleet import default_queue_path, finish_department, load_queue, save_queue
    from .fleetsync import publish_fleet

    output_dir, _, results_db = _fleet_paths(args)
    worker = args.worker or os.environ.get("E14_WORKER_ID", "")
    path = default_queue_path(output_dir)
    if not path.is_file():
        print("fleet-complete: no queue", file=sys.stderr)
        return 1
    queue = load_queue(path)
    depto = args.depto
    if not depto:
        print("fleet-complete: --depto required", file=sys.stderr)
        return 1
    status = finish_department(
        queue,
        depto,
        results_db=results_db if results_db.is_file() else None,
        worker_id=worker or None,
    )
    save_queue(path, queue)
    if args.publish:
        publish_fleet(output_dir, bucket=args.bucket, verbose=True)
    dep = str(depto).zfill(2)
    print(f"fleet-complete: dept {dep} -> {status}")
    return 0


def cmd_pull_fleet(args: argparse.Namespace) -> int:
    from .fleetsync import pull_fleet

    info = pull_fleet(Path(args.output_dir), bucket=args.bucket, verbose=True)
    if info is None:
        print("pull-fleet: nothing to merge")
        return 0
    print(f"pull-fleet: ok ({info.get('sha256', '?')})")
    return 0


def cmd_publish_fleet(args: argparse.Namespace) -> int:
    from .fleetsync import publish_fleet

    info = publish_fleet(Path(args.output_dir), bucket=args.bucket, verbose=True)
    if info is None:
        return 1
    print(f"publish-fleet: ok (sha={info.get('sha256', '?')[:12]})")
    return 0


def cmd_publish_reconcile(args: argparse.Namespace) -> int:
    """Rebuild the upload manifest from the bucket so a fresh machine resumes incrementally."""
    from .publish import reconcile_manifest

    info = reconcile_manifest(
        output_dir=Path(args.output_dir), bucket=args.bucket, prefix=args.prefix
    )
    print(
        f"reconcile: bucket had {info['listed']} crop object(s); "
        f"manifest {info['before']} -> {info['after']} key(s)"
    )
    return 0


def cmd_publish_loop(args: argparse.Namespace) -> int:
    """Continuous, no-pause publisher: upload new crops, then publish the fully-uploaded
    frontier DB. Runs alongside the crop run; the live page grows on its own."""
    import time

    from .dbsync import publish_db
    from .publish import publish_crops

    output_dir = Path(args.output_dir)
    last_db = 0.0
    while True:
        started = time.time()
        try:
            crops = publish_crops(output_dir, bucket=args.bucket, workers=args.workers,
                                  limit=args.upload_limit, verbose=False)
            # Decoupled cadence: upload crops every tick (cheap delta), but republish the DB
            # only every --db-interval (the gzipped snapshot is the bigger transfer).
            db_note = "db not due"
            if args.once or (time.time() - last_db) >= args.db_interval:
                info = publish_db(output_dir, bucket=args.bucket, only_uploaded=True, verbose=False)
                if info is not None:
                    last_db = time.time()
                    db_note = f"frontier {info['kept']} actas (sha {info['sha256'][:8]})"
                else:
                    db_note = "frontier empty"
            print(
                f"[publish-loop] +{crops['uploaded']} crops (fail {crops['failed']}) · "
                f"{db_note} · {time.time()-started:.0f}s",
                flush=True,
            )
        except Exception as exc:  # never let one bad cycle kill the loop
            print(f"[publish-loop] cycle error ({type(exc).__name__}): {exc} · "
                  f"{time.time()-started:.0f}s — continuing", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from . import config
    from .webapp import create_app

    app = create_app(
        results_db=Path(args.results),
        output_dir=Path(args.output_dir),
        community_db=Path(config.COMMUNITY_DB),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_inspect_layout(args: argparse.Namespace) -> int:
    from .cropper import inspect_pdf_layout
    from .pdf_render import PdfRenderError

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        written = inspect_pdf_layout(Path(args.pdf), output_dir=output_dir, dpi=args.dpi)
    except PdfRenderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


def cmd_review_export(args: argparse.Namespace) -> int:
    from .review_export import export_review_cases

    n = export_review_cases(Path(args.results), Path(args.output))
    print(f"review cases: {n} -> {args.output}")
    return 0


def cmd_crop_audit(args: argparse.Namespace) -> int:
    from .crop_audit import export_crop_audit

    n = export_crop_audit(
        results_db=Path(args.results),
        output_html=Path(args.output),
        limit=args.limit,
        document_id=args.document_id,
    )
    print(f"crop audit rows: {n} -> {args.output}")
    return 0


def cmd_label_export(args: argparse.Namespace) -> int:
    from .labeling import export_label_queue

    queue_path, n = export_label_queue(
        output_dir=Path(args.output_dir),
        limit=args.limit,
        only_flagged=args.only_flagged,
        include_labeled=args.include_labeled,
        department=args.department,
        document_id=args.document_id,
        shuffle=args.shuffle,
        seed=args.seed,
    )
    guide = queue_path.parent / "LABELING.md"
    print(f"label queue: {n} crop(s) -> {queue_path}")
    print(f"Hand this to a local Claude Code session: read {guide}, then run `label-import`.")
    return 0


def cmd_label_import(args: argparse.Namespace) -> int:
    from .labeling import import_labels

    t = import_labels(output_dir=Path(args.output_dir), labels_path=args.labels)
    print(
        f"labels applied: {t['dirty']} DIRTY (seeded), {t['clean']} CLEAN "
        f"(confirmed) · skipped {t['skipped']} · unmatched {t['unmatched']}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e14detector",
        description="Local CPU-first visual anomaly detector for E-14 vote-count fields.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    env = sub.add_parser("env-check", help="print runtime and optional acceleration diagnostics")
    env.add_argument("--gpu-mode", choices=config.GPU_MODES, default=config.DEFAULT_GPU_MODE)
    env.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    env.set_defaults(func=cmd_env_check)

    process = sub.add_parser("process", help="process a folder of local E-14 PDFs")
    process.add_argument("--input-dir", default=str(config.DEFAULT_INPUT_DIR))
    process.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    process.add_argument("--limit", type=int)
    process.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS)
    process.add_argument("--dpi", type=int, default=config.DEFAULT_DPI)
    process.add_argument("--vlm-mode", choices=config.VLM_MODES, default=config.DEFAULT_VLM_MODE)
    process.add_argument("--gpu-mode", choices=config.GPU_MODES, default=config.DEFAULT_GPU_MODE)
    process.add_argument("--debug", action="store_true")
    process.add_argument("--force", action="store_true")
    process.add_argument(
        "--crop-only",
        action="store_true",
        help="skip CV analysis; only render+crop (fast national first pass, Gemma is the analyzer)",
    )
    process.add_argument("--depto", help="only process this department code (e.g. 16)")
    process.add_argument("--dept-from", help="inclusive start of department code range (e.g. 17)")
    process.add_argument("--dept-to", help="inclusive end of department code range (e.g. 33)")
    process.set_defaults(func=cmd_process)

    pull_db_cmd = sub.add_parser(
        "pull-db",
        help="download the live published results DB from Tigris/CDN and merge into local (multi-PC sync)",
    )
    pull_db_cmd.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    pull_db_cmd.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    pull_db_cmd.set_defaults(func=cmd_pull_db)

    fleet_init = sub.add_parser("fleet-init", help="build department queue from mesa_universe.csv + local DB progress")
    fleet_init.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    fleet_init.add_argument("--universe", default="data/mesa_universe.csv")
    fleet_init.add_argument("--workers", help="comma-separated worker ids (or E14_FLEET_WORKERS)")
    fleet_init.set_defaults(func=cmd_fleet_init)

    fleet_status = sub.add_parser("fleet-status", help="show fleet queue and worker assignments")
    fleet_status.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    fleet_status.add_argument("--universe", default="data/mesa_universe.csv")
    fleet_status.set_defaults(func=cmd_fleet_status)

    fleet_sched = sub.add_parser(
        "fleet-schedule",
        help="assign next department per idle worker (run on coordinator; publishes queue)",
    )
    fleet_sched.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    fleet_sched.add_argument("--universe", default="data/mesa_universe.csv")
    fleet_sched.add_argument("--workers", help="comma-separated worker ids (or E14_FLEET_WORKERS)")
    fleet_sched.add_argument("--coordinator", help="coordinator worker id (E14_FLEET_COORDINATOR)")
    fleet_sched.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    fleet_sched.add_argument("--pull-db", action="store_true", default=True)
    fleet_sched.add_argument("--no-pull-db", action="store_false", dest="pull_db")
    fleet_sched.add_argument("--pull-fleet", action="store_true", default=True)
    fleet_sched.add_argument("--no-pull-fleet", action="store_false", dest="pull_fleet")
    fleet_sched.add_argument("--publish", action="store_true", default=True)
    fleet_sched.add_argument("--no-publish", action="store_false", dest="publish")
    fleet_sched.add_argument("-q", "--quiet", action="store_true")
    fleet_sched.set_defaults(func=cmd_fleet_schedule)

    fleet_cur = sub.add_parser("fleet-current", help="print assigned department code for this worker (stdout)")
    fleet_cur.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    fleet_cur.add_argument("--universe", default="data/mesa_universe.csv")
    fleet_cur.add_argument("--worker", help="worker id (default: E14_WORKER_ID)")
    fleet_cur.set_defaults(func=cmd_fleet_current)

    fleet_done = sub.add_parser("fleet-complete", help="mark a department done in the fleet queue")
    fleet_done.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    fleet_done.add_argument("--universe", default="data/mesa_universe.csv")
    fleet_done.add_argument("--depto", required=True)
    fleet_done.add_argument("--worker", help="worker id (default: E14_WORKER_ID)")
    fleet_done.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    fleet_done.add_argument("--publish", action="store_true", default=True)
    fleet_done.add_argument("--no-publish", action="store_false", dest="publish")
    fleet_done.set_defaults(func=cmd_fleet_complete)

    pull_fleet_cmd = sub.add_parser("pull-fleet", help="merge live fleet queue from Tigris into local")
    pull_fleet_cmd.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    pull_fleet_cmd.add_argument("--bucket")
    pull_fleet_cmd.set_defaults(func=cmd_pull_fleet)

    pub_fleet = sub.add_parser("publish-fleet", help="publish local fleet queue to Tigris")
    pub_fleet.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    pub_fleet.add_argument("--bucket")
    pub_fleet.set_defaults(func=cmd_publish_fleet)

    process_one = sub.add_parser("process-one", help="process a single PDF (fast iteration) and optionally refresh its audit page")
    process_one.add_argument("--pdf", required=True)
    process_one.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    process_one.add_argument("--dpi", type=int, default=config.DEFAULT_DPI)
    process_one.add_argument("--debug", action="store_true")
    process_one.add_argument("--audit", action="store_true", help="regenerate a one-document crop audit HTML after processing")
    process_one.add_argument("--audit-output", default=str(config.DEFAULT_OUTPUT_DIR / "review" / "one_doc_audit.html"))
    process_one.set_defaults(func=cmd_process_one)

    vlm = sub.add_parser("vlm-review", help="run the VLM pass (CV-flagged fields, or a Gemma sample with --sample-rate)")
    vlm.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    vlm.add_argument("--provider", choices=("mock", "qwen", "openrouter"), default=config.VLM_PROVIDER, help="VLM provider (openrouter=Gemma; needs E14_OPENROUTER_API_KEY)")
    vlm.add_argument("--limit", type=int, help="maximum fields to review this run")
    vlm.add_argument("--concurrency", type=int, default=config.VLM_CONCURRENCY)
    vlm.add_argument("--include-summary", action="store_true", help="also review summary rows; default reviews candidate rows only")
    vlm.add_argument("--document-id", help="restrict review to one document_id")
    vlm.add_argument("--all-candidates", action="store_true", help="review every candidate, not only CV-flagged (use when CV is disabled)")
    vlm.add_argument("--sample-rate", type=float, help="review every candidate in a deterministic fraction of documents (e.g. 0.05); implies --all-candidates")
    vlm.set_defaults(func=cmd_vlm_review)

    confirm = sub.add_parser(
        "vlm-confirm",
        help="second tier: re-check only the screen-flagged crops with a precise model "
        "(CLEAN demotes the seed, DIRTY keeps it)",
    )
    confirm.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    confirm.add_argument("--model", help=f"confirm model (default: {config.CONFIRM_MODEL})")
    confirm.add_argument("--concurrency", type=int, default=config.VLM_CONCURRENCY)
    confirm.set_defaults(func=cmd_vlm_confirm)

    publish = sub.add_parser(
        "publish-crops",
        help="upload new public candidate crops to the object store (Tigris/S3); incremental",
    )
    publish.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    publish.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    publish.add_argument("--limit", type=int, help="cap crops uploaded this run")
    publish.add_argument("--workers", type=int, default=16)
    publish.add_argument("--dry-run", action="store_true", help="count new crops without uploading")
    publish.set_defaults(func=cmd_publish_crops)

    publish_db = sub.add_parser(
        "publish-db",
        help="snapshot the results DB and publish it + pointer to the object store "
        "(the Fly app atomically swaps it in)",
    )
    publish_db.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    publish_db.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    publish_db.add_argument(
        "--only-uploaded", action="store_true",
        help="publish only actas whose crops are all uploaded (the safe frontier)",
    )
    publish_db.add_argument(
        "--allow-shrink", action="store_true",
        help="override the guard that refuses to replace the live DB with one holding <50% its "
             "actas (or, for legacy pointers, <50% its bytes)",
    )
    publish_db.set_defaults(func=cmd_publish_db)

    reconcile = sub.add_parser(
        "publish-reconcile",
        help="rebuild the upload manifest from the bucket so any machine resumes incrementally "
        "(run once before publish-loop on a new publisher)",
    )
    reconcile.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    reconcile.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    reconcile.add_argument("--prefix", default="crops/", help="object key prefix to list")
    reconcile.set_defaults(func=cmd_publish_reconcile)

    publish_loop = sub.add_parser(
        "publish-loop",
        help="continuous publisher: upload new crops + publish the uploaded frontier, on a loop",
    )
    publish_loop.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    publish_loop.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    publish_loop.add_argument("--workers", type=int, default=32, help="crop upload concurrency")
    publish_loop.add_argument("--upload-limit", type=int, default=12000,
                              help="max crops uploaded per cycle (caps cycle time so the frontier publishes often)")
    publish_loop.add_argument("--interval", type=int, default=60, help="seconds between crop-upload ticks")
    publish_loop.add_argument("--db-interval", type=int, default=300, help="seconds between DB publishes")
    publish_loop.add_argument("--once", action="store_true", help="run a single cycle and exit")
    publish_loop.set_defaults(func=cmd_publish_loop)

    serve = sub.add_parser("serve", help="serve a local anomaly review web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    serve.add_argument("--results", default=str(config.DEFAULT_RESULTS_DB))
    serve.set_defaults(func=cmd_serve)

    inspect = sub.add_parser("inspect-layout", help="render one PDF with layout debug overlays")
    inspect.add_argument("--pdf", required=True)
    inspect.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR / "debug"))
    inspect.add_argument("--dpi", type=int, default=config.DEFAULT_DPI)
    inspect.add_argument("--gpu-mode", choices=config.GPU_MODES, default=config.DEFAULT_GPU_MODE)
    inspect.set_defaults(func=cmd_inspect_layout)

    review = sub.add_parser("review-export", help="export suspicious/unclear cases for review")
    review.add_argument("--results", default=str(config.DEFAULT_RESULTS_DB))
    review.add_argument("--output", default=str(config.DEFAULT_OUTPUT_DIR / "review" / "review_cases.csv"))
    review.set_defaults(func=cmd_review_export)

    audit = sub.add_parser("crop-audit", help="write an HTML source/crop/slot contact sheet")
    audit.add_argument("--results", default=str(config.DEFAULT_RESULTS_DB))
    audit.add_argument("--output", default=str(config.DEFAULT_OUTPUT_DIR / "review" / "crop_audit.html"))
    audit.add_argument("--limit", type=int, help="maximum rows to include")
    audit.add_argument("--document-id", help="restrict to one document_id")
    audit.set_defaults(func=cmd_crop_audit)

    label_export = sub.add_parser(
        "label-export",
        help="export a batch of candidate crops for a local Claude session to label as seeds",
    )
    label_export.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    label_export.add_argument("--limit", type=int, help="cap the number of crops to label")
    label_export.add_argument("--department", help="restrict to one department (code or name)")
    label_export.add_argument("--document-id", help="export just this one acta (re-evaluates it, incl. labeled)")
    label_export.add_argument(
        "--only-flagged", action="store_true",
        help="export only crops the screen already flagged (audit Gemma's positives)",
    )
    label_export.add_argument(
        "--include-labeled", action="store_true",
        help="include crops that already have a verdict (default: only unlabeled)",
    )
    label_export.add_argument(
        "--shuffle", action="store_true", help="random sample (use --seed for reproducibility)",
    )
    label_export.add_argument("--seed", type=int, default=0)
    label_export.set_defaults(func=cmd_label_export)

    label_import = sub.add_parser(
        "label-import", help="apply local CLEAN/DIRTY labels to the results DB as seeds",
    )
    label_import.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    label_import.add_argument(
        "--labels", help="labels JSONL (default: <output-dir>/review/label_done.jsonl)",
    )
    label_import.set_defaults(func=cmd_label_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
