# Test unmerged CLI changes locally

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

Native CLI preview: `cargo install --path runtime --bin worklane --locked --offline --root /tmp/worklane-cli`.
Offline `config --show/--save` supports overrides; native dashboard (#52), cutover (#53), and exhaustive presentation parity remain deferred.
