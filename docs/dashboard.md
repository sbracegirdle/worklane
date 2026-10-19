# Terminal dashboard

Run `worklane tui` alongside the controller to view local SQLite state. Use
`--repo PATH` for another checkout and `--refresh 0.5` to change the refresh period
(default: one second). Sessions are grouped by state, with counts and automatic
paging as you select across groups. Viewing and quitting leave the controller and
agents running.

| Key | Action |
| --- | --- |
| Up/Down or `k`/`j` | Select a worker, reviewer or finalizer session |
| Enter | Follow the selected invocation's log tail; press again to return |
| Page Up / Page Down / End | Scroll the tail / return to following output |
| `a` | Resume the selected saved Codex conversation after stopping Worklane |
| `b`, Esc, Backspace or `q` | Return from session logs to the overview |
| `q` in the overview or Ctrl-C | Quit the dashboard |

Protocol markers and Codex MCP transport diagnostics are hidden from the dashboard
and default `worklane logs` output. Use `worklane logs NUMBER --raw` to inspect the
original records, which remain stored in SQLite. Agent errors and meaningful MCP
failures remain visible. Structured agent results show the summary, verdict, PR
and findings as readable text instead of JSON.

The log view shows the latest 40 readable entries from the role's latest invocation.
Use `worklane logs NUMBER` for complete history. The footer shows its saved session ID,
worktree, phase and blocker or PR link. Unicode log lines wrap by display width.
Use an interactive terminal of at least 50 columns and 12 rows with Python curses;
colours are enabled by default even when `NO_COLOR` is set. Use
`worklane tui --no-color` for monochrome. Redirected output should use `status` or
`logs`.

Codex exec cannot accept interactive input while running. To resume a conversation,
run `worklane stop` and wait for jobs to finish before pressing `a`; pausing alone
is insufficient. Resume uses the exact role's saved ID and worktree, workspace
sandboxing and on-request approvals. The controller lock remains held throughout.
Missing sessions/worktrees and live orphaned agents are refused. Leaving Codex
returns to the dashboard; inspect manual changes before restarting Worklane and
use the [retry workflow](operations.md#state-recovery-and-limits) when needed.
