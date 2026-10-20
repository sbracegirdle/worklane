"""Configuration and setup tests never launch real workers or change GitHub."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import curses
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import worklane as w
import worklane_tui as tui
from test_tui import Screen
from test_worklane import FakeBoard, item


class SetupScreen(Screen):
    def get_wch(self):
        return next(self.keys)  # Fail rather than hang if setup consumes extra input.


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / w.CONFIG_FILE
        self.state = w.State("owner/repo", self.root / "state")

    def test_defaults_are_read_only_and_config_is_per_checkout(self):
        self.assertEqual(w.load_config(self.root), w.CONFIG_DEFAULTS)
        self.assertFalse(self.path.exists())
        w.save_config(self.root, {"workers": 2, "model": 'model-"special"', "project": "owner/7"})
        self.assertEqual(w.load_config(self.root)["workers"], 2)
        self.assertEqual(w.load_config(self.root)["model"], 'model-"special"')
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(w.load_config(other), w.CONFIG_DEFAULTS)
        self.assertEqual(self.state.get_meta("config"), None)

    def test_invalid_config_has_path_and_never_silently_uses_defaults(self):
        invalid = ['workers = true', 'workers = 0', 'workers = 17', 'workers = "2"',
                   'interval = 9', 'interval = 10.5', 'interval = false',
                   'model = "a b"', 'model = 3', 'harness = "other"',
                   'effort = "automatic"', 'project = "bad"', 'project = "owner/0"',
                   'project = []', 'unknown = 1', '[nested]\nworkers = 2', 'workers = [']
        for source in invalid:
            with self.subTest(source=source):
                self.path.write_text(source)
                with self.assertRaisesRegex(ValueError, str(self.path)):
                    w.load_config(self.root)
                with self.assertRaises(ValueError):
                    w.save_config(self.root, {"workers": False})
                self.assertEqual(self.path.read_text(), source)

    def test_atomic_save_failure_preserves_previous_file_and_cleans_temporary(self):
        w.save_config(self.root, {"workers": 2})
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                w.save_config(self.root, {"workers": 4})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".worklane-*")), [])

    def cli(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(w, "repository", return_value=("owner/repo", self.root, "main")), \
             patch.object(w, "State", return_value=self.state), \
             patch.object(w.shutil, "which", return_value="/bin/mock"), \
             patch.object(w, "gh", side_effect=AssertionError("unexpected network")), \
             redirect_stdout(output), redirect_stderr(errors):
            code = w.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_check_uses_saved_settings_and_explicit_flags_override_without_saving(self):
        w.save_config(self.root, {"workers": 2, "model": "saved-model", "effort": "medium",
                                 "project": "owner/7", "interval": 20})
        before = self.path.read_bytes()
        with patch.object(w, "Board") as board:
            board.return_value.project = {"url": "board-url"}
            board.return_value.options = {}
            board.return_value.security = {"safe": True}
            board.return_value.items.return_value = []
            code, output, errors = self.cli(["check"])
            self.assertEqual((code, errors), (0, ""))
            data = json.loads(output)
            self.assertEqual((data["workers"], data["model"], data["effort"]), (2, "saved-model", "medium"))
            board.assert_called_with("owner/repo", self.root, "owner/7",
                                     dangerously_allow_public_board=False, check_only=True)
            code, output, errors = self.cli(["check", "--workers", "5", "--model", "override",
                                            "--effort", "low", "--project", "another/8"])
            self.assertEqual((code, errors), (0, ""))
            data = json.loads(output)
            self.assertEqual((data["workers"], data["model"], data["effort"]), (5, "override", "low"))
            board.assert_called_with("owner/repo", self.root, "another/8",
                                     dangerously_allow_public_board=False, check_only=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_settings_fail_before_board_access_but_controls_still_work(self):
        with patch.object(w, "Board", side_effect=AssertionError("board used")):
            for argv in (["check", "--workers", "0"], ["start", "--interval", "9"]):
                self.assertEqual(self.cli(argv)[0], 1)
            self.path.write_text("not valid toml")
            self.assertEqual(self.cli(["check"])[0], 1)
            for action in ("status", "pause", "stop", "logs"):
                self.assertEqual(self.cli([action])[0], 0)

    def test_detached_start_passes_all_effective_settings_to_child(self):
        w.save_config(self.root, {"workers": 5, "model": "saved-model", "effort": "low", "interval": 15})
        child = Mock(pid=123)
        child.poll.return_value = None
        def launch(*args, **kwargs):
            self.state.set_meta("runtime", {"pid": child.pid})
            return child
        with patch.object(w, "Board") as board, patch.object(w.subprocess, "Popen", side_effect=launch) as popen:
            board.return_value.owner, board.return_value.number = "owner", 7
            self.assertEqual(self.cli(["start"])[0], 0)
        argv = popen.call_args.args[0]
        for name, value in {"workers": "5", "model": "saved-model", "harness": "codex",
                            "effort": "low", "interval": "15", "project": "owner/7"}.items():
            self.assertEqual(argv[argv.index("--" + name) + 1], value)

    def test_config_command_is_offline_and_creates_no_runtime_state(self):
        with patch.object(w, "State", side_effect=AssertionError("state used")), \
             patch.object(w, "repository", return_value=("owner/repo", self.root, "main")), \
             patch.object(w, "gh", side_effect=AssertionError("network used")), \
             patch.object(tui, "run_config") as setup:
            self.assertEqual(w.main(["config"]), 0)
        setup.assert_called_once_with(self.root, no_color=False,
                                      options={"project": None, "model": None, "interval": None, "once": None})

    def test_walkthrough_validates_each_answer_and_saves_only_at_confirmation(self):
        keys = ["\n", "\x15", "0", "\n", "\x15", "2", "\n",
                "\x15", *"custom-model", "\n", "\x15", *"medium", "\n",
                "\x15", *"20", "\n", *"owner/7", "\n", *["\n"] * 7]
        screen = SetupScreen(keys)
        with patch.object(curses, "curs_set"):
            self.assertTrue(tui.config_walkthrough(curses, screen, self.root))
        settings = w.load_config(self.root)
        self.assertEqual(settings, {"workers": 2, "model": "custom-model", "effort": "medium",
                                    "interval": 20, "project": "owner/7", "harness": "codex",
                                    **{f"{role}_{option}": "" for role in w.ROLES.values() for option in ("harness", "model")}})
        self.assertTrue(any("between 1 and 16" in frame for frame in screen.frames))
        for index in range(1, 13):
            self.assertTrue(any(f"{index}/12" in frame for frame in screen.frames))
        self.assertIn("Save these settings?", screen.frames[-1])

    def test_cancellation_at_every_step_leaves_existing_and_missing_files_unchanged(self):
        for exists in (False, True):
            if exists:
                w.save_config(self.root, {"workers": 4})
            before = self.path.read_bytes() if exists else None
            for step in range(13):
                with patch.object(curses, "curs_set"):
                    self.assertFalse(tui.config_walkthrough(curses, SetupScreen(["\n"] * step + ["\x1b"]), self.root))
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_opencode_setup_role_overrides_and_small_confirmation_are_reviewable(self):
        keys = ["\x15", *"opencode", "\n", *["\n"] * 7,
                *"codex", "\n", *"review-model", "\n", "\n", "\n",
                curses.KEY_NPAGE, curses.KEY_NPAGE, "\n"]
        screen = SetupScreen(keys, size=(12, 50))
        with patch.object(curses, "curs_set"):
            self.assertTrue(tui.config_walkthrough(curses, screen, self.root))
        settings = w.load_config(self.root)
        self.assertEqual(settings["harness"], "opencode")
        self.assertEqual(settings["model"], "")
        self.assertEqual(settings["reviewer_harness"], "codex")
        self.assertEqual(settings["reviewer_model"], "review-model")
        self.assertTrue(any("finalizer_model:" in frame for frame in screen.frames))
        self.assertTrue(any("reviewer_model: review-model" in frame for frame in screen.frames))

    def test_saved_role_settings_cli_clears_and_background_start_roundtrip(self):
        w.save_config(self.root, {"harness": "opencode", "model": "provider/default",
                                 "reviewer_harness": "codex", "reviewer_model": "saved-review"})
        child = Mock(pid=123, poll=Mock(return_value=None))
        self.state.set_meta("runtime", {"pid": child.pid})
        flags = ["--model", "", "--reviewer-harness", "", "--reviewer-model", ""]
        with patch.object(w, "Board") as board, patch.object(w.subprocess, "Popen", return_value=child) as popen:
            board.return_value.owner, board.return_value.number = "owner", 7
            self.assertEqual(self.cli(["start", *flags])[0], 0)
        argv = popen.call_args.args[0]
        for flag in flags[::2]:
            self.assertEqual(argv[argv.index(flag) + 1], "")
        forwarded = w.parser().parse_args(argv[2:])
        backend = w.HarnessBackend(forwarded, self.root, self.state, 0)
        self.assertEqual(backend.selection("reviewer"), ("opencode", ""))
        self.assertEqual(w.load_config(self.root)["reviewer_harness"], "codex")

    def test_tui_start_forwards_saved_role_settings_and_capacity(self):
        w.save_config(self.root, {"harness": "opencode", "workers": 5,
                                 "reviewer_harness": "codex", "reviewer_model": "review-model"})
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1, root=self.root)
        with patch.object(w, "command", return_value="Started") as command:
            tui.controller_action("start", self.root, dashboard.settings())
        options = w.parser().parse_args(command.call_args.args[0][2:])
        self.assertEqual((options.harness, options.workers, options.reviewer_harness, options.reviewer_model),
                         ("opencode", 5, "codex", "review-model"))

    def test_tui_prompts_once_per_folder_and_config_key_reopens_walkthrough(self):
        keys = ["\n"] * 13 + ["c", "\x1b", "q"]
        screen = SetupScreen(keys)
        with patch.object(tui.sys.stdin, "isatty", return_value=True), \
             patch.object(tui.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False):
            tui.run_tui(self.state, "owner/repo", root=self.root)
        self.assertTrue(self.path.exists())
        self.assertEqual(sum("1/12  Which agent harness?" in frame for frame in screen.frames), 2)
        self.assertTrue(any("Setup cancelled" in frame for frame in screen.frames))
        with patch.object(tui.sys.stdin, "isatty", return_value=True), \
             patch.object(tui.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(SetupScreen(["q"]))), \
             patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(tui, "config_walkthrough", side_effect=AssertionError("setup repeated")):
            tui.run_tui(self.state, "owner/repo", root=self.root)

    def test_saved_settings_override_stale_runtime_and_reach_tui_start_and_capacity(self):
        self.state.set_meta("runtime", {"workers": 3, "model": "old"})
        w.save_config(self.root, {"workers": 2, "model": "new"})
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1, root=self.root, options={"effort": "low"})
        self.assertEqual(dashboard.settings()["model"], "new")
        with patch.object(w, "command", return_value="Started") as command:
            tui.controller_action("start", self.root, dashboard.settings())
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index("--workers") + 1], "2")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        screen = Screen()
        dashboard.draw(screen, tui.snapshot(self.state))
        self.assertIn("0/2", screen.frames[-1])
        with w.locked(self.state.path / "runner.lock"):
            dashboard.draw(screen, tui.snapshot(self.state))
        self.assertIn("0/3", screen.frames[-1])

    def test_config_and_issue_editor_shortcuts_coexist_after_returning_from_setup(self):
        w.save_config(self.root, {"workers": 2})
        task = {"number": 1, "phase": "building", "issue": {"title": "Configured issue"}}
        run = self.state.begin_run(task, "build", self.root)
        self.state.finish_run(run, "completed")
        dashboard = tui.Dashboard(curses, self.state, "owner/repo", 1, root=self.root)
        screen = SetupScreen(["c", "\x1b", "n", "e", "q"])
        with patch.object(curses, "curs_set"), patch.object(curses, "has_colors", return_value=False), \
             patch.object(dashboard, "open_editor") as editor:
            dashboard.run(screen)
        self.assertEqual([call.args for call in editor.call_args_list], [(None,), (1,)])
        for size in ((12, 70), (28, 110)):
            screen = Screen(size=size)
            dashboard.draw(screen, tui.snapshot(self.state))
            for control in ("c config", "n new", "e edit", "q quit"):
                self.assertIn(control, screen.frames[-1])
        self.assertEqual(w.load_config(self.root)["workers"], 2)

    def test_dispatch_uses_configured_workers_and_keeps_one_reviewer(self):
        for count in (1, 5):
            with self.subTest(count=count):
                args = argparse.Namespace(workers=count, once=True, interval=60, base="main")
                board = FakeBoard([item(n) for n in range(1, 8)] + [item(9, w.REVIEW)])
                runner = w.Runner(args, "owner/repo", self.root, self.state, board, Mock())
                runner.reconcile_completed = runner.reconcile_waiting = runner.poll_human_feedback = Mock()
                runner.recover = Mock(return_value=None)
                runner.pick = Mock(side_effect=lambda selected, slot: {"number": selected["content"]["number"], "issue": {"title": "Issue"}})
                runner.execute = Mock()
                runner.run(threading.Event())
                self.assertEqual(runner.execute.call_count, count + 1)
                self.assertEqual(sum(call.args[1] for call in runner.execute.call_args_list), 1)
                self.assertEqual([call.args[1] for call in runner.pick.call_args_list],
                                 [f"worker-{i + 1}" for i in range(count)] + [w.REVIEWER])


if __name__ == "__main__":
    unittest.main()
