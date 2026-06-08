"""Public, version-controlled record of notable fixes and fairness changes.

Rendered at the bottom of /reportes so the veeduría can show its work: when we find a bug
that could bias what the public sees, we say so here, in plain Spanish. This list is
deliberately a static, code-reviewed artifact — NOT a database an operator can quietly
edit — so its git history is the tamper-evident audit trail. That is the whole point: a
fairness claim you can check, not one you have to trust. Newest first; keep entries short.

Each entry:
  date        YYYY-MM-DD (display only)
  tag         short label, e.g. "Imparcialidad" / "Corrección" / "Mejora"
  title       one line
  body        1-3 sentences, plain language
  link        optional public URL (PR/commit) so anyone can verify the change
  link_label  optional label for the link
"""

from __future__ import annotations

TRANSPARENCY_LOG: list[dict] = [
    {
        "date": "2026-06-08",
        "tag": "Imparcialidad",
        "title": "Cada acta tiene la misma probabilidad de salir a revisión",
        "body": (
            "La baraja de revisión mostraba con más frecuencia las actas cargadas primero y "
            "las que tienen más casillas. Lo corregimos: ahora cada acta revisable es igual "
            "de probable, sin importar cuándo se cargó ni su tamaño. Las revisiones previas "
            "siguen siendo válidas; el cambio solo afecta qué acta se muestra a continuación."
        ),
        "link": "https://github.com/jcrubioa2/e14/pull/36",
        "link_label": "Ver el cambio",
    },
]
