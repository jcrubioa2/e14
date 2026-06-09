"""Public, version-controlled record of notable fixes and fairness changes.

Rendered at the bottom of /reportes so the veeduría can show its work: when we find a
problem that could affect what people see, we explain it here in everyday language — and
say clearly whether it is already fixed or still being worked on. This list is deliberately
a static, code-reviewed artifact — NOT a database an operator can quietly edit — so its git
history is the tamper-evident audit trail. That is the whole point: a fairness claim you
can check, not one you have to trust. Newest first; keep entries short and jargon-free.

Each entry:
  date        YYYY-MM-DD (display only)
  status      "fixed"  -> shown as "Corregido" (green)
              "ongoing"-> shown as "En proceso" (amber)
  tag         short category label, e.g. "Imparcialidad"
  title       one friendly line, no jargon
  body        2-3 short sentences a non-technical reader can follow
  link        optional public URL (PR/commit) so anyone can verify the change
  link_label  optional label for the link
"""

from __future__ import annotations

# Corpus-level recovery funnel for non-standard scans (PR #47), as reviewed constants so
# /transparencia can explain what happens to every acta "down the line" past ingestion. These
# describe the whole remediation; the served-DB quarantine flag remains the LIVE truth for how
# many are currently not votable. Update when a recovery batch shifts the split. The from-scratch
# build reproduces this decision in one pass (layout.geometry_disposition).
RECOVERY_FUNNEL: dict = {
    "standard_pct": "98,4",   # % of actas that arrive as the standard scan -> auto-read, votable
    "nonstandard": 1947,      # non-standard geometry the fixed crop coords don't fit
    "recovered": 1048,        # recovered: re-fetched fresh + crop anchored to the scan's format
    "flagged": 899,           # residual shown-but-not-votable (almost all consulado photos)
    "researching": True,      # we are actively working on reading these too (e.g. OCR alignment)
}

TRANSPARENCY_LOG: list[dict] = [
    {
        "date": "2026-06-08",
        "status": "fixed",
        "tag": "Cobertura",
        "title": "Recuperamos actas subidas como foto o escaneo no estándar",
        "body": (
            "Casi todas las actas (98,4%) llegan como un escaneo estándar que nuestro lector "
            "recorta bien. Pero unas 1.947 venían en otro formato —escaneadas más anchas, o "
            "fotografiadas, a veces de lado o con fondo— y el lector no acertaba con las "
            "casillas. Hicimos dos cosas: volvimos a descargar las versiones frescas (la "
            "Registraduría suele republicar un escaneo limpio bajo el mismo enlace) y ajustamos "
            "el recorte al formato de cada escaneo. Con eso recuperamos 1.048 actas, que ya "
            "vuelven a estar disponibles para revisión. Las 899 restantes son casi todas fotos "
            "de consulados en el exterior: se muestran igual y se pueden comparar con el "
            "documento oficial, pero con la votación comunitaria desactivada para no pedirte que "
            "revises números que no pudimos extraer bien. Seguimos trabajando activamente en "
            "formas de leer también esas. Guardamos además una copia de las versiones anteriores."
        ),
        "link": "https://github.com/jcrubioa2/e14/pull/47",
        "link_label": "Ver el cambio",
    },
    {
        "date": "2026-06-08",
        "status": "fixed",
        "tag": "Cobertura",
        "title": "Priorizamos las actas que menos se han revisado",
        "body": (
            "Repartir cada acta por igual era justo, pero dejaba a la gran mayoría sin revisar "
            "y tardaría muchísimo en emparejarse. Ahora la baraja muestra más seguido las actas "
            "que han recibido menos revisiones, para que la veeduría alcance a la mayor cantidad "
            "posible de mesas. Sigue siendo al azar y ninguna acta queda excluida; la prioridad "
            "depende solo de cuántas veces se ha revisado cada una, nunca de su contenido — así "
            "que no se puede manipular para esconder una mesa."
        ),
        "link": "https://github.com/jcrubioa2/e14/pull/40",
        "link_label": "Ver el cambio",
    },
    {
        "date": "2026-06-08",
        "status": "fixed",
        "tag": "Imparcialidad",
        "title": "Ahora todas las actas tienen la misma posibilidad de salir a revisión",
        "body": (
            "Antes, cuando pedías un acta para revisar, aparecían más seguido las que se "
            "habían subido primero y las que tienen más casillas. Ya lo arreglamos: "
            "ahora a cualquiera le puede tocar cualquier acta por igual, sin importar cuándo "
            "se subió ni qué tan grande sea. Lo que ya habías revisado sigue "
            "contando igual; el cambio solo afecta cuál acta te toca a continuación."
        ),
        "link": "https://github.com/jcrubioa2/e14/pull/36",
        "link_label": "Ver el cambio",
    },
]
