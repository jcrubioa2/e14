#!/usr/bin/env python3
"""Download DANE Colombia boundaries and write simplified GeoJSON for /reportes maps.

Source: https://github.com/caticoa3/colombia_mapa (MGN 2018, Geoportal DANE).
Run from repo root:

    python scripts/prepare_map_geojson.py

Outputs:
    e14detector/geo/colombia_municipios.geojson
    e14detector/geo/colombia_departamentos.geojson
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO_DIR = ROOT / "e14detector" / "geo"
SOURCES = {
    "mpio": "https://raw.githubusercontent.com/caticoa3/colombia_mapa/master/co_2018_MGN_MPIO_POLITICO.geojson",
    "dpto": "https://raw.githubusercontent.com/caticoa3/colombia_mapa/master/co_2018_MGN_DPTO_POLITICO.geojson",
}


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _round_coords(obj, prec: int = 4):
    if isinstance(obj, float):
        return round(obj, prec)
    if isinstance(obj, list):
        return [_round_coords(x, prec) for x in obj]
    return obj


def _norm_dep(code: str) -> str:
    return str(code or "").strip().zfill(2)


def _norm_muni(code: str) -> str:
    return str(code or "").strip().zfill(3)


def municipios_feature(props: dict, geometry: dict) -> dict | None:
    dep = _norm_dep(props.get("DPTO_CCDGO") or props.get("dep"))
    muni = _norm_muni(props.get("MPIO_CCDGO") or props.get("muni"))
    if not dep or not muni:
        return None
    return {
        "type": "Feature",
        "properties": {
            "dep": dep,
            "muni": muni,
            "name": (props.get("MPIO_CNMBR") or props.get("name") or "").strip(),
            "dep_name": (props.get("DPTO_CNMBR") or props.get("dep_name") or "").strip(),
        },
        "geometry": _round_coords(geometry),
    }


def departamentos_feature(props: dict, geometry: dict) -> dict | None:
    dep = _norm_dep(props.get("DPTO_CCDGO") or props.get("dep"))
    if not dep:
        return None
    return {
        "type": "Feature",
        "properties": {
            "dep": dep,
            "name": (props.get("DPTO_CNMBR") or props.get("name") or "").strip(),
        },
        "geometry": _round_coords(geometry),
    }


def build_municipios(raw: dict) -> dict:
    feats = []
    for f in raw.get("features", []):
        out = municipios_feature(f.get("properties") or {}, f.get("geometry") or {})
        if out:
            feats.append(out)
    return {"type": "FeatureCollection", "features": feats}


def build_departamentos(raw: dict) -> dict:
    feats = []
    for f in raw.get("features", []):
        out = departamentos_feature(f.get("properties") or {}, f.get("geometry") or {})
        if out:
            feats.append(out)
    return {"type": "FeatureCollection", "features": feats}


def write_geojson(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {path} ({path.stat().st_size // 1024} KiB, {len(data['features'])} features)")


def main() -> int:
    GEO_DIR.mkdir(parents=True, exist_ok=True)
    print("fetching municipios…", flush=True)
    mpio_raw = _fetch(SOURCES["mpio"])
    print("fetching departamentos…", flush=True)
    dpto_raw = _fetch(SOURCES["dpto"])
    mpio_out = GEO_DIR / "colombia_municipios.geojson"
    dpto_out = GEO_DIR / "colombia_departamentos.geojson"
    mpio_data = build_municipios(mpio_raw)
    dpto_data = build_departamentos(dpto_raw)

    # The source codes above are DANE; the app joins the map on Registraduría DIVIPOL codes.
    # Relabel before writing so a fresh download never reintroduces the code-system mismatch.
    from relabel_geojson_divipol import Crosswalk, relabel_departamentos, relabel_municipios
    xw = Crosswalk()
    dm, dmiss = relabel_departamentos(dpto_data, xw)
    mm, mmiss = relabel_municipios(mpio_data, xw)
    print(f"relabeled to DIVIPOL — departamentos {dm}/{dm + len(dmiss)}, "
          f"municipios {mm}/{mm + len(mmiss)} ({len(mmiss)} gray)")

    write_geojson(mpio_out, mpio_data)
    write_geojson(dpto_out, dpto_data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
