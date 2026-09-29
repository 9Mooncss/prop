"""Notification adapters. The core only depends on the ``Notifier`` protocol."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, severity: str, title: str, body: str, payload: dict[str, Any] | None = None) -> bool: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, severity, title, body, payload=None):
        self.sent.append((severity, title, body))
        return True


class WebhookNotifier:
    """Generic JSON webhook (works with most chat/ops tools that accept incoming webhooks)."""

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self._url = url
        self.client = client or httpx.Client(timeout=10)

    def send(self, severity, title, body, payload=None):
        try:
            r = self.client.post(self._url, json={"severity": severity, "title": title, "text": body,
                                                  "payload": payload or {}})
            return r.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("webhook notify failed: %s", type(exc).__name__)
            return False


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=10)

    def send(self, severity, title, body, payload=None):
        try:
            r = self.client.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                                 json={"chat_id": self.chat_id, "text": f"[{severity}] {title}\n{body}"[:4000],
                                       "disable_web_page_preview": True})
            return r.status_code < 300
        except httpx.HTTPError as exc:  # never log the URL (contains token)
            log.warning("telegram notify failed: %s", type(exc).__name__)
            return False


class MultiNotifier:
    def __init__(self, *notifiers: Notifier, min_severity: str = "HIGH") -> None:
        self.notifiers = notifiers
        self.min = min_severity

    def send(self, severity, title, body, payload=None):
        order = ["INFO", "WARNING", "HIGH", "CRITICAL"]
        if order.index(severity) < order.index(self.min):
            return False
        return any([n.send(severity, title, body, payload) for n in self.notifiers])
