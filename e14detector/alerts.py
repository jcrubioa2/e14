"""Lightweight outbound alerting (Telegram) with per-key dedup.

The serving app can observe failures the AWS-side CloudWatch alarms cannot — a stalled
publisher, a vote that won't enqueue, an unhandled 5xx. This module pages a human about
those over Telegram (free, instant, group-friendly: point the chat id at a group and
everyone on call gets it).

Design rules:
- **No-op when unconfigured** (``E14_ALERT_TELEGRAM_TOKEN`` / ``E14_ALERT_TELEGRAM_CHAT_ID``
  unset), so local dev and tests send nothing.
- **Never crash or block the caller.** Every failure is swallowed and the HTTP POST runs on
  a daemon thread, so ``notify`` is safe to call from sync or async code on the hot path.
- **Deduped by key.** A sustained fault calls ``notify`` on every request; we send the same
  ``key`` at most once per ``E14_ALERT_MIN_INTERVAL`` seconds so it doesn't flood the channel.

Total-outage detection (the app being fully down) is deliberately NOT this module's job — a
dead process can't page anyone. That belongs to an external uptime monitor pointed at /health.
"""
from __future__ import annotations

import os
import threading
import time

import requests

# key -> monotonic timestamp of the last send. Process-local (per worker); with N workers a
# fault pages at most N times per interval, which is fine.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()

_SEVERITY_PREFIX = {"info": "ℹ️", "warn": "⚠️", "error": "\U0001f6a8"}


def _min_interval() -> float:
    try:
        return float(os.environ.get("E14_ALERT_MIN_INTERVAL", "600"))
    except ValueError:
        return 600.0


def _should_send(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < _min_interval():
            return False
        _last_sent[key] = now
        return True


def _post_telegram(text: str) -> None:
    token = os.environ.get("E14_ALERT_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("E14_ALERT_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — alerting must never raise into the caller
        pass


def configured() -> bool:
    """True when a Telegram channel is set (shown on the admin health board)."""
    return bool(
        os.environ.get("E14_ALERT_TELEGRAM_TOKEN") and os.environ.get("E14_ALERT_TELEGRAM_CHAT_ID")
    )


def notify(key: str, msg: str, severity: str = "error") -> None:
    """Page about a failure, deduped by ``key``.

    ``key`` is a stable short slug for the failure class (e.g. ``"db-sync"``, ``"vote-publish"``)
    used only for rate-limiting; ``msg`` is the human-readable detail. Fires on a daemon thread,
    so it returns immediately and never blocks the request. No-op when no channel is configured.
    """
    if not _should_send(key):
        return
    prefix = _SEVERITY_PREFIX.get(severity, _SEVERITY_PREFIX["error"])
    site = os.environ.get("E14_SITE_URL", "")
    text = f"{prefix} [e14] {msg}"
    if site:
        text += f"\n{site}"
    threading.Thread(target=_post_telegram, args=(text,), daemon=True).start()
