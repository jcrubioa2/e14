# Colombia map boundaries (GeoJSON)

Simplified department and municipio polygons for the **Reportes** choropleth on `/reportes`.

## Source

- [caticoa3/colombia_mapa](https://github.com/caticoa3/colombia_mapa) — MGN 2018 from [Geoportal DANE](https://geoportal.dane.gov.co/)
- Original files: `co_2018_MGN_MPIO_POLITICO.geojson`, `co_2018_MGN_DPTO_POLITICO.geojson`

## Regenerate

```bash
python scripts/prepare_map_geojson.py
```

This re-downloads the upstream GeoJSON, normalizes properties to `dep` / `muni` / `name` (DIVIPOL-aligned codes), and rounds coordinates for a smaller web payload.

## Join keys

| Field | Matches |
|-------|---------|
| `dep` | `documents.department_code` (2 digits) |
| `muni` | `documents.municipality_code` (3 digits) |

## License

Boundary data is public geographic information from DANE. Check upstream repo and DANE terms for redistribution.
