import sqlite3

import pytest

from m365backup.backup import run_backup
from m365backup.client import GraphClient, GraphError
from m365backup.fakegraph import FakeGraph
from m365backup.store import Store

BASE = "https://graph.example/v1.0"


@pytest.fixture
def env():
    g = FakeGraph(page_size=3, retained_changes=10)
    waits = []
    client = GraphClient(g.transport(), g.issue_token, sleep=waits.append)
    ids = [g.add(f"Subject {i}", f"Body {i}") for i in range(7)]
    return g, Store(), client, ids, waits


def backup(env, **kw):
    g, store, client, _, _ = env
    return run_backup(client, store, g.user, BASE, **kw)


def restored(env, snap):
    g, store, *_ = env
    return store.state_at(g.user, snap)


def test_first_run_is_a_full_paged_sync(env):
    g = env[0]
    rep = backup(env)
    assert (rep.kind, rep.pages, rep.changed) == ("full", 3, 7)
    assert restored(env, rep.snapshot_id) == g.snapshot()


def test_incremental_picks_up_only_changes(env):
    g, _, _, ids, _ = env
    backup(env)
    g.edit(ids[0], "edited")
    g.delete(ids[1])
    new = g.add("New", "fresh")
    rep = backup(env)
    assert rep.kind == "incremental"
    assert (rep.changed, rep.deleted) == (2, 1)
    state = restored(env, rep.snapshot_id)
    assert state == g.snapshot()
    assert ids[1] not in state and new in state


def test_point_in_time_restore_of_every_snapshot(env):
    g, _, _, ids, _ = env
    truth = {}
    for step in range(4):
        rep = backup(env)
        truth[rep.snapshot_id] = g.snapshot()
        g.edit(ids[step], f"v{step}")
        if step == 2:
            g.delete(ids[5])
    for snap, expected in truth.items():
        assert restored(env, snap) == expected


def test_unchanged_content_is_stored_once(env):
    g, store, _, ids, _ = env
    backup(env)
    before = store.stats()["blobs"]
    g.edit(ids[0], "x"); g.edit(ids[0], "Body 0")      # back to identical bytes
    rep = backup(env)
    assert rep.changed == 1 and rep.new_blobs == 0
    assert store.stats()["blobs"] == before


def test_throttling_honours_retry_after(env):
    g, _, client, _, waits = env
    g.throttle_next, g.retry_after = 2, 7
    rep = backup(env)
    assert waits == [7.0, 7.0]
    assert client.stats["throttled"] == 2
    assert restored(env, rep.snapshot_id) == g.snapshot()


def test_gives_up_after_max_retries(env):
    g, _, client, _, _ = env
    g.throttle_next = 99
    with pytest.raises(GraphError):
        backup(env)


def test_expired_access_token_is_refreshed_once(env):
    g, _, client, _, _ = env
    backup(env)
    g.add("after expiry", "x")
    g.expire_tokens()
    rep = backup(env)
    assert client.stats["token_refreshes"] == 1
    assert rep.changed == 1


def test_persistent_401_is_an_error_not_a_loop(env):
    g, store, _, _, _ = env
    client = GraphClient(g.transport(), lambda: "never-valid", sleep=lambda s: None)
    with pytest.raises(GraphError) as e:
        run_backup(client, store, g.user, BASE)
    assert e.value.status == 401


def test_expired_delta_token_triggers_full_resync_and_finds_deletions(env):
    g, _, _, ids, _ = env
    backup(env)
    for i in range(12):                      # push the change log past what the token can cover
        g.edit(ids[2], f"rev {i}")
    g.delete(ids[3])                         # this deletion would be invisible to a plain full listing
    rep = backup(env)
    assert rep.kind == "full (delta expired)"
    assert rep.reconciled_deletes == 1
    assert restored(env, rep.snapshot_id) == g.snapshot()


def test_crash_mid_run_leaves_no_partial_snapshot_and_next_run_recovers(env):
    g, store, _, ids, _ = env
    first = backup(env)
    for i in range(5):
        g.edit(ids[i], f"changed {i}")

    def crash(page):
        if page == 1:
            raise RuntimeError("worker killed")

    with pytest.raises(RuntimeError):
        backup(env, on_page=crash)
    statuses = [s["status"] for s in store.snapshots(g.user)]
    assert statuses == ["committed", "running"]
    assert restored(env, first.snapshot_id) != g.snapshot()   # old snapshot untouched

    rep = backup(env)                                          # restarts from the last committed delta link
    assert rep.changed == 5
    assert restored(env, rep.snapshot_id) == g.snapshot()


def test_versions_are_append_only(env):
    g, store, *_ = env
    backup(env)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("UPDATE versions SET deleted=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("DELETE FROM versions")


def test_running_snapshot_is_invisible_to_restore(env):
    g, store, *_ = env
    snap = store.begin(g.user, "incremental")
    store.put_version(snap, "AAMk99999", b"half-written")
    assert "AAMk99999" not in store.state_at(g.user, snap)


def test_mailboxes_are_isolated(env):
    g, store, client, *_ = env
    backup(env)
    other = FakeGraph(user="sam@contoso.example")
    other.add("Only Sam", "private")
    run_backup(GraphClient(other.transport(), other.issue_token, sleep=lambda s: None), store, other.user, BASE)
    last_alex = store.last_committed(g.user)[0]
    assert "Only Sam" not in "".join(store.state_at(g.user, last_alex).values())
