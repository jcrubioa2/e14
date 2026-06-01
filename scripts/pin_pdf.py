"""Recon: resolve the two sample mesas and pin the exact PDF path/padding.

Run:  .venv/bin/python -m scripts.pin_pdf
"""
import logging
import sys

import requests

from e14 import config
from e14.api import E14Api

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pin")

SAMPLES = [
    # (dep, muni, zona, puesto, mesa, label)
    ("16", "001", "17", "01", "002", "Bogota / La Concordia"),
    ("01", "001", "13", "04", "015", "Antioquia / Villa de la Candelaria"),
]


def main() -> int:
    api = E14Api()

    log.info("Fetching corporations ...")
    corps = api.get_corporations()
    for c in corps:
        log.info("  corp %s %r acronym=%s level=%s",
                 c["idCorporationCode"], c["nameCorporation"], c["acronym"], c["level"])
    # Presidential visor: expect a single (or pick level/most relevant) corporation.
    corp = corps[0]
    log.info("Using corporation idCode=%s acronym=%s", corp["idCorporationCode"], corp["acronym"])

    for dep, muni, zona, puesto, mesa, label in SAMPLES:
        log.info("=== %s  %s/%s/%s/%s/%s ===", label, dep, muni, zona, puesto, mesa)
        nodes = api.get_transmission_codes(
            corp["idCorporationCode"], dep, muni, zona, puesto, first=200
        )
        log.info("  %d transmission nodes returned", len(nodes))
        for n in nodes[:5]:
            log.info("    numberStand=%s expectedName=%r status=%s standCode=%s",
                     n.get("numberStand"), n.get("expectedName"),
                     n.get("idTransmissionCodeStatus"), n.get("standCode"))
        # find the target mesa
        target = next(
            (n for n in nodes if str(n.get("numberStand", "")).zfill(3) == mesa
             and n.get("expectedName")),
            None,
        )
        if not target:
            target = next((n for n in nodes if n.get("expectedName")), None)
            if target:
                log.warning("  exact mesa %s not found; trying first available node "
                            "numberStand=%s", mesa, target.get("numberStand"))
        if not target:
            log.error("  no downloadable node (expectedName) found for this stand")
            continue

        mesa3 = str(target["numberStand"]).zfill(3)
        archivo = target["expectedName"]
        acronym = corp["acronym"]
        # Candidate path orderings to disambiguate empirically.
        candidates = {
            "dep/mun/zona/puesto/mesa/acr/file":
                f"{dep}/{muni}/{zona}/{puesto}/{mesa3}/{acronym}/{archivo}",
            "dep/mun/zona2/puesto/mesa/acr/file":
                f"{dep}/{muni}/{zona.zfill(2)}/{puesto}/{mesa3}/{acronym}/{archivo}",
        }
        for name, fp in candidates.items():
            url = f"{config.PDF_BASE}/{fp}?uuid=pin-test"
            try:
                r = requests.get(url, headers=config.PDF_HEADERS,
                                 timeout=config.PDF_TIMEOUT)
            except Exception as exc:
                log.info("    [%s] ERROR %s", name, exc)
                continue
            magic = r.content[:5]
            ok = r.status_code == 200 and magic == b"%PDF-"
            log.info("    [%s] HTTP %s type=%s bytes=%s magic=%r %s",
                     name, r.status_code, r.headers.get("Content-Type"),
                     len(r.content), magic, "<-- PDF OK" if ok else "")
            if ok:
                out = f"/tmp/sample_{dep}_{muni}_{zona}_{puesto}_{mesa3}.pdf"
                with open(out, "wb") as fh:
                    fh.write(r.content)
                log.info("    saved %s (%d bytes)", out, len(r.content))
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
