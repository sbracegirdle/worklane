"""Issue editor integration with real SQLite and mocked GitHub writes."""
import curses
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import Mock, patch

import worklane as w
import worklane_tui as tui
from test_tui import Screen


class IssueEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.state = w.State("owner/repo", self.root)
        self.dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1, root=self.root,
                                       options={"project": "owner/2"})
        self.board = Mock()
        self.board.field = {"options": [{"name": s} for s in ("Draft", w.READY, "Done")]}
        self.item = {"id": "item-id", "title": "Old title", "status": "Draft", "content": {
            "number": 42, "title": "Old title", "body": "Original\nbody", "url": "issue-url"}}
        self.board.items.return_value = [self.item]
        self.board.item_status.return_value = "draft"
        self.board.add_issue.return_value = "created-url"
        self.enterContext(patch.object(w, "Board", return_value=self.board))
        self.enterContext(patch.object(curses, "curs_set"))
        self.enterContext(patch.object(curses, "has_colors", return_value=False))

    def wait_jobs(self):
        deadline = time.monotonic() + 3
        while self.dashboard.jobs and time.monotonic() < deadline:
            self.dashboard.poll()
            time.sleep(.005)
        self.assertFalse(self.dashboard.jobs, "background action did not complete")

    def edit(self):
        return tui.load_editor("owner/repo", self.root, "owner/2", 42)

    def current(self, **changes):
        return dict(self.item["content"], state="OPEN", **changes)

    def test_create_from_empty_overview_multiline_unicode_status_and_cache_invalidation(self):
        self.state.set_meta("tui:issue:1", {"value": {"title": "stale"}})
        self.state.set_meta("tui:board:owner/2", {"value": {"items": []}})
        self.state.set_meta("control", {"paused": True})
        def keys():
            yield "n"
            self.wait_jobs()
            yield curses.KEY_F2  # Empty title stays in the editor without a write.
            self.assertIn("required", self.dashboard.message)
            self.board.add_issue.assert_not_called()
            yield from "Fix 界 and é"
            yield 9
            yield from "First line"
            yield 10
            yield from 'Literal $HOME and `code`'
            yield 9
            yield curses.KEY_LEFT  # Ready -> Draft from real board options.
            yield curses.KEY_F2
            self.wait_jobs()
            self.assertIsNone(self.dashboard.editor)
            yield "q"
        screen = Screen(keys())
        with patch.object(tui, "controller_action") as control:
            self.dashboard.run(screen)
        self.board.add_issue.assert_called_once_with("Fix 界 and é", "First line\nLiteral $HOME and `code`", "Draft")
        self.assertTrue(any("NEW ISSUE" in frame and "F2 save" in frame for frame in screen.frames))
        self.assertEqual(self.state.get_meta("tui:issue:1", {}), {})
        self.assertEqual(self.state.get_meta("tui:board:owner/2", {}), {})
        self.assertEqual(self.state.get_meta("control"), {"paused": True})
        self.assertEqual(self.state.all(), [])
        control.assert_not_called()

    def test_edit_board_item_and_issue_page_return_refresh_and_project_selection(self):
        for source in ("board", "issue"):
            with self.subTest(source=source):
                page = {"kind": source, "key": "board:owner/7" if source == "board" else "issue:42",
                        "number": 42, "project": "owner/7", "value": {"items": [self.item]}, "index": 0, "offset": 0}
                self.dashboard.page = page
                def keys():
                    yield "e"
                    self.wait_jobs()
                    yield curses.KEY_END
                    yield "!"
                    yield 9
                    yield curses.KEY_HOME
                    yield "界"
                    yield 9
                    yield curses.KEY_RIGHT
                    yield curses.KEY_F2
                    self.wait_jobs()
                    yield 27
                    yield "q"
                with patch.object(w, "gh", return_value=self.current()), patch.object(w, "command") as command, \
                     patch.object(tui, "issue_view", return_value=self.current()), \
                     patch.object(tui, "board_view", return_value={"items": [self.item]}):
                    screen = Screen(keys())
                    self.dashboard.run(screen)
                w.Board.assert_any_call("owner/repo", self.root, "owner/7", required_statuses=())
                command.assert_called_once_with(["gh", "issue", "edit", "42", "--repo", "owner/repo",
                                                "--title", "Old title!", "--body-file", "-"], stdin="Original\n界body")
                self.board.move.assert_called_with(self.item, w.READY, "Draft")
                self.assertTrue(any("EDIT ISSUE #42" in frame for frame in screen.frames))
                self.board.reset_mock()

    def test_noop_and_status_only_edits_do_not_rewrite_issue_text(self):
        with patch.object(w, "gh", return_value=self.current()), patch.object(w, "command") as command:
            editor = self.edit()
            self.assertEqual(tui.save_editor("owner/repo", editor), "issue-url")
            self.board.move.assert_not_called()
            editor["status"] = "Done"
            tui.save_editor("owner/repo", editor)
        command.assert_not_called()
        self.board.move.assert_called_once_with(self.item, "Done", "Draft")

    def test_create_supports_custom_boards_and_case_insensitive_ready_default(self):
        for statuses, expected in ((("Draft", "READY FOR WORK"), "READY FOR WORK"), (("Ideas", "Planned"), "Ideas")):
            with self.subTest(statuses=statuses):
                self.board.field = {"options": [{"name": status} for status in statuses]}
                editor = tui.load_editor("owner/repo", self.root, "owner/2")
                self.assertEqual(editor["status"], expected)
                self.assertEqual(editor["title"], "")
                self.board.ensure_review_status.assert_not_called()

    def test_cache_failure_after_confirmed_creation_cannot_repeat_the_write(self):
        self.dashboard.open_editor()
        self.wait_jobs()
        self.dashboard.editor["title"] = "Created once"
        self.dashboard.save_issue()
        self.dashboard.jobs["view"][0].result(timeout=3)
        with patch.object(self.state, "connect", side_effect=sqlite3.OperationalError("locked")):
            self.wait_jobs()
        self.assertIsNone(self.dashboard.editor)
        self.assertIn("Saved created-url; cache refresh failed", self.dashboard.message)
        self.board.add_issue.assert_called_once()

    def test_stale_text_closed_issue_status_drift_or_removed_item_never_write(self):
        editor = self.edit()
        for current in (dict(self.current(), title="External edit"), dict(self.current(), state="CLOSED")):
            with self.subTest(current=current), patch.object(w, "gh", return_value=current), \
                 patch.object(w, "command") as command:
                with self.assertRaisesRegex(RuntimeError, "Issue changed"):
                    tui.save_editor("owner/repo", editor)
                command.assert_not_called()
        self.board.item_status.return_value = "in progress"
        with patch.object(w, "gh", return_value=self.current()), patch.object(w, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "Board status changed"):
                tui.save_editor("owner/repo", editor)
            command.assert_not_called()
        self.board.items.return_value = []
        with self.assertRaisesRegex(RuntimeError, "no longer"):
            self.edit()
        self.board.move.assert_not_called()

    def test_partial_edit_failure_preserves_saved_text_and_draft_for_safe_retry(self):
        editor = self.edit()
        editor.update(title="New title", body="New\nbody", status="Done")
        self.board.move.side_effect = RuntimeError("permission denied")
        with patch.object(w, "gh", return_value=self.current()), patch.object(w, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "Issue text saved; board status was not confirmed"):
                tui.save_editor("owner/repo", editor)
        command.assert_called_once()
        self.assertEqual(editor["original"]["title"], "New title")
        self.board.move.side_effect = None
        with patch.object(w, "gh", return_value=dict(self.current(), title="New title", body="New\nbody")), \
             patch.object(w, "command") as command:
            tui.save_editor("owner/repo", editor)
        command.assert_not_called()

    def test_failed_creation_keeps_draft_and_blocks_duplicate_save(self):
        for error in (RuntimeError("Issue created: issue-url, but board placement failed; do not rerun add."),
                      subprocess.TimeoutExpired("gh", 120)):
            with self.subTest(error=error):
                self.board.add_issue.reset_mock()
                self.board.add_issue.side_effect = error
                self.dashboard.open_editor()
                self.wait_jobs()
                self.dashboard.editor.update(title="Keep me", body="Saved draft")
                self.dashboard.save_issue()
                self.wait_jobs()
                self.assertIn("inspect the board", self.dashboard.message)
                self.assertEqual(self.dashboard.editor["body"], "Saved draft")
                self.dashboard.save_issue()
                self.board.add_issue.assert_called_once()
                self.dashboard.editor_key(27, "\x1b")
                self.assertIsNone(self.dashboard.editor)

    def test_cancel_loading_and_error_retain_responsive_terminal(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
            raise RuntimeError("offline")
        with patch.object(tui, "load_editor", side_effect=slow), patch.object(w, "command") as command:
            self.dashboard.open_editor()
            self.assertTrue(entered.wait(2))
            screen = Screen([27, "q"])
            try:
                self.dashboard.run(screen)
                self.assertIsNone(self.dashboard.editor)
            finally:
                release.set()
                self.wait_jobs()
        self.assertIn("offline", self.dashboard.message)
        command.assert_not_called()

    def test_save_runs_once_and_cannot_be_cancelled_while_in_flight(self):
        self.dashboard.editor = self.edit()
        self.dashboard.editor["status"] = "Done"
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
            return "issue-url"
        with patch.object(tui, "save_editor", side_effect=slow) as save:
            self.dashboard.save_issue()
            self.assertTrue(entered.wait(2))
            try:
                self.dashboard.editor_key(curses.KEY_F2, "")
                self.dashboard.editor_key(27, "\x1b")
                self.assertIsNotNone(self.dashboard.editor)
                self.assertTrue(self.dashboard.editor["saving"])
            finally:
                release.set()
                self.wait_jobs()
        save.assert_called_once()

    def test_input_navigation_preserves_unicode_newlines_and_literal_hotkeys(self):
        editor = self.edit()
        self.dashboard.editor = editor
        self.dashboard.editor_key(curses.KEY_HOME, "")
        self.dashboard.editor_key(curses.KEY_DC, "")
        self.dashboard.editor_key(ord("q"), "q")
        self.dashboard.editor_key(9, "\t")
        self.dashboard.editor_key(curses.KEY_UP, "")
        self.dashboard.editor_key(curses.KEY_HOME, "")
        self.dashboard.editor_key(ord("界"), "界")
        self.dashboard.editor_key(curses.KEY_DOWN, "")
        self.dashboard.editor_key(curses.KEY_BACKSPACE, "")
        self.dashboard.editor_key(curses.KEY_BTAB, "")
        self.assertEqual(editor["title"], "qld title")
        self.assertEqual(editor["body"], "界Original\nody")
        self.assertEqual(editor["field"], 0)
        self.dashboard.editor_key(27, "\x1b")
        self.assertIsNone(self.dashboard.editor)

    def test_small_terminal_long_title_multiline_body_and_empty_status(self):
        self.item["status"] = ""
        self.dashboard.editor = self.edit()
        editor = self.dashboard.editor
        self.assertEqual(editor["statuses"][0], "")
        editor.update(title="界" * 100, body="\n".join(str(i) for i in range(50)), field=1)
        editor["cursor"] = len(editor["body"])
        screen = Screen(size=(12, 50))
        self.dashboard.draw(screen, tui.snapshot(self.state))
        self.assertIn("49▏", screen.frames[-1])
        self.assertIn("Status: No status", screen.frames[-1])
        self.assertIn("F2 save", screen.frames[-1])
        editor["field"], editor["cursor"] = 0, len(editor["title"])
        self.dashboard.draw(screen, tui.snapshot(self.state))
        self.assertIn("▏", screen.lines[3])

    def test_load_failure_and_budget_cooldown_do_not_write(self):
        self.board.field = {"options": []}
        self.dashboard.open_editor()
        self.wait_jobs()
        self.assertTrue(self.dashboard.editor["failed"])
        self.assertIn("no Status options", self.dashboard.message)
        self.dashboard.editor_key(27, "\x1b")
        budget = w.GitHubBudget(self.state)
        budget.record(w.GitHubRateLimit("rate limit exhausted", time.time() + 300))
        with patch.object(w, "raw_command") as transport:
            self.dashboard.editor = {
                "board": self.board, "item": None, "number": None, "title": "Draft", "body": "", "status": "Draft"}
            self.board.add_issue.side_effect = lambda *args: w.command(["gh", "issue", "create"])
            self.dashboard.save_issue()
            self.wait_jobs()
        transport.assert_not_called()
        self.assertIn("rate limit", self.dashboard.message)

    def test_real_terminal_create_unicode_multiline_status_resize_and_save(self):
        w.save_config(self.root, {})  # Exercise the issue editor after initial setup.
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        capture = self.root / "created.json"
        script = f'''
import json
from pathlib import Path
import worklane as w
import worklane_tui as tui
class Board:
    field = {{"options": [{{"name": "Draft"}}, {{"name": w.READY}}]}}
    def __init__(self, *args, **kwargs):
        pass
    def add_issue(self, title, body, status):
        Path({str(capture)!r}).write_text(json.dumps([title, body, status]))
        return "created-url"
w.Board = Board
state = w.State("owner/repo", Path({str(self.root)!r}))
tui.run_tui(state, "owner/repo", .1, root=Path({str(self.root)!r}))
'''
        process = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm-256color"})
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        def until(token):
            output = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 65536)
                    if token in output:
                        return output
                if process.poll() is not None:
                    break
            self.fail(f"Terminal did not show {token!r}: {output!r}")
        with w.locked(self.state.path / "runner.lock"):
            until(b"ISSUES & SESSIONS")
            os.write(master, b"n")
            until(b"Tab changes fields")
            os.write(master, "Fix 界\tFirst\nSecond\t".encode())
            until(b"Status: Ready for work")
            os.write(master, b"\x1bOD")  # xterm application-mode Left.
            until(b"Draft")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 50, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            until(b"NEW ISSUE")
            os.write(master, b"\x1bOQ")  # xterm F2, unaffected by terminal flow control.
            until(b"Saved created-url")
            os.write(master, b"q")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue(self.state.running())
            self.assertFalse(self.state.paused())
        self.assertEqual(json.loads(capture.read_text()), ["Fix 界", "First\nSecond", "Draft"])


if __name__ == "__main__":
    unittest.main()
