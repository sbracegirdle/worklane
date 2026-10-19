# Worklane

A Python CLI that runs coding workers and an independent reviewer from a GitHub
project board, then hands passing work to a human.

[Source repository](https://github.com/sbracegirdle/worklane) ·
[Project board](https://github.com/users/sbracegirdle/projects/2)

## Install

Requires Linux, Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
[GitHub CLI](https://cli.github.com/) and
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli).

```sh
gh auth login
gh auth refresh -s project
codex login
uv tool install git+https://github.com/sbracegirdle/worklane.git
```

Make sure `~/.local/bin` is on your PATH.

## Run

From a GitHub repository checkout with issues ready on its project board:

```sh
cd /path/to/project
worklane check       # Read-only: show the inferred board, model and eligible queue
worklane board       # Open the project board in your web browser
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
worklane board --repo ~/code/another-project
```

`worklane board` uses the same board discovery as `check` and `run`. Use
`worklane board --project OWNER/NUMBER` to select a board explicitly. It opens the
board through GitHub CLI (honoring `GH_BROWSER` or `BROWSER`) without starting
agents, creating local state, or requiring the board's workflow statuses or Codex.

## Board and handoff behavior
The board uses **Ready for work**, **In Progress**, **Agent review** and
**Human review** statuses. Worklane creates isolated worktrees and PRs; it does
not merge, deploy or publish releases. No project configuration files are needed.

## Documentation

- [Operations](docs/operations.md): board setup, options, review, recovery and limits.
- [Storage](docs/storage.md): SQLite design, migration and backups.
- [Testing](docs/testing.md): test coverage and validation limits.

## Test unmerged CLI changes locally

The CLI's source and the repository it manages are separate. A local integration
checkout, draft PR branch, or exact commit can supply the CLI while `--repo`
continues to select the project being managed. No merge into `main` or release is
needed. Installation commands below replace only your local Worklane tool.

Before changing an installed CLI (including edits to an editable checkout), stop
and drain each controller using it with `worklane stop --repo /path/to/project`.
Use `worklane status --repo /path/to/project` until `running` is false. Reinstalling
does not replace a running controller's code; `resume` does not restart it.

For an existing local integration or PR checkout, install it in editable mode:

```sh
uv tool install --force --editable /absolute/path/to/worklane-integration
worklane --version
worklane check --repo /absolute/path/to/target-project
```

Subsequent source edits are picked up by new CLI processes. If package metadata
or dependencies change, repeat the install. Keep that checkout available while
the editable tool is installed. This does not switch branches in the primary
checkout or merge anything into the integration branch.

To install a remote branch without a local checkout:

```sh
gh auth setup-git --hostname github.com
uv tool install --force --refresh 'git+https://github.com/sbracegirdle/worklane.git@integration'
worklane --version
```

Replace `integration` with the draft PR's head branch or a full commit SHA. The
branch must be pushed to that repository; for fork PRs, use the fork's URL. Repeat
the command to pick up new branch commits; `--refresh` avoids a cached branch
resolution, and a full SHA selects a reproducible revision.

For a temporary invocation that keeps the installed `worklane` command intact:

```sh
uv run --project /absolute/path/to/worklane-integration worklane --version
uv run --project /absolute/path/to/worklane-integration worklane check --repo /absolute/path/to/target-project
```

`--version` works offline and outside a project checkout. It prints the package
version, loaded module path, full source revision, local branch and tracked-file
dirty state. Remote Git installs report the resolved commit from their install
metadata; their branch and dirty state are `null`. Ordinary wheels without Git
metadata have a `null` revision. A source checkout without installed package
metadata reports version `uninstalled`. `check` also shows this CLI information;
`status` includes the CLI information captured when the controller started under
`runtime.cli` (older controllers may lack it).

Run the tests below and use `check` for a read-only board smoke check. Once ready
to exercise real work, explicitly start the selected CLI with
`worklane start --repo /absolute/path/to/target-project` (prepend
`uv run --project /absolute/path/to/worklane-integration` for a temporary
invocation). All installs/checkouts share the usual
per-repository state and controller lock; a preview uses the real queue and saved
sessions. Stop the controller again before switching back. To restore the default
branch installation:

```sh
uv tool install --force --refresh 'git+https://github.com/sbracegirdle/worklane.git@main'
worklane --version
```

Alternatively, restore your previous editable source with `uv tool install
--force --editable /path/to/previous-checkout`. Switching CLI versions preserves
state and sessions, but an older CLI can refuse a newer database schema; keep a
stopped-controller state backup before testing schema changes.

## Develop

Install the local checkout in editable mode, then run validation:

```sh
cd ~/code/worklane
uv tool install --editable .
uv run python -m unittest -v
WORKLANE_TEST_INSTALL=1 uv run python -m unittest -v test_installation
uv run worklane --help
```

Use `uv run worklane check --repo /path/to/project` for a read-only live board check.
