# Public community-poll report for the E-14 detector.
#
# Deliberately lean: the serve path needs only FastAPI/uvicorn/Jinja2/requests/
# Pillow — NOT opencv/PyMuPDF/curl_cffi/torch (those are detector-build deps). The
# read-only results DB + candidate crops are baked in (~33 MB at pilot scale); the
# writable community.sqlite lives on a mounted volume at /data.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    E14_COMMUNITY_DB=/data/community.sqlite

# Minimal serve dependencies (versions mirror pyproject.toml constraints).
RUN pip install --no-cache-dir \
    "fastapi>=0.111,<0.116" \
    "uvicorn[standard]>=0.30,<0.48" \
    "jinja2>=3.1" \
    "requests>=2.31" \
    "Pillow>=10.0"

# Application code + read-only data (results DB + crops). .dockerignore keeps the
# writable community.sqlite and scratch dirs out of the image.
COPY e14detector/ /app/e14detector/
COPY data/detector/ /app/data/detector/

EXPOSE 8000
# Single worker: keeps SQLite writes serialized and the background-adjudication
# threadpool in one process (the app is single-node by design).
CMD ["uvicorn", "e14detector.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
