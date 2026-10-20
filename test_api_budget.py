"""Repeatable request measurements: real controller paths, fake GitHub transport."""
import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import worklane as w
from test_worklane import connection, result


class ApiBudgetTests(unittest.TestCase):
    def test_three_workers_and_review_request_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = w.State("owner/repo", root)
            statuses = {str(n): w.READY if n < 4 else w.REVIEW for n in range(1, 5)}
            board = object.__new__(w.Board)
            board.project, board.field = {"id": "project"}, {"id": "status"}
            board.repo = "owner/repo"
            board.options = {s.casefold(): s for s in (w.READY, w.PROGRESS, w.REVIEW, w.HUMAN)}
            backend = Mock(model=w.MODEL)
            def agent(stage, task, *_):
                output = result()
                output["pr_url"] = f"https://github.com/owner/repo/pull/{task['number']}"
                return output
            backend.run.side_effect = agent
            runner = w.Runner(argparse.Namespace(base="main", once=True, interval=60),
                              "owner/repo", root, state, board, backend)
            runner.worktree = Mock(return_value=root)
            calls = Counter()

            def transport(args, *, stdin=None, **_):
                if args[0] == "git":
                    return "a" * 40 if args[1] == "rev-parse" else ""
                self.assertEqual(args[0], "gh")
                if "graphql" in args:
                    query = json.loads(stdin)["query"]
                    variables = json.loads(stdin)["variables"]
                    if "items(first:100" in query:
                        calls["board page"] += 1
                        nodes = []
                        for n, status in statuses.items():
                            nodes.append({"id": n, "fieldValueByName": {"name": status},
                                "links": {"pullRequests": connection([{"url": f"https://github.com/owner/repo/pull/{n}"}] if n == "4" else [])},
                                "content": {"number": int(n), "title": "Issue", "body": "", "url": "issue",
                                            "repository": {"nameWithOwner": "owner/repo"},
                                            "issueDependenciesSummary": {"blockedBy": 0}}})
                        data = {"node": {"items": dict(connection(nodes), totalCount=4)}}
                    elif "issueDependenciesSummary" in query:
                        calls["dependency check"] += 1
                        data = {"repository": {"issue": {"issueDependenciesSummary": {"blockedBy": 0}}}}
                    elif "reviewThreads(" in query:
                        calls["review threads"] += 1
                        data = {"repository": {"pullRequest": {"reviewThreads": connection([])}}}
                    else:
                        calls["item status"] += 1
                        data = {"node": {"project": board.project,
                                "fieldValueByName": {"name": statuses[variables["id"]]}}}
                    return json.dumps({"data": data})
                if args[1:3] == ["project", "item-edit"]:
                    calls["status mutation"] += 1
                    statuses[args[args.index("--id") + 1]] = args[-1]
                    return ""
                if args[1:3] == ["issue", "view"]:
                    calls["issue"] += 1
                    return json.dumps({"state": "OPEN", "labels": [], "title": "Issue"})
                if args[1:3] == ["pr", "view"]:
                    if args[-1] == "comments":
                        calls["comment read"] += 1
                        return '{"comments":[]}'
                    calls["PR identity"] += 1
                    number = args[3].rsplit("/", 1)[1]
                    return json.dumps({"url": args[3], "state": "OPEN", "isDraft": False,
                        "headRefOid": "a" * 40, "headRefName": f"codex/{number}-worklane", "baseRefName": "main"})
                if args[1:3] == ["pr", "comment"]:
                    calls["comment write"] += 1
                    return ""
                if args[1] == "api":
                    calls["feedback REST page"] += 1
                    return "[[]]"
                self.fail(f"Unexpected transport: {args}")

            with patch.object(w, "raw_command", side_effect=transport):
                runner.run(threading.Event())
            self.assertEqual([state.load(n)["phase"] for n in range(1, 5)],
                             ["awaiting_review"] * 3 + ["human_review"])
            self.assertEqual(calls, Counter({"board page": 1, "dependency check": 4, "item status": 14,
                "status mutation": 7, "issue": 4, "PR identity": 9, "comment read": 5,
                "comment write": 5, "review threads": 6, "feedback REST page": 4}))
            self.assertEqual(sum(calls.values()), 59)


if __name__ == "__main__":
    unittest.main()
