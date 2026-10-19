# Terminal dashboard

Run `worklane tui` alongside the controller to view local SQLite state. Use
`--repo PATH` for another checkout and `--refresh 0.5` to change the refresh period
(default: one second). Issues expand into sessions; viewing and quitting leave agents running.

| Key | Action |
| --- | --- |
| Up/Down or `k`/`j` | Select an issue or visible session |
| Enter | Expand/collapse an issue; open a session transcript |
| Right / Left or `l` / `h` | Expand / collapse to the issue |
| Page Up / Page Down / End | Scroll the tail / return to following output |
| `a` | Resume the selected saved Codex conversation after stopping Worklane |
| `/` | Search session details and readable history; Enter searches, then opens a result |
| `s` / `x` | Start / gracefully stop Worklane from the overview |
| `i` / `B` | Read the selected issue / browse open repository issues on the board |
| `r` | Refresh an issue or board after its 60-second cache expires |
| `n` / `e` | Create from overview/board; edit the selected overview/board issue or open issue |
| Tab / Shift-Tab, F2, Esc | Editor: change fields, save, cancel; arrows select status; Enter adds description lines |
| `b`, Esc, Backspace or `q` | Return from logs to the overview |
| `q` in the overview or Ctrl-C | Quit the dashboard |

Views cache across restarts; failed refreshes retain saved content. Board Enter opens issues; issue arrows scroll descriptions and comments.
Start reuses saved controller settings; `--project`, `--model`, `--interval` and `--once` override them. Stop drains active jobs.
Search is literal and case-insensitive, including older invocations; results show up to 100 matches with bounded transcript context.

The log view shows the latest 40 readable entries from the role's latest invocation; use
`worklane logs NUMBER` for complete history. The transcript footer shows its saved session ID,
worktree and blocker. Unicode log lines wrap by display width.
Use an interactive terminal of at least 50 columns and 12 rows with Python curses;
use `--no-color` or `NO_COLOR=1` for monochrome. Redirected output should use `status` or `logs`.
Transcripts colour activity and hide protocol noise; `logs NUMBER --raw` shows original records.

Codex exec cannot accept interactive input while running. To resume a conversation,
run `worklane stop` and wait for jobs to finish before pressing `a`; pausing alone
is insufficient. Resume uses the exact role's saved ID and worktree, workspace
sandboxing and on-request approvals. The controller lock remains held throughout.
Missing sessions/worktrees and live orphaned agents are refused. Leaving Codex
returns to the dashboard; inspect manual changes before restarting Worklane and
use the [retry workflow](operations.md#state-recovery-and-limits) when needed.
