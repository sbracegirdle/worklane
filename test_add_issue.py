import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import worklane as w
from test_worklane import safe_security


class AddIssueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(w.Board, "security_report", side_effect=safe_security))
        self.url = "https://github.com/owner/repo/issues/42"
        self.project = {"id": "project-id", "number": 2, "title": "repo", "closed": False,
                        "url": "https://github.com/users/owner/projects/2", "owner": {"login": "owner"}}
        self.status = w.READY
        self.fields = [{"id": "status-id", "name": "Status", "options": [
            {"id": "ready-id", "name": w.READY}, {"id": "draft-id", "name": "Draft"},
            {"id": "shaping-id", "name": "Shaping"}]}]
        self.add_error = self.edit_error = self.create_error = None
        self.items = [{"id": "item-id", "content": {"type": "Issue", "repository": "owner/repo"}}]
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.command = self.enterContext(patch.object(w, "command", side_effect=self.run_command))
        self.gh = self.enterContext(patch.object(w, "gh", side_effect=self.run_gh))
        self.graphql = self.enterContext(patch.object(w, "graphql", return_value={
            "repository": {"projectsV2": {"nodes": [self.project]}}}))
        self.enterContext(patch.object(w.Board, "items", side_effect=lambda: [dict(i, status=self.status) for i in self.items]))
        self.repository = self.enterContext(patch.object(w, "repository", return_value=("owner/repo", Path("/checkout"), None)))
        self.enterContext(patch.object(w.shutil, "which", return_value="/bin/gh"))
        self.enterContext(patch.object(w, "State", side_effect=AssertionError("add must not access controller state")))
        self.enterContext(patch.object(w, "CodexBackend", side_effect=AssertionError("add must not launch agents")))
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.errors))

    def run_command(self, args, **kwargs):
        if args[:3] == ["gh", "issue", "create"]:
            if self.create_error:
                raise self.create_error
            return self.url
        if args[:3] == ["gh", "project", "item-edit"]:
            if self.edit_error:
                raise self.edit_error
            return ""
        self.fail(f"Unexpected command: {args}")

    def run_gh(self, *args, **kwargs):
        if args[:2] == ("project", "view"):
            return self.project
        if args[:2] == ("project", "field-list"):
            return {"fields": self.fields}
        if args[:2] == ("project", "item-add"):
            if self.add_error:
                raise self.add_error
            return {"id": "item-id"}
        if args[:2] == ("project", "item-list"):
            return {"items": [dict(i, status=self.status) for i in self.items], "totalCount": len(self.items)}
        self.fail(f"Unexpected gh call: {args}")

    def test_default_creates_issue_with_literal_multiline_description_and_ready_status(self):
        title = 'Fix "quotes", `backticks` and $(expressions)'
        description = "First line\n\n- Details with $HOME and `code`\n"
        self.assertEqual(w.main(["add", "--title", title, "--description", description]), 0)
        self.command.assert_any_call(["gh", "issue", "create", "--repo", "owner/repo",
                                      "--title", title, "--body-file", "-"], stdin=description)
        self.gh.assert_any_call("project", "item-add", "2", "--owner", "owner", "--url", self.url, "--format", "json")
        self.command.assert_any_call(["gh", "project", "item-edit", "--id", "item-id", "--project-id", "project-id",
                                      "--field-id", "status-id", "--single-select-option-id", "ready-id"])
        self.assertEqual(self.command.call_count, 2)
        self.assertIn(self.url, self.output.getvalue())
        self.assertIn(w.READY, self.output.getvalue())
        self.assertEqual(self.errors.getvalue(), "")

    def test_modifiers_select_existing_status_case_insensitively(self):
        for modifier, status, option in (("--draft", "dRaFt", "draft-id"), ("--shaping", "SHAPING", "shaping-id")):
            with self.subTest(modifier=modifier):
                self.status = status
                self.fields[0]["options"] = [{"id": option, "name": status}]
                self.assertEqual(w.main(["add", "-t", "New issue", modifier]), 0)
                self.assertEqual(self.command.call_args.args[0][-1], option)
                # Draft and Shaping remain repository issues, not project draft items.
                self.assertEqual(self.command.call_args_list[-2].args[0][:3], ["gh", "issue", "create"])
                self.assertEqual(self.command.call_args_list[-2].kwargs["stdin"], "")

    def test_concurrent_pickup_of_ready_issue_succeeds_without_resetting_status(self):
        for status in (w.PROGRESS, w.REVIEW, w.HUMAN, w.PROGRESS.swapcase()):
            with self.subTest(status=status):
                self.command.reset_mock()
                self.status = status
                self.assertEqual(w.main(["add", "-t", "Concurrent pickup"]), 0)
                self.assertEqual(self.errors.getvalue(), "")
                self.assertIn(self.url, self.output.getvalue())
                # The initial Ready placement is the only status write; never
                # rewind a controller that has already picked up the issue.
                edits = [c.args[0] for c in self.command.call_args_list
                         if c.args[0][:3] == ["gh", "project", "item-edit"]]
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0][-1], "ready-id")

    def test_workflow_progress_does_not_verify_draft_or_shaping_placement(self):
        self.status = w.PROGRESS
        for modifier in ("--draft", "--shaping"):
            with self.subTest(modifier=modifier):
                self.assertEqual(w.main(["add", "-t", "Unverified placement", modifier]), 1)
                self.assertIn("Could not verify board status", self.errors.getvalue())

    def test_repo_and_explicit_project_are_honored(self):
        self.assertEqual(w.main(["add", "-t", "Title", "--repo", "/other-checkout", "--project", "owner/2", "--body", "Body"]), 0)
        self.repository.assert_called_once_with(Path("/other-checkout"))
        self.gh.assert_any_call("project", "view", "2", "--owner", "owner", "--format", "json")
        self.graphql.assert_not_called()

    def test_invalid_add_input_never_reaches_github(self):
        for args in (["add"], ["add", "-t", "   "], ["add", "42", "-t", "Title"],
                     ["status", "--draft"], ["check", "--description", "Body"], ["run", "-t", "Title"]):
            with self.subTest(args=args):
                self.assertEqual(w.main(args), 1)
        self.repository.assert_not_called()
        self.command.assert_not_called()
        self.gh.assert_not_called()

    def test_draft_and_shaping_cannot_be_combined(self):
        with self.assertRaises(SystemExit) as raised:
            w.main(["add", "-t", "Title", "--draft", "--shaping"])
        self.assertEqual(raised.exception.code, 2)
        self.command.assert_not_called()

    def test_missing_target_status_never_creates_issue_or_edits_fields(self):
        self.fields[0]["options"] = [{"id": "ready-id", "name": w.READY}]
        self.assertEqual(w.main(["add", "-t", "Title", "--shaping"]), 1)
        self.assertIn("missing status 'Shaping'", self.errors.getvalue())
        self.command.assert_not_called()

    def test_missing_status_field_never_creates_issue(self):
        self.fields = []
        self.assertEqual(w.main(["add", "-t", "Title"]), 1)
        self.command.assert_not_called()

    def test_ambiguous_board_never_creates_issue(self):
        self.graphql.return_value["repository"]["projectsV2"]["nodes"].append(dict(self.project, number=3))
        self.assertEqual(w.main(["add", "-t", "Title"]), 1)
        self.assertIn("Choose a board", self.errors.getvalue())
        self.command.assert_not_called()

    def test_missing_gh_does_not_create_issue(self):
        with patch.object(w.shutil, "which", return_value=None):
            self.assertEqual(w.main(["add", "-t", "Title"]), 1)
        self.assertIn("Required command missing: gh", self.errors.getvalue())
        self.command.assert_not_called()
        self.gh.assert_not_called()

    def test_issue_creation_failure_does_not_add_board_item(self):
        self.create_error = RuntimeError("permission denied")
        self.assertEqual(w.main(["add", "-t", "Title"]), 1)
        self.assertEqual(self.command.call_count, 1)
        self.assertFalse(any(c.args[:2] == ("project", "item-add") for c in self.gh.call_args_list))
        self.assertNotIn("Issue created:", self.errors.getvalue())

    def test_partial_failures_retain_issue_url_and_warn_against_duplicate_creation(self):
        for stage in ("attach", "edit", "verify"):
            with self.subTest(stage=stage):
                self.command.reset_mock()
                self.errors.truncate(0)
                self.errors.seek(0)
                self.add_error = RuntimeError("add denied") if stage == "attach" else None
                self.edit_error = RuntimeError("edit denied") if stage == "edit" else None
                self.status = "Draft" if stage == "verify" else w.READY
                self.assertEqual(w.main(["add", "-t", "Title"]), 1)
                self.assertIn(self.url, self.errors.getvalue())
                self.assertIn("do not rerun add", self.errors.getvalue())
                self.assertEqual(sum(c.args[0][:3] == ["gh", "issue", "create"] for c in self.command.call_args_list), 1)

    def test_missing_item_cannot_report_success(self):
        self.items = []
        self.assertEqual(w.main(["add", "-t", "Title"]), 1)
        self.assertIn("Could not verify board status", self.errors.getvalue())
        self.assertEqual(self.output.getvalue(), "")

    def test_tui_default_preserves_explicit_commands_and_issue_numbers(self):
        self.assertEqual(w.parser().parse_args([]).action, "tui")
        self.assertEqual(w.parser().parse_args(["run"]).action, "run")
        for action in ("logs", "retry"):
            self.assertEqual(w.parser().parse_args([action, "42"]).number, 42)


if __name__ == "__main__":
    unittest.main()
