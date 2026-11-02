"""Backup storage: content-addressed blobs, append-only item versions, and snapshots that only count once committed.

- Blobs are keyed by SHA-256, so an unchanged message is stored once however many snapshots include it.
- Versions are only ever inserted, never updated or deleted: a later backup can't damage an earlier one.
- A snapshot is `running` until the whole delta round finishes; its versions and its new delta link become
  visible together when it is committed. A crash mid-run leaves a `running` snapshot that restores ignore
  and the next run starts again from the last committed delta link.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (sha256 TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, mailbox TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL,
    kind TEXT NOT NULL, delta_link TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), item_id TEXT NOT NULL,
    sha256 TEXT REFERENCES blobs(sha256), deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, item_id)
);
CREATE TRIGGER IF NOT EXISTS versions_append_only_u BEFORE UPDATE ON versions
BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_append_only_d BEFORE DELETE ON versions
BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # --- snapshots -------------------------------------------------------------------------------------
    def last_committed(self, mailbox: str) -> tuple[int, str] | None:
        row = self.db.execute("SELECT id, delta_link FROM snapshots WHERE mailbox=? AND status='committed' "
                              "ORDER BY id DESC LIMIT 1", (mailbox,)).fetchone()
        return (row[0], row[1]) if row else None

    def begin(self, mailbox: str, kind: str) -> int:
        cur = self.db.execute("INSERT INTO snapshots (mailbox, started_at, status, kind) VALUES (?,?,?,?)",
                              (mailbox, datetime.now(timezone.utc).isoformat(), "running", kind))
        self.db.commit()
        return cur.lastrowid

    def commit(self, snapshot_id: int, delta_link: str) -> None:
        self.db.execute("UPDATE snapshots SET status='committed', delta_link=? WHERE id=?", (delta_link, snapshot_id))
        self.db.commit()

    def snapshots(self, mailbox: str) -> list[dict]:
        rows = self.db.execute("SELECT id, status, kind FROM snapshots WHERE mailbox=? ORDER BY id", (mailbox,))
        return [{"id": r[0], "status": r[1], "kind": r[2]} for r in rows]

    # --- data ------------------------------------------------------------------------------------------
    def put_version(self, snapshot_id: int, item_id: str, content: bytes | None) -> bool:
        """Records an item's state in this snapshot. Returns True if new blob bytes had to be written."""
        if content is None:
            self.db.execute("INSERT OR IGNORE INTO versions (snapshot_id, item_id, deleted) VALUES (?,?,1)",
                            (snapshot_id, item_id))
            return False
        digest = sha256(content)
        new_blob = self.db.execute("INSERT OR IGNORE INTO blobs (sha256, data) VALUES (?,?)", (digest, content)).rowcount == 1
        self.db.execute("INSERT OR IGNORE INTO versions (snapshot_id, item_id, sha256) VALUES (?,?,?)",
                        (snapshot_id, item_id, digest))
        return new_blob

    def flush(self) -> None:
        self.db.commit()

    def state_at(self, mailbox: str, snapshot_id: int) -> dict[str, str]:
        """The mailbox as of a committed snapshot: each item's latest version up to it, minus deletions."""
        rows = self.db.execute("""
            SELECT v.item_id, v.deleted, b.data FROM versions v
            JOIN snapshots s ON s.id = v.snapshot_id AND s.status = 'committed' AND s.mailbox = ?
            LEFT JOIN blobs b ON b.sha256 = v.sha256
            WHERE v.snapshot_id = (
                SELECT MAX(v2.snapshot_id) FROM versions v2
                JOIN snapshots s2 ON s2.id = v2.snapshot_id AND s2.status = 'committed' AND s2.mailbox = ?
                WHERE v2.item_id = v.item_id AND v2.snapshot_id <= ?)
        """, (mailbox, mailbox, snapshot_id))
        return {item: data.decode() for item, deleted, data in rows if not deleted}

    def known_items(self, mailbox: str, snapshot_id: int) -> set[str]:
        return set(self.state_at(mailbox, snapshot_id))

    def stats(self) -> dict:
        blobs, blob_bytes = self.db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)),0) FROM blobs").fetchone()
        versions = self.db.execute("SELECT COUNT(*) FROM versions WHERE deleted=0").fetchone()[0]
        logical = self.db.execute("SELECT COALESCE(SUM(LENGTH(b.data)),0) FROM versions v JOIN blobs b ON b.sha256=v.sha256").fetchone()[0]
        return {"blobs": blobs, "blob_bytes": blob_bytes, "versions": versions, "logical_bytes": logical}
