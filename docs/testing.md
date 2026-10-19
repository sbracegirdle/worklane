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

Metadata tests use a fake app-server subprocess to check naming, project reuse and
creation, pagination, concurrent roles, resumption, and transport failure cleanup.
