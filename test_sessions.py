import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

import worklane as w
from test_worklane import FakeBoard, item, result

FAKE_CODEX = r'''
import json, os, pathlib, sys, time, uuid
args = sys.argv[1:]
prompt = sys.stdin.read()
root = pathlib.Path(os.environ["FAKE_ROOT"])
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "prompt": prompt}) + "\n")
session = args[-2] if "resume" in args else str(uuid.uuid4())
if os.environ.get("FAKE_MISMATCH"):
    session = str(uuid.uuid4())
if not os.environ.get("FAKE_NO_ID"):
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
if os.environ.get("FAKE_FAIL"):
    print("simulated provider failure", file=sys.stderr)
    sys.exit(1)
value = {"verdict": "pass", "head_sha": "a" * 40, "pr_url": "https://github.com/owner/repo/pull/9",
         "summary": "Checks passed", "findings": []}
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(value))
print(json.dumps({"type": "turn.completed"}), flush=True)
'''


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = w.State("owner/repo", self.root)
        self.tree = self.root / "checkout"
        self.tree.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        executable.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "FAKE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.task = {"number": 1, "phase": "building", "item": item(1, w.PROGRESS),
                     "question": False, "round": 0, "head_sha": "a" * 40,
                     "branch": "codex/1-worklane", "base": "main", "pr_url": "https://github.com/owner/repo/pull/9"}
        self.state.save(self.task)

    def calls(self):
        file = self.root / "calls.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def backend(self, fd, state=None):
        return w.CodexBackend(w.MODEL, self.tree, state or self.state, fd)

    def test_feedback_and_reopen_resume_exact_worker_session(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            self.backend(fd).run("build", self.task, self.tree)
            original = self.state.session(1, "worker")["session_id"]
            reopened = w.State("owner/repo", self.root)
            task = reopened.load(1)
            task["review_result"] = result("changes_requested", findings=["Fix file.py:12"])
            self.backend(fd, reopened).run("build", task, self.tree)
        calls = self.calls()
        self.assertNotIn("resume", calls[0]["args"])
        self.assertIn("resume", calls[1]["args"])
        self.assertEqual(calls[1]["args"][-2], original)
        self.assertNotIn("--last", calls[1]["args"])
        self.assertIn("Fix file.py:12", calls[1]["prompt"])
        self.assertEqual(reopened.session(1, "worker")["session_id"], original)

    def test_worker_reviewer_and_finalizer_are_separate_and_resumable(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            backend = self.backend(fd)
            for stage in ("build", "review", "finalize", "review", "finalize"):
                backend.run(stage, self.task, self.tree)
        sessions = self.state.load(1)["sessions"]
        self.assertEqual(len({s["session_id"] for s in sessions.values()}), 3)
        calls = self.calls()
        self.assertEqual(calls[3]["args"][-2], sessions["reviewer"]["session_id"])
        self.assertEqual(calls[4]["args"][-2], sessions["finalizer"]["session_id"])
        self.assertNotEqual(calls[3]["args"][-2], sessions["worker"]["session_id"])

    def test_session_is_committed_before_turn_finishes(self):
        with patch.dict(os.environ, {"FAKE_DELAY": ".3"}):
            with w.locked(self.state.path / "runner.lock") as fd, ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.backend(fd).run, "build", self.task, self.tree)
                deadline = time.monotonic() + 3
                while not self.state.session(1, "worker") and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertIsNotNone(self.state.session(1, "worker"))
                self.assertFalse(future.done())
                self.assertEqual(self.state.runs(1)[0]["status"], "running")
                future.result()
        self.assertEqual(self.state.runs(1)[0]["status"], "completed")

    def test_provider_failure_keeps_session_for_explicit_retry(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            with patch.dict(os.environ, {"FAKE_FAIL": "1"}):
                with self.assertRaisesRegex(RuntimeError, "exited 1"):
                    self.backend(fd).run("build", self.task, self.tree)
            original = self.state.session(1, "worker")["session_id"]
            self.backend(fd).run("build", self.task, self.tree)
        self.assertEqual(self.calls()[1]["args"][-2], original)
        self.assertEqual([r["status"] for r in self.state.runs(1)], ["failed", "completed"])

    def test_missing_id_never_falls_back_to_new_session(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            with patch.dict(os.environ, {"FAKE_NO_ID": "1"}):
                with self.assertRaisesRegex(RuntimeError, "session ID"):
                    self.backend(fd).run("build", self.task, self.tree)
            with self.assertRaisesRegex(RuntimeError, "replacement conversation"):
                self.backend(fd).run("build", self.task, self.tree)
        self.assertEqual(len(self.calls()), 1)

    def test_mismatched_resume_id_is_rejected(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            self.backend(fd).run("review", self.task, self.tree)
            original = self.state.session(1, "reviewer")["session_id"]
            with patch.dict(os.environ, {"FAKE_MISMATCH": "1"}):
                with self.assertRaisesRegex(RuntimeError, "different session"):
                    self.backend(fd).run("review", self.task, self.tree)
        self.assertEqual(self.state.session(1, "reviewer")["session_id"], original)

    def test_signal_interruption_and_restart_keep_same_conversation(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            backend = self.backend(fd)
            with patch.dict(os.environ, {"FAKE_DELAY": "5"}), ThreadPoolExecutor(1) as pool:
                future = pool.submit(backend.run, "build", self.task, self.tree)
                deadline = time.monotonic() + 3
                while not self.state.session(1, "worker") and time.monotonic() < deadline:
                    time.sleep(.005)
                original = self.state.session(1, "worker")["session_id"]
                os.killpg(self.state.load(1)["child_pid"], signal.SIGTERM)
                with self.assertRaises(w.InterruptedRun):
                    future.result(timeout=3)
        # Simulate a fresh controller after the previous one exited mid-task.
        reopened = w.State("owner/repo", self.root)
        args = argparse.Namespace(base="main", once=True, interval=.01)
        runner = w.Runner(args, "owner/repo", self.tree, reopened, FakeBoard([item(1, w.PROGRESS)]), Mock())
        runner.issue = lambda n: {"state": "OPEN", "title": "continue"}
        runner.recover_tasks()
        self.assertEqual(reopened.load(1)["phase"], "interrupted")
        resumed = runner.recover(runner.board.items(), "worker-2")
        self.assertEqual(resumed["phase"], "building")
        with w.locked(reopened.path / "runner.lock") as fd:
            self.backend(fd, reopened).run("build", resumed, self.tree)
        self.assertEqual(self.calls()[1]["args"][-2], original)
        self.assertEqual(runner.board.moves, [])

    def test_cli_stop_now_then_run_resumes_saved_session(self):
        board = FakeBoard([item(1)])
        board.owner, board.number = "owner", 1
        board.project = {"url": "https://github.com/users/owner/projects/1"}
        board.ensure_review_status = Mock()
        old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, old_term)
        self.addCleanup(signal.signal, signal.SIGINT, old_int)
        # No session existed yet: this is a normal first claim.
        with self.state.connect() as db:
            db.execute("DELETE FROM tasks WHERE number=1")
        def build(runner, task):
            runner.backend.run("build", task, self.tree)
            task["phase"] = "awaiting_review"
            runner.state.save(task)
        def stop_when_started():
            deadline = time.monotonic() + 5
            while not self.state.session(1, "worker") and time.monotonic() < deadline:
                time.sleep(.01)
            w.main(["stop", "--now", "--repo", str(self.tree)])
        issue = {"state": "OPEN", "title": "one task", "labels": []}
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), \
             patch.object(w, "repository", return_value=("owner/repo", self.tree, "main")), \
             patch.object(w, "Board", return_value=board), \
             patch.object(w, "command", return_value=""), \
             patch.object(w.Runner, "issue", return_value=issue), \
             patch.object(w.Runner, "build", build):
            timer = threading.Thread(target=stop_when_started)
            with patch.dict(os.environ, {"FAKE_DELAY": "5"}):
                timer.start()
                self.assertEqual(w.main(["run", "--once", "--repo", str(self.tree)]), 0)
                timer.join(timeout=5)
            self.assertFalse(timer.is_alive())
            self.assertEqual(self.state.load(1)["phase"], "interrupted")
            original = self.state.session(1, "worker")["session_id"]
            self.state.pause(False)
            self.assertEqual(w.main(["run", "--once", "--repo", str(self.tree)]), 0)
        self.assertEqual(self.calls()[1]["args"][-2], original)
        self.assertEqual(self.state.load(1)["phase"], "awaiting_review")

    def test_timeout_retains_session(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            backend = self.backend(fd)
            backend.timeout = .15
            with patch.dict(os.environ, {"FAKE_DELAY": "5"}):
                with self.assertRaises(w.InterruptedRun):
                    backend.run("review", self.task, self.tree)
        self.assertIsNotNone(self.state.session(1, "reviewer"))
        self.assertEqual(self.state.runs(1)[0]["status"], "interrupted")

    def test_deleted_worktree_never_creates_replacement_session(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            self.backend(fd).run("build", self.task, self.tree)
            other = self.root / "different-checkout"
            other.mkdir()
            with self.assertRaisesRegex(RuntimeError, "another worktree"):
                self.backend(fd).run("build", self.task, other)
        self.assertEqual(len(self.calls()), 1)

    def test_log_replays_session_event_after_controller_crash(self):
        path = self.root / "recover.jsonl"
        session_id = str(uuid.uuid4())
        run_id = self.state.begin_run(self.task, "build", self.tree, path, self.root / "result.json")
        path.write_text(json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n")
        reopened = w.State("owner/repo", self.root)
        reopened.recover_runs()
        self.assertEqual(reopened.session(1, "worker")["session_id"], session_id)
        self.assertEqual(reopened.runs(1)[0]["status"], "interrupted")

    def test_database_prevents_cross_role_session_reuse(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            self.backend(fd).run("build", self.task, self.tree)
        session = self.state.session(1, "worker")["session_id"]
        run = self.state.begin_run(self.task, "review", self.tree, self.root / "log", self.root / "result")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.attach_session(run, session, w.MODEL)
        self.assertIsNone(self.state.session(1, "reviewer"))
        self.assertIsNone(self.state.runs(1)[-1]["session_id"])

    def test_stale_task_save_cannot_erase_session(self):
        stale = self.state.load(1)
        with w.locked(self.state.path / "runner.lock") as fd:
            self.backend(fd).run("build", self.task, self.tree)
        self.state.save(stale)
        self.assertIsNotNone(self.state.load(1)["sessions"]["worker"])

    def test_four_concurrent_writers_and_reopen(self):
        def save(number):
            self.state.save({"number": number, "phase": "building", "value": number})
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(save, range(2, 42)))
        self.state.pause(True)
        self.state.set_meta("runtime", {"model": w.MODEL})
        reopened = w.State("owner/repo", self.root)
        self.assertEqual(len(reopened.all()), 41)
        self.assertTrue(reopened.paused())
        self.assertEqual(reopened.get_meta("runtime")["model"], w.MODEL)
        with reopened.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_json_migration_is_atomic_idempotent_and_preserves_originals(self):
        base = self.root / "legacy"
        path = base / "worklane" / "old--repo"
        (path / "tasks").mkdir(parents=True)
        original = {"number": 7, "phase": "building"}
        w.write_json(path / "tasks/7.json", original)
        (path / "tasks/8.json").write_text("invalid")
        with self.assertRaises(ValueError):
            w.State("old/repo", base)
        with closing(sqlite3.connect(path / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        (path / "tasks/8.json").unlink()
        w.write_json(path / "control.json", {"paused": True})
        migrated = w.State("old/repo", base)
        self.assertEqual(migrated.load(7)["phase"], "building")
        self.assertTrue(migrated.paused())
        self.assertEqual(w.read_json(path / "tasks/7.json"), original)
        migrated.save({"number": 7, "phase": "awaiting_review"})
        self.assertEqual(w.State("old/repo", base).load(7)["phase"], "awaiting_review")

    def test_json_migration_recovers_known_session_from_legacy_log(self):
        base = self.root / "legacy"
        path = base / "worklane" / "old--repo"
        (path / "tasks").mkdir(parents=True)
        session_id = str(uuid.uuid4())
        log = path / "worker.jsonl"
        log.write_text(json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n")
        w.write_json(path / "tasks/7.json", {"number": 7, "phase": "awaiting_review", "active_stage": "build", "log": str(log)})
        migrated = w.State("old/repo", base)
        self.assertEqual(migrated.session(7, "worker")["session_id"], session_id)

    def test_recovery_respects_human_board_override(self):
        self.task.update(phase="interrupted", resume_stage="build")
        self.state.save(self.task)
        board = FakeBoard([item(1, w.HUMAN)])
        runner = w.Runner(None, "owner/repo", self.tree, self.state, board, Mock())
        self.assertIsNone(runner.recover(board.items(), "worker-1"))
        self.assertEqual(self.state.load(1)["phase"], "blocked")
        self.assertEqual(board.moves, [])

    def test_interrupted_finalizer_skips_repeat_review(self):
        task = {**self.task, "phase": "finalizing", "resume_stage": "finalize", "round": 1,
                "item": item(1, w.REVIEW), "review_result": result()}
        backend = Mock(model=w.MODEL)
        backend.run.return_value = result()
        runner = w.Runner(None, "owner/repo", self.tree, self.state, FakeBoard([task["item"]]), backend)
        runner.worktree = Mock(return_value=self.tree)
        runner.verify_pr = Mock()
        runner.post = Mock()
        runner.pr = Mock(return_value={"isDraft": False})
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.review(task)
        self.assertEqual([c.args[0] for c in backend.run.call_args_list], ["finalize"])
        self.assertEqual(task["phase"], "human_review")

    def test_reviewer_reuses_worktree_across_changed_pr_heads(self):
        def git(*args, cwd=None):
            return w.command(["git", *args], cwd=cwd or self.tree)
        remote = self.root / "remote.git"
        git("init", "--bare", str(remote))
        git("init", "-b", "main", str(self.tree))
        file = self.tree / "example.txt"
        file.write_text("first\n")
        git("add", "example.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "first")
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "main")
        self.task["head_sha"] = git("rev-parse", "HEAD")
        runner = w.Runner(None, "owner/repo", self.tree, self.state, FakeBoard(), Mock())
        first = runner.worktree(self.task, review=True)
        run = self.state.begin_run(self.task, "review", first, self.root / "log", self.root / "result")
        session_id = str(uuid.uuid4())
        self.state.attach_session(run, session_id, w.MODEL)
        self.state.finish_run(run, "completed")
        file.write_text("second\n")
        git("add", "example.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "second")
        git("push", "origin", "main")
        self.task["head_sha"] = git("rev-parse", "HEAD")
        second = runner.worktree(self.task, review=True)
        self.assertEqual(first, second)
        self.assertEqual(git("rev-parse", "HEAD", cwd=second), self.task["head_sha"])
        self.assertEqual(self.state.session(1, "reviewer")["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
