"""Anonymous AWS auth for the AppSync GraphQL backend.

The site authenticates GraphQL with AWS_IAM (SigV4) using *unauthenticated*
credentials vended by a Cognito Identity Pool. Flow (both calls are public,
unsigned):

    cognito-identity.GetId(IdentityPoolId)            -> IdentityId
    cognito-identity.GetCredentialsForIdentity(Id)    -> temp {AK,SK,Token}

Those temp creds (~1h TTL) then sign each AppSync POST. This module is pure
stdlib + requests (no boto3) so the scraper has no heavy AWS dependency.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass

import requests

from . import config


class AuthError(RuntimeError):
    pass


@dataclass
class Credentials:
    access_key: str
    secret_key: str
    session_token: str
    expiration: float  # epoch seconds

    def expiring_within(self, seconds: float) -> bool:
        return time.time() >= (self.expiration - seconds)


def _cognito_call(target: str, payload: dict, session: requests.Session) -> dict:
    resp = session.post(
        config.COGNITO_IDENTITY_URL,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityService.{target}",
            "User-Agent": config.USER_AGENT,
        },
        data=json.dumps(payload),
        timeout=config.GQL_TIMEOUT,
    )
    if resp.status_code != 200:
        raise AuthError(f"Cognito {target} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def fetch_anonymous_credentials(session: requests.Session | None = None) -> Credentials:
    """Obtain fresh unauthenticated AWS credentials from the Cognito pool."""
    sess = session or requests.Session()
    got = _cognito_call(
        "GetId", {"IdentityPoolId": config.COGNITO_IDENTITY_POOL_ID}, sess
    )
    identity_id = got["IdentityId"]
    creds = _cognito_call(
        "GetCredentialsForIdentity", {"IdentityId": identity_id}, sess
    )["Credentials"]
    # Expiration comes back as a float epoch (seconds).
    return Credentials(
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretKey"],
        session_token=creds["SessionToken"],
        expiration=float(creds["Expiration"]),
    )


# --- SigV4 ---

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def sigv4_headers(
    creds: Credentials,
    method: str,
    url: str,
    body: str,
    region: str = config.AWS_REGION,
    service: str = config.APPSYNC_SERVICE,
    now: _dt.datetime | None = None,
) -> dict:
    """Return SigV4 Authorization + amz headers for a request.

    Signed headers: content-type;host;x-amz-date;x-amz-security-token
    (matching what the Amplify client sends).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    t = now or _dt.datetime.now(_dt.timezone.utc)
    amzdate = t.strftime("%Y%m%dT%H%M%SZ")
    datestamp = t.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-amz-date:{amzdate}\n"
        f"x-amz-security-token:{creds.session_token}\n"
    )
    signed_headers = "content-type;host;x-amz-date;x-amz-security-token"
    canonical_request = (
        f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    signing_key = _signing_key(creds.secret_key, datestamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Type": "application/json",
        "X-Amz-Date": amzdate,
        "X-Amz-Security-Token": creds.session_token,
        "Authorization": authorization,
    }


class CredentialProvider:
    """Thread-safe holder that lazily fetches and refreshes anon credentials."""

    REFRESH_MARGIN = 300  # refresh when <5 min remain

    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._creds: Credentials | None = None
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> Credentials:
        with self._lock:
            if (
                force
                or self._creds is None
                or self._creds.expiring_within(self.REFRESH_MARGIN)
            ):
                self._creds = fetch_anonymous_credentials(self._session)
            return self._creds

    def invalidate(self) -> None:
        with self._lock:
            self._creds = None
