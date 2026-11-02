"""An in-memory stand-in for the parts of Microsoft Graph a mailbox backup touches.

It behaves like the real delta API where it matters for backup correctness:
- `GET /users/{user}/messages/delta` returns pages with `@odata.nextLink`, and the last page carries an
  `@odata.deltaLink` whose token marks a position in the change log;
- deletions come back as `{"id": ..., "@removed": {"reason": "deleted"}}`;
- a delta token older than the retained change log returns **410 Gone** (`syncStateNotFound`), forcing a full resync;
- it can throttle (**429** with `Retry-After`) and expire access tokens (**401**) on demand.

Served through `httpx.MockTransport`, so nothing touches the network and no Microsoft account is needed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx


@dataclass
class Message:
    id: str
    subject: str
    body: str
    version: int = 1


@dataclass
class FakeGraph:
    user: str = "alex@contoso.example"
    page_size: int = 3
    retained_changes: int = 50           # how far back a delta token stays valid
    messages: dict[str, Message] = field(default_factory=dict)
    changes: list[tuple[int, str]] = field(default_factory=list)   # (sequence, message id)
    seq: int = 0
    valid_tokens: set[str] = field(default_factory=lambda: {"token-1"})
    throttle_next: int = 0               # this many upcoming requests get 429
    retry_after: int = 2
    requests: int = 0
    _next_id: int = 0

    # --- mailbox changes (what the user does between backups) ---------------------------------------
    def add(self, subject: str, body: str) -> str:
        self._next_id += 1
        mid = f"AAMk{self._next_id:05d}"
        self.messages[mid] = Message(mid, subject, body)
        self._log(mid)
        return mid

    def edit(self, mid: str, body: str) -> None:
        m = self.messages[mid]
        m.body, m.version = body, m.version + 1
        self._log(mid)

    def delete(self, mid: str) -> None:
        del self.messages[mid]
        self._log(mid)

    def _log(self, mid: str) -> None:
        self.seq += 1
        self.changes.append((self.seq, mid))

    def snapshot(self) -> dict[str, str]:
        """What the mailbox looks like right now: the ground truth a restore must match."""
        return {m.id: f"{m.subject}\n\n{m.body}" for m in self.messages.values()}

    # --- auth -----------------------------------------------------------------------------------------
    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    def issue_token(self) -> str:
        tok = f"token-{len(self.valid_tokens) + self.requests + 2}"
        self.valid_tokens.add(tok)
        return tok

    # --- HTTP -----------------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"Retry-After": str(self.retry_after)},
                                  json={"error": {"code": "TooManyRequests"}})
        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if not request.url.path.endswith("/messages/delta"):
            return httpx.Response(404)
        q = request.url.params
        if "$deltatoken" in q:
            since = int(q["$deltatoken"])
            oldest_kept = self.seq - self.retained_changes
            if since < oldest_kept:
                return httpx.Response(410, json={"error": {"code": "syncStateNotFound",
                                                           "message": "Resync required"}})
            ids = list(dict.fromkeys(mid for s, mid in self.changes if s > since))
            return self._page(ids, 0, request)
        if "$skiptoken" in q:
            state = json.loads(q["$skiptoken"])
            return self._page(state["ids"], state["offset"], request)
        return self._page(sorted(self.messages), 0, request)   # initial / full sync

    def _page(self, ids: list[str], offset: int, request: httpx.Request) -> httpx.Response:
        chunk = ids[offset: offset + self.page_size]
        value = []
        for mid in chunk:
            m = self.messages.get(mid)
            value.append({"id": mid, "@removed": {"reason": "deleted"}} if m is None else
                         {"id": m.id, "subject": m.subject, "body": {"content": m.body}, "changeKey": str(m.version)})
        base = f"https://graph.example/v1.0/users/{self.user}/messages/delta"
        out: dict = {"value": value}
        if offset + self.page_size < len(ids):
            skip = json.dumps({"ids": ids, "offset": offset + self.page_size})
            out["@odata.nextLink"] = str(httpx.URL(base, params={"$skiptoken": skip}))
        else:
            out["@odata.deltaLink"] = str(httpx.URL(base, params={"$deltatoken": str(self.seq)}))
        return httpx.Response(200, json=out)
