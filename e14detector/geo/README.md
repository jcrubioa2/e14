# Colombia map boundaries (GeoJSON)

Simplified department and municipio polygons for the **Reportes** choropleth on `/reportes`.

## Source

- [caticoa3/colombia_mapa](https://github.com/caticoa3/colombia_mapa) — MGN 2018 from [Geoportal DANE](https://geoportal.dane.gov.co/)
- Original files: `co_2018_MGN_MPIO_POLITICO.geojson`, `co_2018_MGN_DPTO_POLITICO.geojson`

## Regenerate

```bash
python scripts/prepare_map_geojson.py
```

This re-downloads the upstream GeoJSON, normalizes properties to `dep` / `muni` / `name`, rounds coordinates for a smaller web payload, and **relabels the codes from DANE to Registraduría DIVIPOL** (see below).

## Code system — important

Upstream boundaries are coded with **DANE** codes (Antioquia = `05`). The app — `documents.department_code`, `/api/reportes/map`, the `/buscar` drill-down — is entirely **Registraduría DIVIPOL** (Antioquia = `01`, from `e14detector/divipol_dictionary.csv`). The two systems share names but not numbers, so a raw code join paints every department with the wrong data.

`scripts/relabel_geojson_divipol.py` rewrites `dep` / `muni` to DIVIPOL codes by joining on **name** against the DIVIPOL dictionary (it runs automatically as the last step of `prepare_map_geojson.py`, and can also be run standalone on the committed files). The original DANE codes are preserved under `dane_dep` / `dane_muni`. A few non-municipalized / no-mesa areas (e.g. Mapiripana, Papunahua) have no DIVIPOL match and render gray.

## Join keys

| Field | Matches |
|-------|---------|
| `dep` | `documents.department_code` — **DIVIPOL** (2 digits) |
| `muni` | `documents.municipality_code` — **DIVIPOL** (3 digits) |
| `dane_dep` / `dane_muni` | original DANE codes (provenance only) |

## License

Boundary data is public geographic information from DANE. Check upstream repo and DANE terms for redistribution.
