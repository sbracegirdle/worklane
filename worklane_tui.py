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
        self.message = "Enter: follow selected session live. a: interactive resume after stopping Worklane."
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

    def draw(self, screen, data):
        c = self.curses
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 12 or width < 50:
            self.put(screen, 0, "Worklane · enlarge terminal to at least 50×12; q quits")
            screen.refresh()
            return
        state = "STOPPED" if not data["busy"] else "PAUSED" if data["paused"] else "RUNNING"
        self.put(screen, 0, f" WORKLANE  /  {self.repo}  /  {state}", c.A_BOLD | self.colors.get("live", 0))
        active = [r for r in data["rows"] if r["active"]]
        workers = sum(r["role"] == "worker" for r in active)
        reviews = sum(r["role"] in {"reviewer", "finalizer"} for r in active)
        self.put(screen, 1, f" Workers [{'●' * workers}{'○' * max(0, len(WORKERS) - workers)}] {workers}/3   Review [{'●' if reviews else '○'}] {reviews}/1   {len(data['rows'])} sessions", self.colors.get("live", 0))
        chosen = data["selected"]
        if self.detail:
            label = "SESSION" if chosen else "CONTROLLER"
            self.put(screen, 3, f" LIVE {label} LOG  ·  b/Esc/Enter/q: back  ·  PgUp/PgDn scroll", c.A_BOLD)
        else:
            self.put(screen, 3, " ISSUE / AGENT         STATE             ACTIVITY", c.A_BOLD)
        body_bottom = max(5, height - 7)
        if not self.detail:
            capacity = max(1, body_bottom - 4)
            pages, page_keys, group = [[]], [set()], None
            for row in data["rows"]:
                current = row_group(row)
                header = current != group or not pages[-1]
                needed = 2 + int(header)
                if pages[-1] and len(pages[-1]) + needed > capacity:
                    pages.append([])
                    page_keys.append(set())
                    header = True
                selected = chosen and row["key"] == chosen["key"]
                attr = self.tone(current) | (c.A_BOLD if selected else 0)
                agent = row["task"].get("slot", row["role"]) if row["active"] else row["role"]
                label = f"{'›' if selected else ' '} #{row['number']} {agent}"
                if capacity < 3:
                    pages[-1].append((f"{label} · {current.replace('_', ' ')}", attr))
                else:
                    if header:
                        count = sum(row_group(r) == current for r in data["rows"])
                        pages[-1].append((f" {current.replace('_', ' ').upper()}  ({count})", c.A_BOLD | self.tone(current)))
                    pages[-1].append((f"{label:<22} {row['status']:<17} {plain(row['snippet'])}", attr))
                    pages[-1].append((f"  {row['title']}", self.tone(current)))
                page_keys[-1].add(row["key"])
                group = current
            page = next((i for i, keys in enumerate(page_keys) if chosen and chosen["key"] in keys), 0)
            self.put(screen, 2, f" State groups · page {page + 1}/{len(pages)} · ↑/↓ selects across groups", self.colors.get("live", 0))
            for y, (text, style) in enumerate(pages[page], 4):
                self.put(screen, y, text, style)
            if not data["rows"]:
                self.put(screen, 4, " No assignments yet. Start Worklane in another terminal to watch agents here.")
        else:
            lines = []
            for log in data["logs"]:
                for line in log["content"].splitlines():
                    lines.extend(wrapped(f"{log['created_at'][11:19]} {line}", max(1, width - 2)))
            capacity = body_bottom - 4
            self.offset = min(self.offset, max(0, len(lines) - capacity))
            end = len(lines) - self.offset
            for y, line in enumerate(lines[max(0, end - capacity):end], 4):
                self.put(screen, y, line)
            if not lines:
                self.put(screen, 4, " Waiting for log output…")
        self.put(screen, body_bottom, "─" * (width - 1))
        if chosen:
            session, run = chosen["session"], chosen["run"]
            self.put(screen, body_bottom + 1, f" #{chosen['number']} {chosen['role']} · {chosen['title']} · phase: {chosen['task'].get('phase', '?')}", c.A_BOLD)
            self.put(screen, body_bottom + 2, f" Session: {session.get('session_id', 'not saved yet')} · activity: {chosen['activity_at']}")
            self.put(screen, body_bottom + 3, f" Worktree: {session.get('worktree') or run.get('worktree', 'not created yet')}")
            self.put(screen, body_bottom + 4, f" {chosen['task'].get('error') or run.get('error') or chosen['task'].get('pr_url') or 'Live attach: Enter follows output; interactive resume requires Worklane stopped.'}", self.tone(chosen["status"]))
        else:
            self.put(screen, body_bottom + 1, f" Database: {self.state.database}")
        self.put(screen, height - 2, f" {self.message}", self.colors.get("warn", 0))
        self.put(screen, height - 1, (" b/Esc/Enter/q back · PgUp/PgDn scroll · End follow · refresh " if self.detail else " ↑/↓ j/k select · Enter logs · a resume · q quit · refresh ") + f"{self.refresh:g}s")
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
            for i, (name, color) in enumerate((("live", c.COLOR_CYAN), ("good", c.COLOR_GREEN), ("warn", c.COLOR_YELLOW), ("bad", c.COLOR_RED)), 1):
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
                self.detail, self.offset = not self.detail, 0
                continue
            if self.detail:
                if key == c.KEY_PPAGE:
                    self.offset += max(1, screen.getmaxyx()[0] - 12)
                elif key == c.KEY_NPAGE:
                    self.offset = max(0, self.offset - max(1, screen.getmaxyx()[0] - 12))
                elif key == c.KEY_END:
                    self.offset = 0
            elif chosen and key in (c.KEY_DOWN, ord("j"), c.KEY_UP, ord("k")):
                index = data["rows"].index(chosen)
                step = 1 if key in (c.KEY_DOWN, ord("j")) else -1
                self.selected = data["rows"][(index + step) % len(data["rows"])]["key"]
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
