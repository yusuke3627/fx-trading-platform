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


class HttpTransport:
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def get_json(self, url: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        with urllib.request.urlopen(
            f"{url}?{query}", timeout=self._timeout
        ) as response:
            return json.loads(response.read())

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request, timeout=self._timeout
        ) as response:
            return json.loads(response.read())
