# Worklane

A reusable Python CLI that takes issues from a GitHub project board, runs up to
three coding workers and one independent reviewer, and hands passing work to a
human. No configuration files are needed in the projects it works on.

[Source repository](https://github.com/sbracegirdle/worklane) ·
[Project board](https://github.com/users/sbracegirdle/projects/2)

Currently uses your authenticated **Codex CLI**, with **`gpt-6.1-sol`** and high
reasoning effort by default. The backend boundary is separate from queue handling
so another CLI, such as OpenCode, can be added later. There are no Python runtime
dependencies. Linux and Python 3.11+ are currently supported.

## Install once

Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
[GitHub CLI](https://cli.github.com/) and
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli).
Authenticate with `gh auth login`, grant GitHub Projects access with
`gh auth refresh -s project`, and run `codex login`.

```sh
uv tool install --editable ~/code/worklane
```

To install directly from GitHub instead:

```sh
uv tool install git+https://github.com/sbracegirdle/worklane.git
```

Make sure `~/.local/bin` is on your PATH. No API key is needed when Codex is already
authenticated through your ChatGPT account. Worklane never stores access tokens.

## Run in any project

```sh
cd ~/code/syntaxiser
worklane check       # Read-only: show the inferred board, model and eligible queue
worklane run         # Foreground; Ctrl-C drains active jobs then exits
```

`worklane` with no arguments is the same as `worklane run`. For background use:

```sh
worklane start       # Start a detached controller; returns immediately
worklane status      # Print state, assignments, sessions and blockers
worklane logs        # Read controller/agent logs from SQLite
worklane logs 123    # Read logs for one issue
worklane pause       # Stop all new dispatches; active workers/reviewer finish
worklane resume      # Resume dispatching in the running controller
worklane stop        # Pause dispatching, drain active jobs, then exit
worklane stop --now  # Interrupt agents; saved conversations continue on next start
```

After `stop`, use `start` again; `resume` does not launch a stopped controller.
Paused work may finish its current review/handoff and update the board. Workers
that finish while paused wait in Agent review until dispatch resumes. No service
is installed, and the controller does not start on login or reboot.

Commands can target another checkout without changing directory:

```sh
worklane start --repo ~/code/another-project
worklane status --repo ~/code/another-project
worklane pause --repo ~/code/another-project
```

## Board and handoff behavior

The repository is inferred from the checkout's `origin` remote. Only github.com
HTTPS/SSH remotes are supported. Worklane looks for a linked project, preferring a
project whose title matches the repository. If none is linked it considers the
repository owner's projects. When there is more than one candidate, choose with
`--project OWNER/NUMBER`; Worklane does not silently choose an ambiguous board.

The Status field must already contain these names (case-insensitive):

```text
Ready for work -> In Progress -> Agent review -> Human review
                       ^              |
                       +-- rework <---+
```

Rework goes back to **Ready for work** so workers only ever claim that status.
The first run adds **Agent review** immediately before Human review, preserving
existing option IDs, colors and descriptions. `check` never changes the board.
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
issue). Failed reviews return to Ready for work on the same branch, with findings
included in the next worker's input. After three failed review rounds, the task is
blocked for inspection instead of looping indefinitely.

Inline PR review threads and all replies are included in worker/reviewer input,
including outdated and resolved discussions. After pushing and verifying a fix,
workers reply with the fix SHA and validation evidence and resolve each addressed
thread using GitHub's review-thread API. Unaddressed or disputed feedback stays
open; outdated threads are not automatically resolved. Workers check existing
replies and resolution state when resuming to avoid duplicate actions. Ordinary
PR conversation comments and review summaries have no resolved state and remain
part of the discussion/evidence.

The controller refreshes thread state before accepting implementation and before
and after the local testing handoff. Open threads block completion, including
outdated threads; GitHub read failures also block rather than assume resolution.
The independent reviewer verifies that resolved feedback is addressed at the
reviewed SHA. If a worker lacks permission to resolve an addressed thread, it
reports a blocker for inspection; the controller never bulk-resolves feedback.

After a pass, the same reviewer slot completes any repository-required local
preview/integration/playground handoff, then moves the issue to Human review.
This keeps local integration changes serialized. The tool does not automatically
merge PRs, publish, deploy, process Human review feedback or mark issues Done.
Move an issue back to Ready for work to request another implementation pass.
If separate legacy feedback automations are enabled, avoid giving them concurrent
ownership of an issue Worklane is handling.

## State, recovery and limits

Runtime data lives outside project checkouts:

```text
~/.local/state/worklane/OWNER--REPO/
  state.sqlite3        # All Worklane state, events, logs, results and session IDs
  worktrees/           # Preserved worker and detached review checkouts
```

SQLite is the only durable Worklane data store. Tasks, nested issue/PR data,
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
and session explicitly. See [the storage decision](STORAGE.md) for the comparison.

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

Worklane saves `thread.started` IDs immediately, before waiting for the turn to
finish. It uses `codex exec resume <EXACT_ID>` for subsequent turns, never `--last`.
Starting another process does not create another conversation. Session transcripts
remain managed by Codex in its own local storage; keep that storage, the Worklane
database and worktrees when moving or backing up an installation.

`stop` lets active work finish. `stop --now`, agent timeout, or a controller crash
leaves an interrupted assignment. On the next `start`, Worklane continues the saved
session and stage, including owned tickets already In Progress. This is continuation
of assigned work, not new ticket intake. A stopped finalizer resumes the handoff
without starting another review round. Board changes made by a human are respected;
unexpected statuses or closed issues block automatic continuation.

Provider errors and other blockers still need inspection and an explicit retry:

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
Codex runs have an eight-hour timeout. Worklane uses Codex's workspace sandbox and
automatic approval review (`--approve-for-me`), retaining the installed CLI's
approval rules. It disables Codex subagents; there are at most three worker CLI
processes and one reviewer CLI process. These agents can write code, run commands
and create GitHub comments/PRs within the authorized task; inspect your queue before
starting. Account usage limits can block a run, in which case retry explicitly.

If a previously launched role has no recoverable session ID, or Codex cannot resume
the saved ID, Worklane blocks rather than silently starting a new conversation.
The `thread.started` event and its session mapping are committed in one transaction.
A crash before any recoverable ID was committed needs manual
inspection. Deleting Codex history or a session worktree also requires recovery;
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

## Develop

```sh
cd ~/code/worklane
uv run python -m unittest -v
uv run worklane --help
uv run worklane check --repo ~/code/syntaxiser
```

Tests use temporary state and mocked GitHub/agent responses. They cover concurrency,
eligible statuses, pause, process locks, status preservation, review and handoff
gates, duplicate work, changed heads and recovery boundaries. A read-only live board
check and empty-queue run can verify authentication and board wiring without claiming
an issue. A real coding ticket is needed to validate a complete agent implementation.

Session tests also launch a fake CLI as a real subprocess, interrupt it and restart
the controller, checking that the exact original session is resumed. A harmless
live Sol 6.1 two-turn check verified both the unchanged session ID and recall of
information present only in the previous turn.
