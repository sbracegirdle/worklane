# Storage decision: SQLite for all Worklane data

Worklane uses SQLite for all of its durable runtime data, including controller
output, agent events, results, issue/PR details and session mappings. There are no
persistent JSON, JSONL, schema, result or log files, and no JSON payload columns.
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

- `tasks`: issue number, indexed phase and timestamp.
- `state_values`: typed relational rows for nested task details, settings,
  structured results and event fields. Containers, parent/child relationships,
  keys and scalar types are preserved without serializing JSON blobs.
- `sessions`: issue, role, exact session ID, backend/model, stable worktree and
  timestamps. `(issue, role)` is unique, and a session ID cannot belong to two roles.
- `runs`: one invocation per row, including role/stage, session ID, process identity,
  status, timing, worktree and error. Results reference typed database values.
- `run_events`: event identity, invocation, type and timestamp; all event fields
  are stored as typed database values.
- `logs`: controller output, stderr and readable agent output. `worklane logs`
  reads this table; `worklane logs NUMBER` filters it by issue.
- `migration_archive`: original bytes of imported legacy files, retained inside
  SQLite before their external copies are removed.

Runtime settings use typed database values too. The schema version is SQLite's
`user_version`. Empty process-lock files and Git worktrees remain on disk as
operational resources; they are not parallel state/log stores.

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

The received `thread.started` event, readable log entry and matching session-ID
mapping commit together. A failed mapping check rolls back all of them; no
separate transcript write needs replaying. If a previous invocation has no recoverable ID, the tool blocks
rather than silently creating a new conversation. A missing Codex transcript or
missing worktree is also a blocker. Worklane's database alone does not contain a
complete copy of Codex's conversation storage.

Interrupted owned assignments can continue while already In Progress or Agent
review. New worker intake still requires Ready for work. Resuming uses the stored
stage and exact worktree; review worktrees are reused across rounds, advancing to
the new reviewed commit only when tracked files are clean. Human board changes,
closed issues and changed PR heads are checked before work continues.

## Migration and operational boundaries

Imports from the first JSON format and the intermediate mixed SQLite/file format
are transactional and idempotent. Original file bytes are archived inside SQLite;
external files are removed only after a successful commit and a content check.
If cleanup is interrupted, the next startup completes it. Explicit stage-labelled logs
can supply a known session ID. The first release did not label every log's role,
so migration does not guess ownership for those old logs. Stop old controllers
before upgrading and do not run the old JSON writer against migrated state.

Codex and GitHub still exchange JSON over their interfaces. For Codex's required
schema and output paths, Linux `memfd_create` descriptors expose in-memory files
through `/proc/self/fd`. Both descriptors are inherited by the child, then closed
after the parsed result is stored in SQLite. They leave no temporary JSON files,
even if a process is killed. Codex's own conversation-history storage is owned by
Codex; Worklane does not replace that backend's transcript implementation.

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
