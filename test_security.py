"""Security boundaries use real board/runner paths and mocked GitHub transport."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import worklane as w
import worklane_tui as tui
from test_worklane import FakeBoard, item


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = w.State("owner/repo", self.root)
        self.project = {"id": "project", "number": 2, "url": "board-url"}
        self.security = {"node": {"public": False, "viewerCanUpdate": True},
                         "repository": {"viewerPermission": "ADMIN"}}
        self.graph = self.enterContext(patch.object(w, "graphql", side_effect=lambda *_: self.security))
        self.gh = self.enterContext(patch.object(w, "gh", return_value=self.project))
        self.command = self.enterContext(patch.object(w, "command", side_effect=AssertionError("unexpected mutation")))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))

    def board(self, override=False, **kwargs):
        return w.Board("owner/repo", self.root, "owner/2", load_fields=False,
                       dangerously_allow_public_board=override, **kwargs)

    def runner(self, board=None):
        return w.Runner(argparse.Namespace(base="main", once=True, interval=10),
                        "owner/repo", self.root, self.state, board or FakeBoard(), Mock())

    def cli(self, argv):
        output = io.StringIO()
        with patch.object(w, "repository", return_value=("owner/repo", self.root, "main")), \
             patch.object(w, "State", return_value=self.state), \
             patch.object(w.shutil, "which", return_value="/bin/fake"), redirect_stdout(output):
            code = w.main(argv)
        return code, output.getvalue()

    def test_private_public_unknown_and_permissions_fail_closed(self):
        self.assertTrue(self.board().security["safe"])
        for public in (True, None, "false", 0):
            with self.subTest(public=public):
                self.security["node"]["public"] = public
                with self.assertRaisesRegex(RuntimeError, "security preflight"):
                    self.board()
        self.security["node"]["public"] = False
        for key, value in (("viewerCanUpdate", False), ("viewerCanUpdate", None)):
            self.security["node"][key] = value
            with self.assertRaisesRegex(RuntimeError, "security preflight"):
                self.board(True)
        self.security["node"]["viewerCanUpdate"] = True
        for permission in ("READ", "TRIAGE", None, "unknown"):
            self.security["repository"]["viewerPermission"] = permission
            with self.assertRaisesRegex(RuntimeError, "security preflight"):
                self.board(True)
        self.command.assert_not_called()

    def test_override_is_explicit_warns_and_never_allows_unknown_visibility(self):
        self.security["node"]["public"] = True
        with self.assertRaises(RuntimeError):
            self.board()
        board = self.board(True)
        self.assertTrue(board.security["public_board_override"])
        self.assertIn("WARNING", self.errors.getvalue())
        self.security["node"].pop("public")
        with self.assertRaises(RuntimeError):
            board.require_safe()

    def test_failed_security_read_refuses_even_with_override(self):
        for error in (RuntimeError("permission denied"), OSError("offline"), w.GitHubRateLimit("quota")):
            with self.subTest(error=error), patch.object(w, "graphql", side_effect=error):
                with self.assertRaises(RuntimeError):
                    self.board(True)
        self.command.assert_not_called()

    def test_visibility_changes_block_each_mutation_dispatch_resume_and_rework(self):
        board = self.board()
        runner = self.runner(board)
        self.security["node"]["public"] = True
        editor = {"board": board, "item": item(1), "title": "edit", "body": "", "status": w.READY}
        actions = [lambda: board.add_issue("Title", ""), board.ensure_review_status,
                   lambda: board.move(item(1), w.PROGRESS, w.READY),
                   lambda: runner.pick(item(1), "worker-1"),
                   lambda: runner.recover([], "worker-1"),
                   lambda: runner.poll_human_feedback([]),
                   lambda: runner.agent_result({"number": 1}, "build", self.root, ""),
                   lambda: runner.post({}, "review", {}), lambda: tui.save_editor("owner/repo", editor)]
        for action in actions:
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "security preflight"):
                action()
        self.command.assert_not_called()
        runner.backend.run.assert_not_called()
        self.assertEqual(self.state.all(), [])

    def test_cli_blocks_public_before_start_run_add_retry_resume_or_tui_edit(self):
        self.security["node"]["public"] = True
        for argv in (["start", "--project", "owner/2"], ["run", "--project", "owner/2", "--background"],
                     ["add", "--project", "owner/2", "-t", "Unsafe"],
                     ["retry", "1", "--project", "owner/2"], ["resume", "--project", "owner/2"]):
            with self.subTest(argv=argv), patch.object(w.subprocess, "Popen") as launch:
                self.assertEqual(self.cli(argv)[0], 1)
                launch.assert_not_called()
        with self.assertRaises(RuntimeError):
            tui.load_editor("owner/repo", self.root, "owner/2")
        self.command.assert_not_called()

    def test_check_reports_rejected_public_unknown_and_failed_checks(self):
        for public in (True, None, False):
            self.security["node"]["public"] = public
            with patch.object(w.Board, "refresh_fields"), patch.object(w.Board, "items", return_value=[]) as items:
                if public is False:
                    # A private board can still have insufficient permissions.
                    self.security["node"]["viewerCanUpdate"] = False
                code, output = self.cli(["check", "--project", "owner/2"])
            report = json.loads(output)["security"]
            self.assertEqual(code, 1)
            self.assertFalse(report["safe"])
            self.assertEqual(report["visibility"], "unknown" if public is None else "public" if public else "private")
            self.assertTrue(report["verification_limits"])
            items.assert_not_called()
        self.graph.side_effect = RuntimeError("offline")
        code, output = self.cli(["check", "--project", "owner/2"])
        self.assertEqual(code, 1)
        self.assertIn('"error": "offline"', output)

    def test_detached_override_propagates_only_when_supplied(self):
        for override in (False, True):
            self.security["node"]["public"] = override
            child = Mock(pid=123)
            child.poll.return_value = None
            def launch(*args, **kwargs):
                self.state.set_meta("runtime", {"pid": child.pid})
                return child
            with patch.object(w.Board, "refresh_fields"), patch.object(w.subprocess, "Popen", side_effect=launch) as popen:
                argv = ["start", "--project", "owner/2"] + (["--dangerously-allow-public-board"] if override else [])
                self.assertEqual(self.cli(argv)[0], 0)
            self.assertEqual("--dangerously-allow-public-board" in popen.call_args.args[0], override)

    def test_tui_propagation_requires_current_options_never_saved_runtime_consent(self):
        self.state.set_meta("runtime", {"project": "owner/2", "dangerously_allow_public_board": True})
        dashboard = tui.Dashboard(None, self.state, "owner/repo", 1, root=self.root)
        self.assertNotIn("dangerously_allow_public_board", dashboard.settings())
        for override in (False, True):
            options = {"project": "owner/2", "dangerously_allow_public_board": override}
            with patch.object(w, "command", return_value="ok") as cmd:
                for action in ("start", "resume", "retry"):
                    tui.controller_action(action, self.root, options)
                    self.assertEqual("--dangerously-allow-public-board" in cmd.call_args.args[0], override)

    def test_public_override_cannot_be_saved_in_checkout_configuration(self):
        with self.assertRaisesRegex(ValueError, "Unknown configuration"):
            w.save_config(self.root, {"dangerously_allow_public_board": True})
        self.assertFalse((self.root / w.CONFIG_FILE).exists())

    def test_interactive_resume_rechecks_live_security_without_runtime_override(self):
        task = {"number": 1}
        run = self.state.begin_run(task, "build", self.root)
        self.state.record_event(run, {"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000001"}, w.MODEL)
        self.state.finish_run(run, "passed")
        self.state.set_meta("runtime", {"project": "owner/2", "dangerously_allow_public_board": True})
        self.security["node"]["public"] = True
        invoke = Mock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(tui.shutil, "which", return_value="/fake/codex"):
            with self.assertRaises(RuntimeError):
                tui.resume_session(self.state, 1, "worker", invoke=invoke)
            invoke.assert_not_called()
            tui.resume_session(self.state, 1, "worker", invoke=invoke,
                               options={"dangerously_allow_public_board": True})
        invoke.assert_called_once()


class FeedbackAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = w.State("owner/repo", self.root)
        self.board = FakeBoard([item(1, w.HUMAN)])
        self.runner = w.Runner(argparse.Namespace(base="main"), "owner/repo", self.root,
                               self.state, self.board, Mock())
        self.runner.issue = Mock(return_value={"state": "OPEN", "labels": []})
        self.runner.pr = Mock(return_value={"state": "OPEN", "headRefName": "codex/1-worklane",
                                            "baseRefName": "main", "headRefOid": "a" * 40})
        self.task = {"number": 1, "item": item(1, w.HUMAN), "phase": "human_review",
                     "branch": "codex/1-worklane", "base": "main", "head_sha": "a" * 40,
                     "pr_url": "https://github.com/owner/repo/pull/9", "human_feedback_seen": {}}
        self.state.save(self.task)
        self.entries = [{"id": 1, "node_id": "comment-1", "body": "Fix it", "author_association": "OWNER",
                         "user": {"login": "outsider", "type": "User"},
                         "created_at": "2026-10-20T01:00:00Z", "state": "CHANGES_REQUESTED", "path": "a.py"}]
        self.permissions = {"outsider": "read", "trusted": "write", "maintainer": "maintain", "admin": "admin"}
        self.provenance = {"node": {"body": "Fix it", "lastEditedAt": None}}
        self.api = self.enterContext(patch.object(w, "gh", side_effect=self.gh))
        self.graph = self.enterContext(patch.object(w, "graphql", side_effect=lambda *_: self.provenance))
        self.clock = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
        self.enterContext(patch.object(w, "now", side_effect=lambda: self.clock.isoformat()))

    def gh(self, *args):
        if args[-1].endswith("/permission"):
            login = args[-1].split("/")[-2]
            return {"permission": self.permissions[login]}
        return [[], self.entries]  # Exercise a nonempty second page for every feedback source.

    def poll_twice(self):
        self.runner.poll_human_feedback(self.board.items())
        self.clock += timedelta(seconds=301)
        self.runner.poll_human_feedback(self.board.items())

    def test_outsider_comments_reviews_and_inline_feedback_never_trigger_rework(self):
        self.poll_twice()
        self.assertEqual(self.board.moves, [])
        self.assertEqual(self.state.load(1)["human_feedback"], {})
        self.graph.assert_not_called()
        self.assertTrue(any("/collaborators/outsider/permission" in call.args[-1] for call in self.api.call_args_list))

    def test_public_override_does_not_authorize_outsider_feedback(self):
        board = object.__new__(w.Board)
        board.repo, board.project = "owner/repo", {"id": "project"}
        board.dangerously_allow_public_board = True
        board.move = Mock()
        self.runner.board = board
        def graph(query, *_):
            if "viewerCanUpdate" in query:
                return {"node": {"public": True, "viewerCanUpdate": True},
                        "repository": {"viewerPermission": "ADMIN"}}
            return self.provenance
        with patch.object(w, "graphql", side_effect=graph), redirect_stderr(io.StringIO()):
            self.poll_twice()
        board.move.assert_not_called()
        self.assertEqual(self.state.load(1)["human_feedback"], {})

    def test_current_write_maintain_admin_permissions_authorize_feedback_not_association(self):
        for login in ("trusted", "maintainer", "admin"):
            with self.subTest(login=login):
                self.entries[0].update(user={"login": login, "type": "User"}, author_association="NONE")
                feedback = self.runner.human_feedback(self.task)
                self.assertEqual(set(feedback), {"issue:1", "pr:1", "review:1", "inline:1"})
        self.entries[0]["user"]["login"] = "trusted"
        self.poll_twice()
        self.assertEqual(self.board.moves, [(1, w.READY)])
        self.assertEqual(self.state.load(1)["phase"], "rework")

    def test_outsider_edits_of_trusted_feedback_cannot_authorize_rework(self):
        self.entries[0]["user"]["login"] = "trusted"
        self.provenance["node"].update(lastEditedAt="2026-10-20T01:00:00Z",
                                       editor={"login": "outsider", "__typename": "User"})
        self.poll_twice()
        self.assertEqual(self.board.moves, [])
        self.provenance["node"]["editor"]["login"] = "trusted"
        self.assertEqual(len(self.runner.human_feedback(self.task)), 4)

    def test_missing_or_racing_edit_provenance_and_permission_failures_fail_closed(self):
        self.entries[0]["user"]["login"] = "trusted"
        for provenance in ({"node": None}, {"node": {"body": "changed", "lastEditedAt": None}}):
            self.provenance = provenance
            with self.assertRaises(RuntimeError):
                self.runner.human_feedback(self.task)
        self.api.side_effect = RuntimeError("permission lookup failed")
        self.poll_twice()
        self.assertEqual(self.board.moves, [])
        self.assertEqual(self.state.load(1)["human_feedback_seen"], {})
        self.assertIn(1, self.runner.feedback_unavailable)

    def test_revoked_author_or_editor_permission_blocks_pending_rework(self):
        self.entries[0]["user"]["login"] = "trusted"
        self.runner.poll_human_feedback(self.board.items())
        self.clock += timedelta(seconds=301)
        self.permissions["trusted"] = "read"
        self.runner.poll_human_feedback(self.board.items())
        self.assertEqual(self.board.moves, [])
        self.assertEqual(self.state.load(1)["human_feedback"], {})

    def test_saved_feedback_is_reauthorized_before_agent_and_status_claim(self):
        self.entries[0]["user"]["login"] = "trusted"
        snapshot = self.runner.human_feedback(self.task)
        self.task.update(phase="rework", human_feedback=snapshot, human_feedback_pending=True,
                         human_feedback_snapshot=snapshot, human_feedback_observed_at="2026-10-19T01:00:00Z")
        self.state.save(self.task)
        self.permissions["trusted"] = "read"
        for action in (lambda: self.runner.agent_result(self.task, "build", self.root, ""),
                       lambda: self.runner.pick(item(1), "worker-1")):
            with self.assertRaisesRegex(RuntimeError, "authorization"):
                action()
        self.runner.backend.run.assert_not_called()
        self.assertEqual(self.board.moves, [])


if __name__ == "__main__":
    unittest.main()
