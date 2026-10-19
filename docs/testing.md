# Testing

See the [README](../README.md#develop) for development and test commands.

Tests use temporary state and mocked GitHub/agent responses. They cover concurrency,
eligible statuses, pause, process locks, status preservation, review and handoff
gates, duplicate work, changed heads and recovery boundaries. A read-only live board
check and empty-queue run can verify authentication and board wiring without claiming
an issue. A real coding ticket is needed to validate a complete agent implementation.

Session tests also launch a fake CLI as a real subprocess, interrupt it and restart
the controller, checking that the exact original session is resumed. A harmless
live Sol 6.1 two-turn check verified both the unchanged session ID and recall of
information present only in the previous turn.

Dashboard tests exercise SQLite refresh, separate role sessions, log tails,
Unicode wrapping without lost content, resume guards, navigation and resizing.
A real curses process in a pseudo-terminal verifies colour output, live refresh,
log following and quitting without stopping the controller. Regressions cover
colour with `NO_COLOR` set, state groups across pages, protocol/MCP filtering with
raw log access, and back navigation after resize, database errors and resume. Interactive resume
uses a mocked invocation; no real coding conversation is resumed by these tests.
