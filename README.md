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
not merge, deploy or publish releases. No project configuration files are needed.

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

- [Operations](docs/operations.md): board setup, options, review, recovery and limits.
- [Dashboard](docs/dashboard.md): controls and session access.
- [Storage](docs/storage.md): SQLite design, migration and backups.
- [Testing](docs/testing.md): test coverage and validation limits.
- [Local installations](docs/installation.md): test unmerged changes and identify the installed revision.

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
