# Terminal dashboard

Run `worklane tui` alongside the controller to view local SQLite state. Use
`--repo PATH` for another checkout and `--refresh 0.5` to change the refresh period
(default: one second). Viewing and quitting leave the controller and agents running.

| Key | Action |
| --- | --- |
| Up/Down or `k`/`j` | Select a worker, reviewer or finalizer session |
| Enter | Follow the selected invocation's log tail; press again to return |
| Page Up / Page Down / End | Scroll the tail / return to following output |
| `a` | Resume the selected saved Codex conversation after stopping Worklane |
| Esc | Return from logs, or quit the overview |
| `q` or Ctrl-C | Quit the dashboard |

The log view shows the latest 40 entries from the role's latest invocation; use
`worklane logs NUMBER` for complete history. The footer shows its saved session ID,
worktree, phase and blocker or PR link. Unicode log lines wrap by display width.
Use an interactive terminal of at least 50 columns and 12 rows with Python curses;
set `NO_COLOR=1` for monochrome. Redirected output should use `status` or `logs`.

Codex exec cannot accept interactive input while running. To resume a conversation,
run `worklane stop` and wait for jobs to finish before pressing `a`; pausing alone
is insufficient. Resume uses the exact role's saved ID and worktree, workspace
sandboxing and on-request approvals. The controller lock remains held throughout.
Missing sessions/worktrees and live orphaned agents are refused. Leaving Codex
returns to the dashboard; inspect manual changes before restarting Worklane and
use the [retry workflow](operations.md#state-recovery-and-limits) when needed.
