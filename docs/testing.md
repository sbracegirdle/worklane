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
log following and quitting without stopping the controller. Interactive resume
uses a mocked invocation; no real coding conversation is resumed by these tests.
TUI controls use mocked launches; cached issue/board views also run in a real terminal.
Input tests cover terminal composition, queued follow-up subprocesses, session routing, completion races and interrupted delivery recovery.
Configuration tests cover validation, atomic saves, cancellation, overrides, dispatch capacity and first-use setup in a real terminal.
Cache tests cover expiry, offline content, comment pagination and shared GitHub cooldowns.

Metadata tests use a fake app-server subprocess to check naming, project reuse and
creation, pagination, concurrent roles, resumption, and transport failure cleanup.

Issue-creation tests cover literal multiline descriptions, destination validation,
Draft/Shaping selection, concurrent controller pickup, and recovery guidance after
partial failure. GitHub writes are mocked; live issue creation and board placement
require a disposable repository and board for manual validation.
