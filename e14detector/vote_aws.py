"""Boto3 client factory for the vote infrastructure (SQS + Aurora Data API).

Why this exists: the app's ``.env`` / Fly secrets already bind ``AWS_ACCESS_KEY_ID``
and ``AWS_SECRET_ACCESS_KEY`` to the **Tigris** S3-compatible store (see
``publish.py``, which talks to Tigris via an explicit ``endpoint_url``). Those keys
are *not* valid for real AWS. A default-chain boto3 client for SQS or rds-data would
pick up the Tigris keys and fail with ``UnrecognizedClientException``.

So the vote infra uses its **own** credentials, under distinct env names, for the
``e14-vote-fly`` IAM user:

    E14_VOTE_AWS_ACCESS_KEY_ID
    E14_VOTE_AWS_SECRET_ACCESS_KEY
    E14_VOTE_AWS_REGION            (default us-east-1)

If the dedicated keys are absent, we fall back to the default credential chain
(profile / instance role) — handy for local dev where the shell is already
authenticated to real AWS and no Tigris keys are loaded.
"""
from __future__ import annotations

import os

import boto3

DEFAULT_REGION = "us-east-1"


def vote_region() -> str:
    return os.environ.get("E14_VOTE_AWS_REGION", DEFAULT_REGION)


def vote_client(service: str):
    """A boto3 client for ``service`` (e.g. ``sqs``, ``rds-data``) using the vote
    infra's dedicated credentials, never the Tigris ``AWS_*`` keys."""
    kwargs = {"region_name": vote_region()}
    ak = os.environ.get("E14_VOTE_AWS_ACCESS_KEY_ID")
    sk = os.environ.get("E14_VOTE_AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        kwargs["aws_access_key_id"] = ak
        kwargs["aws_secret_access_key"] = sk
    return boto3.client(service, **kwargs)
