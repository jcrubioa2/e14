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

TRANSPARENCY_LOG: list[dict] = [
    {
        "date": "2026-06-08",
        "status": "fixed",
        "tag": "Cobertura",
        "title": "Recuperamos actas que se habían subido como foto o escaneo no estándar",
        "body": (
            "Algunas mesas subieron su acta como foto (a veces de lado o con fondo) o en un "
            "formato de escaneo distinto, y nuestro lector automático no lograba recortar bien "
            "las casillas. Vimos que la Registraduría había vuelto a publicar muchas de esas "
            "actas como un escaneo limpio bajo el mismo enlace, así que descargamos de nuevo las "
            "versiones frescas y recuperamos los números de la gran mayoría, que ya vuelven a "
            "estar disponibles para revisión. Las pocas que siguen siendo solo una foto difícil "
            "de leer se muestran igual, pero con la votación desactivada y explicadas en "
            "Transparencia. Guardamos además una copia de las versiones anteriores."
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
