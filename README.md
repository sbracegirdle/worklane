# Worklane

A native Rust CLI/TUI that runs coding workers and an independent reviewer from a GitHub
project board, then hands passing work to a human.

[Source repository](https://github.com/sbracegirdle/worklane) ·
[Project board](https://github.com/users/sbracegirdle/projects/2)

## Install

Requires Linux, Rust 1.85+,
[GitHub CLI](https://cli.github.com/) and
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) or [OpenCode 2.x](https://opencode.ai/docs/cli/).

```sh
gh auth login
gh auth refresh -s project
codex login # For OpenCode: opencode auth login
cargo install --git https://github.com/sbracegirdle/worklane.git --bin worklane --bin worklane-tui --locked
```

Put Cargo's bin directory on PATH; existing installations require the [drained cutover](docs/installation.md).

## Run

From a GitHub repository checkout with issues ready on its project board:

```sh
cd /path/to/project
worklane check       # Read-only: show the inferred board, model and eligible queue
worklane board       # Open the project board in your web browser
worklane run         # Foreground; Ctrl-C drains active jobs then exits
```

`worklane` opens the TUI; use `worklane run` for the foreground controller. For background use:

```sh
worklane start       # Start a detached controller; returns immediately
worklane status      # Print state, assignments, sessions and blockers
worklane tui         # Live agent overview and session logs
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
not merge, deploy or publish releases. Optional [configuration](docs/configuration.md) has a first-use TUI walkthrough.

## Add issues

Create an issue in **Ready for work** by default, or choose **Draft** or **Shaping** with the corresponding flag.
See [issue creation](docs/operations.md#add-issues) for requirements, verification and recovery.

```sh
worklane add --title "Fix the settings page" --description "Save changes when Apply is clicked."
worklane add -t "Explore offline support" -d "Document caching options." --shaping
worklane add -t "Early idea" --draft
worklane add -t "Fix startup" --repo ~/code/another-project --project OWNER/NUMBER
```

## Documentation

- [Harnesses](docs/harnesses.md): global defaults, role overrides and OpenCode.
- [Operations](docs/operations.md): board setup, options, review, recovery and limits.
- [Dashboard](docs/dashboard.md): controls and session access.
- [Configuration](docs/configuration.md): setup walkthrough, settings and overrides.
- [Storage](docs/storage.md): SQLite design, migration and backups.
- [Testing](docs/testing.md): test coverage and validation limits.
- [Local installations](docs/installation.md): test unmerged changes and identify the installed revision.

## Develop

Build the native commands; Python/uv provide the pinned regression oracle:

```sh
cd ~/code/worklane
cargo build --locked
cargo test --locked --offline
WORKLANE_TEST_RUST=1 uv run --no-project python -m unittest -q
target/debug/worklane --help
```

Use `target/debug/worklane check --repo /path/to/project` for a read-only live board check.
