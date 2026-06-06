#!/usr/bin/env python3
"""Relabel the bundled DANE GeoJSON with Registraduría DIVIPOL codes.

The /reportes choropleth joins map polygons to report stats by department/municipio
CODE. The boundaries (e14detector/geo/*.geojson) ship with DANE codes (Antioquia=05),
but every code the app produces — documents.department_code, the /api/reportes/map
output, the /buscar drill-down, the dropdown — uses the Registraduría DIVIPOL codes
from divipol_dictionary.csv (Antioquia=01). The two systems share NAMES but not
NUMBERS, so a code-to-code join paints each department with the wrong data.

This rewrites the GeoJSON `dep`/`muni` properties to DIVIPOL codes by joining on
NAME against the DIVIPOL dictionary, so the whole app stays DIVIPOL-native and the
map needs no special-casing. Idempotent: matching keys off `name`/`dep_name`, which
this never modifies. The original DANE codes are preserved under `dane_dep`/`dane_muni`.

Run from repo root (operates on the committed files in place):

    python scripts/relabel_geojson_divipol.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO_DIR = ROOT / "e14detector" / "geo"
DIVIPOL_CSV = ROOT / "e14detector" / "divipol_dictionary.csv"

# DANE department name (as it appears in the GeoJSON) -> DIVIPOL department name.
# Only the handful that differ beyond accents/punctuation.
DEP_NAME_ALIAS = {
    "VALLE DEL CAUCA": "VALLE",
    "ARCHIPIELAGO DE SAN ANDRES PROVIDENCIA Y SANTA CATALINA": "SAN ANDRES",
    "NORTE DE SANTANDER": "NORTE DE SAN",
}
# (normalized dep_name, normalized GeoJSON muni name) -> DIVIPOL muni name, for the
# few spelling variants the fuzzy matcher can't resolve safely.
MUNI_NAME_ALIAS = {
    ("BOYACA", "VILLA DE LEYVA"): "VILLA DE LEIVA",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s)).strip()


class Crosswalk:
    def __init__(self, dict_path: Path = DIVIPOL_CSV):
        self._dep: dict[str, str] = {}                     # norm dept name -> cod_dep
        self._muni_exact: dict[tuple[str, str], str] = {}  # (cod_dep, norm muni) -> cod_muni
        self._dep_munis: dict[str, dict[str, str]] = {}    # cod_dep -> {norm muni: cod_muni}
        with open(dict_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cd = str(r["cod_departamento"]).strip().zfill(2)
                cm = str(r["cod_municipio"]).strip().zfill(3)
                self._dep.setdefault(_norm(r["departamento"]), cd)
                raw = r["municipio"]
                # Index the full name, plus the parts of "NAME (ALIAS)" forms, plus a
                # spaceless variant (SANTAFE vs SANTA FE).
                variants = {_norm(raw)}
                m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", raw)
                if m:
                    variants |= {_norm(m.group(1)), _norm(m.group(2))}
                for v in list(variants):
                    if v:
                        variants.add(v.replace(" ", ""))
                for v in variants:
                    if v:
                        self._muni_exact.setdefault((cd, v), cm)
                self._dep_munis.setdefault(cd, {})[_norm(raw)] = cm

    def dep_code(self, dep_name: str) -> str | None:
        n = _norm(dep_name)
        return self._dep.get(DEP_NAME_ALIAS.get(n, n))

    def muni_code(self, dep_name: str, muni_name: str) -> str | None:
        cd = self.dep_code(dep_name)
        if not cd:
            return None
        n = _norm(muni_name)
        alias = MUNI_NAME_ALIAS.get((_norm(dep_name), n))
        if alias:
            n = _norm(alias)
        hit = self._muni_exact.get((cd, n)) or self._muni_exact.get((cd, n.replace(" ", "")))
        if hit:
            return hit
        # Unique-containment fallback, preferring the most specific (longest) dictionary
        # name that is a substring of the GeoJSON name (CARTAGENA DE INDIAS -> CARTAGENA,
        # SAN JOSE DE TOLUVIEJO -> TOLUVIEJO not TOLU).
        cands = [(dnm, cm) for dnm, cm in self._dep_munis[cd].items() if dnm in n or n in dnm]
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0][1]
        contained = [(dnm, cm) for dnm, cm in cands if dnm in n]
        if contained:
            contained.sort(key=lambda kv: len(kv[0]), reverse=True)
            if len(contained) == 1 or len(contained[0][0]) > len(contained[1][0]):
                return contained[0][1]
        return None  # genuinely ambiguous -> leave unmatched (renders gray)


def relabel_departamentos(data: dict, xw: Crosswalk) -> tuple[int, list[str]]:
    matched, missed = 0, []
    for f in data.get("features", []):
        p = f["properties"]
        code = xw.dep_code(p.get("name"))
        if code:
            p.setdefault("dane_dep", p["dep"])
            p["dep"] = code
            matched += 1
        else:
            missed.append(p.get("name"))
    return matched, missed


def relabel_municipios(data: dict, xw: Crosswalk) -> tuple[int, list[str]]:
    matched, missed = 0, []
    for f in data.get("features", []):
        p = f["properties"]
        dep = xw.dep_code(p.get("dep_name"))
        muni = xw.muni_code(p.get("dep_name"), p.get("name"))
        p.setdefault("dane_dep", p["dep"])
        p.setdefault("dane_muni", p["muni"])
        if dep:
            p["dep"] = dep
        if dep and muni:
            p["muni"] = muni
            matched += 1
        else:
            missed.append(f"{p.get('dep_name')} / {p.get('name')}")
    return matched, missed


def _rewrite(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    xw = Crosswalk()
    dpath = GEO_DIR / "colombia_departamentos.geojson"
    mpath = GEO_DIR / "colombia_municipios.geojson"
    ddata = json.loads(dpath.read_text(encoding="utf-8"))
    mdata = json.loads(mpath.read_text(encoding="utf-8"))

    dm, dmiss = relabel_departamentos(ddata, xw)
    mm, mmiss = relabel_municipios(mdata, xw)
    _rewrite(dpath, ddata)
    _rewrite(mpath, mdata)

    print(f"departamentos: {dm}/{dm + len(dmiss)} relabeled to DIVIPOL", flush=True)
    if dmiss:
        print(f"  UNMATCHED depts ({len(dmiss)}): {dmiss}")
    print(f"municipios:    {mm}/{mm + len(mmiss)} relabeled to DIVIPOL "
          f"({100 * len(mmiss) / max(1, mm + len(mmiss)):.1f}% gray)")
    for x in mmiss:
        print(f"  gray (no DIVIPOL match): {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
