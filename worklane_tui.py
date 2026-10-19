"""Local, live terminal views of Worklane's SQLite ledger (standard library only)."""
from __future__ import annotations

import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import uuid

from worklane import ROLES, WORKERS, locked, process_identity, readable_log


GROUP_ORDER = ("running", "blocked", "failed", "interrupted", "retry", "awaiting_review", "human_review", "completed", "queued")


def row_group(row):
    if row["active"]:
        return "running"
    status = row["status"]
    return "queued" if status in {"claiming", "building", "reviewing", "finalizing", "ready"} else status


def group_rank(row):
    group = row_group(row)
    return (GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER), group, row["number"], row["role"])


def issue_groups(rows):
    """One parent per issue, with role sessions kept in workflow order."""
    issues = {}
    for row in rows:
        issue = issues.setdefault(row["number"], {"number": row["number"], "title": row["title"],
                                               "task": row["task"], "sessions": []})
        issue["sessions"].append(row)
    for issue in issues.values():
        sessions = issue["sessions"]
        sessions.sort(key=lambda row: ("worker", "reviewer", "finalizer").index(row["role"]))
        phase = issue["task"].get("phase", "unknown")
        phase = "completed" if phase == "done" else phase
        if phase in {"blocked", "failed", "interrupted", "retry", "awaiting_review", "human_review", "completed"}:
            status = phase
        elif any(row["active"] for row in sessions):
            status = "running"
        elif phase in {"claiming", "building", "reviewing", "finalizing"}:
            status = "interrupted" if any(row["run"] for row in sessions) else "queued"
        else:
            status = phase
        issue["status"] = status
    return sorted(issues.values(), key=lambda issue: (
        GROUP_ORDER.index(issue["status"]) if issue["status"] in GROUP_ORDER else len(GROUP_ORDER), issue["number"]))


def snapshot(state, selected=None):
    """One consistent read, latest invocation per issue/role, bounded log tails."""
    busy = state.running()
    with state.connect() as db:
        db.create_function("readable_log", 2, readable_log, deterministic=True)
        db.execute("BEGIN")
        control = state._get(db, "meta:control", {})
        tasks = {r[0]: state._get(db, f"task:{r[0]}", {}) for r in db.execute("SELECT number FROM tasks ORDER BY number")}
        sessions = {(r["number"], r["role"]): dict(r) for r in db.execute("SELECT * FROM sessions")}
        runs = {(r["number"], r["role"]): dict(r) for r in db.execute("""
            SELECT r.* FROM runs r WHERE r.rowid = (
                SELECT newer.rowid FROM runs newer
                WHERE newer.number=r.number AND newer.role=r.role
                ORDER BY newer.started_at DESC,newer.rowid DESC LIMIT 1)
        """)}
        keys = set(sessions) | set(runs)
        for number, task in tasks.items():
            role = ROLES.get(task.get("active_stage", task.get("resume_stage", "build")), "worker")
            role = {"reviewing": "reviewer", "finalizing": "finalizer", "claiming": "worker", "building": "worker"}.get(task.get("phase"), role)
            keys.add((number, role))
        rows = []
        for number, role in keys:
            task, run = tasks[number], runs.get((number, role), {})
            session = sessions.get((number, role), {})
            active = busy and run.get("status") in {"starting", "running"}
            status = run.get("status", task.get("phase", "unknown"))
            if not busy and status in {"starting", "running"}:
                status = "interrupted"
            latest = None
            if run:
                latest = db.execute("""SELECT readable_log(stream,content) AS content,created_at FROM logs WHERE run_id=?
                    AND readable_log(stream,content) != '' ORDER BY id DESC LIMIT 1""", (run["id"],)).fetchone()
            current_role = ROLES.get(task.get("active_stage", task.get("resume_stage", "build")), "worker")
            if current_role == role and task.get("phase") in {"blocked", "interrupted", "retry", "awaiting_review", "human_review"}:
                status = task["phase"]
            rows.append({"key": (number, role), "number": number, "role": role, "task": task,
                         "title": task.get("issue", {}).get("title") or task.get("item", {}).get("content", {}).get("title", "Untitled issue"),
                         "run": run, "session": session, "active": active, "status": status,
                         "snippet": latest["content"] if latest else "Waiting for activity…",
                         "activity_at": latest["created_at"] if latest else run.get("started_at", "")})
        rows.sort(key=group_rank)
        chosen = next((row for row in rows if row["key"] == selected), rows[0] if rows else None)
        if chosen and chosen["run"]:
            logs = db.execute("SELECT id,run_id,stream,readable_log(stream,content) AS content,created_at FROM logs WHERE run_id=? AND readable_log(stream,content) != '' ORDER BY id DESC LIMIT 40", (chosen["run"]["id"],)).fetchall()
        elif chosen:
            logs = []  # Never attribute unowned/controller output to a pending role.
        else:
            logs = db.execute("SELECT id,run_id,stream,readable_log(stream,content) AS content,created_at FROM logs WHERE run_id IS NULL AND readable_log(stream,content) != '' ORDER BY id DESC LIMIT 40").fetchall()
        return {"busy": busy, "paused": control.get("paused", False),
                "rows": rows, "selected": chosen,
                "logs": [dict(row) for row in reversed(logs)]}


def resume_session(state, number, role, *, invoke=None):
    """Hold the controller lock throughout interactive use, including orphan safety."""
    invoke = invoke or subprocess.run
    with locked(state.path / "runner.lock", blocking=False) as fd:
        # Reload under the lock: a displayed selection can be stale by keypress time.
        session = state.session(number, role)
        if not session:
            raise RuntimeError("No saved session yet. Enter follows live logs as soon as they arrive.")
        session_id = str(uuid.UUID(session["session_id"]))
        if session.get("backend", "codex") != "codex":
            raise RuntimeError("Interactive resume is supported only for Codex sessions.")
        tree = Path(session["worktree"])
        if not tree.is_dir():
            raise RuntimeError("Saved session worktree is missing; inspect it before resuming.")
        for run in state.runs():
            if run.get("pid") and run.get("identity") and process_identity(run["pid"]) == run["identity"]:
                raise RuntimeError("An agent process is still alive. Stop Worklane and wait before resuming.")
        if not shutil.which("codex"):
            raise RuntimeError("Required command missing: codex")
        result = invoke(["codex", "resume", session_id, "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
                         "--disable", "multi_agent"], cwd=tree, pass_fds=(fd,))
        if result.returncode:
            raise RuntimeError(f"Codex resume exited {result.returncode}; saved session retained.")


def plain(value):
    """Keep log content from injecting control characters into the terminal."""
    return "".join(" " if char.isspace() else char for char in str(value)
                   if not unicodedata.category(char).startswith("C") or char.isspace())


def cell_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def wrapped(value, width):
    """Split sanitized text into display-cell rows without dropping content."""
    rows, row, used = [], [], 0
    for char in plain(value):
        size = cell_width(char)
        if row and used + size > width:
            space = max((i for i, value in enumerate(row) if value == " "), default=-1)
            if space > 0 and any(value != " " for value in row[:space]):
                rows.append("".join(row[:space + 1]))
                row = row[space + 1:]
                used = sum(cell_width(value) for value in row)
            else:
                rows.append("".join(row))
                row, used = [], 0
        row.append(char)
        used += size
    return rows + ["".join(row)]


def clipped(value, width):
    result, used = [], 0
    for char in plain(value):
        size = cell_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


class Dashboard:
    def __init__(self, curses, state, repo, refresh, *, no_color=False):
        self.curses, self.state, self.repo, self.refresh = curses, state, repo, refresh
        self.selected, self.detail, self.offset = None, False, 0
        self.no_color = no_color
        self.focus, self.expanded = None, None
        self.message = "Select an issue to see its sessions; Enter on a session opens its transcript."
        self.colors = {}

    def put(self, screen, y, text, style=0):
        height, width = screen.getmaxyx()
        if 0 <= y < height:
            try:
                screen.addstr(y, 0, clipped(text, max(0, width - 1)), style)
            except self.curses.error:
                pass  # Resize or a terminal's lower-right cell.

    def tone(self, status):
        name = "bad" if status in {"blocked", "failed"} else "warn" if status in {"interrupted", "retry"} else "good" if status in {"completed", "human_review"} else "live"
        return self.colors.get(name, 0)

    def paint(self, screen, y, spans):
        """Draw one row with independently styled text spans."""
        height, width = screen.getmaxyx()
        if not 0 <= y < height:
            return
        x = 0
        for text, style in spans:
            text = clipped(text, max(0, width - x - 1))
            if not text:
                continue
            try:
                screen.addstr(y, x, text, style)
            except self.curses.error:
                pass
            x += sum(cell_width(char) for char in text)

    def nodes(self, issues, chosen):
        if self.expanded is None:
            self.expanded = {chosen["number"]} if chosen else set()
        nodes = []
        for issue in issues:
            nodes.append(((issue["number"], None), issue, None))
            if issue["number"] in self.expanded:
                nodes.extend((row["key"], issue, row) for row in issue["sessions"])
        if self.focus not in {node[0] for node in nodes}:
            self.focus = (chosen["number"], None) if chosen else None
            if self.focus not in {node[0] for node in nodes}:
                self.focus = nodes[0][0] if nodes else None
        return nodes

    def draw_overview(self, screen, data, issues, bottom):
        c = self.curses
        muted = c.A_DIM
        nodes = self.nodes(issues, data["selected"])
        lines, group, focus_line, parent_lines = [], None, 0, {}
        for key, issue, row in nodes:
            if row is None and issue["status"] != group:
                group = issue["status"]
                count = sum(item["status"] == group for item in issues)
                lines.append([(f"  {group.replace('_', ' ').upper()}  ·  {count} issue{'s' if count != 1 else ''}", self.tone(group) | c.A_BOLD)])
            if key == self.focus:
                focus_line = len(lines)
            if row is None:
                parent_lines[issue["number"]] = len(lines)
                selected = key == self.focus
                expanded = issue["number"] in self.expanded
                lines.append([(" › " if selected else "   ", self.colors.get("live", 0) | c.A_BOLD),
                              ("▾ " if expanded else "▸ ", self.tone(issue["status"])),
                              (f"#{issue['number']}  {issue['title']}", c.A_BOLD if selected else 0),
                              (f"  {len(issue['sessions'])} session{'s' if len(issue['sessions']) != 1 else ''}", muted)])
            else:
                selected = key == self.focus
                branch = "└─" if row is issue["sessions"][-1] else "├─"
                status = row_group(row).replace("_", " ")
                snippet = next((line.strip() for line in row["snippet"].splitlines() if line.strip()), "Waiting for activity…")
                lines.append([(" › " if selected else "   ", self.colors.get("live", 0) | c.A_BOLD),
                              (f"  {branch} ", muted),
                              (f"{row['role']:<10}", self.colors.get("agent", 0) | (c.A_BOLD if selected else 0)),
                              (f" {status:<16}", self.tone(row_group(row))),
                              (f"  {snippet}", 0 if selected else muted)])
        capacity = max(1, bottom - 4)
        # Keep the focused item and its issue/state context visible when scrolling.
        start = max(0, focus_line - capacity + 2)
        if focus_line < capacity:
            start = 0
        if self.focus and self.focus[1] is not None:
            parent_line = parent_lines[self.focus[0]]
            if start > parent_line:
                start = parent_line
        visible = lines[start:start + capacity]
        self.put(screen, 3, "  ISSUES & SESSIONS", c.A_BOLD)
        for y, spans in enumerate(visible, 4):
            self.paint(screen, y, spans)
        if not nodes:
            self.put(screen, 5, "  No issues yet. Start Worklane in another terminal to follow progress.", muted)
        self.put(screen, 2, f"  {len(issues)} issues  ·  {len(data['rows'])} sessions  ·  Enter expands issues / opens sessions", muted)

    def transcript_style(self, text, kind):
        c = self.curses
        text = text.strip()
        if text == "Passed":
            return self.colors.get("good", 0) | c.A_BOLD
        if text in {"Blocked", "Changes requested"}:
            return self.colors.get("bad" if text == "Blocked" else "warn", 0) | c.A_BOLD
        if text.startswith(("PR:", "Revision:", "$ ")):
            return self.colors.get("live", 0)
        if text == "Findings:" or text.startswith("#"):
            return self.colors.get("agent", 0) | c.A_BOLD
        if text.startswith("- "):
            return self.colors.get("warn", 0)
        return self.colors.get(kind, 0) if kind in {"live", "bad", "warn", "files"} else 0

    def draw_transcript(self, screen, data, bottom):
        c = self.curses
        chosen = data["selected"]
        label = f"#{chosen['number']} / {chosen['role']}" if chosen else "controller"
        name = "SESSION" if chosen else "CONTROLLER"
        self.paint(screen, 3, [(f"  LIVE {name} LOG  ", c.A_BOLD), (label, self.colors.get("agent", 0)),
                               ("  ·  b / Esc / q back", c.A_DIM)])
        width = max(1, screen.getmaxyx()[1] - 7)
        lines, headers = [], []
        for log in data["logs"]:
            content = log["content"]
            stream = log.get("stream", "agent")
            if content.startswith("$ "):
                speaker, kind = "COMMAND", "live"
            elif content.startswith("Files:"):
                speaker, kind = "FILES", "files"
            elif stream in {"stderr", "controller-error"}:
                speaker, kind = "WARNING" if "warn" in content.lower() else "ERROR", "warn" if "warn" in content.lower() else "bad"
            else:
                speaker, kind = ("AGENT", "agent") if stream == "agent" else ("OUTPUT", "live")
            headers.append(len(lines))
            lines.append([("  " + log["created_at"][11:19] + "  ", c.A_DIM),
                          (speaker, self.colors.get(kind, 0) | c.A_BOLD)])
            for line in content.splitlines():
                for part in wrapped(line, width):
                    lines.append([("  │ ", self.colors.get(kind, 0)), (part, self.transcript_style(line, kind))])
            lines.append([])
        capacity = max(1, bottom - 4)
        self.offset = min(self.offset, max(0, len(lines) - capacity))
        end = len(lines) - self.offset
        start = max(0, end - capacity)
        visible = lines[start:end]
        if visible and start not in headers and visible[0]:
            header = next((index for index in reversed(headers) if index < start), None)
            if header is not None:
                visible = [lines[header] + [("  ·  continued", c.A_DIM)]] + visible
                blank = next((i for i, spans in enumerate(visible) if not spans), None)
                if blank is not None:
                    del visible[blank]
                else:
                    del visible[1]
        for y, spans in enumerate(visible, 4):
            self.paint(screen, y, spans)
        if not lines:
            self.put(screen, 5, "  Waiting for log output…", c.A_DIM)
        self.put(screen, 2, "  " + ("FOLLOWING LIVE" if not self.offset else "SCROLLED BACK") + "  ·  PgUp / PgDn scroll  ·  End follows", c.A_DIM)

    def draw(self, screen, data):
        c = self.curses
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 12 or width < 50:
            self.put(screen, 0, "Worklane · enlarge terminal to at least 50×12; q quits")
            screen.refresh()
            return
        state = "STOPPED" if not data["busy"] else "PAUSED" if data["paused"] else "RUNNING"
        self.paint(screen, 0, [("  WORKLANE", c.A_BOLD | self.colors.get("live", 0)),
                              ("  /  " + self.repo, c.A_DIM),
                              ("   ● " + state, self.colors.get("good" if state == "RUNNING" else "warn", 0) | c.A_BOLD)])
        active = [row for row in data["rows"] if row["active"]]
        workers = sum(row["role"] == "worker" for row in active)
        reviews = sum(row["role"] in {"reviewer", "finalizer"} for row in active)
        self.paint(screen, 1, [("  Workers ", c.A_DIM),
                              (f"{'●' * workers}{'○' * max(0, len(WORKERS) - workers)} {workers}/3", self.colors.get("live", 0)),
                              ("    Review ", c.A_DIM), (f"{'●' if reviews else '○'} {reviews}/1", self.colors.get("agent", 0))])
        issues = issue_groups(data["rows"])
        bottom = height - (6 if self.detail else 4)
        if self.detail:
            self.draw_transcript(screen, data, bottom)
        else:
            self.draw_overview(screen, data, issues, bottom)
        self.put(screen, bottom, "  " + "─" * max(0, width - 5), c.A_DIM)
        chosen = data["selected"]
        if self.detail and chosen:
            session, run = chosen["session"], chosen["run"]
            self.paint(screen, bottom + 1, [(f"  #{chosen['number']}  ", c.A_BOLD), (chosen["title"], 0)])
            self.put(screen, bottom + 2, f"  {chosen['role'].upper()}  ·  Session {session.get('session_id', 'not saved yet')}", c.A_DIM)
            self.put(screen, bottom + 3, f"  {session.get('worktree') or run.get('worktree', 'Worktree not created yet')}", c.A_DIM)
        elif chosen:
            self.paint(screen, bottom + 1, [(f"  #{chosen['number']}  ", c.A_BOLD),
                                           (chosen["title"], 0), (f"  /  {chosen['role']}", self.colors.get("agent", 0))])
        error = chosen["task"].get("error") if chosen else None
        self.put(screen, height - 2, "  " + (error or self.message),
                 self.colors.get("warn", 0) if error else c.A_DIM)
        controls = "b / Esc / Enter / q back   PgUp / PgDn scroll   End follow" if self.detail else "↑ ↓ / j k select   Enter expand / open   ← collapse   a resume   q quit"
        self.put(screen, height - 1, "  " + controls, self.colors.get("live", 0))
        screen.refresh()

    def run(self, screen):
        c = self.curses
        try:
            c.curs_set(0)
        except c.error:
            pass
        if c.has_colors() and not self.no_color:
            c.start_color()
            background = c.COLOR_BLACK
            try:
                c.use_default_colors()
                background = -1
            except c.error:
                pass
            for i, (name, color) in enumerate((("live", c.COLOR_CYAN), ("good", c.COLOR_GREEN), ("warn", c.COLOR_YELLOW), ("bad", c.COLOR_RED), ("agent", c.COLOR_MAGENTA), ("files", c.COLOR_BLUE)), 1):
                c.init_pair(i, color, background)
                self.colors[name] = c.color_pair(i)
        c.set_escdelay(25)
        screen.keypad(True)
        screen.timeout(max(1, int(self.refresh * 1000)))
        while True:
            data, chosen = None, None
            try:
                data = snapshot(self.state, self.selected)
                chosen = data["selected"]
                issues = issue_groups(data["rows"])
                nodes = self.nodes(issues, chosen)
                focused = next((node for node in nodes if node[0] == self.focus), None)
                if focused:
                    issue, row = focused[1:]
                    chosen = row or next((r for r in issue["sessions"] if r["active"]), issue["sessions"][-1])
                    if chosen["key"] != self.selected and self.detail:
                        data = snapshot(self.state, chosen["key"])
                    data["selected"] = chosen
                self.selected = chosen["key"] if chosen else None
                self.draw(screen, data)
            except sqlite3.Error as error:
                self.message = f"Database temporarily unavailable: {error}"
                self.put(screen, screen.getmaxyx()[0] - 2, self.message, self.colors.get("bad", 0))
                screen.refresh()
            key = screen.getch()
            if key in (27, ord("b"), ord("q"), 8, 127, c.KEY_BACKSPACE):
                if self.detail:
                    self.detail, self.offset = False, 0
                elif key == ord("q"):
                    return
                continue
            if key in (10, 13, c.KEY_ENTER):
                if not self.detail and self.focus and self.focus[1] is None:
                    number = self.focus[0]
                    if number in self.expanded:
                        self.expanded.remove(number)
                    else:
                        self.expanded.add(number)
                else:
                    self.detail, self.offset = not self.detail, 0
                continue
            if self.detail:
                if key == c.KEY_PPAGE:
                    self.offset += max(1, screen.getmaxyx()[0] - 12)
                elif key == c.KEY_NPAGE:
                    self.offset = max(0, self.offset - max(1, screen.getmaxyx()[0] - 12))
                elif key == c.KEY_END:
                    self.offset = 0
            elif data and self.focus:
                if key in (c.KEY_DOWN, ord("j"), c.KEY_UP, ord("k")):
                    index = next(i for i, node in enumerate(nodes) if node[0] == self.focus)
                    step = 1 if key in (c.KEY_DOWN, ord("j")) else -1
                    self.focus = nodes[(index + step) % len(nodes)][0]
                elif key in (c.KEY_RIGHT, ord("l")):
                    self.expanded.add(self.focus[0])
                elif key in (c.KEY_LEFT, ord("h")):
                    self.expanded.discard(self.focus[0])
                    self.focus = (self.focus[0], None)
            if key == ord("a") and chosen:
                # Refuse before leaving curses if the controller is obviously busy;
                # resume_session acquires the lock again to close the race.
                if data["busy"]:
                    self.message = "Stop Worklane and wait for agents to finish before interactive resume. Enter follows live output."
                    continue
                c.def_prog_mode()
                c.endwin()
                try:
                    resume_session(self.state, chosen["number"], chosen["role"])
                    self.message = "Returned from Codex. Worklane was not started; inspect changes before restarting."
                except (RuntimeError, OSError, ValueError) as error:
                    self.message = str(error)
                finally:
                    c.reset_prog_mode()
                    c.flushinp()
                    self.detail, self.offset = False, 0
                    screen.keypad(True)
                    screen.timeout(max(1, int(self.refresh * 1000)))
                    screen.clear()


def run_tui(state, repo, refresh=1, *, no_color=False):
    if not math.isfinite(refresh) or not .1 <= refresh <= 60:
        raise RuntimeError("--refresh must be between 0.1 and 60 seconds.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("worklane tui requires an interactive terminal; use worklane status or worklane logs for redirected output.")
    try:
        import curses
    except ImportError:
        raise RuntimeError("This Python build has no curses support; use worklane status or worklane logs.") from None
    try:
        curses.wrapper(Dashboard(curses, state, repo, refresh, no_color=no_color).run)
    except KeyboardInterrupt:
        pass  # Quitting a view must never stop the controller.
    except curses.error as error:
        raise RuntimeError(f"Could not initialize terminal UI: {error}; check TERM or use worklane status.") from None
