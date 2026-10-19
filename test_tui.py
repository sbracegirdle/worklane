"""Dashboard integration tests use temporary SQLite state; no real agents/boards."""
import curses
import fcntl
import io
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

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines = {}

    def addstr(self, y, x, text, style):
        assert 0 <= y < self.size[0]
        self.lines[y] = text

    def refresh(self):
        self.frames.append("\n".join(self.lines.values()))

    def getch(self):
        return next(self.keys, ord("q"))

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
        screen = Screen([ord("j"), 10, curses.KEY_PPAGE, curses.KEY_END, 27, ord("a"), ord("q")])
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
        self.assertEqual(tui.clipped("你好world", 5), "你好w")
        self.assertEqual(tui.clipped("e\u0301", 1), "e\u0301")
        self.assertNotIn("\x1b", tui.plain("\x1b[31mred\x1b[0m"))
        self.assertNotIn("\x07", tui.plain("bell\x07"))

    def test_real_curses_terminal_refresh_resize_logs_and_quit(self):
        run = self.run_record(text="Inspecting source")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        # A real CLI process against a disposable checkout/state, never the live controller.
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        subprocess.run(["git", "-C", str(self.tree), "remote", "add", "origin", "https://github.com/owner/repo.git"], check=True)
        env = {**os.environ, "TERM": "xterm-256color", "XDG_STATE_HOME": str(self.root)}
        env.pop("NO_COLOR", None)
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
            self.state.append_log(run, "agent", "Fresh output from agent")
            until(b"Fresh output")
            os.write(master, b"\n")
            until(b"LIVE SESSION LOG")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 5, 30, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            until(b"enlarge terminal")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            until(b"LIVE SESSION LOG")
            os.write(master, b"q")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue(self.state.running())
            self.assertFalse(self.state.paused())

    def test_event_summaries_include_commands_file_changes_and_errors(self):
        self.assertEqual(w.event_text({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "main.py"}]}}), "Files: main.py")
        self.assertEqual(w.event_text({"type": "turn.failed", "error": {"message": "Provider failure"}}), "Provider failure")

    def test_cli_routes_offline_and_rejects_non_terminal_and_invalid_refresh(self):
        with patch.object(w, "repository", return_value=("owner/repo", self.tree, None)), \
             patch.object(w, "State", return_value=self.state), \
             patch.object(w, "gh", side_effect=AssertionError("network used")), \
             patch.object(tui, "run_tui") as run:
            self.assertEqual(w.main(["tui", "--refresh", "0.5"]), 0)
            run.assert_called_once_with(self.state, "owner/repo", .5)
        with patch.object(tui.sys, "stdin", io.StringIO()), patch.object(tui.sys, "stdout", io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
                tui.run_tui(self.state, "owner/repo")
        for value in (0, -.1, 61, float("nan"), float("inf")):
            with self.assertRaisesRegex(RuntimeError, "--refresh"):
                tui.run_tui(self.state, "owner/repo", value)


if __name__ == "__main__":
    unittest.main()
