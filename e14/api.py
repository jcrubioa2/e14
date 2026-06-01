"""GraphQL client for the AppSync backend + typed query wrappers.

Every request is SigV4-signed with anonymous Cognito credentials (see auth.py).
The backend is mview-backed and slow, so timeouts are long and retries patient.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from . import config
from .auth import CredentialProvider
from .util import RateLimiter, RetryableError, retry

log = logging.getLogger("e14.api")


# --- GraphQL operations (verbatim from the JS bundles) ---

Q_CORPORATIONS = """
query CorpIndex($first: Int = 50) {
  allCorporations(first: $first, orderBy: ID_CORPORATION_CODE_ASC) {
    edges { node { idCorporationCode nameCorporation acronym level } }
  }
}
"""

Q_DEPARTMENTS_TREE = """
query DepartmentsTree($first: Int = 500000) {
  departmentsTree(first: $first, orderBy: "DEPARTMENT_NAME_ASC") {
    edges {
      node {
        idDepartmentCode
        departmentName
        municipalities {
          idMunicipality
          municipalityCode
          municipalityName
          zones {
            idZone
            idZoneCode
            zoneName
            corporations
            stands { idStand standCode standName countTable }
          }
        }
      }
    }
  }
}
"""

Q_TRANSMISSION_CODES_BY_STAND = """
query TransmissionCodesByStand(
  $idCorporationCode: String!, $first: Int!,
  $idDepartmentCode: String, $municipalityCode: String,
  $idZoneCode: String, $standCode: String
) {
  status11: allTransmissionCodes(
    first: $first
    condition: {
      idTransmissionCodeStatus: 11
      idCorporationCode: $idCorporationCode
      idDepartmentCode: $idDepartmentCode
      municipalityCode: $municipalityCode
      idZoneCode: $idZoneCode
      standCode: $standCode
    }
    orderBy: ["NUMBER_STAND_ASC"]
  ) { nodes { numberStand expectedName idStand idCorporationCode idTransmissionCodeStatus standCode idZoneCode idDepartmentCode municipalityCode } }
  status3: allTransmissionCodes(
    first: $first
    condition: {
      idTransmissionCodeStatus: 3
      idCorporationCode: $idCorporationCode
      idDepartmentCode: $idDepartmentCode
      municipalityCode: $municipalityCode
      idZoneCode: $idZoneCode
      standCode: $standCode
    }
    orderBy: ["NUMBER_STAND_ASC"]
  ) { nodes { numberStand expectedName idStand idCorporationCode idTransmissionCodeStatus standCode idZoneCode idDepartmentCode municipalityCode } }
}
"""


class GraphQLError(RuntimeError):
    pass


class E14Api:
    def __init__(
        self,
        creds: CredentialProvider | None = None,
        rate_limiter: RateLimiter | None = None,
        session: requests.Session | None = None,
    ):
        self.session = session or requests.Session()
        self.creds = creds or CredentialProvider(self.session)
        self.rate = rate_limiter or RateLimiter(config.DEFAULT_RATE_LIMIT)

    @retry(attempts=5, base=2.0, cap=60.0, exceptions=(RetryableError,))
    def gql(self, query: str, variables: dict | None = None) -> dict[str, Any]:
        """Sign and POST a GraphQL query; return the `data` object.

        Raises RetryableError on transient failures (timeouts, 5xx, 401-expiry,
        AppSync throttling) so the @retry wrapper backs off.
        """
        from .auth import sigv4_headers

        body = json.dumps({"query": query, "variables": variables or {}})
        self.rate.acquire()
        creds = self.creds.get()
        headers = {
            **config.BROWSER_HEADERS,
            **sigv4_headers(creds, "POST", config.GRAPHQL_URL, body),
        }
        try:
            resp = self.session.post(
                config.GRAPHQL_URL, headers=headers, data=body,
                timeout=config.GQL_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RetryableError(f"network: {exc}") from exc

        if resp.status_code == 401:
            # Credentials likely expired/invalid — force refresh and retry.
            self.creds.invalidate()
            raise RetryableError("401 Unauthorized (refreshing creds)")
        if resp.status_code in (429, 500, 502, 503, 504):
            raise RetryableError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise GraphQLError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        if payload.get("errors"):
            msgs = "; ".join(e.get("message", "") for e in payload["errors"])
            # AppSync throttling surfaces as errors with 200 status.
            if any(k in msgs.lower() for k in ("throttl", "timeout", "rate exceeded")):
                raise RetryableError(f"GraphQL throttle: {msgs}")
            raise GraphQLError(f"GraphQL errors: {msgs}")
        return payload.get("data", {})

    # --- typed wrappers ---

    def get_corporations(self) -> list[dict]:
        data = self.gql(Q_CORPORATIONS, {"first": 50})
        return [e["node"] for e in data["allCorporations"]["edges"]]

    def get_departments_tree(self, first: int = 500000) -> list[dict]:
        data = self.gql(Q_DEPARTMENTS_TREE, {"first": first})
        return [e["node"] for e in data["departmentsTree"]["edges"]]

    def get_transmission_codes(
        self,
        corp_code: str,
        dep_code: str,
        muni_code: str,
        zone_code: str,
        stand_code: str,
        first: int = 200,
    ) -> list[dict]:
        """Return combined transmission-code nodes (both status aliases) for a
        single stand (puesto). Each node carries numberStand + expectedName."""
        data = self.gql(
            Q_TRANSMISSION_CODES_BY_STAND,
            {
                "idCorporationCode": corp_code,
                "first": first,
                "idDepartmentCode": dep_code,
                "municipalityCode": muni_code,
                "idZoneCode": zone_code,
                "standCode": stand_code,
            },
        )
        nodes: list[dict] = []
        for alias in ("status11", "status3"):
            block = data.get(alias) or {}
            nodes.extend(block.get("nodes", []) or [])
        return nodes
