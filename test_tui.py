"""Dashboard integration tests use temporary SQLite state; no real agents/boards."""
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

import worklane as w
import worklane_tui as tui


class Screen:
    def __init__(self, keys=(), size=(28, 110)):
        self.keys, self.size, self.lines = iter(keys), size, {}
        self.frames = []
        self.styles = {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines = {}
        self.styles = {}

    def addstr(self, y, x, text, style):
        assert 0 <= y < self.size[0]
        before = self.lines.get(y, "")
        self.lines[y] = before[:x].ljust(x) + text
        self.styles.setdefault(y, []).append((text, style))

    def refresh(self):
        self.frames.append("\n".join(self.lines.values()))

    def get_wch(self):
        key = next(self.keys, ord("q"))
        return chr(key) if isinstance(key, int) and 0 <= key < 256 else key

    def keypad(self, enabled):
        pass

    def timeout(self, ms):
        pass

    def clear(self):
        self.erase()


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = w.State("owner/repo", self.root)
        self.tree = self.root / "checkout"
        self.tree.mkdir()

    def run_record(self, number=1, stage="build", text="Inspecting source"):
        task = self.state.load(number) or {"number": number, "issue": {"title": f"Task {number}"}}
        task["phase"] = {"build": "building", "review": "reviewing", "finalize": "finalizing"}[stage]
        run = self.state.begin_run(task, stage, self.tree)
        session = self.state.session(number, w.ROLES[stage])
        self.state.record_event(run, {"type": "thread.started", "thread_id": session["session_id"] if session else str(uuid.uuid4())}, w.MODEL)
        self.state.record_event(run, {"type": "item.completed", "item": {"type": "agent_message", "text": text}}, w.MODEL)
        return run

    def test_snapshot_tracks_live_changes_and_separate_roles_without_writing(self):
        run = self.run_record()
        with w.locked(self.state.path / "runner.lock"):
            data = tui.snapshot(self.state)
            self.assertTrue(data["busy"])
            self.assertTrue(data["selected"]["active"])
            self.assertEqual(data["selected"]["snippet"], "Inspecting source")
            before = self.state.load(1)
            self.state.record_event(run, {"type": "item.completed", "item": {"type": "command_execution", "command": "uv run python -m unittest", "aggregated_output": "OK"}}, w.MODEL)
            self.state.record_event(run, {"type": "turn.completed"}, w.MODEL)
            self.state.finish_run(run, "completed")
            review = self.run_record(stage="review", text="Checking acceptance criteria")
            self.state.pause(True)
            data = tui.snapshot(self.state, (1, "worker"))
            self.assertTrue(data["paused"])
            self.assertEqual(len(data["rows"]), 2)
            self.assertEqual(data["selected"]["key"], (1, "worker"))
            self.assertEqual(data["rows"][0]["role"], "reviewer")
            self.assertIn("$ uv run python -m unittest\nOK", data["selected"]["snippet"])
            self.assertEqual(data["selected"]["status"], "completed")
            self.assertFalse(data["selected"]["active"])
            self.assertNotEqual(data["rows"][0]["session"]["session_id"], data["selected"]["session"]["session_id"])
            after = self.state.load(1)
            tui.snapshot(self.state)
            self.assertEqual(self.state.load(1), after)
            self.assertEqual(before["number"], after["number"])
            self.assertEqual(self.state.runs(1)[-1]["id"], review)

    def test_latest_invocation_only_and_bounded_tail(self):
        old = self.run_record(text="Old invocation")
        self.state.finish_run(old, "completed")
        new = self.run_record(text="New invocation")
        for i in range(100):
            self.state.append_log(new, "stderr", f"line {i}")
        data = tui.snapshot(self.state)
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["selected"]["run"]["id"], new)
        self.assertEqual(len(data["logs"]), 40)
        self.assertEqual(data["logs"][0]["content"], "line 60")
        self.assertEqual(data["selected"]["snippet"], "line 99")
        self.assertEqual(data["selected"]["status"], "interrupted")

    def test_completed_issue_updates_all_role_rows_without_rewriting_history(self):
        for stage in ("build", "review", "finalize"):
            run = self.run_record(stage=stage)
            self.state.finish_run(run, "failed" if stage == "build" else "completed")
        task = self.state.load(1)
        task["phase"] = "done"
        self.state.save(task)
        history = self.state.runs(1)
        data = tui.snapshot(self.state)
        self.assertEqual({row["status"] for row in data["rows"]}, {"completed"})
        self.assertEqual(tui.issue_groups(data["rows"])[0]["status"], "completed")
        self.assertEqual(self.state.runs(1), history)

    def test_search_finds_metadata_and_historical_text_outside_latest_tail(self):
        old = self.run_record(text="Historical needle")
        old_session = self.state.session(1, "worker")["session_id"]
        for i in range(65):
            self.state.append_log(old, "agent", f"Old filler {i}")
        self.state.finish_run(old, "completed")
        new = self.run_record(text="Latest invocation only")
        new_session = self.state.session(1, "worker")["session_id"]
        self.run_record(stage="review", text="Reviewer needle")
        self.run_record(2, text="Other issue needle")
        rows = tui.snapshot(self.state)["rows"]
        before = (self.state.all(), self.state.runs(), self.state.log_entries())
        for query in ("#1", "TASK 1", "worker", new_session, str(self.tree)):
            with self.subTest(query=query):
                self.assertTrue(tui.search_sessions(self.state, query, rows)["results"])
        saved = tui.search_sessions(self.state, old_session, rows)["results"]
        self.assertTrue(any(r["row"]["run"]["id"] == old and r["log"] is None for r in saved))
        results = tui.search_sessions(self.state, "NEEDLE", rows)["results"]
        self.assertEqual({r["row"]["key"] for r in results}, {(1, "worker"), (1, "reviewer"), (2, "worker")})
        hit = next(r for r in results if r["row"]["run"]["id"] == old)
        self.assertEqual(hit["row"]["session"]["session_id"], old_session)
        context = tui.search_context(self.state, hit)
        self.assertIn("Historical needle", [log["content"] for log in context])
        self.assertLessEqual(len(context), 40)
        self.assertEqual({log["run_id"] for log in context}, {old})
        self.assertEqual(before, (self.state.all(), self.state.runs(), self.state.log_entries()))

    def test_search_is_literal_unicode_readable_and_bounded(self):
        run = self.run_record(text="Straße 界 %_' literal")
        self.state.append_log(run, "agent", "ß" * 100 + " unique marker")
        self.state.append_log(run, "agent", "item.completed")
        self.state.append_log(run, "stderr", "2026-10-19T10:05:00Z WARN codex_mcp::client: hidden diagnostic")
        self.state.append_log(None, "controller", "Controller secret")
        rows = tui.snapshot(self.state)["rows"]
        for query in ("STRASSE", "界", "%_'", "unique"):
            with self.subTest(query=query):
                result = tui.search_sessions(self.state, query, rows)["results"]
                self.assertEqual(len(result), 1)
                self.assertIn(query.casefold(), result[0]["text"].casefold())
        for query in ("item.completed", "hidden diagnostic", "Controller secret", "absent", " "):
            self.assertEqual(tui.search_sessions(self.state, query, rows)["results"], [])
        for i in range(105):
            self.state.append_log(run, "agent", f"capped match {i}")
        result = tui.search_sessions(self.state, "capped match", rows)
        self.assertEqual(len(result["results"]), 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["results"][0]["text"], "capped match 104")

    def test_search_input_navigation_historical_context_and_back(self):
        old = self.run_record(text="Historic query 界ć")
        self.state.finish_run(old, "completed")
        self.run_record(text="Current output")
        keys = [ord("/"), *"query 界ćx", curses.KEY_BACKSPACE, 10, 10, ord("a"),
                curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_END, ord("b"), 27, ord("q")]
        screen = Screen(keys)
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        before = self.state.all()
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(tui, "resume_session") as resume:
            dashboard.run(screen)
        self.assertEqual(dashboard.search_query, "query 界ć")
        match = next(frame for frame in screen.frames if "MATCH SESSION LOG" in frame)
        self.assertIn("Historic query 界ć", match)
        self.assertNotIn("Current output", match)
        self.assertTrue(any("Return to the overview" in frame for frame in screen.frames))
        self.assertIn("ISSUES & SESSIONS", screen.frames[-1])
        self.assertFalse(dashboard.searching)
        self.assertEqual(dashboard.focus, (1, None))
        self.assertEqual(before, self.state.all())
        resume.assert_not_called()

    def test_search_input_and_historical_transcript_do_not_control_runner(self):
        self.run_record(text="sxiB")
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        dashboard.control = Mock()
        dashboard.open_page = Mock()
        keys = [ord("/")] + list("sxiB") + [10, 10, ord("s"), ord("x"), ord("q"), ord("q"), ord("q")]
        with patch.object(curses, "has_colors", return_value=False), patch.object(curses, "curs_set"), patch.object(curses, "set_escdelay"):
            dashboard.run(Screen(keys))
        self.assertEqual(dashboard.query, "sxiB")
        dashboard.control.assert_not_called()
        dashboard.open_page.assert_not_called()

    def test_search_no_matches_edit_cancel_and_restore_transcript(self):
        self.run_record(text="Current output")
        screen = Screen([ord("j"), 10, ord("/"), *"missing", 10, ord("/"), 27,
                         ord("/"), 27, ord("b"), ord("q")])
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            dashboard.run(screen)
        self.assertTrue(any("No matches" in frame for frame in screen.frames))
        self.assertTrue(any("LIVE SESSION LOG" in frame for frame in screen.frames[14:]))
        self.assertFalse(dashboard.searching)

    def test_search_metadata_results_open_selected_session_and_scroll(self):
        for number in range(1, 9):
            self.run_record(number, text=f"Output {number}")
        screen = Screen([ord("/"), *"worker", 10, curses.KEY_UP, 10, ord("b"),
                         ord("/"), "x", 27, curses.KEY_DOWN, 10, ord("b"), ord("q"), ord("q")], size=(16, 80))
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            dashboard.run(screen)
        matches = [frame for frame in screen.frames if "MATCH SESSION LOG" in frame]
        self.assertIn("Output 8", matches[0])
        self.assertIn("Output 1", matches[-1])
        self.assertEqual(dashboard.query, "worker")
        self.assertEqual(dashboard.search_query, "worker")

    def test_search_opens_match_inside_long_wrapped_log(self):
        for text in ("visible needle", "visible\nneedle"):
            with self.subTest(text=text):
                self.run_record(text="prefix " * 500 + text + " suffix" * 500)
                screen = Screen([ord("/"), *"visible needle", 10, 10, ord("b"), ord("q"), ord("q")])
                dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
                with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
                    dashboard.run(screen)
                match = next(frame for frame in screen.frames if "MATCH SESSION LOG" in frame)
                self.assertIn("visible", match)
                self.assertIn("needle", match)

    def test_search_boundary_match_is_visible_in_small_terminals(self):
        for prefix, needle, suffix in (("x" * 172, "NEEDLE", "y" * 300),
                                       ("界" * 84, "探針", "y" * 300)):
            self.run_record(text=prefix + needle + suffix)
            for height in (12, 13, 14, 28):
                with self.subTest(height=height, needle=needle):
                    screen = Screen(["/", *needle, 10, 10, "b", "q", "q"], size=(height, 50))
                    dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
                    with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
                        dashboard.run(screen)
                    match = next(frame for frame in screen.frames if "MATCH SESSION LOG" in frame)
                    self.assertIn(needle, match)

    def test_empty_and_pre_session_assignments_and_blockers(self):
        self.assertEqual(tui.snapshot(self.state)["rows"], [])
        self.state.save({"number": 9, "phase": "blocked", "error": "Provider unavailable"})
        self.state.append_log(None, "controller", "Controller output belongs to no session")
        self.run_record()
        data = tui.snapshot(self.state, (99, "worker"))
        self.assertEqual(data["selected"]["number"], 9)
        self.assertEqual(data["selected"]["task"]["error"], "Provider unavailable")
        self.assertEqual(data["logs"], [])
        task = self.state.load(1)
        task["phase"] = "finalizing"
        self.state.save(task)
        self.assertIn((1, "finalizer"), [r["key"] for r in tui.snapshot(self.state)["rows"]])

    def test_readers_can_follow_concurrent_committed_output(self):
        run = self.run_record()
        def write():
            for i in range(20):
                self.state.append_log(run, "agent", f"update {i}")
        writer = threading.Thread(target=write)
        writer.start()
        for _ in range(10):
            self.assertEqual(tui.snapshot(self.state)["selected"]["run"]["id"], run)
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(tui.snapshot(self.state)["selected"]["snippet"], "update 19")

    def test_resume_uses_exact_role_session_and_holds_inherited_lock(self):
        self.run_record(stage="build")
        self.run_record(stage="review")
        session = self.state.session(1, "reviewer")["session_id"]
        before = self.state.load(1)
        def invoke(args, cwd, pass_fds):
            self.assertEqual(args[:3], ["codex", "resume", session])
            self.assertIn("workspace-write", args)
            self.assertEqual(cwd, self.tree)
            self.assertTrue(self.state.running())
            self.assertTrue(pass_fds)
            with self.assertRaises(RuntimeError):
                with w.locked(self.state.path / "runner.lock", blocking=False):
                    pass
            return subprocess.CompletedProcess(args, 0)
        with patch.object(tui.shutil, "which", return_value="/fake/codex"):
            tui.resume_session(self.state, 1, "reviewer", invoke=invoke)
        self.assertFalse(self.state.running())
        self.assertEqual(before, self.state.load(1))

    def test_resume_refuses_controller_race_or_orphan_process(self):
        run = self.run_record()
        invoke = Mock()
        with w.locked(self.state.path / "runner.lock"):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                tui.resume_session(self.state, 1, "worker", invoke=invoke)
        task = self.state.load(1)
        self.state.attach_process(run, task, os.getpid())
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            tui.resume_session(self.state, 1, "worker", invoke=invoke)
        invoke.assert_not_called()

    def test_resume_missing_session_missing_worktree_and_failed_cli(self):
        with self.assertRaisesRegex(RuntimeError, "No saved session"):
            tui.resume_session(self.state, 1, "worker")
        self.run_record()
        self.tree.rmdir()
        with self.assertRaisesRegex(RuntimeError, "worktree is missing"):
            tui.resume_session(self.state, 1, "worker")
        self.tree.mkdir()
        with patch.object(tui.shutil, "which", return_value="/fake/codex"):
            with self.assertRaisesRegex(RuntimeError, "exited 2"):
                tui.resume_session(self.state, 1, "worker", invoke=Mock(return_value=subprocess.CompletedProcess([], 2)))
        self.assertFalse(self.state.running())

    def test_terminal_rendering_navigation_and_quitting_do_not_control_runner(self):
        self.run_record()
        self.run_record(2, text="Running tests")
        screen = Screen([ord("j"), ord("j"), 10, ord("j"), 10, curses.KEY_PPAGE, curses.KEY_END, 27, ord("a"), ord("q")])
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        before = self.state.all()
        with w.locked(self.state.path / "runner.lock"), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(tui, "resume_session") as resume:
            dashboard.run(screen)
            resume.assert_not_called()
        self.assertEqual(dashboard.selected, (2, "worker"))
        self.assertTrue(any("LIVE SESSION LOG" in frame for frame in screen.frames))
        self.assertTrue(any("Running tests" in frame for frame in screen.frames))
        self.assertTrue(any("2/3" in frame for frame in screen.frames))
        self.assertTrue(any("Stop Worklane" in frame for frame in screen.frames))
        self.assertEqual(before, self.state.all())
        self.assertFalse(self.state.paused())

    def test_small_terminal_and_monochrome_and_unicode_clipping(self):
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        for size in ((1, 1), (5, 30), (12, 50), (28, 110)):
            screen = Screen(size=size)
            dashboard.draw(screen, tui.snapshot(self.state))
            self.assertTrue(screen.frames)
            if size[0] >= 12 and size[1] >= 50:
                for control in ("s start", "x stop", "i issue", "B board", "q quit"):
                    self.assertIn(control, screen.frames[-1])
        self.assertEqual(tui.clipped("你好world", 5), "你好w")
        paragraph = "Inspecting source before running tests"
        self.assertEqual("".join(tui.wrapped(paragraph, 20)), paragraph)
        self.assertEqual(tui.wrapped(paragraph, 20)[0], "Inspecting source ")
        self.assertEqual(tui.clipped("e\u0301", 1), "e\u0301")
        self.assertNotIn("\x1b", tui.plain("\x1b[31mred\x1b[0m"))
        self.assertNotIn("\x07", tui.plain("bell\x07"))

    def test_detail_wraps_by_terminal_cells_without_losing_unicode(self):
        content = "界" * 60
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        dashboard.detail = True
        screen = Screen(size=(28, 80))
        data = tui.snapshot(self.state)
        data["logs"] = [{"created_at": "2026-10-19T12:34:56Z", "content": content}]
        dashboard.draw(screen, data)
        body = "".join(line[4:] for y, line in screen.lines.items() if 4 <= y < 22 and line.startswith("  │ "))
        self.assertEqual(body, content)

    def test_real_curses_terminal_refresh_resize_logs_and_quit(self):
        old = self.run_record(text="Historical terminal needle")
        self.state.finish_run(old, "completed")
        run = self.run_record(text="Inspecting source")
        issue = {"number": 1, "title": "Task 1", "body": "Issue details from persistent cache", "comments": []}
        self.state.set_meta("tui:issue:1", {"value": issue, "retry_at": time.time() + 60})
        self.state.set_meta("tui:board:auto", {"value": {"url": "board-url", "items": [
            {"status": "In Progress", "title": "Task 1", "content": issue}]}, "retry_at": time.time() + 60})
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        # A real CLI process against a disposable checkout/state, never the live controller.
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        subprocess.run(["git", "-C", str(self.tree), "remote", "add", "origin", "https://github.com/owner/repo.git"], check=True)
        env = {**os.environ, "TERM": "xterm-256color", "XDG_STATE_HOME": str(self.root)}
        env.pop("NO_COLOR", None)  # Exercise colour output independently of the caller's environment.
        process = subprocess.Popen([sys.executable, str(Path(w.__file__).resolve()), "tui", "--repo", str(self.tree), "--refresh", "0.1"],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
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
            output = until(b"Inspecting source")
            self.assertIn(b"WORKLANE", output)
            self.assertIn(b"[36m", output)  # Cyan activity indicators in a colour terminal.
            os.write(master, b"B")
            until(b"PROJECT BOARD")
            os.write(master, b"\n")
            until(b"Issue details from persistent cache")
            os.write(master, b"b")
            until(b"PROJECT BOARD")
            os.write(master, b"b")
            until(b"ISSUES & SESSIONS")
            self.state.append_log(run, "agent", "Fresh output from agent")
            until(b"Fresh output")
            os.write(master, b"j\n")
            transcript = until(b"LIVE SESSION LOG")
            self.assertRegex(transcript, rb"\x1b\[[0-9;]*35m")
            self.state.append_log(run, "agent", "$ uv run tests\nTranscript command output")
            command = until(b"Transcript command output")
            self.assertIn(b"COMMAND", command)
            self.assertRegex(command, rb"\x1b\[[0-9;]*36m")
            os.write(master, b"m/run pwd\n")
            until(b"queued for the selected session")
            self.assertTrue(any(log["stream"] == "user" and log["content"].endswith("\npwd")
                                for log in self.state.log_entries(1)))
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 5, 30, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            until(b"enlarge terminal")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            until(b"LIVE SESSION LOG")
            # All back keys must leave the session view, including after resize.
            for key in (b"b", b"\x1b", b"q"):
                os.write(master, key)
                until(b"ISSUES & SESSIONS")
                self.assertIsNone(process.poll())
                if key != b"q":
                    os.write(master, b"\n")
                    until(b"LIVE SESSION LOG")
            os.write(master, b"/terminal needle\n")
            until(b"SEARCH")
            os.write(master, b"\n")
            match = until(b"MATCH SESSION LOG")
            self.assertIn(b"Historical terminal needle", match)
            os.write(master, b"b")
            until(b"SEARCH")
            os.write(master, b"\x1b")
            until(b"ISSUES & SESSIONS")
            os.write(master, b"q")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue(self.state.running())
            self.assertFalse(self.state.paused())

    def test_message_composer_handles_unicode_commands_and_cancel_without_shortcuts(self):
        self.run_record()
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        dashboard.control, dashboard.open_page = Mock(), Mock()
        keys = [ord("j"), 10, ord("m"), *"/help", 10, *"sxiBq 界x", curses.KEY_BACKSPACE, 10,
                ord("m"), *"/clear", 10, *"cancel me", 27,
                ord("m"), *"/bad", 10, 21, *"/run pwd", 10,
                ord("m"), *"//literal", 10, ord("b"), ord("q")]
        screen = Screen(keys)
        with w.locked(self.state.path / "runner.lock"), patch.object(curses, "curs_set"), \
             patch.object(curses, "has_colors", return_value=False):
            dashboard.run(screen)
        users = [log["content"] for log in self.state.log_entries(1) if log["stream"] == "user"]
        self.assertEqual(users, ["sxiBq 界", "Run this command in the assigned session worktree, within existing safeguards:\npwd", "/literal"])
        self.assertTrue(any("Unknown or incomplete command" in frame for frame in screen.frames))
        self.assertTrue(any("YOU" in frame for frame in screen.frames))
        self.assertTrue(any("Input cancelled" in frame for frame in screen.frames))
        self.assertFalse(dashboard.composing)
        dashboard.control.assert_not_called()
        dashboard.open_page.assert_not_called()

    def test_composer_preserves_draft_on_late_submission_and_pins_original_run(self):
        run = self.run_record()
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        dashboard.composing, dashboard.input_run, dashboard.draft = True, run, "Keep this draft"
        with w.locked(self.state.path / "runner.lock"):
            self.state.finish_run(run, "completed")
            self.run_record(2)
            dashboard.send_input()
        self.assertTrue(dashboard.composing)
        self.assertEqual(dashboard.draft, "Keep this draft")
        self.assertIn("no longer accepting", dashboard.message)
        self.assertFalse(any(log["stream"] == "user" for log in self.state.log_entries()))

    def test_issue_editor_and_session_composer_keep_input_separate(self):
        self.run_record()
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        editor = {"number": None, "title": "", "body": "", "status": "Draft",
                  "statuses": ["Draft"], "field": 0, "cursor": 0}
        def keys():
            yield "n"
            self.wait_jobs(dashboard)
            yield from "m/run editor title"
            yield 9
            yield from "Session shortcuts m B q stay in the issue body"
            self.assertFalse(dashboard.composing)
            yield 27
            yield "j"
            yield 10
            yield "m"
            yield from "n e stay in the session message"
            yield 10
            yield "b"
            yield "q"
        screen = Screen(keys())
        with w.locked(self.state.path / "runner.lock"), patch.object(curses, "curs_set"), \
             patch.object(curses, "has_colors", return_value=False), \
             patch.object(tui, "load_editor", return_value=editor) as load:
            dashboard.run(screen)
        load.assert_called_once()
        self.assertEqual(editor["title"], "m/run editor title")
        self.assertEqual(editor["body"], "Session shortcuts m B q stay in the issue body")
        self.assertEqual([log["content"] for log in self.state.log_entries(1) if log["stream"] == "user"],
                         ["n e stay in the session message"])
        self.assertTrue(any("NEW ISSUE" in frame for frame in screen.frames))
        self.assertTrue(any("Enter queue" in frame for frame in screen.frames))
        self.assertIsNone(dashboard.editor)
        self.assertFalse(dashboard.composing)

    def test_composer_refuses_historical_and_stopped_sessions(self):
        run = self.run_record(text="needle")
        self.state.finish_run(run, "completed")
        screen = Screen([ord("j"), 10, ord("m"), ord("b"), ord("/"), *"needle", 10, 10,
                         ord("m"), ord("b"), ord("q"), ord("q")])
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            dashboard.run(screen)
        self.assertTrue(any("Open an active saved session" in frame for frame in screen.frames))
        self.assertTrue(any("Return to the live transcript" in frame for frame in screen.frames))
        self.assertFalse(any(log["stream"] == "user" for log in self.state.log_entries()))

    def test_protocol_and_mcp_noise_do_not_replace_activity_or_fill_tail(self):
        run = self.run_record(text="Useful agent activity")
        warning = "2026-10-19T10:05:00Z WARN codex_mcp::rmcp_client: MCP server startup failed"
        transport = "2026-10-19T10:05:00Z ERROR rmcp::transport::worker: worker quit with fatal"
        for _ in range(60):
            self.state.append_log(run, "stderr", warning)
            self.state.append_log(run, "stderr", transport)
            self.state.append_log(run, "agent", "item.started")
            self.state.append_log(run, "agent", "item.completed")
        data = tui.snapshot(self.state)
        self.assertEqual(data["selected"]["snippet"], "Useful agent activity")
        self.assertEqual([log["content"] for log in data["logs"]], ["Useful agent activity"])
        self.state.append_log(run, "stderr", "Agent failed: required MCP tool unavailable")
        self.assertIn("required MCP tool unavailable", tui.snapshot(self.state)["selected"]["snippet"])
        self.assertTrue(any(log["content"] == warning for log in self.state.log_entries(1)))

    def test_structured_results_render_as_summary_and_findings_without_json(self):
        report = {"verdict": "changes_requested", "summary": "Review complete. Add tests for retries.",
                  "pr_url": "https://github.com/owner/repo/pull/1", "head_sha": "a" * 40,
                  "findings": ["Handle the interrupted task.", "Keep the original session."]}
        for indent in (None, 2):
            with self.subTest(indent=indent):
                raw = json.dumps(report, indent=indent)
                run = self.run_record(text=raw)
                data = tui.snapshot(self.state)
                self.assertTrue(data["selected"]["snippet"].startswith(report["summary"]))
                text = data["logs"][-1]["content"]
                self.assertIn("Changes requested", text)
                self.assertIn("PR: " + report["pr_url"], text)
                self.assertIn("Revision: " + report["head_sha"], text)
                self.assertIn("- Handle the interrupted task.", text)
                self.assertNotIn('"verdict"', text)
                self.assertNotIn('"summary"', text)
                dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
                screen = Screen(size=(36, 110))
                dashboard.detail = True
                dashboard.draw(screen, data)
                self.assertIn(report["summary"], screen.frames[-1])
                self.assertNotIn('"findings"', screen.frames[-1])
                self.assertEqual(self.state.log_entries(1)[-1]["content"], raw)
                self.state.finish_run(run, "completed")
        report.update(verdict="pass", findings=[])
        text = w.readable_log("agent", json.dumps(report))
        self.assertIn("Passed", text)
        self.assertNotIn("Findings:", text)
        # Ordinary command JSON and incomplete output remain available verbatim.
        for content in ('{"answer": 42}', '{"summary": "unfinished',
                        '{"summary":"Invalid report", "verdict":"pass", "findings":42}'):
            self.assertEqual(w.readable_log("agent", content), content)

    def test_logs_default_to_readable_output_with_raw_opt_in(self):
        run = self.run_record(text="Meaningful output")
        warning = "2026-10-19T10:05:00Z WARN codex_mcp::rmcp_client: startup failed"
        self.state.append_log(run, "stderr", warning)
        self.state.append_log(run, "agent", "item.completed")
        with patch.object(w, "repository", return_value=("owner/repo", self.tree, None)), \
             patch.object(w, "State", return_value=self.state):
            out = io.StringIO()
            with patch.object(w.sys, "stdout", out):
                self.assertEqual(w.main(["logs", "1"]), 0)
            self.assertIn("Meaningful output", out.getvalue())
            self.assertNotIn("item.completed", out.getvalue())
            self.assertNotIn("codex_mcp", out.getvalue())
            out = io.StringIO()
            with patch.object(w.sys, "stdout", out):
                self.assertEqual(w.main(["logs", "1", "--raw"]), 0)
            self.assertIn("item.completed", out.getvalue())
            self.assertIn("codex_mcp", out.getvalue())

    def test_issue_tree_groups_each_issue_once_with_role_sessions_underneath(self):
        worker = self.run_record(1)
        self.state.finish_run(worker, "completed")
        self.run_record(1, stage="review", text="Independent review")
        self.state.save({"number": 2, "phase": "blocked", "issue": {"title": "Blocked task"}})
        run = self.run_record(3)
        self.state.finish_run(run, "completed")
        task = self.state.load(3)
        task["phase"] = "human_review"
        self.state.save(task)
        with w.locked(self.state.path / "runner.lock"):
            data = tui.snapshot(self.state)
            issues = tui.issue_groups(data["rows"])
            self.assertEqual([(i["number"], i["status"]) for i in issues], [(1, "running"), (2, "blocked"), (3, "human_review")])
            self.assertEqual([r["role"] for r in issues[0]["sessions"]], ["worker", "reviewer"])
            dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
            screen = Screen(size=(28, 110))
            dashboard.draw(screen, data)
            frame = screen.frames[-1]
            self.assertIn("RUNNING  ·  1 issue", frame)
            self.assertIn("BLOCKED  ·  1 issue", frame)
            self.assertEqual(sum("#1  Task 1" in line for y, line in screen.lines.items() if y < 24), 1)
            self.assertIn("├─ worker", frame)
            self.assertIn("└─ reviewer", frame)
            self.assertNotIn("Session:", frame)
            self.assertNotIn(str(self.tree), frame)
            # An issue remains visible with its selected child in a small viewport.
            dashboard.focus = (1, "reviewer")
            screen = Screen(size=(12, 50))
            dashboard.draw(screen, data)
            self.assertTrue(any("#1  Task 1" in line for y, line in screen.lines.items() if 4 <= y < 8))
            self.assertTrue(any("reviewer" in line for y, line in screen.lines.items() if 4 <= y < 8))

    def test_issue_expand_collapse_and_session_navigation_open_the_correct_logs(self):
        self.run_record(1, text="Worker one")
        self.run_record(1, stage="review", text="Reviewer one")
        self.run_record(2, text="Worker two")
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        # Collapse issue one, select and expand issue two, then open its worker.
        screen = Screen([curses.KEY_LEFT, ord("j"), curses.KEY_RIGHT, ord("j"), 10, ord("b"), curses.KEY_LEFT, ord("q")])
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            dashboard.run(screen)
        detail = next(frame for frame in screen.frames if "LIVE SESSION LOG" in frame)
        self.assertIn("Worker two", detail)
        self.assertNotIn("Reviewer one", detail)
        self.assertEqual(dashboard.focus, (2, None))
        self.assertNotIn(2, dashboard.expanded)

    def test_transcript_uses_coloured_speakers_commands_errors_and_verdicts(self):
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        dashboard.detail = True
        dashboard.colors = {"live": 1, "good": 2, "warn": 3, "bad": 4, "agent": 5, "files": 6}
        data = tui.snapshot(self.state)
        data["logs"] = [
            {"created_at": "2026-10-19T12:34:56Z", "stream": "agent", "content": "Inspecting source"},
            {"created_at": "2026-10-19T12:34:57Z", "stream": "agent", "content": "$ uv run tests\nTests passed"},
            {"created_at": "2026-10-19T12:34:58Z", "stream": "agent", "content": "Files: main.py"},
            {"created_at": "2026-10-19T12:34:59Z", "stream": "stderr", "content": "Provider failure"},
            {"created_at": "2026-10-19T12:35:00Z", "stream": "agent", "content": "Passed\nPR: https://github.com/owner/repo/pull/1"},
        ]
        screen = Screen(size=(40, 110))
        dashboard.draw(screen, data)
        spans = [span for y, spans in screen.styles.items() if 4 <= y < 34 for span in spans]
        self.assertIn(("AGENT", 5 | curses.A_BOLD), spans)
        self.assertIn(("$ uv run tests", 1), spans)
        self.assertIn(("Files: main.py", 6), spans)
        self.assertIn(("Provider failure", 4), spans)
        self.assertIn(("Passed", 2 | curses.A_BOLD), spans)
        self.assertIn(("PR: https://github.com/owner/repo/pull/1", 1), spans)

    def test_back_keys_restore_overview_even_during_database_errors(self):
        self.run_record()
        for key in (27, 10, 13, curses.KEY_ENTER, ord("b"), ord("q"), 8, 127, curses.KEY_BACKSPACE):
            with self.subTest(key=key):
                screen = Screen([ord("j"), 10, key, ord("q")])
                dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
                with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
                    dashboard.run(screen)
                self.assertIn("LIVE SESSION LOG", screen.frames[2])
                self.assertIn("ISSUES & SESSIONS", screen.frames[3])
                self.assertFalse(dashboard.detail)
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        dashboard.detail = True
        screen = Screen([ord("b"), ord("q")])
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(tui, "snapshot", side_effect=tui.sqlite3.OperationalError("busy")):
            dashboard.run(screen)
        self.assertFalse(dashboard.detail)

    def test_interactive_resume_returns_to_overview_and_restores_keypad(self):
        self.run_record()
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        screen = Screen([ord("j"), 10, ord("a"), ord("q")])
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(curses, "def_prog_mode"), patch.object(curses, "endwin"), \
             patch.object(curses, "reset_prog_mode"), patch.object(curses, "flushinp"), \
             patch.object(tui, "resume_session") as resume:
            dashboard.run(screen)
        resume.assert_called_once_with(self.state, 1, "worker")
        self.assertIn("ISSUES & SESSIONS", screen.frames[3])
        self.assertFalse(dashboard.detail)

    def test_explicit_monochrome_does_not_initialize_colours(self):
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1, no_color=True)
        with patch.dict(os.environ, {"NO_COLOR": ""}), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=True), \
             patch.object(curses, "start_color") as start:
            dashboard.run(Screen())
        start.assert_not_called()

    def test_no_color_environment_does_not_initialize_colours(self):
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1)
        with patch.dict(os.environ, {"NO_COLOR": "1"}), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=True), \
             patch.object(curses, "start_color") as start, patch.object(curses, "use_default_colors"), \
             patch.object(curses, "init_pair"), patch.object(curses, "color_pair", return_value=0):
            dashboard.run(Screen())
        start.assert_not_called()

    def test_event_summaries_include_commands_file_changes_and_errors(self):
        self.assertEqual(w.event_text({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "main.py"}]}}), "Files: main.py")
        self.assertEqual(w.event_text({"type": "turn.failed", "error": {"message": "Provider failure"}}), "Provider failure")

    def test_cli_routes_offline_and_rejects_non_terminal_and_invalid_refresh(self):
        with patch.object(w, "repository", return_value=("owner/repo", self.tree, None)), \
             patch.object(w, "State", return_value=self.state), \
             patch.object(w, "gh", side_effect=AssertionError("network used")), \
             patch.object(tui, "run_tui") as run:
            self.assertEqual(w.main(["tui", "--refresh", "0.5"]), 0)
            options = {"project": None, "model": None, "interval": None, "once": None}
            run.assert_called_once_with(self.state, "owner/repo", .5, no_color=False, root=self.tree, options=options)
            run.reset_mock()
            self.assertEqual(w.main([]), 0)
            run.assert_called_once_with(self.state, "owner/repo", 1, no_color=False, root=self.tree, options=options)
            run.reset_mock()
            self.assertEqual(w.main(["tui", "--no-color"]), 0)
            run.assert_called_once_with(self.state, "owner/repo", 1, no_color=True, root=self.tree, options=options)
            run.reset_mock()
            self.assertEqual(w.main(["tui", "--project", "owner/2", "--model", w.MODEL, "--interval", "60", "--once"]), 0)
            run.assert_called_once_with(self.state, "owner/repo", 1, no_color=False, root=self.tree,
                                        options={"project": "owner/2", "model": w.MODEL, "interval": 60, "once": True})
        with patch.object(tui.sys, "stdin", io.StringIO()), patch.object(tui.sys, "stdout", io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
                tui.run_tui(self.state, "owner/repo")
        for value in (0, -.1, 61, float("nan"), float("inf")):
            with self.assertRaisesRegex(RuntimeError, "--refresh"):
                tui.run_tui(self.state, "owner/repo", value)

    def wait_jobs(self, dashboard):
        deadline = time.monotonic() + 5
        while dashboard.jobs and time.monotonic() < deadline:
            dashboard.poll()
            time.sleep(.005)
        self.assertFalse(dashboard.jobs, "background action did not complete")

    def test_view_cache_persists_expires_and_retains_stale_content_on_failure(self):
        loader = Mock(return_value={"body": "Saved description"})
        cache = tui.ViewCache(self.state)
        with patch.object(tui.time, "time", return_value=1000):
            self.assertEqual(cache.read("issue:1", loader), (loader.return_value, ""))
        with patch.object(tui.time, "time", return_value=1059):
            self.assertEqual(tui.ViewCache(self.state).read("issue:1", loader), (loader.return_value, ""))
        loader.assert_called_once()
        loader.side_effect = RuntimeError("GitHub offline")
        with patch.object(tui.time, "time", return_value=1060):
            self.assertEqual(cache.read("issue:1", loader), ({"body": "Saved description"}, "GitHub offline"))
        with patch.object(tui.time, "time", return_value=1119):
            cache.read("issue:1", loader)
        self.assertEqual(loader.call_count, 2)
        loader.side_effect = None
        loader.return_value = {"body": "Updated description"}
        with patch.object(tui.time, "time", return_value=1120):
            self.assertEqual(cache.read("issue:1", loader), (loader.return_value, ""))
        self.assertEqual(loader.call_count, 3)

    def test_views_share_controller_github_cooldown_and_paginate_comments(self):
        issue = {"number": 7, "body": "Description"}
        comments = [[{"body": "First"}], [{"body": "Last"}]]
        with patch.object(w, "gh", side_effect=[issue, comments]) as gh:
            self.assertEqual(tui.issue_view("owner/repo", 7)["comments"], comments[0] + comments[1])
        self.assertEqual(gh.call_args_list[1].args,
                         ("api", "repos/owner/repo/issues/7/comments?per_page=100", "--paginate", "--slurp"))
        budget = w.GitHubBudget(self.state)
        budget.record(w.GitHubRateLimit("rate limit exhausted", time.time() + 300))
        with patch.object(w, "raw_command") as transport:
            value, error = tui.ViewCache(self.state).read("issue:7", lambda: tui.issue_view("owner/repo", 7))
        self.assertIsNone(value)
        self.assertIn("rate limit", error)
        transport.assert_not_called()

    def test_board_view_is_read_only_and_uses_existing_paginated_intake(self):
        with patch.object(w, "Board") as board:
            board.return_value.project = {"url": "https://github.com/users/owner/projects/2"}
            board.return_value.items.return_value = [{"content": {"number": 2}}]
            view = tui.board_view("owner/repo", self.tree, "owner/2")
        board.assert_called_once_with("owner/repo", self.tree, "owner/2", load_fields=False)
        self.assertEqual(view["items"], [{"content": {"number": 2}}])
        board.return_value.ensure_review_status.assert_not_called()

    def test_controller_controls_reuse_cli_settings_and_preserve_graceful_stop(self):
        self.state.set_meta("runtime", {"project": "owner/2", "model": "saved-model", "interval": 25, "once": False})
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1, root=self.tree,
                                  options={"model": w.MODEL, "interval": 60, "project": None})
        with patch.object(w, "command", return_value="Started") as command:
            dashboard.control("start")
            self.wait_jobs(dashboard)
        argv = command.call_args.args[0]
        self.assertEqual(argv[:5], [sys.executable, str(Path(w.__file__).resolve()), "start", "--repo", str(self.tree)])
        self.assertEqual(argv[5:], ["--project", "owner/2", "--model", w.MODEL, "--interval", "60"])
        self.assertEqual(dashboard.message, "Started")
        with w.locked(self.state.path / "runner.lock"), patch.object(w, "command") as command:
            dashboard.control("start")
            self.assertIn("already running", dashboard.message)
            command.assert_not_called()
        with patch.object(w, "repository", return_value=("owner/repo", self.tree, "main")), \
             patch.object(w, "State", return_value=self.state), patch.object(w, "gh") as gh, \
             patch.object(w.os, "killpg") as killpg, patch.object(w.sys, "stdout", io.StringIO()):
            self.assertEqual(w.main(["stop"]), 0)
        self.assertTrue(self.state.paused())
        self.assertTrue(self.state.get_meta("shutdown")["requested"])
        self.assertFalse(self.state.get_meta("shutdown")["immediate"])
        killpg.assert_not_called()
        gh.assert_not_called()
        with w.locked(self.state.path / "runner.lock"):
            self.assertTrue(tui.snapshot(self.state)["stopping"])

    def test_background_github_does_not_block_stop_navigation_or_quit(self):
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1, root=self.tree)
        entered, release = threading.Event(), threading.Event()
        def slow_board(*_):
            entered.set()
            release.wait(5)
            return {"items": []}
        with patch.object(tui, "board_view", side_effect=slow_board), \
             patch.object(tui, "controller_action", return_value="Stopping") as control, \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            try:
                dashboard.open_page("board")
                self.assertTrue(entered.wait(2))
                screen = Screen([ord("b"), ord("x"), ord("q")])
                dashboard.run(screen)
                self.assertFalse(dashboard.page)
                self.assertTrue(any("PROJECT BOARD" in frame for frame in screen.frames))
            finally:
                release.set()
                self.wait_jobs(dashboard)
        control.assert_called_once_with("stop", self.tree, {})

    def test_board_issue_navigation_scrolling_cache_and_offline_fallback(self):
        item = {"status": "Ready for work", "title": "Unclaimed issue", "content":
                {"number": 9, "title": "Unclaimed issue", "body": "Cached body", "url": "issue-url"}}
        self.state.set_meta("tui:board:auto", {"value": {"items": [item], "url": "board-url"},
                                             "retry_at": time.time() + 60})
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1)
        def keys():
            yield ord("B")
            self.wait_jobs(dashboard)
            yield 10
            self.wait_jobs(dashboard)
            yield curses.KEY_END
            yield curses.KEY_HOME
            yield ord("b")
            yield ord("b")
            yield ord("i")  # Empty overview has no issue selection.
            yield ord("q")
        with patch.object(tui, "board_view") as board, patch.object(tui, "issue_view", side_effect=RuntimeError("offline")) as issue, \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            screen = Screen(keys())
            dashboard.run(screen)
        board.assert_not_called()
        issue.assert_called_once_with("owner/repo", 9)
        self.assertTrue(any("ISSUE #9" in frame and "Cached body" in frame for frame in screen.frames))
        self.assertTrue(any("Showing saved content. offline" in frame for frame in screen.frames))
        self.assertGreaterEqual(sum("PROJECT BOARD" in frame for frame in screen.frames), 2)
        self.assertFalse(self.state.all())  # Browsing never claims an unassigned board item.

    def test_start_key_reports_errors_and_issue_view_reads_saved_task_comments(self):
        self.state.save({"number": 1, "phase": "blocked", "error": "Prior blocker", "issue":
                         {"title": "Task one", "body": "Description", "comments": [
                             {"author": {"login": "reader"}, "body": "Human discussion"}]}})
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", .1, root=self.tree)
        def keys():
            yield ord("s")
            self.wait_jobs(dashboard)
            yield -1
            yield ord("i")
            self.wait_jobs(dashboard)
            yield curses.KEY_NPAGE
            yield curses.KEY_HOME
            yield ord("q")
            yield ord("q")
        with patch.object(tui, "controller_action", side_effect=RuntimeError("Controller exited; see worklane logs.")) as control, \
             patch.object(tui, "issue_view", side_effect=RuntimeError("offline")), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            screen = Screen(keys())
            dashboard.run(screen)
        control.assert_called_once_with("start", self.tree, {})
        self.assertTrue(any("Controller exited" in frame for frame in screen.frames))
        self.assertTrue(any("ISSUE #1" in frame and "Human discussion" in frame for frame in screen.frames))
        self.assertEqual(self.state.load(1)["phase"], "blocked")


if __name__ == "__main__":
    unittest.main()
