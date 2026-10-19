# Worklane

A Python CLI that runs up to three coding workers and one independent reviewer
from a GitHub project board, then hands passing work to a human.

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
worklane check       # Show the inferred board and eligible queue without changes
worklane run         # Run in the foreground; Ctrl-C drains active jobs then exits
```

The board uses **Ready for work**, **In Progress**, **Agent review** and
**Human review** statuses. Worklane creates isolated worktrees and PRs; it does
not merge, deploy or publish releases. No project configuration files are needed.

## Documentation

- [Operations](docs/operations.md): board setup, commands, review, recovery and limits.
- [Storage](docs/storage.md): SQLite design, migration and backups.
- [Development](docs/development.md): editable install and validation.

## Test

```sh
uv run python -m unittest -v
```
