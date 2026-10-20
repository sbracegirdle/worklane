"""Dependency gates use real dispatch/state paths without launching agents."""
import argparse
import copy
from contextlib import redirect_stdout
import io
import json
import curses
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import worklane as w
import worklane_tui as tui
from test_worklane import FakeBoard, item
from test_tui import Screen


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = w.State('owner/repo', self.root)
        self.board = FakeBoard([item(1), item(2)])
        self.runner = w.Runner(argparse.Namespace(base='main', once=True, interval=60),
                               'owner/repo', self.root, self.state, self.board, Mock())
        self.runner.issue = Mock(return_value={'state': 'OPEN', 'labels': [], 'title': 'Issue'})
        self.runner.execute = Mock()

    def real_board(self):
        board = object.__new__(w.Board)
        board.repo = 'owner/repo'
        board.require_safe = Mock()
        return board

    def response(self, count):
        return {'repository': {'issue': {'issueDependenciesSummary': {'blockedBy': count}}}}

    def test_summary_counts_only_open_blockers_and_has_no_pagination_limit(self):
        for count in (0, 1, 101):
            with self.subTest(count=count):
                self.assertEqual(w.dependency_count({'issueDependenciesSummary':
                    {'blockedBy': count, 'totalBlockedBy': 500}}), count)

    def test_missing_or_malformed_summary_fails_closed(self):
        for summary in (None, {}, {'blockedBy': None}, {'blockedBy': -1}, {'blockedBy': False}):
            with self.subTest(summary=summary), self.assertRaisesRegex(RuntimeError, 'dependency status'):
                w.dependency_count({'issueDependenciesSummary': summary})

    def test_known_blockers_do_not_make_per_slot_requests(self):
        with patch.object(w, 'graphql') as api:
            self.assertFalse(self.real_board().dependencies_ready(dict(item(1), blocked_by_count=1)))
        api.assert_not_called()

    def test_fresh_blocker_overrides_unblocked_snapshot_before_claim(self):
        board = self.real_board()
        board.move = Mock()
        self.runner.board = board
        selected = dict(item(1), blocked_by_count=0)
        with patch.object(w, 'graphql', return_value=self.response(1)) as api:
            self.assertIsNone(self.runner.pick(selected, w.WORKERS[0]))
        self.assertEqual(selected['blocked_by_count'], 1)
        self.assertEqual(api.call_args.args[1], {'owner': 'owner', 'name': 'repo', 'number': 1})
        board.move.assert_not_called()
        self.runner.issue.assert_not_called()
        self.assertEqual(self.state.all(), [])

    def test_dependency_api_failure_or_hidden_issue_cannot_claim(self):
        self.runner.board = self.real_board()
        for response in ({'repository': None}, {'repository': {'issue': None}}, self.response(None)):
            with self.subTest(response=response), patch.object(w, 'graphql', return_value=response):
                with self.assertRaisesRegex(RuntimeError, 'dependency status'):
                    self.runner.pick(item(1), w.WORKERS[0])
        with patch.object(w, 'graphql', side_effect=w.GitHubRateLimit('exhausted')):
            with self.assertRaises(w.GitHubRateLimit):
                self.runner.pick(item(1), w.WORKERS[0])
        self.assertEqual(self.state.all(), [])

    def test_blocked_intake_and_rework_leave_slots_for_independent_work(self):
        for other, phase in enumerate((None, 'rework'), 2):
            with self.subTest(phase=phase):
                if phase:
                    self.state.save({'number': 1, 'phase': phase})
                self.board.queue = [dict(item(1), blocked_by_count=1), item(other)]
                self.runner.run(threading.Event())
                self.assertEqual(self.runner.execute.call_args.args[0]['number'], other)
                self.assertEqual(self.board.moves, [(other, w.PROGRESS)])
                self.assertEqual(self.state.load(1).get('phase'), phase)
                self.board.moves.clear()
                self.state.save({'number': other, 'phase': 'done'})

    def test_closing_or_removing_final_blocker_allows_next_poll_without_retry(self):
        self.board.queue = [dict(item(1), blocked_by_count=1)]
        self.runner.run(threading.Event())
        self.runner.execute.assert_not_called()
        self.assertEqual(self.state.all(), [])
        self.board.queue[0]['blocked_by_count'] = 0
        self.runner.run(threading.Event())
        self.runner.execute.assert_called_once()
        self.assertEqual(self.state.load(1)['phase'], 'building')

    def test_interrupted_and_retry_stages_remain_untouched_while_blocked(self):
        for phase in ('interrupted', 'retry'):
            for stage in ('build', 'review', 'finalize'):
                with self.subTest(phase=phase, stage=stage):
                    selected = dict(item(1, w.READY if stage == 'build' else w.REVIEW), blocked_by_count=1)
                    task = {'number': 1, 'phase': phase, 'resume_stage': stage, 'item': selected}
                    self.state.save(task)
                    before = copy.deepcopy(self.state.load(1))
                    self.assertIsNone(self.runner.recover([selected], w.WORKERS[0] if stage == 'build' else w.REVIEWER))
                    self.assertEqual(self.state.load(1), before)
        self.assertEqual(self.board.moves, [])
        self.runner.issue.assert_not_called()

    def test_blocked_review_is_not_assigned(self):
        self.assertIsNone(self.runner.pick(dict(item(1, w.REVIEW), blocked_by_count=1), w.REVIEWER))
        self.assertEqual(self.state.all(), [])

    def test_check_distinguishes_ready_from_dependency_blocked(self):
        self.board.project = {"url": "board-url"}
        self.board.options = {w.REVIEW.casefold(): "review-id"}
        self.board.queue = [dict(item(1), blocked_by_count=2), dict(item(2), blocked_by_count=0)]
        output = io.StringIO()
        with patch.object(w, "repository", return_value=('owner/repo', self.root, 'main')), \
                patch.object(w, "State", return_value=self.state), \
                patch.object(w, "Board", return_value=self.board), \
                patch.object(w.shutil, "which", return_value='/bin/fake'), redirect_stdout(output):
            self.assertEqual(w.main(['check']), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data['ready'], [2])
        self.assertEqual(data['dependency_blocked'], {'1': 2})
        self.assertEqual(self.state.all(), [])

    def test_tui_board_explains_blocked_ready_issue(self):
        selected = dict(item(1), title='Waiting for prerequisite', blocked_by_count=2)
        dashboard = tui.Dashboard(curses, self.state, 'owner/repo', .1, root=self.root)
        dashboard.page = {'kind': 'board', 'value': {'items': [selected]}, 'index': 0}
        screen = Screen()
        dashboard.draw_page(screen, 20)
        self.assertIn('Ready for work (blocked by 2)', '\n'.join(screen.lines.values()))


if __name__ == '__main__':
    unittest.main()
