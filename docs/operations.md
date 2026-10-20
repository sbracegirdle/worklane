# Operations

Uses authenticated [Codex or OpenCode harnesses](harnesses.md), with global defaults and per-role overrides.
Linux and Python 3.11+ are supported; no Python runtime dependencies or stored access tokens.

See the [README](../README.md#run) for basic usage and controller commands.

## Board and handoff behavior

The repository is inferred from the checkout's `origin` remote. Only github.com
HTTPS/SSH remotes are supported. Worklane looks for a linked project, preferring a
project whose title matches the repository. If none is linked it considers the
repository owner's projects. When there is more than one candidate, choose with
`--project OWNER/NUMBER`; Worklane does not silently choose an ambiguous board.
Public boards require `--dangerously-allow-public-board`; `check` reports safety and project access/automation verification limits.
Feedback requires current repository Write/Maintain/Admin authors and editors; dispatch and writes revalidate security and fail closed.

The Status field must already contain these names (case-insensitive):

```text
Ready for work -> In Progress -> Agent review -> Human review
                       ^              |
                       +-- rework <---+
```

Rework goes back to **Ready for work** so workers only ever claim that status.
The first run adds **Agent review** immediately before Human review, preserving
existing option IDs, colors and descriptions. `check` never changes the board.
Native GitHub “blocked by” dependencies prevent assignment and recovery until all blockers close; checks fail closed.
Only open issues belonging to the current repository are eligible. Board API item
order determines pickup order; custom view sort/filter order is not available
through this CLI. Projects larger than 10,000 items are refused rather than read
partially.

Workers take one issue each, move it to In Progress, create isolated worktrees and
`codex/NUMBER-worklane` branches, implement/test/document it and open/update a PR
against the default branch. The controller verifies the open PR, pushed head SHA
and clean worker checkout before moving the issue to Agent review. Existing linked
PRs are not claimed as new implementation work. A question-labelled issue produces
reviewable investigation findings without requiring a PR.

The reviewer independently checks acceptance criteria and tests against the exact
PR head. Changed heads, blocked checks, unresolved findings and incomplete handoffs
cannot advance to Human review. Review findings are posted to the PR (or question
issue). Workers verify each fix and resolve its review thread;
unresolved threads prevent advancement to Human review. Failed reviews return to Ready for work on the same branch, with findings
included in the next worker's input. After three failed review rounds, the task is
blocked for inspection instead of looping indefinitely.
Merge conflicts return to workers; feedback arriving during implementation remains pending after handoff.

After a pass, the same reviewer slot completes any repository-required local
preview/integration/playground handoff, then moves the issue to Human review.
Draft PRs are promoted and verified ready before entering Human review; promotion failures block advancement.
This keeps local integration changes serialized. The tool does not automatically
merge PRs, publish, deploy or mark issues Done. New human feedback on owned Human
review issues is batched until five quiet minutes, then resumes the original worker
conversation. New comments during that wait extend the batch. Move an issue back to
Ready for work to request another implementation pass manually.
If separate legacy feedback automations are enabled, avoid giving them concurrent
ownership of an issue Worklane is handling.

## Add issues

`worklane add` creates a regular repository issue and places it on the selected
project board. The title is required; the description is optional (empty by
default), accepts multiline text, and also supports `--body` as an alias.
Repository and board selection follow the same rules as `check` and `run`;
`--repo` and `--project OWNER/NUMBER` override inference.

New issues enter **Ready for work** by default, so a running controller may pick
them up immediately. If pickup advances the issue to In Progress, Agent review,
or Human review before verification, `add` still reports successful initial
placement and preserves the newer status. `--draft` and `--shaping` choose the
board's **Draft** and **Shaping** statuses instead; these flags cannot be combined.
Both still create regular repository issues, and verification requires the selected
status for those destinations.

`add` requires authenticated `gh` with issue/project write access and the selected
Status option already on the board (case-insensitive). It validates the destination
before creating the issue and does not add or rename board fields. It does not
require Codex, start workers, or change controller state. Adding an issue does not
require the controller's other workflow statuses.

Issue creation and board placement are separate GitHub operations. If placement
fails, the command exits unsuccessfully and prints the created issue's URL. Keep
that issue and finish its placement on the named board manually; rerunning `add`
would create another issue. If creation itself times out, check GitHub before
retrying because the request may already have succeeded.

## State, recovery and limits

GitHub requests use targeted, paginated reads and share a persisted cooldown
across controllers using the same state root; agent subprocesses and other
machines are outside that coordination. Quota failures honor reset/retry headers,
otherwise backing off from one minute to one hour, and resume saved work without
repeating completed agent turns. Unchanged Human review feedback is checked every
five minutes; pending feedback retains the controller interval. Security preflights add live reads before dispatch, mutations, and agent turns.
Live GraphQL costs and the conflicting REST/GraphQL quota reports remain unverified.

Runtime data lives outside project checkouts:

```text
~/.local/state/worklane/OWNER--REPO/
  state.sqlite3        # All Worklane state, events, logs, results and session IDs
  worktrees/           # Preserved worker and detached review checkouts
```

SQLite stores durable runtime data; [checkout configuration](configuration.md) lives in `.worklane.toml`. Tasks, nested issue/PR data,
events, results, controller output, agent output, controls and session mappings
are stored in database tables. There are no JSON payload columns or persistent
JSON, JSONL, result, schema or log files. JSON is only an API/CLI exchange format;
Codex's required schema/result paths use memory-backed file descriptors.

Existing JSON state and mixed SQLite/file storage are migrated transactionally.
Original file bytes are archived inside SQLite before the files are removed, so
rollback evidence is retained without leaving separate storage files. The old
controller must be stopped before this migration.
Unlabelled old logs are not guessed into a worker/reviewer session: uncertain
legacy conversation ownership requires inspection. New runs record their role
and session explicitly. See [the storage decision](storage.md) for the comparison.

`XDG_STATE_HOME` changes the base directory. All checkouts of the same repository
share this state and one controller lock on the current machine. The lock is
inherited by agent processes so a controller crash cannot immediately start a
second team over orphaned workers. There is no distributed lock between machines:
run one controller per repository on one machine. A manual board status change is
checked before a transition; GitHub Projects does not provide atomic compare-and-
swap claims, so do not run a second unrelated dispatcher on the same queue.

Each issue keeps separate **worker**, **reviewer**, and **finalizer** session IDs.
Review feedback resumes the original worker conversation, even when another worker
slot picks it up. Subsequent review rounds resume that issue's reviewer conversation
in its stable review worktree. The finalizer has a separate resumable conversation
so implementation, independent review and local preview actions do not share history.

Codex sessions use names like `owner/repo #123 [worker]: Fix login`.
Worklane groups them in the checkout's Codex project, creating it if needed.

Worklane saves `thread.started` IDs immediately, before waiting for the turn to
finish. It uses `codex exec resume <EXACT_ID>` for subsequent turns, never `--last`.
Starting another process does not create another conversation. Session transcripts
remain managed by Codex in its own local storage; keep that storage, the Worklane
database and worktrees when moving or backing up an installation.

`stop` drains active jobs; TUI `X` interrupts immediately. Status records shutdown requests and remaining processes. `stop --now`, agent timeout, or a controller crash
leaves an interrupted assignment. On the next `start`, Worklane continues the saved
session and stage, including owned tickets already In Progress. This is continuation
of assigned work, not new ticket intake. A stopped finalizer resumes the handoff
without starting another review round. Board changes made by a human are respected;
unexpected statuses or closed issues block automatic continuation.
Merged PRs complete saved issues and role displays automatically; GitHub issue and board statuses remain human-controlled.

Provider errors and other blockers still need inspection and an explicit retry:
Board drift blocks visibly; explicit retry revalidates saved work without rerunning the completed worker.

```sh
worklane stop
worklane status
worklane retry 123
worklane start
```

`retry` retains the original role's session and does not change board status or delete work.
Already-owned In Progress/Agent review tickets can continue in place. A changed PR
head needs a fresh review turn in the same reviewer conversation. Worktrees and logs are retained
for recovery; cleanup is manual, using `git worktree remove` once changes are safe.
Agent runs have an eight-hour timeout. Worklane uses Codex's workspace sandbox and
automatic approval review (`--approve-for-me`), retaining the installed CLI's
approval rules. It disables Codex subagents; the default is three worker CLI
processes and one reviewer CLI process. These agents can write code, run commands
and create GitHub comments/PRs within the authorized task; inspect your queue before
starting. Account usage limits can block a run, in which case retry explicitly.

If a previously launched role has no recoverable session ID, or its harness cannot resume
the saved ID, Worklane blocks rather than silently starting a new conversation.
The `thread.started` event and its session mapping are committed in one transaction.
A crash before any recoverable ID was committed needs manual
inspection. Deleting harness history or a session worktree also requires recovery;
Worklane does not substitute another session or checkout.

Optional flags:

```sh
worklane run --project sbracegirdle/1
worklane run --once          # One dispatch batch, wait for it, then exit
worklane run --interval 120  # Poll every two minutes (default 60s, minimum 10s)
worklane run --model MODEL   # Explicit model override
```

`--once` does not continue filling slots or review work completed later in that
batch. Use continuous mode for the full pipeline. Each project runs its own bounded
team; limits are per repository, not shared across all projects.

`worklane stack` lists pinned ancestors and PRs; the TUI shows ordered stacks and blockers.
Single-parent stacks merge forward after upstream changes; conflicts or dirty worktrees block without rewriting history or auto-merging PRs.

A stop during startup preflight cancels that earlier start; shutdown and paused state remain intact.
