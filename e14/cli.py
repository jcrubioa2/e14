"""Command-line interface for the E-14 acta scraper.

Subcommands:
  build-universe   Fetch the national acta list -> data/mesa_universe.csv
  refresh-universe Refresh the count-model snapshot (total_global + mesas_informadas)
  download         Download acta PDFs (resumable; --depto/--muni/--limit filters)
  estimate         Print national volume + runtime projection (no downloads)
  stats            Show manifest status summary
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import config
from .manifest import Manifest
from .session import CdnSession
from .universe import (
    SNAPSHOT_PATH, UniverseShrinkError, fetch_names, fetch_universe,
    fetch_universe_counts, filter_records, load_universe_csv,
    write_dictionary_csv, write_index_csv, write_universe_csv,
    write_universe_snapshot,
)

DICTIONARY_CSV = Path("data") / "divipol_dictionary.csv"
INDEX_CSV = Path("data") / "index.csv"

# Total de mesas instaladas a nivel nacional (reportado por la Registraduría en
# el divulgador oficial). El conjunto descargable crece a medida que avanza el
# escrutinio; la diferencia con lo descargado son mesas aún sin escrutar.
NATIONAL_MESAS = 122020

DATA = Path("data")
UNIVERSE_CSV = DATA / "mesa_universe.csv"
MANIFEST_DB = DATA / "manifest.db"
ACTAS_DIR = DATA / "actas"
FAILED_CSV = DATA / "failed.csv"

log = logging.getLogger("e14")


def _setup_logging(verbose: bool) -> None:
    Path("logs").mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler("logs/e14.log", encoding="utf-8"),
        ],
    )


def _load_records(refresh: bool, session: CdnSession | None = None):
    """Load the universe from CSV, fetching it first if missing/refresh."""
    if refresh or not UNIVERSE_CSV.exists():
        recs = fetch_universe(session)
        write_universe_csv(recs, UNIVERSE_CSV)
        return recs
    return load_universe_csv(UNIVERSE_CSV)


def cmd_build_universe(args) -> int:
    recs = _load_records(refresh=True)
    deps = sorted({r.dep.zfill(2) for r in recs})
    print(f"Universe: {len(recs)} actas across {len(deps)} departments "
          f"-> {UNIVERSE_CSV}")
    return 0


def cmd_refresh_universe(args) -> int:
    """Refresh the count-model snapshot from the divulgador's allTransmissionCodes.json.

    That JSON enumerates the **mesas_informadas** (mesas with a downloadable acta). The election's
    **total_global** (installed mesas) lives on the results portal (resultados.registraduria.gov.co),
    which is bot-protected, so it's supplied via --total-global / $E14_TOTAL_GLOBAL (read off the
    portal) and carried forward across refreshes until changed. The shrink-guard refuses a
    truncated fetch unless --allow-shrink.
    """
    from .universe import fetch_results_summary, load_universe_snapshot

    recs, nodes_total = fetch_universe_counts()
    write_universe_csv(recs, UNIVERSE_CSV)

    # Resolve total_global (+ mesas_escrutadas): explicit flag > env > results-portal API >
    # carried forward from the last snapshot > unknown. The portal JSON is the preferred automatic
    # source (metota = installed mesas, mesesc = counted); the flag/env stay as manual overrides.
    total_global = source = mesas_escrutadas = None
    if getattr(args, "total_global", None):
        total_global, source = int(args.total_global), "results portal (--total-global)"
    elif os.environ.get("E14_TOTAL_GLOBAL", "").isdigit():
        total_global, source = int(os.environ["E14_TOTAL_GLOBAL"]), "results portal ($E14_TOTAL_GLOBAL)"
    summary = fetch_results_summary()
    if summary:
        mesas_escrutadas = summary.get("mesas_escrutadas")
        if total_global is None:
            total_global, source = summary["total_mesas"], "results portal (ACT/PR/00.json)"
    if total_global is None:
        prev = load_universe_snapshot(SNAPSHOT_PATH)
        if prev and prev.get("total_global"):
            total_global, source = int(prev["total_global"]), (prev.get("total_global_source") or "heredado")
            mesas_escrutadas = mesas_escrutadas or prev.get("mesas_escrutadas")

    try:
        snap = write_universe_snapshot(
            recs, total_global=total_global, total_global_source=source,
            mesas_escrutadas=mesas_escrutadas, allow_shrink=args.allow_shrink)
    except UniverseShrinkError as exc:
        print(f"✗ {exc}")
        return 1
    informadas = snap["mesas_informadas"]
    print(f"Universe snapshot -> {SNAPSHOT_PATH}")
    if snap["total_global"] is not None:
        print(f"  total_global       = {snap['total_global']:,}  ({source})")
    else:
        print(f"  total_global       = — (sin dato del portal; pasa --total-global N)")
    if snap["mesas_escrutadas"] is not None:
        print(f"  mesas_escrutadas   = {snap['mesas_escrutadas']:,}  (results portal · mesesc)")
    print(f"  mesas_informadas   = {informadas:,}  (allTransmissionCodes · acta images)")
    if snap["total_global"] is not None:
        print(f"  backlog de reporte = {max(0, snap['total_global'] - informadas):,}  "
              f"(total_global − informadas)")
    print(f"  fetched_at         = {snap['fetched_at']}")
    return 0


def _sync_out(args) -> Path:
    return Path(getattr(args, "output_dir", None) or "data/detector")


def cmd_sync_status(args) -> int:
    from e14detector.sync import do_status
    return do_status(_sync_out(args), cdn_base=args.cdn_base)


def cmd_sync_verify(args) -> int:
    from e14detector.sync import do_verify
    return do_verify(_sync_out(args), bucket=args.bucket, cdn_base=args.cdn_base,
                     check_crops=args.check_crops, check_content=args.check_content)


def cmd_sync_restore(args) -> int:
    from e14detector.sync import do_restore
    return do_restore(_sync_out(args), bucket=args.bucket, cdn_base=args.cdn_base, prefix=args.prefix)


def cmd_sync_run(args) -> int:
    from e14detector.sync import do_run
    return do_run(
        _sync_out(args), bucket=args.bucket, cdn_base=args.cdn_base,
        refresh_universe=not args.no_universe, workers=args.workers,
        upload_limit=args.upload_limit, interval=args.interval, db_interval=args.db_interval,
        once=args.once, department=args.department, allow_locked=args.allow_locked,
        allow_shrink=args.allow_shrink,
    )


def cmd_sync_backup(args) -> int:
    from e14detector.sync import do_backup
    return do_backup(_sync_out(args), dest=Path(args.dest), bucket=args.bucket, cdn_base=args.cdn_base)


def cmd_sync_stamp_pointer(args) -> int:
    from e14detector.sync import do_stamp_pointer
    return do_stamp_pointer(_sync_out(args), bucket=args.bucket, cdn_base=args.cdn_base)


def cmd_sync_fleet(args) -> int:
    # Multi-machine orchestration still lives in the detector CLI (fleet-init/status/schedule/
    # complete/pull-fleet/publish-fleet). Point operators there rather than duplicate it.
    print("e14 sync fleet: la orquestación multi-máquina vive en el CLI del detector.")
    print("  python -m e14detector.cli fleet-init | fleet-status | fleet-schedule | fleet-complete")
    print("  (pull-fleet / publish-fleet para fusionar/publicar la cola)")
    return 0


def cmd_estimate(args) -> int:
    recs = _load_records(refresh=args.refresh)
    recs = filter_records(recs, args.depto, args.muni)
    variants = ["delegados"] if args.variant != "both" else ["delegados"]
    n = len(recs) * len(variants)
    # ~97 KB/acta observed; rate is requests/sec across the run.
    avg_kb = 97
    est_bytes = n * avg_kb * 1024
    secs = n / max(args.rate, 0.1)
    print(f"Actas (filtered): {len(recs)}  x variants {variants} = {n} downloads")
    print(f"Est. size: ~{est_bytes/1e9:.1f} GB at ~{avg_kb} KB/acta")
    print(f"Est. time at {args.rate} req/s: ~{secs/3600:.1f} h "
          f"(~{secs/60:.0f} min); with {args.concurrency} workers, wall-clock "
          f"is bounded by the rate limit.")
    return 0


def cmd_download(args) -> int:
    from .downloader import run_download
    recs = _load_records(refresh=args.refresh)
    recs = filter_records(recs, args.depto, args.muni, args.limit)
    if not recs:
        print("No actas match the given filters.")
        return 1
    manifest = Manifest(MANIFEST_DB)
    results_path = Path("logs") / "results.jsonl"
    t0 = time.monotonic()

    # Pass 0 = main download; passes 1..N = auto-retry of failures (overnight-safe).
    passes = [("main", args.retry_failed)]
    passes += [("retry", True) for _ in range(max(0, args.auto_retry))]

    last = {}
    for i, (label, only_failed) in enumerate(passes):
        # stop early if a retry pass has nothing left to do
        if only_failed and i > 0 and not manifest.pending_keys("delegados", only_failed=True):
            print(f"[pass {i}] no failures left — stopping retries.")
            break
        print(f"\n[pass {i}: {label}] starting "
              f"(concurrency={args.concurrency}, rate={args.rate}) ...")
        last = run_download(
            recs, manifest, ACTAS_DIR, variant="delegados",
            concurrency=args.concurrency, rate=args.rate,
            force=args.force and i == 0, only_failed=only_failed or (i > 0),
            results_path=results_path,
        )
        print(f"[pass {i}] done={last['done']} failed={last['failed']} "
              f"skipped={last['skipped']}")
        if last["reasons"]:
            print("  failure reasons:")
            for reason, n in sorted(last["reasons"].items(), key=lambda x: -x[1]):
                print(f"    {n:>6}  {reason}")
        if i > 0 and last["failed"] == 0:
            break

    elapsed = time.monotonic() - t0
    n_failed = manifest.export_failed(FAILED_CSV, "delegados")
    counts = manifest.counts("delegados")
    print("\n=== FINAL SUMMARY ===")
    print(f"  manifest totals: {counts}")
    print(f"  total on disk: {manifest.total_bytes('delegados')/1e9:.2f} GB")
    print(f"  elapsed: {elapsed/60:.1f} min")
    print(f"  per-acta log: {results_path}")
    if n_failed:
        print(f"  ⚠ {n_failed} still failed -> {FAILED_CSV} "
              f"(re-run: e14 download --retry-failed)")
    else:
        print("  ✓ no failures outstanding")
    return 0


def cmd_dictionary(args) -> int:
    """Build the human-readable DIVIPOL dictionary + per-acta index."""
    recs = _load_records(refresh=args.refresh)
    names = fetch_names()
    write_dictionary_csv(names, DICTIONARY_CSV)
    write_index_csv(recs, names, INDEX_CSV)
    print(f"Dictionary: {len(names)} puestos -> {DICTIONARY_CSV}")
    print(f"Index: {len(recs)} actas (codes + names + path) -> {INDEX_CSV}")
    return 0


def _write_spanish_readme(dist: Path, n_actas: int, n_deps: int, gb: float) -> None:
    """Genera dist/LEEME.md: resumen en español para quien publica/descarga.

    Usa cifras en vivo (lo realmente empaquetado) para que nunca queden obsoletas.
    """
    from datetime import datetime, timezone
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    faltan = max(0, NATIONAL_MESAS - n_actas)
    pct = (n_actas / NATIONAL_MESAS * 100) if NATIONAL_MESAS else 0.0
    txt = f"""# Actas E-14 — Elección Presidencial de Colombia 2026

Este es un archivo público y gratuito de las **actas E-14 de Delegados**: los
formularios oficiales donde se anota el conteo de votos de cada mesa. Son una
copia fiel de las publicadas por la **Registraduría Nacional**, descargadas tal
cual, sin modificarlas.

- **Fuente oficial:** https://divulgacione14presidente.registraduria.gov.co
- **Fecha de esta copia:** {fecha}

## Qué contiene

- **{n_actas:,} actas en PDF** (sin modificar) — una por cada mesa que ya estaba
  escrutada y publicada cuando se tomó esta copia.
- **{n_deps} departamentos**, alrededor de **{gb:.1f} GB** en total.
- Están agrupadas por departamento en la carpeta `por_departamento/` (un
  archivo `.zip` por departamento, para que pueda descargar solo el que le
  interese).

## Qué falta y por qué (importante)

- La Registraduría reporta **{NATIONAL_MESAS:,} mesas** en todo el país. Aquí
  están **{n_actas:,}**, es decir el **{pct:.1f}%**.
- Las **{faltan:,} mesas restantes todavía no habían sido escrutadas ni
  publicadas** cuando se tomó esta copia: el sitio oficial aún no tenía su acta,
  por eso no aparecen aquí. **No es un error ni un faltante deliberado.**
- El conteo avanza con el tiempo, así que una copia posterior tendrá más actas.
  La fecha indicada arriba marca el corte exacto de este archivo.

## Cómo encontrar el acta de una mesa

Abra **`indice.csv`** (en Excel, Google Sheets o similar). Tiene una fila por
acta con el **departamento, municipio, zona, puesto y lugar de votación en
palabras**, además de la mesa y la ruta del archivo. Busque por nombre, por
ejemplo «MEDELLÍN» o el nombre de su colegio/puesto, y verá en qué `.zip` y con
qué nombre está el PDF.

- Si prefiere ubicarse por códigos, **`diccionario_divipol.csv`** traduce cada
  código numérico a su nombre.
- Las carpetas dentro de los `.zip` usan los **códigos oficiales** del país
  (departamento / municipio / zona / puesto), que son la forma estándar de
  cruzar estas actas con los resultados numéricos publicados.

## Cómo comprobar que un archivo no fue alterado

Cada PDF tiene una «huella digital» única registrada en
**`VERIFICACION_SHA256.txt`**. Esto permite confirmar que un archivo es idéntico
al que se descargó de la Registraduría y que nadie lo modificó. (Es un paso
opcional, pensado para quien quiera auditar la integridad de los archivos.)

## Antes de sacar conclusiones

Esto es **material de transparencia electoral**, no una denuncia. Que una mesa
parezca tener una inconsistencia **no es prueba de fraude**: la causa más común
son errores al leer o digitar la cifra, y además circulan actas falsas. Use
siempre estos PDF comparándolos con la fuente oficial, con calma y criterio.
"""
    (dist / "LEEME.md").write_text(txt, encoding="utf-8")


def cmd_package(args) -> int:
    """Bundle the raw PDFs into per-department zips + checksums for sharing.

    Leaves the raw file-by-file tree untouched; writes shareable artifacts to
    dist/.
    """
    import hashlib
    import zipfile

    if not ACTAS_DIR.exists():
        print("No actas downloaded yet.")
        return 1
    dist = Path(args.out)
    by_dep = dist / "por_departamento"
    by_dep.mkdir(parents=True, exist_ok=True)

    # group files by department dir (data/actas/{dep}/...)
    deps: dict[str, list[Path]] = {}
    for pdf in ACTAS_DIR.rglob("*.pdf"):
        deps.setdefault(pdf.relative_to(ACTAS_DIR).parts[0], []).append(pdf)
    if not deps:
        print("No PDFs found.")
        return 1

    sha_lines: list[str] = []
    total = 0
    for dep in sorted(deps):
        files = sorted(deps[dep])
        zpath = by_dep / f"{dep}.zip"
        print(f"  packaging dep {dep}: {len(files)} actas -> {zpath.name}")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:  # PDFs already compressed
            for f in files:
                data = f.read_bytes()
                arc = str(f.relative_to(ACTAS_DIR))
                zf.writestr(arc, data)
                sha_lines.append(f"{hashlib.sha256(data).hexdigest()}  {arc}")
                total += 1

    (dist / "VERIFICACION_SHA256.txt").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

    # ensure the human-readable dictionary + index exist, then include them
    if not INDEX_CSV.exists() or not DICTIONARY_CSV.exists():
        print("  building dictionary + index (names) ...")
        try:
            recs = _load_records(refresh=False)
            names = fetch_names()
            write_dictionary_csv(names, DICTIONARY_CSV)
            write_index_csv(recs, names, INDEX_CSV)
        except Exception as exc:
            print(f"  (warning: could not build names index: {exc})")

    # Public-facing only: the human-readable index + dictionary, Spanish names.
    # Technical artifacts (ENDPOINTS.md, README.md, mesa_universe.csv, failed.csv)
    # stay in the repo and are intentionally NOT shipped to the public bundle.
    public = {INDEX_CSV: "indice.csv", DICTIONARY_CSV: "diccionario_divipol.csv"}
    for src, name in public.items():
        if src.exists():
            (dist / name).write_bytes(src.read_bytes())

    # Spanish summary for publication (live numbers, never goes stale).
    gb = (Manifest(MANIFEST_DB).total_bytes("delegados") / 1e9) if MANIFEST_DB.exists() else 0.0
    _write_spanish_readme(dist, total, len(deps), gb)

    print(f"\nEmpaquetadas {total} actas de {len(deps)} departamentos -> {dist}/")
    print("  LEEME.md + por_departamento/*.zip + VERIFICACION_SHA256.txt + "
          "indice.csv + diccionario_divipol.csv")
    print("  Listo para publicar (Internet Archive / torrent). Sube TODO el contenido de dist/.")
    return 0


def cmd_stats(args) -> int:
    if not MANIFEST_DB.exists():
        print("No manifest yet. Run `download` first.")
        return 1
    m = Manifest(MANIFEST_DB)
    counts = m.counts()
    total = sum(counts.values())
    print(f"Manifest: {total} rows -> {counts}")
    print(f"Downloaded bytes: {m.total_bytes()/1e9:.2f} GB")
    if (n := m.export_failed(FAILED_CSV)):
        print(f"Failed exported: {n} rows -> {FAILED_CSV}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="e14", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--depto", help="filter by department code (e.g. 16)")
        sp.add_argument("--muni", help="filter by municipality code (e.g. 001)")
        sp.add_argument("--variant", choices=["delegados", "both"],
                        default="delegados",
                        help="acta variant (only 'delegados' available on this site)")
        sp.add_argument("--concurrency", type=int, default=config.DEFAULT_CONCURRENCY)
        sp.add_argument("--rate", type=float, default=config.DEFAULT_RATE_LIMIT,
                        help="global rate limit (requests/sec)")
        sp.add_argument("--refresh", action="store_true",
                        help="re-fetch the universe JSON before running")

    sp = sub.add_parser("build-universe", help="fetch national acta list to CSV")
    sp.set_defaults(func=cmd_build_universe)

    sp = sub.add_parser("refresh-universe",
                        help="refresh the universe snapshot (mesas_informadas from the divulgador; "
                             "total_global from the results portal via --total-global)")
    sp.add_argument("--total-global", type=int,
                    help="installed-mesa total read off resultados.registraduria.gov.co "
                         "(bot-protected, so supplied here); carried forward until changed")
    sp.add_argument("--allow-shrink", action="store_true",
                    help="accept a snapshot smaller than the last (override the shrink-guard)")
    sp.set_defaults(func=cmd_refresh_universe)

    sp = sub.add_parser("estimate", help="print volume + runtime estimate")
    common(sp)
    sp.set_defaults(func=cmd_estimate)

    sp = sub.add_parser("download", help="download acta PDFs (resumable)")
    common(sp)
    sp.add_argument("--limit", type=int, help="cap number of actas (test runs)")
    sp.add_argument("--force", action="store_true", help="re-download even if done")
    sp.add_argument("--retry-failed", action="store_true",
                    help="only re-attempt entries marked failed")
    sp.add_argument("--auto-retry", type=int, default=2, metavar="N",
                    help="after the main pass, auto-retry failures up to N times "
                         "(default 2; set 0 to disable). Makes overnight runs self-healing.")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("stats", help="show manifest status summary")
    sp.set_defaults(func=cmd_stats)

    # --- Unified sync (consolidation; consistency rules baked in) ---------------------------
    sync = sub.add_parser(
        "sync",
        help="unified incremental sync — one tool with the count-model rules baked in "
             "(lock-aware, frontier-only, shrink-guard, chain-stamp, verify-first)")
    sync_sub = sync.add_subparsers(dest="sync_cmd", required=True)

    def _sync_common(p):
        p.add_argument("--output-dir", default="data/detector")
        p.add_argument("--bucket", help="object-store bucket (default: $BUCKET_NAME)")
        p.add_argument("--cdn-base", default=os.environ.get("E14_CDN_BASE_URL", "") or None,
                       help="published CDN base URL (default: $E14_CDN_BASE_URL)")

    sp = sync_sub.add_parser("status", help="print the count chain + cobertura + backlogs")
    _sync_common(sp)
    sp.set_defaults(func=cmd_sync_status)

    sp = sync_sub.add_parser("verify", help="assert the invariant chain (nonzero exit on inversion)")
    _sync_common(sp)
    sp.add_argument("--check-crops", action="store_true",
                    help="also confirm every served crop exists in the bucket (full sweep)")
    sp.add_argument("--check-content", action="store_true",
                    help="also flag served crops whose source PDF drifted (content integrity)")
    sp.set_defaults(func=cmd_sync_verify)

    sp = sync_sub.add_parser("restore", help="resume on a fresh machine (reconcile manifest + pull DB)")
    _sync_common(sp)
    sp.add_argument("--prefix", default="crops/", help="object key prefix to list")
    sp.set_defaults(func=cmd_sync_restore)

    sp = sync_sub.add_parser("run", help="the one safe publisher loop (universe + crops + frontier DB)")
    _sync_common(sp)
    sp.add_argument("--once", action="store_true", help="run a single cycle and exit (then verify)")
    sp.add_argument("--no-universe", action="store_true", help="skip the universe-snapshot refresh")
    sp.add_argument("--workers", type=int, default=32, help="crop upload concurrency")
    sp.add_argument("--upload-limit", type=int, default=12000, help="max crops uploaded per cycle")
    sp.add_argument("--interval", type=int, default=60, help="seconds between crop-upload ticks")
    sp.add_argument("--db-interval", type=int, default=300, help="seconds between DB publishes")
    sp.add_argument("--department", help="only upload crops for this department (code or name)")
    sp.add_argument("--allow-locked", action="store_true", help="publish even over a locked round")
    sp.add_argument("--allow-shrink", action="store_true", help="override the shrink-guard")
    sp.set_defaults(func=cmd_sync_run)

    sp = sync_sub.add_parser("backup", help="write one off-Tigris DR copy of the published snapshot")
    _sync_common(sp)
    sp.add_argument("--dest", required=True, help="destination directory for the DR copy")
    sp.set_defaults(func=cmd_sync_backup)

    sp = sync_sub.add_parser(
        "stamp-pointer",
        help="add/refresh the reconciliation block on the LIVE pointer without rebuilding the DB "
             "(safe one-off for a frozen/locked round)")
    _sync_common(sp)
    sp.set_defaults(func=cmd_sync_stamp_pointer)

    sp = sync_sub.add_parser("fleet", help="multi-machine orchestration (points to the detector CLI)")
    sp.set_defaults(func=cmd_sync_fleet)

    sp = sub.add_parser("dictionary",
                        help="build human-readable DIVIPOL dictionary + per-acta index")
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_dictionary)

    sp = sub.add_parser("package", help="bundle raw PDFs into per-department zips + checksums")
    sp.add_argument("--out", default="dist", help="output directory (default: dist)")
    sp.set_defaults(func=cmd_package)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
