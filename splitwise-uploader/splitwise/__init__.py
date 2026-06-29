"""Thin Splitwise REST API helper for adding/splitting expenses."""

from .client import SplitwiseClient, SplitwiseError, BASE_URL

__all__ = ["SplitwiseClient", "SplitwiseError", "BASE_URL"]
