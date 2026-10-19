import argparse
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import worklane as w


def item(number, status=w.READY, repo="owner/repo"):
    return {"id": f"item-{number}", "status": status, "content": {
        "number": number, "type": "Issue", "repository": repo}}


def result(verdict="pass", sha="a" * 40, findings=None):
    return {"verdict": verdict, "head_sha": sha, "pr_url": "https://github.com/owner/repo/pull/9",
            "summary": "Validated acceptance criteria", "findings": findings or []}


class FakeBoard:
    def __init__(self, items=()):
        self.queue = list(items)
        self.moves = []

    def items(self):
        return copy.deepcopy(self.queue)

    def move(self, selected, status, expected):
        target = next(i for i in self.queue if i["id"] == selected["id"])
        if target["status"] != expected:
            raise RuntimeError("Human changed the board")
        self.moves.append((target["content"]["number"], status))
        target["status"] = status


class BoardCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/checkout")
        self.project = {"number": 7, "title": "repo", "closed": False,
                        "owner": {"login": "board-owner"},
                        "url": "https://github.com/users/board-owner/projects/7"}
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        patches = {
            "repository": {"return_value": ("owner/repo", self.root, None)},
            "graphql": {"return_value": {"repository": {"projectsV2": {"nodes": [self.project]}}}},
            "gh": {"side_effect": AssertionError("unexpected GitHub request")},
            "command": {"return_value": ""},
            "State": {"side_effect": AssertionError("board must not touch controller state")},
        }
        for name, kwargs in patches.items():
            patcher = patch.object(w, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(w.shutil, "which", side_effect=lambda name: "/bin/gh" if name == "gh" else None)
        self.which = patcher.start()
        self.addCleanup(patcher.stop)

    def run_board(self, *args):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return w.main(["board", *args])

    def assert_opened(self):
        self.command.assert_called_once_with(
            ["gh", "project", "view", "7", "--owner", "board-owner", "--web"], cwd=self.root)
        self.assertIn(self.project["url"], self.stdout.getvalue())
        self.State.assert_not_called()
        self.which.assert_called_once_with("gh")

    def test_opens_inferred_board_without_codex_or_workflow_fields(self):
        self.assertEqual(self.run_board(), 0)
        self.assert_opened()
        self.gh.assert_not_called()

    def test_explicit_project_and_checkout(self):
        self.gh.side_effect = None
        self.gh.return_value = self.project
        self.assertEqual(self.run_board("--repo", "/another-checkout", "--project", "board-owner/7"), 0)
        self.repository.assert_called_once_with(Path("/another-checkout"))
        self.graphql.assert_not_called()
        self.gh.assert_called_once_with("project", "view", "7", "--owner", "board-owner", "--format", "json")
        self.assert_opened()

    def test_prefers_linked_board_matching_repository_name(self):
        other = dict(self.project, title="Other", number=8)
        self.graphql.return_value["repository"]["projectsV2"]["nodes"].append(other)
        self.assertEqual(self.run_board(), 0)
        self.assert_opened()

    def test_falls_back_to_owner_projects(self):
        self.graphql.return_value["repository"]["projectsV2"]["nodes"] = []
        self.gh.side_effect = None
        self.gh.return_value = {"projects": [self.project]}
        self.assertEqual(self.run_board(), 0)
        self.gh.assert_called_once_with("project", "list", "--owner", "owner", "--limit", "100", "--format", "json")
        self.assert_opened()

    def test_no_board_or_ambiguous_board_does_not_open_browser(self):
        for projects in ([], [dict(self.project, title="One"), dict(self.project, title="Two", number=8)]):
            with self.subTest(projects=projects):
                self.graphql.return_value["repository"]["projectsV2"]["nodes"] = projects
                self.gh.side_effect = None
                self.gh.return_value = {"projects": []}
                self.assertEqual(self.run_board(), 1)
                self.assertIn("--project OWNER/NUMBER", self.stderr.getvalue())
                self.command.assert_not_called()
                self.State.assert_not_called()

    def test_github_lookup_failure_is_reported(self):
        self.graphql.side_effect = RuntimeError("authentication required")
        self.assertEqual(self.run_board(), 1)
        self.assertIn("authentication required", self.stderr.getvalue())
        self.command.assert_not_called()

    def test_browser_failure_is_reported(self):
        self.command.side_effect = RuntimeError("gh failed: browser unavailable")
        self.assertEqual(self.run_board(), 1)
        self.assertIn("browser unavailable", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_missing_github_cli_is_reported(self):
        self.which.side_effect = None
        self.which.return_value = None
        self.assertEqual(self.run_board(), 1)
        self.assertIn("Required command missing: gh", self.stderr.getvalue())
        self.graphql.assert_not_called()
        self.command.assert_not_called()

    def test_controller_board_still_loads_workflow_fields_by_default(self):
        with patch.object(w.Board, "refresh_fields") as refresh:
            w.Board("owner/repo", self.root)
        refresh.assert_called_once_with()


class WorklaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = w.State("owner/repo", self.root)
        self.args = argparse.Namespace(base="main", once=True, interval=.01)

    def runner(self, board=None, outputs=None):
        backend = unittest.mock.Mock(model=w.MODEL)
        backend.run.side_effect = outputs or []
        runner = w.Runner(self.args, "owner/repo", self.root, self.state, board or FakeBoard(), backend)
        runner.worktree = unittest.mock.Mock(return_value=self.root)
        runner.post = unittest.mock.Mock()
        runner.verify_pr = unittest.mock.Mock(return_value={})
        runner.pr = unittest.mock.Mock(return_value={"isDraft": False})
        runner.human_feedback = unittest.mock.Mock(return_value={})
        return runner

    def task(self, status=w.REVIEW):
        selected = item(1, status)
        return {"number": 1, "item": selected, "phase": "reviewing", "question": False,
                "round": 1, "head_sha": "a" * 40, "branch": "codex/1-worklane",
                "base": "main", "pr_url": "https://github.com/owner/repo/pull/9"}

    def test_duplicate_controller_is_locked(self):
        with w.locked(self.state.path / "runner.lock"):
            self.assertTrue(self.state.running())
            with self.assertRaises(RuntimeError):
                with w.locked(self.state.path / "runner.lock", blocking=False):
                    self.fail("duplicate lock acquired")
        self.assertFalse(self.state.running())

    def test_repository_case_uses_same_controller_lock(self):
        mixed = w.State("OWNER/Repo", self.root)
        self.assertEqual(mixed.path, self.state.path)

    def test_lock_is_retained_by_child_after_parent_closes(self):
        with w.locked(self.state.path / "runner.lock") as fd:
            process = subprocess.Popen(["sleep", ".1"], pass_fds=(fd,))
        self.assertTrue(self.state.running())
        process.wait()
        self.assertFalse(self.state.running())

    def test_status_and_pause_do_not_need_github(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/owner/repo.git"], check=True)
        with patch.object(w, "gh", side_effect=AssertionError("network used")):
            self.assertEqual(w.repository(repo), ("owner/repo", repo, None))

    def test_board_excludes_other_repos_and_non_issues(self):
        board = object.__new__(w.Board)
        board.repo, board.owner, board.number = "owner/repo", "owner", 1
        data = [item(1), item(2, repo="owner/other"), item(3)]
        data[-1]["content"]["type"] = "PullRequest"
        with patch.object(w, "gh", return_value={"items": data, "totalCount": 3}):
            self.assertEqual([i["content"]["number"] for i in board.items()], [1])

    def test_incomplete_board_fails_closed(self):
        board = object.__new__(w.Board)
        board.repo, board.owner, board.number = "owner/repo", "owner", 1
        with patch.object(w, "gh", return_value={"items": [], "totalCount": 10001}):
            with self.assertRaises(RuntimeError):
                board.items()

    def test_existing_linked_pr_not_implemented_twice(self):
        selected = item(1)
        selected["linked pull requests"] = ["https://github.com/owner/repo/pull/9"]
        runner = self.runner()
        runner.issue = lambda n: {"state": "OPEN", "labels": []}
        with self.assertRaisesRegex(RuntimeError, "linked PR"):
            runner.pick(selected, "worker-1")

    def test_closed_issues_are_never_claimed(self):
        runner = self.runner()
        runner.issue = lambda n: {"state": "CLOSED"}
        self.assertIsNone(runner.pick(item(1), "worker-1"))

    def test_blocked_task_never_auto_restarts(self):
        self.state.save({"number": 1, "phase": "blocked"})
        runner = self.runner()
        runner.issue = lambda n: self.fail("blocked task was fetched")
        self.assertIsNone(runner.pick(item(1), "worker-1"))

    def test_passing_review_and_handoff_required_for_human(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(), result()])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.review(task)
        self.assertEqual(board.moves, [(1, w.HUMAN)])
        self.assertEqual(task["phase"], "human_review")
        self.assertEqual([call.args[0] for call in runner.backend.run.call_args_list], ["review", "finalize"])

    def test_stale_review_never_moves_to_human(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(sha="b" * 40)])
        runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")

    def test_pass_with_findings_is_blocked(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(findings=["Bug at file.py:4"])])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")

    def test_failed_handoff_does_not_move_to_human(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(), result("blocked")])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")

    def test_review_changes_return_to_ready(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result("changes_requested", findings=["Fix boundary"] )])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.review(task)
        self.assertEqual(board.moves, [(1, w.READY)])
        self.assertEqual(task["phase"], "rework")

    def test_three_failed_reviews_require_inspection(self):
        task = self.task()
        task["round"] = 3
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result("changes_requested")])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")

    def test_human_status_override_is_respected(self):
        task = self.task()
        board = FakeBoard([item(1, w.HUMAN)])
        runner = self.runner(board, [result(), result()])
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")

    def test_pause_prevents_dispatch(self):
        self.state.pause(True)
        runner = self.runner(FakeBoard([item(1)]))
        runner.pick = lambda *a: self.fail("picked while paused")
        runner.run(threading.Event())

    def test_caps_three_workers_one_reviewer_and_only_authorized_statuses(self):
        board = FakeBoard([*[item(n) for n in range(1, 7)],
                           *[item(n, w.REVIEW) for n in range(7, 10)], item(10, "Shaping"), item(11, w.HUMAN)])
        runner = self.runner(board)
        self.args.once = False
        stop, mutex = threading.Event(), threading.Lock()
        active, maximum, seen = {"build": 0, "review": 0}, {"build": 0, "review": 0}, set()
        def pick(selected, slot):
            number = selected["content"]["number"]
            if number in seen:
                return None
            seen.add(number)
            return {"number": number, "issue": {"title": str(number)}}
        def execute(task, review):
            kind = "review" if review else "build"
            with mutex:
                active[kind] += 1
                maximum[kind] = max(maximum[kind], active[kind])
            time.sleep(.03)
            with mutex:
                active[kind] -= 1
                if len(seen) == 9 and sum(active.values()) == 0:
                    stop.set()
        runner.pick, runner.execute = pick, execute
        timer = threading.Timer(3, stop.set)
        timer.start()
        try:
            runner.run(stop)
        finally:
            timer.cancel()
        self.assertEqual(maximum, {"build": 3, "review": 1})
        self.assertEqual(seen, set(range(1, 10)))

    def test_cross_repository_pr_is_rejected(self):
        runner = self.runner()
        with self.assertRaises(RuntimeError):
            w.Runner.pr(runner, "https://github.com/other/repo/pull/9")

    def test_pr_head_changes_require_fresh_review(self):
        runner = self.runner()
        runner.pr.return_value = {"state": "OPEN", "headRefOid": "b" * 40}
        with self.assertRaisesRegex(RuntimeError, "fresh review"):
            w.Runner.verify_pr(runner, self.task(), "a" * 40)

    def test_explicit_retry_reviews_current_head(self):
        task = self.task()
        task["phase"] = "retry"
        self.state.save(task)
        runner = self.runner()
        runner.issue = lambda n: {"state": "OPEN", "labels": []}
        runner.pr.return_value = {"state": "OPEN", "headRefOid": "b" * 40,
                                  "headRefName": task["branch"], "baseRefName": "main"}
        selected = runner.pick(task["item"], w.REVIEWER)
        self.assertEqual(selected["head_sha"], "b" * 40)
        self.assertEqual(selected["round"], 2)

    def test_board_status_ids_are_preserved_when_review_is_added(self):
        board = object.__new__(w.Board)
        board.options, board.field = {}, {"id": "status-field"}
        original = [{"id": "ready-id", "name": w.READY, "color": "YELLOW", "description": "Keep"},
                    {"id": "human-id", "name": w.HUMAN, "color": "BLUE", "description": "Keep"}]
        calls = []
        def graph(query, variables):
            calls.append(variables)
            return {"node": {"options": copy.deepcopy(original)}} if query.startswith("query") else {}
        def refresh():
            board.options[w.REVIEW.casefold()] = "review-id"
        board.refresh_fields = refresh
        with patch.object(w, "graphql", side_effect=graph):
            board.ensure_review_status()
        updated = calls[1]["input"]["singleSelectOptions"]
        self.assertEqual([updated[0], updated[2]], original)
        self.assertEqual(updated[1]["name"], w.REVIEW)


if __name__ == "__main__":
    unittest.main()
