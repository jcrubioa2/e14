"""Minimal Splitwise REST API client.

Single-user/personal use: authenticate with a personal API key (registered at
https://secure.splitwise.com/apps) sent as a Bearer token. No OAuth flow.

The key is read from the SPLITWISE_API_KEY environment variable by default, so a
future session can use it without re-setup once that var is configured.

Notable Splitwise quirk handled here: create_expense returns HTTP 200 even on a
validation failure, with the problem reported in a non-empty ``errors`` object in
the JSON body. ``_request`` inspects that and raises ``SplitwiseError``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation

import requests

BASE_URL = "https://secure.splitwise.com/api/v3.0"
DEFAULT_TIMEOUT = 30


class SplitwiseError(RuntimeError):
    """Raised for missing config, HTTP failures, or Splitwise-reported errors."""


class SplitwiseClient:
    def __init__(self, api_key=None, base_url=BASE_URL, session=None, timeout=DEFAULT_TIMEOUT):
        api_key = api_key or os.environ.get("SPLITWISE_API_KEY")
        if not api_key:
            raise SplitwiseError(
                "No API key. Set SPLITWISE_API_KEY (register an app at "
                "https://secure.splitwise.com/apps to get one) or pass api_key=."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    # -- low level ---------------------------------------------------------
    def _request(self, method, path, data=None):
        url = f"{self.base_url}/{path}"
        resp = self.session.request(method, url, data=data, timeout=self.timeout)
        if resp.status_code >= 400:
            raise SplitwiseError(f"HTTP {resp.status_code} from {path}: {resp.text[:500]}")
        try:
            payload = resp.json()
        except ValueError:
            raise SplitwiseError(f"Non-JSON response from {path}: {resp.text[:500]}")
        # Splitwise returns 200 with a non-empty `errors` object on validation failure.
        if payload.get("errors"):
            raise SplitwiseError(f"Splitwise API error from {path}: {payload['errors']}")
        return payload

    # -- reads -------------------------------------------------------------
    def get_current_user(self):
        return self._request("GET", "get_current_user")["user"]

    def get_groups(self):
        return self._request("GET", "get_groups")["groups"]

    def get_friends(self):
        return self._request("GET", "get_friends")["friends"]

    # -- writes ------------------------------------------------------------
    def create_expense(self, cost, description, group_id=0, split_equally=True, shares=None):
        """Create an expense.

        - Default (``split_equally=True``, no ``shares``): split evenly among the
          group; the current user is recorded as payer.
        - ``shares``: list of ``{"user_id" | "email", "paid_share", "owed_share"}``.
          paid_share and owed_share must each sum to ``cost`` (Splitwise's rule).
        """
        cost = str(cost)
        data = {"cost": cost, "description": description, "group_id": group_id}

        if shares:
            self._validate_shares(cost, shares)
            for i, s in enumerate(shares):
                if s.get("email"):
                    data[f"users__{i}__email"] = s["email"]
                else:
                    data[f"users__{i}__user_id"] = s["user_id"]
                data[f"users__{i}__paid_share"] = str(s["paid_share"])
                data[f"users__{i}__owed_share"] = str(s["owed_share"])
        elif split_equally:
            data["split_equally"] = "true"
        else:
            raise SplitwiseError("Provide split_equally=True or a non-empty shares list.")

        return self._request("POST", "create_expense", data=data)

    @staticmethod
    def _validate_shares(cost, shares):
        try:
            target = Decimal(cost)
            paid = sum(Decimal(str(s["paid_share"])) for s in shares)
            owed = sum(Decimal(str(s["owed_share"])) for s in shares)
        except (InvalidOperation, KeyError) as exc:
            raise SplitwiseError(f"Invalid share values: {exc}")
        if paid != target:
            raise SplitwiseError(f"paid_share sum {paid} != cost {target}")
        if owed != target:
            raise SplitwiseError(f"owed_share sum {owed} != cost {target}")


# -- thin CLI (invoke directly; not a packaged entry point) ----------------
def _build_shares(cost, payer, owed_items):
    if not payer:
        raise SplitwiseError("--payer is required when using --owed.")
    payer = int(payer)
    owed = {}
    for item in owed_items:
        uid, sep, amt = item.partition(":")
        if not sep:
            raise SplitwiseError(f"--owed expects USER_ID:AMOUNT, got {item!r}")
        owed[int(uid)] = amt
    owed.setdefault(payer, "0")
    return [
        {
            "user_id": uid,
            "paid_share": cost if uid == payer else "0",
            "owed_share": amt,
        }
        for uid, amt in owed.items()
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="splitwise", description="Add expenses to Splitwise.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="Show the authenticated account (API-key smoke test).")
    sub.add_parser("groups", help="List groups with their IDs.")
    sub.add_parser("friends", help="List friends with their IDs.")

    padd = sub.add_parser("add", help="Create an expense.")
    padd.add_argument("--cost", required=True, help='Total amount, e.g. "40.00".')
    padd.add_argument("--desc", required=True, help="Expense description.")
    padd.add_argument("--group", type=int, default=0, help="Group ID (0 = non-group).")
    padd.add_argument("--equal", action="store_true", help="Split equally (default).")
    padd.add_argument("--owed", action="append", metavar="USER_ID:AMOUNT",
                      help="Per-user owed amount (repeatable). Requires --payer.")
    padd.add_argument("--payer", help="User ID who paid (with --owed).")

    args = parser.parse_args(argv)

    try:
        client = SplitwiseClient()
        if args.command == "whoami":
            u = client.get_current_user()
            print(f"{u.get('first_name', '')} {u.get('last_name', '') or ''}".strip(),
                  f"<{u.get('email')}>  id={u.get('id')}")
        elif args.command == "groups":
            for g in client.get_groups():
                print(f"{g['id']}\t{g['name']}")
        elif args.command == "friends":
            for f in client.get_friends():
                name = f"{f.get('first_name','')} {f.get('last_name','') or ''}".strip()
                print(f"{f['id']}\t{name}\t{f.get('email','')}")
        elif args.command == "add":
            if args.owed:
                shares = _build_shares(args.cost, args.payer, args.owed)
                result = client.create_expense(args.cost, args.desc, group_id=args.group, shares=shares)
            else:
                result = client.create_expense(args.cost, args.desc, group_id=args.group, split_equally=True)
            print(json.dumps(result.get("expenses", result), indent=2, default=str))
    except SplitwiseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
