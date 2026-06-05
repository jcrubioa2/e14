"""Build the mesa/acta universe from the site's static national JSON.

`allTransmissionCodes.json` contains every acta the site exposes, each with its
geographic codes and the canonical PDF filename (`expectedName`). This is the
authoritative, enumerable download list — one node == one acta PDF.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .session import CdnSession

log = logging.getLogger("e14.universe")

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
    """Fetch the national list, returning (informed_records, total_global).

    ``total_global`` counts *every* node in ``allTransmissionCodes.json`` — including mesas
    not yet informed (no ``expectedName``). The returned records are only the informed ones
    (``expected_name`` set), so ``len(records) == mesas_informadas``. This is the single
    place the two external counts of the count model are derived together, from one fetch, so
    they can never disagree by being read at different times.
    """
    session = session or CdnSession()
    url = f"{config.JSON_BASE}/{config.JSON_FILES['transmission_codes']}"
    log.info("fetching national acta list (with totals): %s", url)
    payload = session.get_json(url)
    data = payload.get("data", payload)
    records: list[ActaRecord] = []
    total_global = 0
    for block in data.values():
        for node in (block or {}).get("nodes", []) or []:
            total_global += 1
            rec = _node_to_record(node)
            if rec.expected_name:
                records.append(rec)
    log.info("universe: total_global=%d mesas_informadas=%d", total_global, len(records))
    return records, total_global


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
    total_global: int,
    path: Path = SNAPSHOT_PATH,
    *,
    allow_shrink: bool = False,
    shrink_factor: float = 0.5,
) -> dict:
    """Atomically write the universe snapshot, guarding against a shrunk fetch.

    Records both external counts plus the full sorted informed-key list (so the publisher can
    diff served-vs-informed to surface the ingest backlog). Refuses — raising
    ``UniverseShrinkError`` — when the new ``total_global`` or ``mesas_informadas`` is below
    ``shrink_factor`` of the previously accepted snapshot, unless ``allow_shrink``.
    """
    path = Path(path)
    mesas_informadas = len(records)
    prev = load_universe_snapshot(path)
    if prev and not allow_shrink:
        prev_total = int(prev.get("total_global") or 0)
        prev_inf = int(prev.get("mesas_informadas") or 0)
        if prev_total and total_global < shrink_factor * prev_total:
            raise UniverseShrinkError(
                f"refusing snapshot: total_global {total_global} < {shrink_factor:.0%} of "
                f"the last accepted {prev_total} — looks like a truncated fetch. "
                f"Pass allow_shrink=True to override.")
        if prev_inf and mesas_informadas < shrink_factor * prev_inf:
            raise UniverseShrinkError(
                f"refusing snapshot: mesas_informadas {mesas_informadas} < {shrink_factor:.0%} "
                f"of the last accepted {prev_inf} — looks like a truncated fetch. "
                f"Pass allow_shrink=True to override.")
    snap = {
        "total_global": int(total_global),
        "mesas_informadas": int(mesas_informadas),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keys": sorted(r.key for r in records),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    tmp.replace(path)
    log.info("wrote universe snapshot total_global=%d informadas=%d -> %s",
             total_global, mesas_informadas, path)
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
