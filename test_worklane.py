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


def review_thread(identifier="thread-1", *, resolved=False, outdated=False):
    return {"id": identifier, "isResolved": resolved, "isOutdated": outdated,
            "viewerCanResolve": True, "path": "file.py", "line": 4,
            "comments": [{"id": "comment-1", "body": "Fix the boundary condition",
                          "url": "https://github.com/owner/repo/pull/9#discussion_r1",
                          "author": {"login": "reviewer"}}]}


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


class FakeBoard:
    def __init__(self, items=()):
        self.queue = list(items)
        self.moves = []

    def items(self):
        return copy.deepcopy(self.queue)

    def item_status(self, selected):
        target = next((i for i in self.queue if i["id"] == selected["id"]), None)
        return target["status"].casefold() if target else None

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
        runner.verify_pr = unittest.mock.Mock(return_value={"isDraft": False})
        runner.pr = unittest.mock.Mock(return_value={"isDraft": False})
        runner.human_feedback = unittest.mock.Mock(return_value={})

        runner.review_threads = unittest.mock.Mock(return_value=[])
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

    def board_page(self, nodes=(), *, total=None, cursor=None):
        return {"node": {"items": {"nodes": list(nodes), "totalCount": len(nodes) if total is None else total,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}}

    def board_node(self, number, repo="owner/repo"):
        return {"id": f"item-{number}", "fieldValueByName": {"name": w.READY},
                "links": {"pullRequests": {"nodes": [{"url": "https://github.com/owner/repo/pull/9"}],
                          "pageInfo": {"hasNextPage": False}}}, "content": {
            "number": number, "title": "Issue", "body": "Description", "url": "https://example.com/issue",
            "repository": {"nameWithOwner": repo}}}

    def board(self):
        board = object.__new__(w.Board)
        board.repo, board.owner, board.number = "owner/repo", "owner", 1
        board.project = {"id": "project-1"}
        return board

    def test_board_excludes_other_repos_and_non_issues(self):
        closed = self.board_node(5)
        closed["content"]["state"] = "CLOSED"
        data = [self.board_node(1), self.board_node(2, "owner/other"), {"id": "pr", "content": {}},
                {"id": "draft", "content": None}, closed]
        with patch.object(w, "graphql", return_value=self.board_page(data)):
            items = self.board().items()
        self.assertEqual([i["content"]["number"] for i in items], [1])
        self.assertEqual(items[0]["linked pull requests"], ["https://github.com/owner/repo/pull/9"])
        self.assertEqual(items[0]["status"], w.READY)

    def test_incomplete_board_fails_closed(self):
        with patch.object(w, "graphql", return_value=self.board_page(total=10001)):
            with self.assertRaisesRegex(RuntimeError, "10,000"):
                self.board().items()

    def test_board_paginates_with_project_id_and_retains_all_issues(self):
        pages = [self.board_page([self.board_node(1)], total=2, cursor="next"),
                 self.board_page([self.board_node(2)], total=2)]
        with patch.object(w, "graphql", side_effect=pages) as api:
            self.assertEqual([i["content"]["number"] for i in self.board().items()], [1, 2])
        self.assertEqual(api.call_args_list[0].args[1], {"id": "project-1", "cursor": None})
        self.assertEqual(api.call_args_list[1].args[1]["cursor"], "next")

    def test_board_paginates_linked_pr_field_without_closing_reference(self):
        node = self.board_node(1)
        node["links"]["pullRequests"]["pageInfo"] = {"hasNextPage": True, "endCursor": "links-next"}
        extra = {"node": {"links": {"pullRequests": {"nodes": [{"url": "second"}],
                    "pageInfo": {"hasNextPage": False}}}}}
        with patch.object(w, "graphql", side_effect=[self.board_page([node]), extra]) as api:
            links = self.board().items()[0]["linked pull requests"]
        self.assertEqual(links, ["https://github.com/owner/repo/pull/9", "second"])
        self.assertEqual(api.call_args_list[1].args[1], {"id": "item-1", "cursor": "links-next"})
        self.assertIn('fieldValueByName(name:"Linked pull requests")', api.call_args_list[0].args[0])

    def test_board_rejects_stuck_cursor(self):
        page = self.board_page(total=2, cursor="stuck")
        with patch.object(w, "graphql", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "pagination"):
                self.board().items()

    def test_rate_limit_backoff_survives_new_runner_and_is_bounded(self):
        runner = self.runner()
        with patch.object(w.time, "time", return_value=1000):
            runner.github_backoff(w.GitHubRateLimit("exhausted"))
            self.assertEqual(runner.budget.snapshot()["until"], 1060)
            runner = self.runner()
            runner.github_backoff(w.GitHubRateLimit("exhausted"))
            self.assertEqual(runner.budget.snapshot()["delay"], 120)
            for _ in range(8):
                runner.github_backoff(w.GitHubRateLimit("exhausted"))
            self.assertEqual(runner.budget.snapshot()["delay"], 3600)

    def test_exhausted_quota_does_not_poll_every_loop(self):
        runner = self.runner()
        runner.board.items = unittest.mock.Mock(side_effect=w.GitHubRateLimit("exhausted"))
        self.args.once = False
        clock = [1000]
        stop = unittest.mock.Mock()
        stop.is_set.side_effect = lambda: clock[0] >= 1050
        stop.wait.side_effect = lambda _: clock.__setitem__(0, clock[0] + 10)
        with patch.object(w.time, "time", side_effect=lambda: clock[0]):
            runner.run(stop)
        runner.board.items.assert_called_once_with()

    def test_rate_limit_during_claim_does_not_try_remaining_slots(self):
        runner = self.runner(FakeBoard([item(1), item(2), item(3)]))
        runner.pick = unittest.mock.Mock(side_effect=w.GitHubRateLimit("exhausted"))
        runner.run(threading.Event())
        runner.pick.assert_called_once()
        self.assertEqual(self.state.all(), [])

    def test_claim_quota_failure_recovers_before_and_after_status_write(self):
        for wrote in (False, True):
            with self.subTest(wrote=wrote):
                board = FakeBoard([item(1)])
                runner = self.runner(board)
                runner.issue = unittest.mock.Mock(return_value={"state": "OPEN", "labels": [], "title": "Issue"})
                def fail(selected, status, expected):
                    if wrote:
                        FakeBoard.move(board, selected, status, expected)
                    raise w.GitHubRateLimit("claim exhausted")
                board.move = fail
                with self.assertRaises(w.GitHubRateLimit):
                    runner.pick(item(1), "worker-1")
                self.assertEqual(self.state.load(1)["phase"], "interrupted")
                board.move = lambda *args: FakeBoard.move(board, *args)
                recovered = runner.recover(board.items(), "worker-2")
                self.assertEqual(recovered["phase"], "building")
                self.assertEqual(board.moves, [(1, w.PROGRESS)])
                self.state.save({"number": 1, "phase": "rework"})

    def test_claim_failures_increase_backoff_after_successful_board_reads(self):
        runner = self.runner(FakeBoard([item(1)]))
        runner.pick = unittest.mock.Mock(side_effect=lambda *_: (_ for _ in ()).throw(w.GitHubRateLimit("exhausted")))
        for clock, delay in ((1000, 60), (1061, 120), (1182, 240)):
            with patch.object(w.time, "time", return_value=clock):
                runner.run(threading.Event())
            self.assertEqual(runner.budget.snapshot()["delay"], delay)

    def test_concurrent_failure_stops_remaining_claims_and_survives_clear(self):
        runner = self.runner(FakeBoard([item(1), item(2), item(3)]))
        def pick(*_):
            runner.github_backoff(w.GitHubRateLimit("worker failed"))
        runner.pick = unittest.mock.Mock(side_effect=pick)
        runner.run(threading.Event())
        runner.pick.assert_called_once()
        self.assertEqual(runner.budget.snapshot()["error"], "worker failed")
        before = runner.budget.snapshot()
        runner.github_backoff(w.GitHubRateLimit("new worker failure"))
        runner.budget.clear(before)
        self.assertEqual(runner.budget.snapshot()["error"], "new worker failure")

    def test_budget_shared_by_repositories_and_blocks_commands(self):
        one = self.runner()
        other = w.GitHubBudget(w.State("other/repo", self.root))
        with patch.object(w.time, "time", return_value=1000):
            one.github_backoff(w.GitHubRateLimit("quota", retry_at=5000))
            with patch.object(w.subprocess, "run") as process:
                with self.assertRaises(w.GitHubRateLimit):
                    other.call(lambda: w.command(["gh", "pr", "list"]))
            process.assert_not_called()
        self.assertEqual(other.snapshot()["until"], 5000)

    def test_budget_serializes_calls_across_repository_instances(self):
        first = self.runner().budget
        second = w.GitHubBudget(w.State("other/repo", self.root))
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        def request():
            entered.set()
            release.wait(2)
            return "done"
        with w.ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first.call, request)
            self.assertTrue(entered.wait(2))
            def another():
                waiting.set()
                return second.call(lambda: "later")
            two = pool.submit(another)
            self.assertTrue(waiting.wait(2))
            self.assertFalse(two.done())
            release.set()
            self.assertEqual(one.result(timeout=2), "done")
            self.assertEqual(two.result(timeout=2), "later")

    def test_success_exhausting_last_point_defers_next_request(self):
        budget = self.runner().budget
        with patch.object(w.time, "time", return_value=1000):
            response = budget.call(lambda: "HTTP/2.0 200 OK\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: 5000\n\n{}")
            self.assertIn("200 OK", response)
            with self.assertRaises(w.GitHubRateLimit):
                budget.call(lambda: self.fail("request escaped cooldown"))
        self.assertEqual(budget.snapshot()["until"], 5001)

    def test_idle_hour_and_exhausted_hour_have_bounded_request_counts(self):
        for exhausted, expected in ((False, 60), (True, 6)):
            runner = self.runner()
            runner.budget.state.set_meta("github_backoff", {})
            def items():
                if exhausted:
                    raise w.GitHubRateLimit("exhausted")
                return []
            runner.board.items = unittest.mock.Mock(side_effect=items)
            self.args.once, self.args.interval = False, 60
            clock = [1000]
            stop = unittest.mock.Mock()
            stop.is_set.side_effect = lambda: clock[0] >= 4600
            stop.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
            with patch.object(w.time, "time", side_effect=lambda: clock[0]):
                runner.run(stop)
            self.assertEqual(runner.board.items.call_count, expected)

    def test_completed_build_retries_verification_without_another_agent_turn(self):
        task = self.task(w.PROGRESS)
        task.update(phase="building", issue={"title": "Issue"})
        runner = self.runner(FakeBoard([task["item"]]), [result()])
        runner.verify_pr.side_effect = [w.GitHubRateLimit("verify exhausted"), {}]
        runner.execute(task, False)
        self.assertEqual(self.state.load(1)["phase"], "interrupted")
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(self.state.load(1), False)
        runner.backend.run.assert_called_once()
        self.assertEqual(self.state.load(1)["phase"], "awaiting_review")

    def test_waiting_review_drift_is_visible_and_preserves_external_status(self):
        for status in (w.PROGRESS, w.READY, w.HUMAN, "Done", None):
            with self.subTest(status=status):
                task = {**self.task(), "phase": "awaiting_review", "build_result": result()}
                self.state.save(task)
                board = FakeBoard([item(1, status)] if status else [])
                runner = self.runner(board)
                runner.run(threading.Event())
                saved = self.state.load(1)
                self.assertEqual(saved["phase"], "blocked")
                self.assertEqual(saved["blocker"], "board_status_drift")
                self.assertIn("worklane retry 1", saved["error"])
                self.assertEqual(board.moves, [])
                runner.backend.run.assert_not_called()

    def test_merged_pr_completes_missing_board_task_before_drift_checks(self):
        for phase in ("human_review", "awaiting_review", "blocked"):
            with self.subTest(phase=phase):
                task = {**self.task(), "phase": phase, "build_result": result(), "error": "Old blocker",
                        "resume_stage": "review", "human_feedback_pending": True}
                self.state.set_meta("completion:1", {})
                run = self.state.begin_run(task, "review", self.root)
                self.state.attach_session(run, "12345678-1234-4234-8234-123456789012", w.MODEL)
                self.state.finish_run(run, "completed", result=result())
                history = self.state.runs(1)
                session = self.state.session(1, "reviewer")
                runner = self.runner(FakeBoard())
                runner.pr.return_value = {"state": "MERGED", "headRefName": task["branch"],
                    "baseRefName": "main", "headRefOid": "b" * 40, "mergedAt": "2026-10-19T12:00:00Z"}
                runner.run(threading.Event())
                saved = self.state.load(1)
                self.assertEqual(saved["phase"], "done")
                self.assertEqual(saved["merged_head_sha"], "b" * 40)
                self.assertEqual(saved["build_result"], result())
                self.assertNotIn("error", saved)
                self.assertNotIn("resume_stage", saved)
                self.assertNotIn("human_feedback_pending", saved)
                self.assertEqual(self.state.runs(1), history)
                self.assertEqual(self.state.session(1, "reviewer"), session)
                self.assertEqual(runner.board.moves, [])
                runner.backend.run.assert_not_called()

    def test_merged_task_cannot_be_dispatched_from_stale_ready_board_status(self):
        task = {**self.task(w.READY), "phase": "human_review"}
        self.state.save(task)
        runner = self.runner(FakeBoard([task["item"]]))
        runner.pr.return_value = {"state": "MERGED", "headRefName": task["branch"],
                                 "baseRefName": "main", "headRefOid": "a" * 40}
        runner.run(threading.Event())
        self.assertEqual(self.state.load(1)["phase"], "done")
        runner.backend.run.assert_not_called()
        self.assertEqual(runner.board.moves, [])

    def test_closed_unmerged_or_retargeted_pr_does_not_complete_task(self):
        for change in ({"state": "CLOSED"}, {"baseRefName": "other"}, {"headRefName": "other"}):
            with self.subTest(change=change):
                task = {**self.task(w.HUMAN), "phase": "human_review"}
                self.state.save(task)
                self.state.set_meta("completion:1", {})
                runner = self.runner()
                runner.pr.return_value = {"state": "MERGED", "headRefName": task["branch"],
                                         "baseRefName": "main", "headRefOid": "a" * 40, **change}
                runner.reconcile_completed([])
                self.assertEqual(self.state.load(1)["phase"], "human_review")
                self.assertEqual(runner.board.moves, [])

    def test_completion_checks_are_bounded_without_changing_feedback_cutoff(self):
        task = {**self.task(w.HUMAN), "phase": "human_review"}
        self.state.save(task)
        before = self.state.load(1)
        runner = self.runner(FakeBoard([task["item"]]))
        runner.pr.return_value = {"state": "OPEN", "headRefName": task["branch"], "baseRefName": "main"}
        with patch.object(w, "now", return_value="2026-10-19T12:00:00+00:00"):
            runner.reconcile_completed(runner.board.items())
        self.assertEqual(self.state.load(1), before)
        reopened = self.runner(runner.board)
        reopened.pr = runner.pr
        with patch.object(w, "now", return_value="2026-10-19T12:04:59+00:00"):
            reopened.reconcile_completed(runner.board.items())
        runner.pr.assert_called_once()
        runner.pr.return_value.update(state="MERGED", headRefOid="a" * 40)
        # Closed issues disappear from the board intake; check that change immediately.
        with patch.object(w, "now", return_value="2026-10-19T12:04:59+00:00"):
            reopened.reconcile_completed([])
        self.assertEqual(self.state.load(1)["phase"], "done")

    def test_completion_does_not_touch_live_assignments_or_completed_tasks(self):
        for phase, active in (("building", ()), ("human_review", (1,)), ("done", ())):
            with self.subTest(phase=phase):
                task = {**self.task(), "phase": phase}
                self.state.save(task)
                runner = self.runner()
                runner.reconcile_completed([], active)
                runner.pr.assert_not_called()
                self.assertEqual(self.state.load(1)["phase"], phase)

    def test_completion_api_failure_preserves_task_and_rate_limit(self):
        task = {**self.task(), "phase": "human_review"}
        self.state.save(task)
        before = self.state.load(1)
        runner = self.runner()
        runner.pr.side_effect = RuntimeError("network unavailable")
        runner.reconcile_completed([])
        self.assertEqual(self.state.load(1), before)
        self.assertEqual(self.state.get_meta("completion:1", {}), {})
        runner.pr.side_effect = w.GitHubRateLimit("exhausted")
        with self.assertRaises(w.GitHubRateLimit):
            runner.reconcile_completed([])
        self.assertEqual(self.state.load(1), before)

    def test_waiting_review_uses_direct_status_when_listing_lags(self):
        task = {**self.task(), "phase": "awaiting_review", "build_result": result()}
        self.state.save(task)
        board = FakeBoard([item(1, w.REVIEW)])
        board.items = unittest.mock.Mock(return_value=[item(1, w.PROGRESS)])
        runner = self.runner(board)
        runner.issue = unittest.mock.Mock(return_value={"state": "OPEN", "labels": [], "title": "Issue"})
        runner.execute = unittest.mock.Mock()
        runner.run(threading.Event())
        runner.execute.assert_called_once()
        self.assertTrue(runner.execute.call_args.args[1])
        self.assertEqual(board.moves, [])

    def test_waiting_review_reconciliation_skips_active_assignments(self):
        task = {**self.task(), "phase": "awaiting_review"}
        self.state.save(task)
        runner = self.runner(FakeBoard([item(1, w.PROGRESS)]))
        runner.reconcile_waiting(runner.board.items(), {1})
        self.assertEqual(self.state.load(1)["phase"], "awaiting_review")

    def test_retry_status_drift_reuses_completed_build(self):
        task = {**self.task(w.PROGRESS), "phase": "awaiting_review", "build_result": result()}
        self.state.save(task)
        runner = self.runner(FakeBoard([task["item"]]))
        runner.reconcile_waiting(runner.board.items())
        with patch.object(w, "repository", return_value=("owner/repo", self.root, "main")), \
                patch.object(w, "State", return_value=self.state):
            self.assertEqual(w.main(["retry", "1"]), 0)
        runner.issue = unittest.mock.Mock(return_value={"state": "OPEN"})
        selected = runner.recover(runner.board.items(), "worker-1")
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(selected, False)
        runner.backend.run.assert_not_called()
        runner.verify_pr.assert_called_once()
        self.assertEqual(self.state.load(1)["phase"], "awaiting_review")
        self.assertEqual(runner.board.moves, [(1, w.REVIEW)])

    def test_completed_review_and_finalizer_survive_quota_failure(self):
        task = self.task()
        runner = self.runner(FakeBoard([task["item"]]), [result(), result()])
        del runner.verify_pr
        pr = {"state": "OPEN", "headRefOid": task["head_sha"], "headRefName": task["branch"],
              "baseRefName": task["base"], "isDraft": True}
        runner.pr.side_effect = ([pr] * 4 + [w.GitHubRateLimit("ready verification exhausted")]
                                 + [dict(pr, isDraft=False)] * 4)
        with patch.object(w, "command", side_effect=["a" * 40, "", "", "a" * 40, ""]) as cmd:
            runner.execute(task, True)
            self.assertEqual(task["resume_stage"], "finalize")
            self.assertEqual(runner.board.moves, [])
            runner.execute(self.state.load(1), True)
        self.assertEqual([c.args[0] for c in cmd.call_args_list if c.args[0][0] == "gh"],
                         [["gh", "pr", "ready", task["pr_url"], "--repo", runner.repo]])
        self.assertEqual([c.args[0] for c in runner.backend.run.call_args_list], ["review", "finalize"])
        self.assertEqual(self.state.load(1)["phase"], "human_review")

    def test_board_and_link_cursor_cycles_fail_closed(self):
        for cursors in (("a", "b", "a"),):
            pages = [self.board_page(cursor=c) for c in cursors]
            with patch.object(w, "graphql", side_effect=pages):
                with self.assertRaisesRegex(RuntimeError, "cyclic"):
                    self.board().items()
            connection = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "a"}}
            more = [{"node": {"links": {"pullRequests": {"nodes": [], "pageInfo": {
                "hasNextPage": True, "endCursor": c}}}}} for c in ("b", "a")]
            with patch.object(w, "graphql", side_effect=more):
                with self.assertRaisesRegex(RuntimeError, "cyclic"):
                    self.board().linked_prs("item", connection)

    def test_review_thread_and_reply_cursor_cycles_fail_closed(self):
        runner = self.runner()
        pages = [{"repository": {"pullRequest": {"reviewThreads": connection([], c)}}}
                 for c in ("a", "b", "a")]
        with patch.object(w, "graphql", side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, "cyclic"):
                w.Runner.review_threads(runner, self.task()["pr_url"])
        thread = review_thread()
        thread["comments"] = connection([], "a")
        pages = [{"repository": {"pullRequest": {"reviewThreads": connection([thread])}}}]
        pages += [{"node": {"comments": connection([], c)}} for c in ("b", "a")]
        with patch.object(w, "graphql", side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, "cyclic"):
                w.Runner.review_threads(runner, self.task()["pr_url"])

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

    def test_human_handoff_verifies_ready_pr_and_only_promotes_drafts(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                task = self.task()
                board = FakeBoard([task["item"]])
                runner = self.runner(board, [result(), result()])
                del runner.verify_pr
                pr = {"state": "OPEN", "headRefOid": task["head_sha"], "headRefName": task["branch"],
                      "baseRefName": task["base"], "isDraft": draft}
                runner.pr.side_effect = lambda _: dict(pr)

                def command(args, **kwargs):
                    if args[0] == "git":
                        return task["head_sha"] if args[1] == "rev-parse" else ""
                    self.assertEqual(args, ["gh", "pr", "ready", task["pr_url"], "--repo", runner.repo])
                    self.assertEqual(board.moves, [])
                    self.assertEqual([c.args[0] for c in runner.backend.run.call_args_list], ["review", "finalize"])
                    self.assertIn("human_feedback_seen", task)
                    pr["isDraft"] = False
                    return ""

                with patch.object(w, "command", side_effect=command) as cmd:
                    runner.review(task)
                self.assertEqual(sum(c.args[0][0] == "gh" for c in cmd.call_args_list), int(draft))
                self.assertEqual(runner.pr.call_count, 5 if draft else 4)
                self.assertFalse(pr["isDraft"])
                self.assertEqual(board.moves, [(1, w.HUMAN)])
                self.assertEqual(self.state.load(1)["phase"], "human_review")

    def test_failed_or_unverified_draft_promotion_blocks_human_review(self):
        cases = (
            ({}, RuntimeError("promotion denied"), "promotion denied"),
            ({}, None, "still a draft"),
            ({"isDraft": False, "headRefOid": "b" * 40}, None, "head changed"),
            ({"isDraft": False, "state": "CLOSED"}, None, "closed"),
            ({"isDraft": False, "headRefName": "other"}, None, "branch/base"),
            ({"isDraft": False, "baseRefName": "other"}, None, "branch/base"),
            (RuntimeError("verification unavailable"), None, "verification unavailable"),
        )
        for change, failure, error in cases:
            with self.subTest(error=error, change=change):
                task = self.task()
                board = FakeBoard([task["item"]])
                runner = self.runner(board, [result(), result()])
                del runner.verify_pr
                pr = {"state": "OPEN", "headRefOid": task["head_sha"], "headRefName": task["branch"],
                      "baseRefName": task["base"], "isDraft": True}
                after = change if isinstance(change, Exception) else {**pr, **change}
                runner.pr.side_effect = [pr] * 4 + [after]
                with patch.object(w, "command", side_effect=[task["head_sha"], "", failure or ""]) as cmd:
                    runner.execute(task, True)
                self.assertEqual(cmd.call_args_list[-1].args[0],
                                 ["gh", "pr", "ready", task["pr_url"], "--repo", runner.repo])
                self.assertEqual(board.moves, [])
                self.assertEqual(self.state.load(1)["phase"], "blocked")
                self.assertIn(error, task["error"])

    def test_changed_pr_before_promotion_blocks_human_review(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(), result()])
        del runner.verify_pr
        pr = {"state": "OPEN", "headRefOid": task["head_sha"], "headRefName": task["branch"],
              "baseRefName": task["base"], "isDraft": True}
        runner.pr.side_effect = [pr] * 3 + [dict(pr, headRefOid="b" * 40)]
        with patch.object(w, "command", side_effect=[task["head_sha"], ""]) as cmd:
            runner.execute(task, True)
        self.assertTrue(all(c.args[0][0] == "git" for c in cmd.call_args_list))
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")
        self.assertIn("head changed", task["error"])

    def test_review_threads_include_all_pages_and_replies(self):
        runner = self.runner()
        first = review_thread()
        comments = first["comments"] * 100
        first["comments"] = connection(comments, "reply-page-2")
        rest = [review_thread(f"thread-{i}", resolved=True) for i in range(2, 101)]
        for thread in rest:
            thread["comments"] = connection(thread["comments"])
        last = review_thread("thread-101", outdated=True)
        last["viewerCanResolve"] = False
        last["comments"] = connection(last["comments"])
        reply = {"id": "reply", "body": "Still needs a fix", "url": "reply-url", "author": None}
        pages = [{"repository": {"pullRequest": {"reviewThreads": connection([first, *rest], "thread-page-2")}}},
                 {"node": {"comments": connection([reply])}},
                 {"repository": {"pullRequest": {"reviewThreads": connection([last])}}}]
        with patch.object(w, "graphql", side_effect=pages) as api:
            threads = w.Runner.review_threads(runner, self.task()["pr_url"])
        self.assertEqual(len(threads), 101)
        self.assertEqual(threads[0]["comments"], comments + [reply])
        self.assertTrue(threads[1]["isResolved"])
        self.assertTrue(threads[-1]["isOutdated"])
        self.assertFalse(threads[-1]["viewerCanResolve"])
        self.assertEqual(api.call_args_list[0].args[1], {"owner": "owner", "name": "repo", "number": 9, "cursor": None})
        self.assertEqual(api.call_args_list[1].args[1], {"id": "thread-1", "cursor": "reply-page-2"})
        self.assertEqual(api.call_args_list[2].args[1]["cursor"], "thread-page-2")
        self.assertTrue(all(call.args[0].startswith("query") for call in api.call_args_list))

    def test_review_thread_api_errors_fail_closed(self):
        runner = self.runner()
        with patch.object(w, "command", return_value='{"data": {}, "errors": [{"message": "Forbidden"}]}'):
            with self.assertRaisesRegex(RuntimeError, "Forbidden"):
                w.Runner.review_threads(runner, self.task()["pr_url"])

    def test_review_thread_pagination_cannot_silently_truncate(self):
        runner = self.runner()
        page = {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}}}}
        with patch.object(w, "graphql", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "incomplete or cyclic pagination"):
                w.Runner.review_threads(runner, self.task()["pr_url"])

    def test_review_thread_lookup_rejects_other_repositories(self):
        with patch.object(w, "graphql") as api:
            with self.assertRaisesRegex(RuntimeError, "outside the assigned repository"):
                w.Runner.review_threads(self.runner(), "https://github.com/other/repo/pull/9")
        api.assert_not_called()

    def test_worker_receives_threads_and_must_resolve_them_before_completion(self):
        task = self.task(w.PROGRESS)
        task["phase"] = "building"
        board = FakeBoard([task["item"]])
        runner = self.runner(board)
        open_thread = review_thread()
        resolved_thread = review_thread(resolved=True)
        runner.review_threads.side_effect = [[open_thread], [resolved_thread]]

        def build(stage, supplied, tree, extra):
            self.assertEqual(stage, "build")
            self.assertEqual(supplied["review_threads"], [open_thread])
            return result()

        runner.backend.run.side_effect = build
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.build(task)
        self.assertEqual(board.moves, [(1, w.REVIEW)])
        self.assertEqual(self.state.load(1)["review_threads"], [resolved_thread])

    def test_worker_pass_with_open_thread_does_not_advance(self):
        for existing_pr in (False, True):
            with self.subTest(existing_pr=existing_pr):
                task = self.task(w.PROGRESS)
                task["phase"] = "building"
                if not existing_pr:
                    task["pr_url"] = ""
                board = FakeBoard([task["item"]])
                runner = self.runner(board, [result()])
                runner.review_threads.return_value = [review_thread()]
                with patch.object(w, "command", side_effect=["a" * 40, ""]):
                    runner.execute(task, False)
                self.assertEqual(board.moves, [])
                self.assertEqual(task["phase"], "blocked")
                self.assertIn("thread-1 (file.py)", task["error"])
                runner.post.assert_not_called()

    def test_open_threads_block_passing_review_even_when_outdated(self):
        for outdated in (False, True):
            with self.subTest(outdated=outdated):
                task = self.task()
                board = FakeBoard([task["item"]])
                runner = self.runner(board, [result()])
                runner.review_threads.return_value = [review_thread(outdated=outdated)]
                with patch.object(w, "command", side_effect=["a" * 40, ""]):
                    runner.execute(task, True)
                self.assertEqual(board.moves, [])
                self.assertEqual(task["phase"], "blocked")
                self.assertIn("Unresolved PR review threads", task["error"])
                self.assertEqual([c.args[0] for c in runner.backend.run.call_args_list], ["review"])

    def test_resolved_threads_are_supplied_to_reviewer_and_allow_handoff(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board)
        threads = [review_thread(resolved=True, outdated=True)]
        runner.review_threads.return_value = threads

        def review(stage, supplied, tree, extra):
            self.assertEqual(supplied["review_threads"], threads)
            return result()

        runner.backend.run.side_effect = review
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.review(task)
        self.assertEqual(board.moves, [(1, w.HUMAN)])

    def test_thread_opened_during_handoff_blocks_human_review(self):
        task = self.task()
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(), result()])
        runner.review_threads.side_effect = [[], [], [review_thread()]]
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.execute(task, True)
        self.assertEqual(board.moves, [])
        self.assertEqual(task["phase"], "blocked")
        self.assertEqual([c.args[0] for c in runner.backend.run.call_args_list], ["review", "finalize"])

    def test_thread_fetch_failure_never_advances_worker_or_reviewer(self):
        for reviewing in (False, True):
            with self.subTest(reviewing=reviewing):
                task = self.task(w.REVIEW if reviewing else w.PROGRESS)
                board = FakeBoard([task["item"]])
                runner = self.runner(board)
                runner.review_threads.side_effect = RuntimeError("GitHub unavailable")
                runner.execute(task, reviewing)
                self.assertEqual(board.moves, [])
                self.assertEqual(task["phase"], "blocked")
                runner.backend.run.assert_not_called()

    def test_question_review_does_not_fetch_pr_threads(self):
        task = self.task()
        task.update(question=True, pr_url="", head_sha="")
        board = FakeBoard([task["item"]])
        runner = self.runner(board, [result(sha="")])
        runner.review(task)
        runner.review_threads.assert_not_called()
        self.assertEqual(board.moves, [(1, w.HUMAN)])

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
        runner.review_threads.return_value = [review_thread()]
        with patch.object(w, "command", side_effect=["a" * 40, ""]):
            runner.review(task)
        self.assertEqual(board.moves, [(1, w.READY)])
        self.assertEqual(task["phase"], "rework")

    def test_merge_conflict_requeues_worker_without_launching_reviewer(self):
        task = self.task()
        runner = self.runner(FakeBoard([task["item"]]))
        runner.verify_pr.return_value = {"mergeable": "CONFLICTING"}
        runner.execute(task, True)
        self.assertEqual(task["phase"], "rework")
        self.assertIn("merge conflicts", task["review_result"]["findings"][0])
        self.assertEqual(runner.board.moves, [(1, w.READY)])
        runner.backend.run.assert_not_called()
        runner.worktree.assert_not_called()
        runner.post.assert_called_once()

    def test_conflict_during_finalizer_never_advances_to_human(self):
        task = self.task()
        runner = self.runner(FakeBoard([task["item"]]), [result(), result()])
        runner.verify_pr.side_effect = [{"isDraft": False}] * 3 + [{"isDraft": True, "mergeable": "CONFLICTING"}]
        with patch.object(w, "command", side_effect=["a" * 40, ""]) as command:
            runner.execute(task, True)
        self.assertEqual(task["phase"], "rework")
        self.assertEqual(runner.board.moves, [(1, w.READY)])
        self.assertFalse(any(c.args[0][:3] == ["gh", "pr", "ready"] for c in command.call_args_list))

    def test_unknown_mergeability_retries_saved_handoff_without_another_turn(self):
        task = {**self.task(), "active_stage": "finalize"}
        runner = self.runner(FakeBoard([task["item"]]), [result(), result()])
        runner.verify_pr.side_effect = ([{"isDraft": False}] * 3
                                        + [{"isDraft": True, "mergeable": "UNKNOWN"}]
                                        + [{"isDraft": False, "mergeable": "MERGEABLE"}] * 4)
        with patch.object(w, "command", side_effect=["a" * 40, "", "a" * 40, ""]):
            runner.execute(task, True)
            self.assertEqual(task["phase"], "interrupted")
            self.assertEqual(task["resume_stage"], "finalize")
            self.assertEqual(runner.board.moves, [])
            runner.execute(self.state.load(1), True)
        self.assertEqual(self.state.load(1)["phase"], "human_review")
        self.assertEqual([c.args[0] for c in runner.backend.run.call_args_list], ["review", "finalize"])

    def test_conflicts_respect_round_limit_and_human_board_edits(self):
        for limit, status in ((3, w.REVIEW), (1, w.HUMAN)):
            with self.subTest(limit=limit, status=status):
                task = {**self.task(), "round": limit}
                runner = self.runner(FakeBoard([item(1, status)]))
                runner.verify_pr.return_value = {"mergeable": "CONFLICTING"}
                runner.execute(task, True)
                self.assertEqual(task["phase"], "blocked")
                self.assertEqual(runner.board.moves, [])
                runner.backend.run.assert_not_called()

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


class GitHubErrorTests(unittest.TestCase):
    def test_primary_reset_and_secondary_retry_after_headers(self):
        with patch.object(w.time, "time", return_value=1000):
            primary = w.rate_limit_error('X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 5000\r\n{"type":"RATE_LIMIT"}')
            secondary = w.rate_limit_error("Retry-After: 120\nsecondary rate limit exceeded")
        self.assertEqual(primary.retry_at, 5001)
        self.assertEqual(secondary.retry_at, 1120)

    def test_graphql_error_body_is_classified_even_with_success_exit(self):
        response = 'HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 5000\r\n\r\n{"errors":[{"type":"RATE_LIMIT"}]}'
        with patch.object(w, "command", return_value=response):
            with self.assertRaises(w.GitHubRateLimit) as error:
                w.graphql("query { viewer { login } }")
        self.assertEqual(error.exception.retry_at, 5001)

    def test_header_reset_avoids_an_extra_probe(self):
        failure = subprocess.CompletedProcess([], 1, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 5000", "API rate limit exceeded")
        with patch.object(w.subprocess, "run", return_value=failure) as call:
            with self.assertRaises(w.GitHubRateLimit):
                w.command(["gh", "pr", "list"])
        self.assertEqual(call.call_count, 1)

    def test_probe_timeout_keeps_known_quota_failure(self):
        failure = subprocess.CompletedProcess([], 1, "", "API rate limit exceeded")
        with patch.object(w.subprocess, "run", side_effect=[failure, subprocess.TimeoutExpired("gh", 120)]):
            with self.assertRaises(w.GitHubRateLimit):
                w.command(["gh", "pr", "list"])

    def test_rate_limit_is_distinguished_from_other_errors(self):
        failed = subprocess.CompletedProcess([], 1, "", "GraphQL: API rate limit exceeded")
        with patch.object(w.subprocess, "run", return_value=failed):
            with self.assertRaises(w.GitHubRateLimit):
                w.command(["gh", "pr", "list"])

    def test_project_owner_error_reports_underlying_rate_limit(self):
        responses = [subprocess.CompletedProcess([], 1, "", "unknown owner type"),
                     subprocess.CompletedProcess([], 1, "", "API rate limit exceeded")]
        with patch.object(w.subprocess, "run", side_effect=responses) as call:
            with self.assertRaises(w.GitHubRateLimit):
                w.command(["gh", "project", "view", "1"])
        self.assertEqual(call.call_count, 2)

    def test_real_owner_error_is_not_misreported_as_rate_limit(self):
        responses = [subprocess.CompletedProcess([], 1, "", "unknown owner type"),
                     subprocess.CompletedProcess([], 0, '{"data":{"viewer":{"login":"me"}}}', "")]
        with patch.object(w.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "unknown owner type") as error:
                w.command(["gh", "project", "view", "1"])
        self.assertNotIsInstance(error.exception, w.GitHubRateLimit)


if __name__ == "__main__":
    unittest.main()
