# Configuration

The TUI asks questions on first use; `c` or `worklane config` reopens setup without starting agents.
Enter advances, Ctrl-U clears, Esc cancels; final confirmation atomically saves `.worklane.toml` at the checkout root, outside SQLite.

| Setting | Default | Values |
| --- | --- | --- |
| `harness` | `"codex"` | `codex` or `opencode` (OpenCode 2.x) |
| `workers` | `3` | 1–16 coding workers; one separate reviewer/finalizer slot |
| `model` | `""` | Empty uses the harness default; otherwise an authenticated model name |
| `effort` | `"high"` | Codex: `low`, `medium`, `high`, `xhigh`; OpenCode uses its own settings |
| `interval` | `60` | Integer polling seconds, minimum 10 |
| `project` | `""` | Automatic discovery, or `OWNER/NUMBER` |
| `worker_*`, `reviewer_*`, `finalizer_*` | `""` | Role `harness` and `model` overrides; empty inherits global settings |

```toml
harness = "codex"
workers = 2
model = ""
effort = "high"
interval = 60
project = ""
```

CLI flags override file settings without saving; missing settings use defaults, while invalid files fail visibly.
Changes apply on next start; checkout configurations are separate, but the repository's controller lock and sessions remain shared.

```sh
worklane config --repo /path/to/checkout
worklane check --workers 2 --model MODEL --effort medium
worklane start --project OWNER/NUMBER --interval 30
```
