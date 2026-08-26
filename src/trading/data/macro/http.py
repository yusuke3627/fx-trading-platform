"""Minimal JSON HTTP transport for the statistical-agency collectors.

Injected into every collector so parsing runs against fakes in tests; only
this module touches the network. A transport failure raises — like the tick
collector, an outage must not read as "no data", so retry policy stays with
the host scheduler.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30.0
# BOE and ONS answer 403 to urllib's default agent string (observed live
# 2026-08-26); a named non-browser agent passes and tells the agencies who
# is calling.
USER_AGENT = "fx-trading-platform-collector/1.0"


def _parse_json(raw: bytes, url: str) -> Any:
    # Some agencies answer errors as an HTML page with HTTP 200 (observed
    # live: Census "Missing Key"), so a parse failure must show what came
    # back instead of a bare JSONDecodeError.
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"non-JSON response from {url}: {raw[:120]!r}") from exc


class HttpTransport:
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def _fetch(self, request: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def get_bytes(self, url: str) -> bytes:
        """Raw fetch for non-JSON sources (MOF publishes Shift_JIS CSV and
        HTML, the BOE database plain CSV); the caller owns decoding."""
        return self._fetch(
            urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        )

    def get_json(self, url: str, params: dict[str, str | list[str]]) -> Any:
        # doseq expands list values into repeated parameters (Eurostat's
        # geo=EA21&geo=EA20).
        query = urllib.parse.urlencode(params, doseq=True)
        target = f"{url}?{query}" if query else url
        raw = self._fetch(
            urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        )
        return _parse_json(raw, url)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        return _parse_json(self._fetch(request), url)
