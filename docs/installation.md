# Native installation

`worklane` and `worklane tui` run Rust; Python remains a development oracle, never a production fallback.

## Stage a candidate

```sh
cargo install --path runtime --bin worklane --bin worklane-tui --locked --offline --root /tmp/worklane-native
/tmp/worklane-native/bin/worklane --version
file /tmp/worklane-native/bin/worklane
WORKLANE_TEST_RUST=1 WORKLANE_TEST_CLI_BINARY=/tmp/worklane-native/bin/worklane uv run --no-project python -m unittest -q test_native_cli test_native_integration
```

Installed PTY evidence: `WORKLANE_TUI_BIN=/tmp/worklane-native/bin/worklane-tui uv run --no-project python tools/native_tui.py REPORT.json`.
Stages preserve production installations; #53 stays open and desktop #40–#48 remain blocked until actual cutover verification.

## Operator cutover after independent review and merge

Stop every managed repository with its current command; wait until `status` reports `running=false` before merging/installing.
Back up provider histories and referenced worktrees separately; the installer preserves them in place.

```sh
worklane stop --repo /path/to/project
worklane status --repo /path/to/project
# After the reviewed merge, use a clean checkout at freshly fetched origin/main:
gh auth setup-git --hostname github.com
git fetch origin main
git rev-parse HEAD origin/main
cargo install --path runtime --bin worklane --bin worklane-tui --locked --offline --root /tmp/worklane-native-main
uv run --no-project python tools/install_native.py \
  --staged /tmp/worklane-native-main/bin/worklane --revision "$(git rev-parse origin/main)" \
  --backup /absolute/path/to/new-private-backup --repo /path/to/project
```

Repeat `--repo` for every managed checkout; defaults replace `~/.local/bin/worklane` and this checkout's `.venv/bin/worklane` atomically per path.
Use repeated `--destination` for installations elsewhere, including the primary checkout; no checkout or integration branch changes automatically.

The installer requires clean reviewed-main identity, locks stopped controllers/launches, backs up SQLite WAL/config/commands, and rolls back partial replacements.
Backup `manifest.json` records identities and original paths; `production_verified=false` requires the remaining operator checks.

```sh
hash -r
command -v worklane
readlink -f ~/.local/bin/worklane /path/to/primary/.venv/bin/worklane
file ~/.local/bin/worklane /path/to/primary/.venv/bin/worklane
~/.local/bin/worklane --version
/path/to/primary/.venv/bin/worklane --version
worklane check --repo /path/to/project
worklane tui --repo /path/to/project
# Navigate logs/search, resize, cancel configuration, then q; verify terminal restoration.
worklane start --repo /path/to/project
worklane status --repo /path/to/project
```

Both ELF revisions and restarted `runtime.cli.revision` must equal freshly fetched `origin/main`; verify the controlled backlog and preserved sessions/input.
Record results before closing #53; `resume` never restarts an old controller. No automatic merge, release, or desktop unblock occurs.
