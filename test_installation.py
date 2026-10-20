from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import worklane as w


@unittest.skipUnless(os.environ.get("WORKLANE_TEST_INSTALL") == "1",
                     "Set WORKLANE_TEST_INSTALL=1 to exercise isolated uv tool installs")
class PreviewInstallTests(unittest.TestCase):
    def test_editable_branch_refresh_pinned_commit_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "integration checkout"
            checkout.mkdir()
            project = Path(w.__file__).resolve().parent
            for name in ("worklane.py", "worklane_harness.py", "worklane_tui.py", "pyproject.toml"):
                shutil.copyfile(project / name, checkout / name)
            env = dict(os.environ, UV_TOOL_DIR=str(root / "tools"),
                       UV_TOOL_BIN_DIR=str(root / "bin"), XDG_STATE_HOME=str(root / "state"))

            def run(*args, cwd=root):
                completed = subprocess.run(args, cwd=cwd, env=env, text=True,
                                           capture_output=True, timeout=120)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            def git(*args):
                return run("git", *args, cwd=checkout)

            def commit(message):
                git("add", ".")
                git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", message)
                return git("rev-parse", "HEAD")

            def installed():
                # Run outside the checkout, through the actual installed entry point.
                return json.loads(run(str(root / "bin" / "worklane"), "--version"))

            git("init", "-q", "-b", "integration")
            first = commit("First preview")
            run("uv", "tool", "install", "--force", "--editable", str(checkout))
            info = installed()
            self.assertEqual(info["module"], str(checkout / "worklane.py"))
            self.assertEqual(info["revision"], first)
            self.assertEqual(info["branch"], "integration")
            self.assertFalse(info["dirty"])
            with (checkout / "worklane.py").open("a") as source:
                source.write("\n# Updated preview\n")
            self.assertTrue(installed()["dirty"])
            second = commit("Second preview")
            requirement = "git+" + checkout.as_uri()
            run("uv", "tool", "install", "--force", "--refresh", requirement + "@integration")
            self.assertEqual(installed()["revision"], second)
            self.assertIsNone(installed()["branch"])
            self.assertIsNone(installed()["dirty"])
            with (checkout / "worklane.py").open("a") as source:
                source.write("\n# New branch head\n")
            third = commit("Third preview")
            run("uv", "tool", "install", "--force", "--refresh", requirement + "@integration")
            self.assertEqual(installed()["revision"], third)
            run("uv", "tool", "install", "--force", "--refresh", requirement + "@" + first)
            self.assertEqual(installed()["revision"], first)
            run("uv", "tool", "install", "--force", "--editable", str(checkout))
            self.assertEqual(installed()["module"], str(checkout / "worklane.py"))
            self.assertEqual(installed()["revision"], third)
            self.assertFalse((root / "state").exists(), "Version probes must not create controller state")


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "worklane.py"
        self.source.write_text("# test CLI\n")
        source_patch = patch.object(w, "__file__", str(self.source))
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.dist = Mock(version="0.1.0")
        self.dist.read_text.return_value = None
        dist_patch = patch.object(w.metadata, "distribution", return_value=self.dist)
        dist_patch.start()
        self.addCleanup(dist_patch.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
                              capture_output=True, check=True).stdout.strip()

    def checkout(self):
        self.git("init", "-q", "-b", "integration")
        self.git("add", "worklane.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-qm", "Preview CLI")
        return self.git("rev-parse", "HEAD")

    def test_local_branch_dirty_edits_and_detached_revision(self):
        revision = self.checkout()
        info = w.installation_info()
        self.assertEqual(info, {"version": "0.1.0", "module": str(self.source),
                                "revision": revision, "branch": "integration", "dirty": False})
        self.source.write_text("# locally edited CLI\n")
        self.assertTrue(w.installation_info()["dirty"])
        self.git("checkout", "--detach", "-q")
        info = w.installation_info()
        self.assertEqual(info["revision"], revision)
        self.assertIsNone(info["branch"])

    def test_vcs_install_uses_resolved_commit_without_git(self):
        self.dist.read_text.return_value = json.dumps({
            "vcs_info": {"vcs": "git", "requested_revision": "integration", "commit_id": "a" * 40}})
        with patch.object(w, "command", side_effect=FileNotFoundError("git")):
            info = w.installation_info()
        self.assertEqual(info["revision"], "a" * 40)
        self.assertIsNone(info["branch"])
        self.assertIsNone(info["dirty"])

    def test_plain_wheel_and_missing_distribution_have_unknown_revision(self):
        self.assertIsNone(w.installation_info()["revision"])
        with patch.object(w.metadata, "distribution", side_effect=w.metadata.PackageNotFoundError):
            info = w.installation_info()
        self.assertEqual(info["version"], "uninstalled")
        self.assertIsNone(info["revision"])

    def test_untracked_script_does_not_inherit_enclosing_git_revision(self):
        self.checkout()
        nested = self.root / "unrelated"
        nested.mkdir()
        script = nested / "worklane.py"
        script.write_text("# unrelated script\n")
        with patch.object(w, "__file__", str(script)):
            self.assertIsNone(w.installation_info()["revision"])

    def test_version_does_not_open_target_repo_state_or_network(self):
        output = io.StringIO()
        with patch.object(w, "repository", side_effect=AssertionError("repository lookup")), \
                patch.object(w, "State", side_effect=AssertionError("state access")), \
                patch.object(w, "gh", side_effect=AssertionError("network access")), \
                redirect_stdout(output):
            self.assertEqual(w.main(["--version", "--repo", "/missing/project"]), 0)
        self.assertEqual(json.loads(output.getvalue())["module"], str(self.source))

    def test_check_reports_cli_without_changing_board(self):
        board = Mock(project={"url": "https://github.com/users/owner/projects/1"}, options={})
        board.items.return_value = []
        output = io.StringIO()
        with patch.object(w, "repository", return_value=("owner/repo", self.root, "main")), \
                patch.object(w, "State"), patch.object(w, "Board", return_value=board), \
                patch.object(w.shutil, "which", return_value="/mock/binary"), \
                redirect_stdout(output):
            self.assertEqual(w.main(["check"]), 0)
        self.assertEqual(json.loads(output.getvalue())["cli"]["module"], str(self.source))
        board.ensure_review_status.assert_not_called()
        board.move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
