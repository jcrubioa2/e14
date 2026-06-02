"""ASGI entry point for the public community-poll report.

Used by ``uvicorn e14detector.asgi:app`` (the Docker/Fly CMD). Kept deliberately
thin so serving never imports the heavy CV/PDF stack — only the webapp, which in
turn pulls just FastAPI, Jinja2, requests and Pillow.

Paths come from the environment so the same image runs locally and on Fly:
- ``E14_RESULTS_DB`` / ``E14_OUTPUT_DIR`` point at the read-only data baked into the
  image (defaults under ``data/detector``).
- ``E14_COMMUNITY_DB`` points at the writable votes DB (on the persistent volume,
  e.g. ``/data/community.sqlite`` in production).
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config
from .webapp import create_app

RESULTS_DB = Path(os.environ.get("E14_RESULTS_DB", str(config.DEFAULT_RESULTS_DB)))
OUTPUT_DIR = Path(os.environ.get("E14_OUTPUT_DIR", str(config.DEFAULT_OUTPUT_DIR)))

app = create_app(
    results_db=RESULTS_DB,
    output_dir=OUTPUT_DIR,
    community_db=Path(config.COMMUNITY_DB),
)
