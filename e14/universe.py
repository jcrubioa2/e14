"""Build the mesa/acta universe from the site's static national JSON.

`allTransmissionCodes.json` contains every acta the site exposes, each with its
geographic codes and the canonical PDF filename (`expectedName`). This is the
authoritative, enumerable download list — one node == one acta PDF.
"""
from __future__ import annotations

import csv
import json
import logging
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .session import CdnSession

log = logging.getLogger("e14.universe")

# The official results portal's machine-readable national summary (presidential, ámbito 00).
# Unlike the SPA HTML (bot-blocked, 403), this JSON endpoint serves 200 to a browser-like GET and
# carries the authoritative installed/counted mesa totals: ``totales.act.metota`` (mesas
# instaladas = total_global) and ``mesesc`` (mesas escrutadas / counted). This is the source the
# divulgador's acta-image list (allTransmissionCodes) does NOT give us — see fetch_universe_counts.
RESULTS_SUMMARY_URL = "https://resultados.registraduria.gov.co/json/ACT/PR/00.json"

# The count model's external source of truth lives here (see the count-model plan /
# memory): one JSON snapshot recording total_global (every mesa in the election) and
# mesas_informadas (mesas with a transmitted acta) the moment we last scraped the
# registraduría. The publisher reads it to stamp the reconciliation chain into the
# pointer; nothing else hardcodes a national total.
SNAPSHOT_PATH = Path("data") / "universe_snapshot.json"


class UniverseShrinkError(RuntimeError):
    """A freshly-fetched universe is drastically smaller than the last accepted one.

    The election universe only grows (mesas get installed/informed); a sharp drop means a
    truncated/partial fetch, not real news. Refusing it keeps a bad scrape from inverting
    the coverage chain (a shrunken denominator would manufacture a fake >100% cobertura).
    """


@dataclass(frozen=True)
class ActaRecord:
    dep: str          # idDepartmentCode (2)
    muni: str         # municipalityCode (3)
    zona: str         # idZoneCode (3 in path)
    puesto: str       # standCode (2)
    mesa: str         # numberStand (3)
    corp: str         # idCorporationCode (001)
    expected_name: str  # PDF filename (content-addressed hash.pdf)
    id_stand: str
    id_transmission_code: str
    status: int       # idTransmissionCodeStatus (11 = normal, 3 = other)

    @property
    def key(self) -> str:
        return f"{self.dep.zfill(2)}_{self.muni.zfill(3)}_{self.zona.zfill(3)}_{self.puesto.zfill(2)}_{self.mesa.zfill(3)}"

    def pdf_url(self) -> str:
        return config.pdf_url(self.dep, self.muni, self.zona, self.puesto,
                              self.mesa, self.expected_name)

    def rel_dir(self) -> str:
        return f"{self.dep.zfill(2)}/{self.muni.zfill(3)}/{self.zona.zfill(3)}/{self.puesto.zfill(2)}"

    def filename(self, variant: str = "delegados") -> str:
        return (f"E14_{config.CORP_ACRONYM}_{self.dep.zfill(2)}_{self.muni.zfill(3)}_"
                f"{self.zona.zfill(3)}_{self.puesto.zfill(2)}_{self.mesa.zfill(3)}_{variant}.pdf")


def _node_to_record(n: dict) -> ActaRecord:
    return ActaRecord(
        dep=str(n.get("idDepartmentCode", "")),
        muni=str(n.get("municipalityCode", "")),
        zona=str(n.get("idZoneCode", "")),
        puesto=str(n.get("standCode", "")),
        mesa=str(n.get("numberStand", "")),
        corp=str(n.get("idCorporationCode", "")),
        expected_name=str(n.get("expectedName", "")),
        id_stand=str(n.get("idStand", "")),
        id_transmission_code=str(n.get("idTransmissionCode", "")),
        status=int(n.get("idTransmissionCodeStatus", 0) or 0),
    )


def fetch_universe(session: CdnSession | None = None) -> list[ActaRecord]:
    """Fetch and parse allTransmissionCodes.json into ActaRecords."""
    session = session or CdnSession()
    url = f"{config.JSON_BASE}/{config.JSON_FILES['transmission_codes']}"
    log.info("fetching national acta list: %s", url)
    payload = session.get_json(url)
    data = payload.get("data", payload)
    records: list[ActaRecord] = []
    for block in data.values():
        for node in (block or {}).get("nodes", []) or []:
            rec = _node_to_record(node)
            if rec.expected_name:
                records.append(rec)
    log.info("parsed %d actas with filenames", len(records))
    return records


def fetch_universe_counts(session: CdnSession | None = None) -> tuple[list[ActaRecord], int]:
    """Fetch the national list, returning (informed_records, nodes_total).

    IMPORTANT — what allTransmissionCodes.json is, and isn't: every node in it carries an
    ``expectedName`` (a downloadable acta), so it enumerates the **mesas_informadas** — the mesas
    that have transmitted an acta — NOT the election's total_global. The official *total* of
    installed mesas lives on the results portal (``resultados.registraduria.gov.co``), which is
    bot-protected (403) and not machine-enumerable, so total_global is operator-supplied (see
    ``cmd_refresh_universe`` / ``write_universe_snapshot``). ``len(records) == mesas_informadas``;
    ``nodes_total`` is the raw node count (== informadas unless a future JSON adds un-informed
    placeholder nodes) and is kept only for reference.
    """
    session = session or CdnSession()
    url = f"{config.JSON_BASE}/{config.JSON_FILES['transmission_codes']}"
    log.info("fetching national acta list: %s", url)
    payload = session.get_json(url)
    data = payload.get("data", payload)
    records: list[ActaRecord] = []
    nodes_total = 0
    for block in data.values():
        for node in (block or {}).get("nodes", []) or []:
            nodes_total += 1
            rec = _node_to_record(node)
            if rec.expected_name:
                records.append(rec)
    log.info("universe: nodes_total=%d mesas_informadas=%d", nodes_total, len(records))
    return records, nodes_total


def _int_co(v) -> int | None:
    """Parse a possibly thousands-formatted Colombian integer string ('122.020' / '122020')."""
    try:
        return int(str(v).replace(".", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_results_summary(timeout: float = 30.0) -> dict | None:
    """Fetch the official results-portal national summary; None on any failure.

    Returns ``{total_mesas, mesas_escrutadas, pct_escrutado}`` where ``total_mesas`` (``metota``)
    is the installed-mesa total (the count model's ``total_global``) and ``mesas_escrutadas``
    (``mesesc``) is the mesas counted in the official results. Both are external truth from the
    results portal — distinct from the divulgador's published acta-image count (mesas_informadas).
    """
    req = urllib.request.Request(RESULTS_SUMMARY_URL, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CO,es;q=0.9",
        "Referer": "https://resultados.registraduria.gov.co/resultados/0/00",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — portal optional; absence just leaves total_global unknown
        log.warning("results-portal summary unreachable: %s", RESULTS_SUMMARY_URL)
        return None
    act = ((payload.get("totales") or {}).get("act")) or {}
    total = _int_co(act.get("metota"))
    if total is None:
        return None
    log.info("results portal: metota=%s mesesc=%s (%s)",
             act.get("metota"), act.get("mesesc"), act.get("pmesesc"))
    return {
        "total_mesas": total,
        "mesas_escrutadas": _int_co(act.get("mesesc")),
        "pct_escrutado": act.get("pmesesc"),
    }


def load_universe_snapshot(path: Path = SNAPSHOT_PATH) -> dict | None:
    """Return the last accepted universe snapshot dict, or None if absent/unreadable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt snapshot reads as "none yet", never crashes
        log.warning("universe snapshot unreadable: %s", path)
        return None


def write_universe_snapshot(
    records: list[ActaRecord],
    path: Path = SNAPSHOT_PATH,
    *,
    total_global: int | None = None,
    total_global_source: str | None = None,
    mesas_escrutadas: int | None = None,
    allow_shrink: bool = False,
    shrink_factor: float = 0.5,
) -> dict:
    """Atomically write the universe snapshot, guarding against a shrunk fetch.

    ``mesas_informadas`` = ``len(records)`` (mesas with a downloadable acta in
    allTransmissionCodes.json). ``total_global`` is the election's installed-mesa total from the
    results portal — bot-protected, so **operator-supplied** (``None`` when unknown, in which case
    the ``total_global`` chain row reads "—" and backlog-de-reporte is left unknown rather than a
    false 0). Records the sorted informed-key list so the publisher can diff served-vs-informed for
    the ingest backlog. Raises ``UniverseShrinkError`` if informadas (or a supplied total_global)
    collapses below ``shrink_factor`` of the last accepted snapshot, unless ``allow_shrink``.
    """
    path = Path(path)
    mesas_informadas = len(records)
    prev = load_universe_snapshot(path)
    if prev and not allow_shrink:
        prev_inf = int(prev.get("mesas_informadas") or 0)
        if prev_inf and mesas_informadas < shrink_factor * prev_inf:
            raise UniverseShrinkError(
                f"refusing snapshot: mesas_informadas {mesas_informadas} < {shrink_factor:.0%} "
                f"of the last accepted {prev_inf} — looks like a truncated fetch. "
                f"Pass allow_shrink=True to override.")
        prev_total = int(prev.get("total_global") or 0)
        if prev_total and total_global is not None and total_global < shrink_factor * prev_total:
            raise UniverseShrinkError(
                f"refusing snapshot: total_global {total_global} < {shrink_factor:.0%} of "
                f"the last accepted {prev_total}. Pass allow_shrink=True to override.")
    snap = {
        "total_global": int(total_global) if total_global is not None else None,
        "total_global_source": total_global_source,
        # mesas the results portal reports as counted (mesesc); sits between total_global and
        # mesas_informadas (acta images). None when the portal wasn't reachable.
        "mesas_escrutadas": int(mesas_escrutadas) if mesas_escrutadas is not None else None,
        "mesas_informadas": int(mesas_informadas),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keys": sorted(r.key for r in records),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    tmp.replace(path)
    log.info("wrote universe snapshot total_global=%s informadas=%d -> %s",
             snap["total_global"], mesas_informadas, path)
    return snap


def write_universe_csv(records: list[ActaRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fields = list(asdict(records[0]).keys()) if records else [
        f.name for f in ActaRecord.__dataclass_fields__.values()]
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))
    tmp.replace(path)
    log.info("wrote %d rows -> %s", len(records), path)


def load_universe_csv(path: Path) -> list[ActaRecord]:
    out: list[ActaRecord] = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            row["status"] = int(row.get("status") or 0)
            out.append(ActaRecord(**row))
    return out


def fetch_names(session: CdnSession | None = None) -> dict[tuple, dict]:
    """Fetch departmentsTree.json -> {(dep2,muni3,zona3,puesto2): names}.

    Provides the human-readable DIVIPOL dictionary (department / municipality /
    zone names and the puesto `Lugar` = standName) keyed by the same padded
    codes used in the acta paths.
    """
    session = session or CdnSession()
    url = f"{config.JSON_BASE}/{config.JSON_FILES['departments_tree']}"
    log.info("fetching names tree: %s", url)
    payload = session.get_json(url)
    data = payload.get("data", payload)
    out: dict[tuple, dict] = {}
    for edge in data["departmentsTree"]["edges"]:
        d = edge["node"]
        dep = str(d["idDepartmentCode"]).zfill(2)
        dep_name = d.get("departmentName", "")
        for m in d.get("municipalities", []) or []:
            muni = str(m["municipalityCode"]).zfill(3)
            muni_name = m.get("municipalityName", "")
            for z in m.get("zones", []) or []:
                zona = str(z["idZoneCode"]).zfill(3)
                zona_name = z.get("zoneName", "")
                for st in z.get("stands", []) or []:
                    puesto = str(st["standCode"]).zfill(2)
                    out[(dep, muni, zona, puesto)] = {
                        "dep_name": dep_name, "muni_name": muni_name,
                        "zona_name": zona_name, "lugar": st.get("standName", ""),
                        "count_table": st.get("countTable"),
                    }
    log.info("names dictionary: %d puestos", len(out))
    return out


def write_dictionary_csv(names: dict[tuple, dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cod_departamento", "departamento", "cod_municipio", "municipio",
                    "cod_zona", "zona", "cod_puesto", "lugar_votacion", "num_mesas"])
        for (dep, muni, zona, puesto), v in sorted(names.items()):
            w.writerow([dep, v["dep_name"], muni, v["muni_name"], zona,
                        v["zona_name"], puesto, v["lugar"], v["count_table"]])


def write_index_csv(records: list[ActaRecord], names: dict[tuple, dict],
                    path: Path, variant: str = "delegados") -> None:
    """Human-friendly index: one row per acta with names + numeric path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cod_departamento", "departamento", "cod_municipio", "municipio",
                    "cod_zona", "zona", "cod_puesto", "lugar_votacion",
                    "mesa", "archivo", "enlace_oficial"])
        for r in records:
            key = (r.dep.zfill(2), r.muni.zfill(3), r.zona.zfill(3), r.puesto.zfill(2))
            nm = names.get(key, {})
            w.writerow([
                r.dep.zfill(2), nm.get("dep_name", ""), r.muni.zfill(3),
                nm.get("muni_name", ""), r.zona.zfill(3), nm.get("zona_name", ""),
                r.puesto.zfill(2), nm.get("lugar", ""), r.mesa.zfill(3),
                f"{r.rel_dir()}/{r.filename(variant)}", r.pdf_url(),
            ])


def filter_records(records, dep: str | None = None, muni: str | None = None,
                   limit: int | None = None) -> list[ActaRecord]:
    out = records
    if dep:
        out = [r for r in out if r.dep.zfill(2) == dep.zfill(2)]
    if muni:
        out = [r for r in out if r.muni.zfill(3) == muni.zfill(3)]
    if limit:
        out = out[:limit]
    return out
