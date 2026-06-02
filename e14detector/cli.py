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


def cmd_publish_db(args: argparse.Namespace) -> int:
    from .dbsync import publish_db

    info = publish_db(
        output_dir=Path(args.output_dir), bucket=args.bucket, only_uploaded=args.only_uploaded
    )
    if info is None:
        print("published db: nothing in the uploaded frontier yet")
        return 0
    print(f"published db: {info['key']} ({info['size']/1e6:.1f} MB, sha={info['sha256'][:12]})")
    return 0


def cmd_publish_loop(args: argparse.Namespace) -> int:
    """Continuous, no-pause publisher: upload new crops, then publish the fully-uploaded
    frontier DB. Runs alongside the crop run; the live page grows on its own."""
    import time

    from .dbsync import publish_db
    from .publish import publish_crops

    output_dir = Path(args.output_dir)
    while True:
        started = time.time()
        crops = publish_crops(output_dir, bucket=args.bucket, workers=args.workers, verbose=False)
        info = publish_db(output_dir, bucket=args.bucket, only_uploaded=True, verbose=False)
        front = "empty" if info is None else f"{info['kept']} actas (sha {info['sha256'][:8]})"
        print(
            f"[publish-loop] +{crops['uploaded']} crops (fail {crops['failed']}) · "
            f"frontier {front} · {time.time()-started:.0f}s",
            flush=True,
        )
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
    process.set_defaults(func=cmd_process)

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
    publish_db.set_defaults(func=cmd_publish_db)

    publish_loop = sub.add_parser(
        "publish-loop",
        help="continuous publisher: upload new crops + publish the uploaded frontier, on a loop",
    )
    publish_loop.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR))
    publish_loop.add_argument("--bucket", help="bucket name (default: $BUCKET_NAME)")
    publish_loop.add_argument("--workers", type=int, default=32, help="crop upload concurrency")
    publish_loop.add_argument("--interval", type=int, default=120, help="seconds between cycles")
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
