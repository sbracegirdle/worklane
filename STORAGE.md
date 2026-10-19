# Storage decision: SQLite metadata, JSONL transcripts

Worklane uses SQLite for controller state and keeps streamed agent output as files.
This remains a local CLI with no server or additional Python runtime dependency:
the implementation uses Python's standard-library `sqlite3` module.

| Choice | Strengths | Limitations here |
| --- | --- | --- |
| Separate JSON files | Easy to inspect, simple atomic replacement of one file | Task, session, run and control updates cannot be committed together; history and cross-role uniqueness need custom locking and bookkeeping |
| SQLite | Transactions, constraints, indexed run history, concurrent readers, short queued writes | A database migration is needed; live backups must include pending WAL data |
| PostgreSQL or another server | Appropriate for distributed controllers and many concurrent writers | Requires a service and configuration that this local tool does not need |

SQLite fits the current workload: one controller per repository, three workers and
one reviewer slot, with short metadata writes. SQLite's own guidance recommends it
for device-local storage with low writer concurrency; it allows one writer at a
time, with writers taking turns. This is a design judgment for this tool, not a
claim that SQLite would coordinate multiple distributed queue owners.

## What is stored

- `tasks`: issue number, indexed phase, flexible JSON payload, timestamp. The JSON
  payload keeps issue/PR evidence extensible, inside a database transaction rather
  than as an independently replaced file.
- `sessions`: issue, role, exact session ID, backend/model, stable worktree and
  timestamps. `(issue, role)` is unique, and a session ID cannot belong to two roles.
- `runs`: one invocation per row, including role/stage, session ID, process identity,
  status, log/result paths, timing, structured result and error.
- `metadata`: controller runtime, pause/shutdown state and migration/schema version.

Each thread opens a short-lived connection. WAL mode permits readers alongside
writes; writers wait up to 30 seconds. Foreign keys and uniqueness constraints
protect relationships. `synchronous=FULL` asks SQLite to sync commits for durability.
No transaction spans an agent turn or network operation.

Creating a run and recording its active stage are one transaction. Recording the
session and binding it to the run are another transaction, committed immediately
when Codex emits `thread.started`. A stale task save cannot overwrite the normalized
session mapping. Completed invocations retain their structured results in the run
ledger even if the controller exits before its next workflow update.

## Sessions and recovery

Codex owns conversation transcripts; Worklane owns the mapping from each issue's
worker/reviewer/finalizer role to its exact Codex session ID. Subsequent invocations
use `codex exec resume <ID>`, never the machine's most recent session. Reviewer
history remains separate from implementation history. Finalization has its own
resumable session because it may prepare local integration artifacts.

The JSONL log is flushed and synced before the matching session-ID transaction.
If the controller crashes between those operations, startup replays that event
from the log. If a previous invocation has no recoverable ID, the tool blocks
rather than silently creating a new conversation. A missing Codex transcript or
missing worktree is also a blocker. Worklane's database alone does not contain a
complete copy of Codex's conversation storage.

Interrupted owned assignments can continue while already In Progress or Agent
review. New worker intake still requires Ready for work. Resuming uses the stored
stage and exact worktree; review worktrees are reused across rounds, advancing to
the new reviewed commit only when tracked files are clean. Human board changes,
closed issues and changed PR heads are checked before work continues.

## Migration and operational boundaries

The one-time JSON import is transactional and idempotent. Original JSON files are
left untouched; later runs use SQLite exclusively. Explicit stage-labelled logs
can supply a known session ID. The first release did not label every log's role,
so migration does not guess ownership for those old logs. Stop old controllers
before upgrading and do not run the old JSON writer against migrated state.

The existing local process lock still limits one controller and is inherited by
agents. SQLite does not replace that lock or make GitHub status updates atomic.
GitHub writes and local commits cannot form one database transaction: startup
reconciles already-completed handoffs/rework transitions where recorded evidence
supports them, and otherwise stops for inspection. There is no exactly-once
guarantee across a GitHub network timeout.

Keep state on a local filesystem. Back up with SQLite's backup API or while the
controller is stopped; copying only the main file of a live WAL database can omit
recent writes. Restore Codex's local session storage and referenced worktrees too.

## Primary references

- [SQLite: Appropriate uses](https://www.sqlite.org/whentouse.html)
- [SQLite: Write-ahead logging](https://www.sqlite.org/wal.html)
- [Python sqlite3 documentation](https://docs.python.org/3/library/sqlite3.html)
- [Codex: Non-interactive mode, JSON events and session resume](https://learn.chatgpt.com/docs/non-interactive-mode)
