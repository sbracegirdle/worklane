# Rust CLI/TUI parity contract

Choose **Rust** for the complete port. Python remains operational until stage 5 passes.

| Decision | Rationale |
| --- | --- |
| Tauri | Reuse a Rust runtime inside the future [Tauri backend](https://v2.tauri.app/concept/architecture/). |
| Terminal | Use a native terminal event loop; preserve curses behavior, display cells, colour and resize semantics. |
| Linux | Explicitly retain flock inheritance, `/proc` birth identities, ownership tokens, signals and memory-backed descriptors. |
| SQLite | Retain schema, transactions, migrations, WAL and FULL durability; existing state must remain readable. |
| Packaging | Ship one native CLI/TUI executable; retain external git, gh and selected authenticated agent CLIs. |
| Maintenance | One typed runtime owns workflow/security invariants; CLI and TUI contain presentation and input routing. |
| Sharing | Future desktop code consumes that runtime; this stage defines no desktop protocol or application. |

## Executable reference

- Pin: `5122d0abc1b60fa0110d9c90937c6ffa1ab73d3c`; planning baseline: `ed9b43bdb6ce25ce9339271ffa986446105f75dd`.
- Include merged OpenCode, process-tree stopping, pinned stacks and hard completion gates; reuse #30/#38 behavior without reimplementing those tickets.
- [Inventory](../parity/inventory.json): all source/doc hashes, symbol spans, parser defaults/aliases, configuration, terminal branches and explicit failures.
- [Matrix](../parity/matrix.md): individual executable behavioral checks and owning stages; the JSON also indexes implementation branches.
- Each check names its pinned test method; its setup, responses, inputs and assertions are executable fixture definitions.
- `suite:MODULE` expands through `inventory.suites`; source/failure rows require every check in their owning suites.
- These broad rows prevent inventory omissions; they do **not** prove unasserted branches correct merely because tests pass.
- Before each stage and cutover: fetch main, review its complete diff, update fixtures/inventory, and rerun both executables.
- The inventory audit fails on reference drift; local unmerged edits never enter the archived Python oracle.

## Owning stages

| Stage | Required delivery |
| --- | --- |
| 1: contract | This decision, source inventory, executable reference checks, portable fixtures and comparison harness. |
| 2: runtime | Rust domain types, storage, configuration, GitHub/agent transports, workflows, process ownership and fixture adapter. |
| 3: CLI | Every parser surface and command; use the frozen runtime interface and fixtures independently of stage 4. |
| 4: TUI | Every screen, key branch, editor, search, cache and resume behavior; use stage 2 independently of stage 3. |
| 5: cutover | Refresh main, pass every matrix row on both implementations, verify installed executable and retain rollback state. |

## Command and terminal obligations

| Surface | Observable contract |
| --- | --- |
| No action / `tui` | Launch dashboard; first-use walkthrough; redirected terminal fails with guidance; quitting preserves the controller. |
| `run` | Foreground controller, inherited lock, early stop handler, authentication, bounded dispatch, durable controller logs. |
| `start` | Detached controller; generation-checked startup; preserve effective options; report startup failure/timeout without duplicate launch. |
| `check` | Read-only queue/dependencies/stack readiness, inferred project, effective settings and installation identity. |
| `board` | Discover/open board through gh; require neither workflow fields, agents nor runtime state. |
| `status` / `stack` | Offline JSON; preserve task/session/shutdown/process evidence and ordered pinned ancestor provenance. |
| `logs [NUMBER]` | Complete ordered readable history; `--raw` retains protocol events and diagnostics. |
| `config` | Offline walkthrough; validate every answer; cancellation preserves files; confirmation atomically saves checkout TOML. |
| `pause` / `resume` | Persist intake state; pause waits at dispatch boundary; resume never launches a stopped controller. |
| `stop` / `stop --now` | Publish without waiting for dispatch/GitHub; drain or kill owned descendants; repeated requests never downgrade immediate stop. |
| `retry NUMBER` | Explicit inspection boundary; retain sessions/worktrees; revalidate current authorization/head without board mutations. |
| `add` | Literal multiline title/body aliases; Ready/Draft/Shaping; validate before writing; preserve concurrent pickup and partial-failure URL. |
| Help / version | Preserve parser errors, usage, aliases and defaults; expose installation/source/revision identity without repository/state access. |
| CLI exits | Success 0; parser errors 2; reported runtime/config/IO/SQLite/timeout failures 1; preserve stderr versus stdout. |
| Options | Every parser entry, including hidden `--background`/`--generation`, is listed with type, choices, aliases and default. |
| Configuration | CLI > checkout TOML > defaults; role overrides inherit globally; saved conversations retain harness/model. |
| Overview | Group issues and role sessions; expand/collapse; arrow/j/k/h/l navigation; visible PR refresh; ordered stack/blocker display. |
| Transcript | Readable latest-role tail, 40 entries; PageUp/PageDown/End; speaker/tool/error colouring; exact saved-session footer. |
| Search | Literal Unicode casefold; metadata and historical invocations; 100-result cap; bounded context and highlighted wrapped matches. |
| Composer | `m`, Enter, Esc, Ctrl-U, Backspace; 8000 characters; pin displayed live run; `/run` preserves safeguards. |
| Views | `i`, `B`, `p`, `r`; board/issue navigation and comments; cache 60 seconds; offline stale fallback without duplicate writes. |
| Issue editor | `n`/`e`, Tab/Shift-Tab, F2, Esc; cursor/Home/End/Delete/arrows; multiline Unicode; validate drift and preserve partial writes. |
| Controls | `s` starts; `x` drains; `X` immediately stops; `c` configures; `a` resumes only after controller/orphan checks. |
| Exit/back | `b`, Esc, Backspace, `q`; overview `q`/Ctrl-C exits dashboard; restore terminal/keypad after interactive agent resume. |
| Terminal limits | Minimum 50×12, refresh 0.1–60 seconds, `NO_COLOR`/`--no-color`, combining/wide characters and resize recovery. |

## Runtime invariants

| Area | Required behavior / executable checks |
| --- | --- |
| Intake | Only open same-repository Ready issues; native blockers fail closed; linked PRs prevent duplicate implementation. `test_dependencies`, `test_worklane`. |
| Projects | HTTPS/SSH github.com origin; title preference; explicit ambiguity; paginated items/links/threads/replies; detect cycles/truncation/10,000-item limit. |
| Workflow | claiming → building → awaiting_review → reviewing → finalizing → human_review; rework returns Ready, preserving branch and role sessions. |
| Guards | Head/branch/base/clean checkout verified; unresolved/outdated threads, findings, failed handoff, draft promotion or changed heads block advancement. |
| Recovery | interrupted/retry stages reuse exact session/worktree; completed turns survive quota failures; human status edits and closed issues prevent unauthorized continuation. |
| Rounds | Three failed reviews block inspection; changed heads require fresh independent review; question-labelled issues use findings without PR/finalizer. |
| Feedback | All human comment/review kinds/pages; ignore bot/approval markers; edits count; five quiet minutes; persist cutoffs and unconsumed follow-ups. |
| Quota | Persist shared cooldown; honor retry/reset headers; bounded fallback 60–3600 seconds; stop further claims on exhaustion. |
| Budgets | Preserve measured assertions: idle/exhausted hourly reads, feedback polling and three-worker/reviewer operation counts; pinned tests outrank stale prose. |
| Stacks | Explicit same-repository CHILD:PARENT only; exact reviewed/handoff heads, no pending feedback/draft/conflict; preserve ordered ancestors and base SHA. |
| Stack recovery | Pinned ancestry; merge forward without force; abort conflicts; retain dirty work; resume lost push/retarget/board responses once; invalidate review. |
| Hard gates | Closed alone is insufficient: merged exact reviewed/handoff head required; completion gates cannot be stackable. `test_stacking`. |
| Processes | Birth-identity checks, inherited locks, launch serialization, startup stop latches, detached/reparented descendants, TERM→KILL; unrelated processes survive. |
| Sessions | Separate worker/reviewer/finalizer IDs; commit events and mapping together; missing/mismatched history never creates a replacement. |
| Input | Queue ≤20; sending survives restart; consume atomically before handoff; interrupted delivery may repeat actions; reject late/stopped/historical submissions. |
| Harnesses | Codex and OpenCode 2.x role routing, auth, results, metadata and exact resumes; PWD sanitization; preserve explicit permission denials/subagent restrictions. |
| Persistence | Schema v2 typed relational rows; no persistent JSON payload/log/schema/result files; transactional v1/JSON imports, archived bytes, idempotent cleanup. |
| Offline | Status/logs/config/controls remain usable; stale views remain visible; failed GitHub reads never authorize stale claims or repeat confirmed writes. |
| Completion | Observe merged PRs without moving human board statuses; preserve history and feedback cutoffs; bound polling and exclude live assignments. |

## Frozen runtime boundary

Types use UTF-8 strings, repository-qualified identifiers, UTC timestamps and explicit absent values; preserve integer/bool distinctions.

| Interface | Inputs → results; ownership |
| --- | --- |
| `ConfigStore` | checkout + explicit overrides → validated effective settings; atomic save after confirmation only. |
| `RepositoryLocator` | path/origin/project override → root, repo, default branch, unambiguous board. |
| `StateStore` | task/run/session/event/input/control operations → committed snapshots; retain pinned schema and migration transactions. |
| `GitHubTransport` | argv + stdin + deadline → stdout/stderr/exit; budget wraps every logical call; paginated APIs fail closed. |
| `AgentHarness` | role, exact saved session, worktree, model/effort, prompt/schema → wire events and validated structured result. |
| `ProcessSupervisor` | PID/birth identities, ownership tokens, generation, locks → launch/drain/interrupt outcomes and remaining owned PIDs. |
| `Workflow` | authorized board snapshot + saved task + clock → persisted transition and bounded external effects; never presentation-owned. |
| `ReadModel` | repository/issue/role/run/query → task tree, readable/raw logs, search context, cached pages, PR/stack states. |
| `Actions` | start/stop/pause/resume/retry/input/resume-session/editor intent → success or visible typed failure; share CLI safeguards. |
| `Clock` / `IdSource` | injected UTC/monotonic time and unique IDs; production uses real sources; fixtures provide deterministic values. |

- Stage 2 freezes these interfaces as a Rust library; CLI/TUI link the same crate, without invoking each other internally.
- The [runtime fixtures](../parity/fixtures/runtime.json) define test-adapter requests and shared values, not a desktop API.
- The [Python adapter](../tools/port_reference_driver.py) executes the archived runtime; Rust supplies an equivalent test executable.
- Existing test methods remain mandatory oracle fixtures; stage owners must implement equivalent candidate observations for every assertion and failure path.
- Public CLI output, terminal frames, persisted data, remote writes and failure semantics are contract observations, not implementation choices.

## Differential harness

```sh
uv run --no-project python tools/port_inventory.py
uv run --no-project python tools/port_checks.py --evidence /tmp/reference-checks.json
uv run --no-project python tools/port_parity.py --evidence /tmp/reference-cli.json
uv run --no-project python tools/port_parity.py --fixtures parity/fixtures/runtime.json --evidence /tmp/reference-runtime.json
uv run --no-project python tools/port_parity.py --candidate '["/absolute/path/to/worklane"]' --evidence /tmp/differential-cli.json
uv run --no-project python tools/port_parity.py --fixtures parity/fixtures/runtime.json --candidate '["/absolute/path/to/runtime-fixture-adapter"]' --evidence /tmp/differential-runtime.json
```

- JSON fixtures use argv arrays, stdin, expected exits/substrings, files, environment, strict ordered tool responses, SQL observations and PTY actions.
- Every execution gets fresh repository, HOME, XDG state/config/data/cache, fake gh/Codex/OpenCode and isolated Git configuration.
- No real GitHub/provider calls are made; unexpected calls, unused responses, unmet terminal actions and timeouts fail.
- Compare stdout/stderr, exit status, complete tool calls, selected file bytes and explicit read-only SQLite observations.
- Only exact sandbox paths normalize automatically; IDs/timestamps remain exact unless supplied through explicit deterministic runtime dependencies.
- ANSI, Unicode, errors, missing data and call counts remain visible; reference-only evidence cannot establish candidate parity.
- PTY fixtures retain raw output and terminal signals/resizes; event-driven input avoids arbitrary sleeps.
- New gap fixtures cover parser failures, literal transport data, partial issue creation, stack policy, queued-input restart and TUI `X`.

## Cutover evidence gate

- **100% functionality means every matrix row passes on both executables with evidence, with zero missing/deferred/stubbed cases.**
- Require exact reference SHA, candidate binary hash, fixture hash, command/environment and individual check observations for each row.
- Expand every suite reference; skipped, failed, missing, manual-unverified or Python-only checks block cutover.
- Source/doc rows require explicit review of unasserted behavior and additional executable fixtures before approving candidate coverage.
- Test counts and line coverage alone never establish parity; published stage-1 evidence establishes the Python baseline only.
- Installation/OpenCode opt-in checks and live authenticated full-workflow validation remain mandatory before cutover; no provider turn is authorized here.
- Preserve database/worktrees/agent history and Python rollback command; switch installations only in stage 5 after all checks pass.

[Interface signatures and value types](../parity/runtime-api.json) freeze the stage-2 boundary used independently by CLI and TUI.

```sh
uv run --no-project python tools/port_gate.py /tmp/differential-cli.json /tmp/differential-runtime.json --evidence /tmp/cutover.json
```

- Gate reports also require one `release_sha256`; use harness `--release PATH` when testing a separate runtime adapter.
- Source audits identify row, validation commands, artifact SHA-256 and an empty `unasserted_behaviors` list; missing audits block cutover.
- Fixture Git fetch/push uses a disposable local upstream; GitHub origin discovery remains unchanged. This harness is not an OS sandbox.
