import argparse
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import worklane as w
from test_worklane import FakeBoard, item, result


class HumanFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = w.State("owner/repo", self.root)
        self.board = FakeBoard([item(1, w.HUMAN)])
        self.args = argparse.Namespace(base="main", once=True, interval=.01)
        self.runner = w.Runner(self.args, "owner/repo", self.root, self.state, self.board, Mock())
        self.runner.issue = Mock(return_value={"state": "OPEN", "labels": [], "title": "Feedback"})
        self.runner.pr = Mock(return_value={"state": "OPEN", "headRefName": "codex/1-worklane",
                                            "baseRefName": "main", "headRefOid": "a" * 40})
        self.task = {"number": 1, "item": item(1, w.HUMAN), "phase": "human_review",
                     "branch": "codex/1-worklane", "base": "main", "head_sha": "a" * 40,
                     "pr_url": "https://github.com/owner/repo/pull/9", "question": False,
                     "round": 3, "human_feedback_seen": {}, "worker_worktree": str(self.root)}
        self.state.save(self.task)
        self.clock = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
        self.time_patch = patch.object(w, "now", side_effect=lambda: self.clock.isoformat())
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def feedback(self, body="Fix the boundary", source="pr:1"):
        return {source: {"body": body, "author": "human", "state": "",
                         "updated_at": self.clock.isoformat(), "url": "https://github.com/comment"}}

    def poll(self, feedback):
        with patch.object(self.runner, "human_feedback", return_value=feedback):
            self.runner.poll_human_feedback(self.board.items())

    def advance(self, seconds=301):
        self.clock += timedelta(seconds=seconds)

    def test_batch_waits_for_quiet_and_survives_restart(self):
        first = self.feedback()
        self.poll(first)
        self.advance(240)
        second = {**first, **self.feedback("Also fix validation", "inline:2")}
        self.poll(second)
        self.advance(240)
        # Reopening SQLite must not restart or forget the timer.
        self.runner.state = w.State("owner/repo", self.root)
        self.poll(second)
        self.assertEqual(self.board.moves, [])
        self.advance(61)
        self.poll(second)
        self.assertEqual(self.board.moves, [(1, w.READY)])
        selected = self.runner.pick(self.board.items()[0], "worker-2")
        self.assertEqual(self.board.moves[-1], (1, w.PROGRESS))
        self.assertEqual(selected["phase"], "building")
        self.assertEqual(selected["round"], 0)
        self.assertEqual(selected["branch"], self.task["branch"])
        self.assertEqual(selected["worker_worktree"], str(self.root))
        self.assertEqual(selected["human_feedback"], second)
        self.assertEqual(selected["human_feedback_seen"], second)
        self.assertNotIn("human_feedback_pending", selected)

    def test_later_comment_during_work_is_not_acknowledged(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        self.poll(first)
        selected = self.runner.pick(self.board.items()[0], "worker-1")
        # A later follow-up arrives after the batch was assigned.
        second = {**first, **self.feedback("One more fix", "issue:2")}
        selected.update(phase="human_review", item=item(1, w.HUMAN))
        self.board.queue = [item(1, w.HUMAN)]
        self.state.save(selected)
        self.poll(second)
        self.assertEqual(self.state.load(1)["human_feedback"], {"issue:2": second["issue:2"]})
        self.advance()
        self.poll(second)
        self.assertEqual(self.board.moves[-1], (1, w.READY))

    def test_queued_batch_extends_when_all_workers_busy(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        self.poll(first)
        second = {**first, **self.feedback("More", "issue:2")}
        self.poll(second)
        self.assertIsNone(self.runner.pick(self.board.items()[0], "worker-1"))
        self.advance()
        self.poll(second)
        self.assertIsNotNone(self.runner.pick(self.board.items()[0], "worker-1"))

    def test_edited_comments_are_new_feedback(self):
        first = self.feedback()
        self.task["human_feedback_seen"] = first
        self.state.save(self.task)
        self.poll(first)
        self.assertEqual(self.board.moves, [])
        edited = self.feedback("Edited request")
        self.poll(edited)
        self.advance()
        self.poll(edited)
        self.assertEqual(self.state.load(1)["human_feedback"], edited)
        self.assertEqual(self.board.moves, [(1, w.READY)])

    def test_inline_position_changes_do_not_replay_feedback(self):
        first = self.feedback(source="inline:1")
        first["inline:1"].update(path="code.py", line=42)
        self.task["human_feedback_seen"] = first
        self.state.save(self.task)
        changed = copy.deepcopy(first)
        changed["inline:1"]["line"] = None  # Outdated after a rework commit.
        self.poll(changed)
        self.advance()
        self.poll(changed)
        self.assertEqual(self.state.load(1)["human_feedback"], {})
        self.assertEqual(self.board.moves, [])

    def test_active_finalizer_cannot_race_with_feedback_state(self):
        with patch.object(self.runner, "human_feedback") as feedback:
            self.runner.poll_human_feedback(self.board.items(), {1})
        feedback.assert_not_called()
        self.assertEqual(self.board.moves, [])

    def test_deleted_pending_feedback_does_not_start_worker(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        self.poll(first)
        self.poll({})
        self.assertIsNone(self.runner.pick(self.board.items()[0], "worker-1"))

    def test_failed_api_read_does_not_dispatch_stale_batch(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        self.poll(first)
        with patch.object(self.runner, "human_feedback", side_effect=RuntimeError("network")):
            self.runner.poll_human_feedback(self.board.items())
        self.assertIsNone(self.runner.pick(self.board.items()[0], "worker-1"))
        self.assertEqual(self.state.load(1)["human_feedback_seen"], {})

    def test_manual_status_closed_issue_and_unowned_items_are_preserved(self):
        self.board.queue[0]["status"] = "Done"
        self.poll(self.feedback())
        self.board.queue[0]["status"] = w.HUMAN
        self.runner.issue.return_value["state"] = "CLOSED"
        self.poll(self.feedback())
        self.board.queue.append(item(2, w.HUMAN))
        self.assertEqual(self.board.moves, [])
        self.assertEqual(self.runner.issue.call_count, 1)

    def test_pause_prevents_feedback_poll_and_dispatch(self):
        self.state.pause(True)
        self.runner.poll_human_feedback = Mock(side_effect=AssertionError("polled while paused"))
        self.runner.run(threading.Event())
        self.assertEqual(self.board.moves, [])

    def test_run_dispatches_human_rework_once(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        self.runner.execute = Mock()
        with patch.object(self.runner, "human_feedback", return_value=first):
            self.runner.run(threading.Event())
        self.runner.execute.assert_called_once()
        selected, review = self.runner.execute.call_args.args
        self.assertFalse(review)
        self.assertEqual(selected["human_feedback"], first)
        self.assertEqual(self.board.moves, [(1, w.READY), (1, w.PROGRESS)])

    def test_transition_reconciles_crash_after_remote_move(self):
        first = self.feedback()
        self.poll(first)
        self.advance()
        original_move = self.board.move
        def move_then_crash(*args):
            original_move(*args)
            raise RuntimeError("controller lost the response")
        with patch.object(self.board, "move", side_effect=move_then_crash):
            self.poll(first)
        self.assertEqual(self.state.load(1)["phase"], "human_review")
        self.poll(first)
        self.assertEqual(self.state.load(1)["phase"], "rework")
        self.assertIsNotNone(self.runner.pick(self.board.items()[0], "worker-1"))

    def test_existing_task_baseline_keeps_feedback_since_handoff(self):
        self.task.pop("human_feedback_seen")
        self.state.save(self.task)
        old = self.feedback()
        self.advance()
        new = self.feedback("New request", "inline:2")
        self.poll({**old, **new})
        self.assertEqual(self.state.load(1)["human_feedback"], new)

    def test_question_feedback_uses_issue_without_a_pr(self):
        self.task.update(question=True, pr_url="", head_sha="")
        raw = self.raw_comment(1)
        with patch.object(w, "gh", return_value=[[raw]]) as api:
            feedback = self.runner.human_feedback(self.task)
        self.assertEqual(set(feedback), {"issue:1"})
        self.assertEqual(api.call_count, 1)
        self.runner.pr.assert_not_called()

    @staticmethod
    def raw_comment(number, **extra):
        return {"id": number, "body": "Please fix", "user": {"login": "human", "type": "User"},
                "created_at": "2026-10-19T10:00:00Z", "html_url": "https://github.com/comment", **extra}

    def test_reads_all_pages_and_kinds_ignoring_bots_markers_and_approvals(self):
        pages = [
            [[self.raw_comment(1)], [self.raw_comment(2)]],
            [[self.raw_comment(3, body="<!-- worklane:1:agent review -->\nPass"),
              self.raw_comment(4, user={"login": "bot[bot]", "type": "Bot"}),
              self.raw_comment(5, user=None)]],
            [[self.raw_comment(6, state="APPROVED"), self.raw_comment(7, state="CHANGES_REQUESTED", body=""),
              self.raw_comment(8, state="COMMENTED"), self.raw_comment(9, state="PENDING")]],
            [[self.raw_comment(10, path="code.py", line=42)]],
        ]
        with patch.object(w, "gh", side_effect=pages) as api:
            feedback = self.runner.human_feedback(self.task)
        self.assertEqual(set(feedback), {"issue:1", "issue:2", "review:7", "review:8", "inline:10"})
        self.assertEqual(feedback["inline:10"]["line"], 42)
        for call in api.call_args_list:
            self.assertIn("--paginate", call.args)
            self.assertIn("--slurp", call.args)

    def test_closed_changed_or_retargeted_pr_is_not_reworked(self):
        for change in ({"state": "MERGED"}, {"headRefOid": "b" * 40}, {"baseRefName": "other"}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.runner.pr.return_value)
                self.runner.pr.return_value.update(change)
                with patch.object(w, "gh") as api:
                    with self.assertRaisesRegex(RuntimeError, "inspect before rework"):
                        self.runner.human_feedback(self.task)
                api.assert_not_called()
                self.runner.pr.return_value = original

    def test_handoff_preserves_unconsumed_followups(self):
        self.task.update(phase="reviewing", round=1, item=item(1, w.REVIEW))
        self.board.queue = [item(1, w.REVIEW)]
        self.runner.backend.run.side_effect = [result(), result()]
        self.runner.verify_pr = Mock()
        self.runner.worktree = Mock(return_value=self.root)
        self.runner.post = Mock()
        self.runner.pr.return_value["isDraft"] = False
        with patch.object(w, "command", side_effect=["a" * 40, ""]), \
                patch.object(self.runner, "human_feedback") as feedback:
            self.runner.review(self.task)
        feedback.assert_not_called()
        self.assertEqual(self.task["human_feedback_seen"], {})
        self.assertEqual(self.board.moves, [(1, w.HUMAN)])

    def test_question_handoff_comments_are_unique_across_human_batches(self):
        task = {**self.task, "pr_url": "", "head_sha": "", "human_feedback_generation": 2}
        with patch.object(w, "gh", return_value={"comments": []}), patch.object(w, "command") as command:
            self.runner.post(task, "implementation complete", result())
        self.assertIn(":human-2 -->", command.call_args.kwargs["stdin"])


if __name__ == "__main__":
    unittest.main()
