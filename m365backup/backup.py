"""One incremental backup run for one mailbox."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .client import GraphClient, GraphError
from .store import Store


@dataclass
class RunReport:
    snapshot_id: int
    kind: str                   # "full" | "incremental" | "full (delta expired)"
    pages: int = 0
    changed: int = 0
    deleted: int = 0
    new_blobs: int = 0
    reconciled_deletes: int = 0
    client_stats: dict = field(default_factory=dict)


def _content(item: dict) -> bytes:
    return f"{item.get('subject', '')}\n\n{item.get('body', {}).get('content', '')}".encode()


def run_backup(client: GraphClient, store: Store, mailbox: str, base_url: str,
               on_page: Callable[[int], None] = lambda n: None) -> RunReport:
    last = store.last_committed(mailbox)
    url = last[1] if last else f"{base_url}/users/{mailbox}/messages/delta"
    kind = "incremental" if last else "full"
    try:
        first = client.get(url)
    except GraphError as e:
        if e.status != 410:
            raise
        # The delta token is too old (the tenant's change log moved on). Start a full resync; items we
        # hold that the full listing no longer contains were deleted while we weren't looking.
        url, kind = f"{base_url}/users/{mailbox}/messages/delta", "full (delta expired)"
        first = client.get(url)

    snap = store.begin(mailbox, kind)
    rep = RunReport(snap, kind)
    seen: set[str] = set()
    page = first
    while True:
        rep.pages += 1
        for item in page["value"]:
            seen.add(item["id"])
            if "@removed" in item:
                store.put_version(snap, item["id"], None)
                rep.deleted += 1
            else:
                rep.new_blobs += store.put_version(snap, item["id"], _content(item))
                rep.changed += 1
        store.flush()
        on_page(rep.pages)                      # tests use this to simulate a crash mid-run
        if "@odata.nextLink" in page:
            page = client.get(page["@odata.nextLink"])
            continue
        delta_link = page["@odata.deltaLink"]
        break

    if kind == "full (delta expired)" and last:
        for gone in store.known_items(mailbox, last[0]) - seen:
            store.put_version(snap, gone, None)
            rep.reconciled_deletes += 1
    store.commit(snap, delta_link)              # versions and the new delta link become visible together
    rep.client_stats = dict(client.stats)
    return rep
