# M365 Backup Sync

[![CI](https://github.com/chinthanshetty07/m365-backup-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/chinthanshetty07/m365-backup-sync/actions/workflows/ci.yml)

An incremental backup engine for Microsoft 365 mailboxes, built around the Microsoft Graph **delta query**
pattern, with point-in-time restore that is checked against the source after every run.

Built as an independent learning project after applying for a SaaS O365 data-protection role. It is not
affiliated with Rubrik or Microsoft. It runs against an in-memory fake of the Graph delta API, so no tenant,
account or network is needed, and all mailbox data is made up.

## What it handles

| Situation | What happens |
|---|---|
| First run | Full sync, paged with `@odata.nextLink`; the final page's `@odata.deltaLink` is saved for next time |
| Later runs | Only changes since the saved delta link: new, edited and deleted (`@removed`) messages |
| Throttling (`429`, `503`, `504`) | Waits exactly the `Retry-After` the service asks for (exponential backoff if it gives none), then gives up after a limit instead of looping |
| Expired access token (`401`) | Refreshes once per call; a second `401` is reported as a real auth failure, not retried forever |
| Expired delta token (`410 Gone`, `syncStateNotFound`) | Falls back to a full resync, then **reconciles**: anything the previous snapshot had that the full listing no longer contains was deleted while the token was stale, and is recorded as deleted. A naive resync would silently keep those items forever. |
| Worker killed mid-run | The snapshot stays `running`; restores ignore it, and the next run starts again from the last **committed** delta link. Nothing is lost or half-visible. |
| Unchanged content | Stored once: blobs are keyed by SHA-256, so an item that changes and changes back costs nothing new |
| A later run going wrong | Item versions are append-only, enforced by database triggers (`UPDATE`/`DELETE` abort), so a bad run can't damage an earlier backup |

## Design

```
GraphClient (retry/backoff, token refresh)
   -> run_backup: follow nextLinks, record each item's version in a new snapshot
   -> Store: blobs (sha256 -> bytes), versions (snapshot, item, sha256 | deleted), snapshots (running|committed, deltaLink)
   -> state_at(snapshot): latest committed version of each item up to that snapshot, minus deletions
```

The one rule everything depends on: **a snapshot's versions and its new delta link become visible in the same
commit.** If the delta link were saved before the versions (or vice versa), a crash in between would either skip
changes forever or replay them into the wrong snapshot.

## Demo

```bash
pip install -r requirements-dev.txt
python -m m365backup.demo
```

```
run 1 initial full                       full                  3 pages, 10 changed
run 2 incremental, throttled twice       incremental           2 changed, 1 deleted
run 3 incremental, token expired         incremental           1 changed
run 4 delta token expired -> resync      full (delta expired)  10 listed, 1 new blob, 1 reconciled delete
run 5 incremental                        incremental           1 changed

All 5 snapshots restore exactly. Stored 617 bytes in 15 blobs for 986 logical bytes across 24 item versions
(dedup 1.6x). Client: 13 requests, 2 throttled, 1 token refresh.
```

Every snapshot is restored after the run and compared with what the fake mailbox actually looked like at that
moment. In the resync run, only 1 of 10 listed messages needed new storage: the others were byte-identical.

## Tests (`pytest -q`, 13)

Full paged sync; incremental changes and deletions; point-in-time restore of every snapshot; dedup of identical
content; `Retry-After` honoured; retry limit; single token refresh; persistent `401` fails fast; `410` resync with
deletion reconciliation; crash mid-run leaves no partial snapshot and the next run recovers; versions are
append-only at the database level; running snapshots invisible to restore; mailboxes isolated from each other.

## What a real product needs that this doesn't have

- Real Graph auth (client-credentials flow per tenant) and the other workloads: OneDrive, SharePoint, Teams, calendars.
- Attachments and large items via chunked download, and per-item failure handling so one poison item doesn't
  stall a mailbox.
- Parallelism across thousands of mailboxes with per-tenant throttling budgets.
- Encryption at rest, retention policies, immutable object storage instead of SQLite, and restore back into the tenant.
- Change notifications (webhooks) to trigger syncs instead of polling.

Built with Claude Code; the tests and the restore check after every demo run are how I checked what it wrote.
