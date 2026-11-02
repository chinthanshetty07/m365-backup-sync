"""Five backup runs against a changing fake mailbox, with throttling, a token expiry, a crash and an expired
delta token along the way. Every snapshot is restored and checked against what the mailbox looked like then.

    python -m m365backup.demo
"""
from __future__ import annotations

from .backup import run_backup
from .client import GraphClient
from .fakegraph import FakeGraph
from .store import Store

BASE = "https://graph.example/v1.0"


def main() -> list[dict]:
    g = FakeGraph(page_size=4, retained_changes=12)
    store = Store()
    client = GraphClient(g.transport(), g.issue_token, sleep=lambda s: None)
    truth: dict[int, dict] = {}
    ids = [g.add(f"Invoice {i}", f"Amount due: {100 * i}. Attachment hash {i * 7919}") for i in range(1, 11)]
    log = []

    def run(label):
        rep = run_backup(client, store, g.user, BASE)
        truth[rep.snapshot_id] = g.snapshot()
        log.append({"run": label, "kind": rep.kind, "pages": rep.pages, "changed": rep.changed, "deleted": rep.deleted,
                    "new_blobs": rep.new_blobs, "reconciled_deletes": rep.reconciled_deletes})

    run("1 initial full")
    g.edit(ids[0], "Amount due: 100. PAID"); g.delete(ids[1]); g.add("Welcome", "Hello team")
    g.throttle_next = 2
    run("2 incremental, throttled twice")
    g.add("Board deck", "Q3 numbers"); g.expire_tokens()
    run("3 incremental, token expired")
    for i in range(15):
        g.edit(ids[3], f"revision {i}")
    g.delete(ids[4])
    run("4 delta token expired -> full resync")
    g.add("Late reply", "Sorry for the delay")
    run("5 incremental")

    for snap_id, expected in truth.items():
        assert store.state_at(g.user, snap_id) == expected, f"snapshot {snap_id} does not restore correctly"
    for row in log:
        print(row)
    s = store.stats()
    print(f"\nAll {len(truth)} snapshots restore exactly. Stored {s['blob_bytes']} bytes in {s['blobs']} blobs for "
          f"{s['logical_bytes']} logical bytes across {s['versions']} item versions "
          f"(dedup {s['logical_bytes'] / max(s['blob_bytes'], 1):.1f}x). Client: {client.stats}")
    return log


if __name__ == "__main__":
    main()
