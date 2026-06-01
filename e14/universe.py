"""Build the mesa/acta universe from the site's static national JSON.

`allTransmissionCodes.json` contains every acta the site exposes, each with its
geographic codes and the canonical PDF filename (`expectedName`). This is the
authoritative, enumerable download list — one node == one acta PDF.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .session import CdnSession

log = logging.getLogger("e14.universe")


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
