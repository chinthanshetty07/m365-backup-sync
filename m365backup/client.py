"""A Graph client that survives what real tenants do to backup jobs: throttling, expiring tokens, flaky servers."""
from __future__ import annotations

import time
from typing import Callable

import httpx


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"{status} {code}")
        self.status, self.code = status, code


class GraphClient:
    def __init__(self, transport: httpx.BaseTransport, get_token: Callable[[], str],
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 5) -> None:
        self.http = httpx.Client(transport=transport, timeout=30)
        self.get_token = get_token
        self.sleep = sleep
        self.max_retries = max_retries
        self.token = get_token()
        self.stats = {"requests": 0, "throttled": 0, "token_refreshes": 0, "waited_s": 0.0}

    def get(self, url: str) -> dict:
        refreshed = False
        for attempt in range(self.max_retries + 1):
            self.stats["requests"] += 1
            r = self.http.get(url, headers={"Authorization": f"Bearer {self.token}"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and not refreshed:   # one refresh per call; a second 401 is a real auth problem
                self.token, refreshed = self.get_token(), True
                self.stats["token_refreshes"] += 1
                continue
            if r.status_code in (429, 503, 504) and attempt < self.max_retries:
                # Graph tells us how long to back off; without a header, back off exponentially.
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                self.stats["throttled"] += 1
                self.stats["waited_s"] += wait
                self.sleep(wait)
                continue
            code = (r.json().get("error", {}).get("code", "") if r.headers.get("content-type", "").startswith("application/json") else "")
            raise GraphError(r.status_code, code)
        raise GraphError(429, "RetriesExhausted")
