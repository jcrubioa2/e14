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
# boto3 is now required (not just for publishing): the web process enqueues votes
# to SQS + reads Aurora via the Data API, and the worker drains SQS -> Aurora.
RUN pip install --no-cache-dir \
    "fastapi>=0.111,<0.116" \
    "uvicorn[standard]>=0.30,<0.48" \
    "jinja2>=3.1" \
    "requests>=2.31" \
    "Pillow>=10.0" \
    "boto3>=1.34"

# Application code + read-only data (results DB + crops). .dockerignore keeps the
# writable community.sqlite and scratch dirs out of the image.
COPY e14detector/ /app/e14detector/
COPY data/detector/ /app/data/detector/

EXPOSE 8000
# Default command = the web process. fly.toml's [processes] overrides this per group
# (web = uvicorn, worker = vote_worker). Votes now go to Aurora via SQS, so SQLite
# write-serialization is no longer the reason for one worker; scale web horizontally
# across machines instead.
CMD ["uvicorn", "e14detector.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
