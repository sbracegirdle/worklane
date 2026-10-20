# Harnesses

Codex remains the default; OpenCode requires 2.x and provider authentication through `opencode auth login`.

```sh
worklane start --harness opencode
worklane run --worker-harness opencode --worker-model anthropic/claude-sonnet-4-5 --reviewer-harness codex
worklane run --harness opencode --finalizer-harness codex --reviewer-model openai/gpt-5
```

`--harness` and `--model` set global defaults; `--worker-*`, `--reviewer-*`, and `--finalizer-*` override each role.
Omitted models use Codex's Worklane default or OpenCode's configured model; saved conversations retain their harness and model selection.

Background starts and TUI restarts preserve these options; existing conversations never switch harnesses or silently replace missing history.
Both harnesses share isolation, review gates, logs, result validation, interruption recovery, and interactive TUI resume.
Child environments discard inherited `PWD`, keeping runs, history checks, and interactive resumes in their assigned worktree.

OpenCode uses private CLI servers, JSON events, session titles, and prompted JSON results; malformed or incomplete results block progress.
Its `--auto` respects explicit permission denials; Worklane disables subagents, but OpenCode provides no Codex workspace sandbox.
