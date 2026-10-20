"""Harness adapters: commands, wire events, results and session metadata."""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid

from worklane_process import identity, terminate_owned

MODEL = "gpt-6.1-sol"


def validate_session_id(backend, value):
    if backend == "codex":
        return str(uuid.UUID(value))
    if backend == "opencode" and isinstance(value, str) and re.fullmatch(r"ses_[A-Za-z0-9_-]+", value):
        return value
    raise ValueError(f"Invalid {backend} session ID.")


def session_name(repo, number, role, task):
    issue = task.get("issue", {})
    content = task.get("item", {}).get("content", {})
    title = " ".join((issue.get("title") or content.get("title") or "Issue").split())[:120]
    return f"{repo} #{number} [{role}]: {title}"


def terminate(process):
    if process.poll() is not None:
        return
    terminate_owned([(process.pid, getattr(process, "worklane_identity", None) or identity(process.pid))])
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class CodexAppServer:
    """A bounded metadata-only JSON-RPC connection; never starts agent turns."""

    def __init__(self, cwd, timeout=15):
        self.cwd, self.timeout = cwd, timeout
        self.process = None

    def __enter__(self):
        self.deadline = time.monotonic() + self.timeout
        self.pending, self.request_id = b"", 0
        self.process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"], cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.process.worklane_identity = identity(self.process.pid)
        try:
            self.request("initialize", {
                "clientInfo": {"name": "worklane", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        self.send({"id": request_id, "method": method, "params": params})
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Codex metadata request timed out: {method}")
                if b"\n" not in self.pending:
                    if not selector.select(remaining):
                        continue
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError(f"Codex app-server closed during {method}")
                    self.pending += chunk
                    if len(self.pending) > 4 * 1024 * 1024:
                        raise RuntimeError("Codex metadata response exceeded 4 MiB")
                    continue
                line, self.pending = self.pending.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue  # Notifications may precede the matching response.
                if "error" in message:
                    raise RuntimeError(f"Codex {method}: {message['error'].get('message', message['error'])}")
                return message["result"]

    def __exit__(self, *_):
        try:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                terminate(self.process)
        finally:
            self.process.stdout.close()


class CodexHarness:
    name = "codex"
    effort = "high"
    auth_command = ["codex", "login", "status"]

    def __init__(self, model, root, state):
        self.model, self.root, self.state = model or MODEL, root, state
        self.metadata_lock = threading.Lock()

    def name_session(self, run_id, session_id, stage, task):
        """Use Codex APIs rather than editing its database or transcript files."""
        role = {"build": "worker", "review": "reviewer", "finalize": "finalizer"}[stage]
        name = session_name(self.state.repo, task["number"], role, task)
        root = self.root.resolve()
        try:
            # One metadata connection at a time per controller. The deterministic
            # creation key also prevents duplicate projects after a crash/retry.
            with self.metadata_lock, CodexAppServer(root) as server:
                server.request("thread/name/set", {"threadId": session_id, "name": name})
                matches, cursor, seen = [], None, set()
                while True:
                    page = server.request("project/list", {"cursor": cursor, "limit": 100})
                    matches.extend(p for p in page["data"] if any(
                        Path(r["path"]).resolve() == root for r in p["roots"]))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("Codex project/list repeated a pagination cursor")
                    seen.add(cursor)
                if len(matches) > 1:
                    raise RuntimeError(f"Multiple Codex projects have root {root}; inspect project membership")
                project = matches[0] if matches else server.request("project/create", {
                    "name": self.state.repo, "roots": [{"path": str(root)}],
                    "idempotencyKey": str(uuid.uuid5(uuid.NAMESPACE_URL, f"worklane-project:{root}")),
                })["project"]
                server.request("thread/metadata/update", {"threadId": session_id, "projectId": project["id"]})
            self.state.append_log(run_id, "session", f"Named {session_id}: {name}; Codex project {project['id']}")
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
            self.state.append_log(run_id, "warning", f"Codex session metadata could not be updated: {error}. Will retry on the next turn.")

    def prepare(self, stage, task, worktree, session, prompt, schema, schema_fd, result_fd):
        os.write(schema_fd, json.dumps(schema).encode())
        os.lseek(schema_fd, 0, os.SEEK_SET)
        args = ["codex", "exec", "--approve-for-me", "--cd", str(worktree),
                "--config", f'model_reasoning_effort="{self.effort}"', "--config", "features.multi_agent=false"]
        if stage == "finalize":
            args += ["--add-dir", str(self.root)]
        if session:
            args += ["resume"]
        args += ["--model", self.model, "--json", "--output-schema", f"/proc/self/fd/{schema_fd}",
                 "--output-last-message", f"/proc/self/fd/{result_fd}"]
        args += [session["session_id"], "-"] if session else ["-"]
        return args, prompt, None, CodexTurn(result_fd)

    @staticmethod
    def environment():
        return None

    @staticmethod
    def resume_args(session):
        value = validate_session_id("codex", session["session_id"])
        return ["codex", "resume", value, "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
                "--disable", "multi_agent"]


class CodexTurn:
    keep_wire = False

    def __init__(self, result_fd):
        self.result_fd = result_fd

    def events(self, event):
        return [event]

    def result(self):
        os.lseek(self.result_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(self.result_fd), "rb") as handle:
            return json.load(handle)


class OpenCodeHarness:
    name = "opencode"
    auth_command = ["opencode", "auth", "list"]

    def __init__(self, model, root, state):
        self.model, self.root, self.state = model, root, state

    @staticmethod
    def check_session(session, worktree):
        # OpenCode 2 can create a missing --session ID. Export must succeed first.
        validate_session_id("opencode", session["session_id"])
        check = subprocess.run(["opencode", "session", "export", "--standalone", session["session_id"]],
                               cwd=worktree, env=OpenCodeHarness.environment(), capture_output=True, timeout=30)
        if check.returncode:
            raise RuntimeError("OpenCode cannot read the saved session; refusing to create a replacement conversation.")

    def prepare(self, stage, task, worktree, session, prompt, schema, schema_fd, result_fd):
        if session:
            self.check_session(session, worktree)
        role = {"build": "worker", "review": "reviewer", "finalize": "finalizer"}[stage]
        args = ["opencode", "run", "--standalone", "--format", "json", "--auto", "--agent", "build",
                "--title", session_name(self.state.repo, task["number"], role, task)]
        if self.model:
            args += ["--model", self.model]
        if session:
            args += ["--session", session["session_id"]]
        env = self.environment()
        prompt += "\nReturn only one JSON object matching this schema, without Markdown fences:\n" + json.dumps(schema)
        return args, prompt, env, OpenCodeTurn()

    @staticmethod
    def environment():
        env = dict(os.environ)
        # OpenCode prefers inherited PWD over the subprocess's assigned cwd.
        env.pop("PWD", None)
        config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        permissions = config.setdefault("permission", {})
        if isinstance(permissions, str):
            permissions = config["permission"] = {"*": permissions}
        permissions["task"] = "deny"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        return env

    def name_session(self, *_):
        pass  # --title names the session; OpenCode associates its worktree project.

    @classmethod
    def resume_args(cls, session):
        cls.check_session(session, Path(session["worktree"]))
        return ["opencode", str(session["worktree"]), "--session", session["session_id"]]


class OpenCodeTurn:
    keep_wire = True

    def __init__(self):
        self.text = []

    def events(self, event):
        output = []
        if event.get("sessionID"):
            output.append({"type": "thread.started", "thread_id": validate_session_id("opencode", event["sessionID"])})
        kind, part = event.get("type"), event.get("part") or {}
        if kind == "step_start":
            self.text = []
            output.append({"type": "turn.started"})
        elif kind == "text":
            self.text.append(part["text"])
            output.append({"type": "item.completed", "item": {"type": "agent_message", "text": part["text"]}})
        elif kind == "tool_use":
            state = part.get("state") or {}
            text = state.get("output") or state.get("error") or part.get("tool", "tool")
            output.append({"type": "item.completed", "item": {"type": "tool", "text": str(text)}})
        elif kind == "step_finish":
            reason = part.get("reason")
            if reason == "stop":
                output.append({"type": "turn.completed"})
            elif reason != "tool-calls":
                output.append({"type": "turn.failed", "message": f"OpenCode finish reason: {reason}"})
        elif kind == "error":
            output.append({"type": "error", "error": event.get("error")})
        return output

    def result(self):
        return json.loads("".join(self.text))


HARNESSES = {"codex": CodexHarness, "opencode": OpenCodeHarness}
