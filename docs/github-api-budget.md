# GitHub API budget and recovery

Audit for [issue #18](https://github.com/sbracegirdle/worklane/issues/18),
19 October 2026, using `gh 2.93.0`. Counts below distinguish controller operations,
HTTP requests and GraphQL primary points. They are different quantities.

## Reproducible controller measurements

Run without GitHub credentials or a running controller:

```sh
uv run python -m unittest -v test_api_budget \
  test_worklane.WorklaneTests.test_idle_hour_and_exhausted_hour_have_bounded_request_counts \
  test_human_feedback.HumanFeedbackTests.test_idle_feedback_reads_twelve_times_per_hour_and_survives_restart
```

The tests execute real scheduling, transition, verification, posting and feedback
paths with a fake transport and temporary SQLite databases; no agent or GitHub
write occurs. Fixtures use one board page, one page per discussion connection,
open non-draft PRs and no inline findings. Counts exclude startup discovery and
agent-owned requests.

| Scenario | Measured controller operations |
| --- | ---: |
| Empty idle queue, default 60-second interval, one hour | 60 board pages |
| Persistent quota failure without reset headers, one hour | 6 failed intake attempts |
| Three builds plus one existing PR review and handoff | 55 GitHub operations |
| One unchanged owned Human review task, one hour | 12 feedback snapshots; 72 operations plus board polling |
| One status transition | 2 item status queries + 1 mutation |
| One PR identity verification | 1 PR metadata query |

The batch's 55 operations break down into 1 board page, 14 item status reads,
7 status mutations, 4 issue reads, 9 PR identity reads, 5 comment reads,
5 comment writes, 6 review-thread queries and 4 feedback REST pages. A PR comment
CLI command also resolves its subject before its mutation, so an operation is
not always a single HTTP request. More discussion pages increase HTTP requests;
worktree `git fetch` traffic and Codex's own `gh` calls are additional.
For this fixture, five comment subject lookups imply approximately 60 HTTP
requests (56 GraphQL and 4 REST), from the pinned CLI source. With small discussion
connections, the published primary-cost formula predicts roughly 56 GraphQL
points for the batch. These HTTP/point figures are derived, not live measurements.

Unchanged feedback costs two GraphQL reads (issue + PR) and four REST connections
per snapshot, or 24 GraphQL reads and 48 REST requests per hour per owned PR when
each connection fits one page. Questions need only the issue read and issue-comment
REST connection. Pending batches continue polling at the controller interval to
refresh edits/deletions before dispatch. Feedback polling pauses while all four
slots are occupied. Budget for the total saved Human review population and all
repositories sharing the account; worker count alone does not bound that cost.

## Why the old board query amplified cost

The [version-pinned gh query source](https://github.com/cli/cli/blob/v2.93.0/pkg/cmd/project/shared/queries/queries.go)
shows that `project item-list --limit 10000` requests pages of **100**, not 10,000
items in one request. It first resolves owner type, then reads the project, its
fields and items. Each item requests `fieldValues(first:100)` and nested labels,
linked PRs, users and reviewers (`first:10`). It also fetches PR/draft content that
this issue queue discards. The limit permits up to 100 item pages and does not
cache a board between polls. Owner resolution can mask a GraphQL error as
`unknown owner type`: `OwnerIDAndType` handles NOT_FOUND specially, then discards
other GraphQL errors before returning that message.

Using GitHub's [published cost estimation method](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api#predicting-the-point-value-of-a-query),
the old broad shape has roughly `2 + 100 + 4*100*100 = 40102` potential connection
requests, or about **401 primary points per full page**, plus owner lookup. The
new shape has one item connection and at most 100 linked-PR connections, roughly
**1 primary point per page**. These are source-derived estimates, **not observed
server costs**; union selection, returned data and GitHub's evolving accounting
can affect billing. The reset-limited account prevented a successful live
comparison of these resource queries during the audit. A quota-only query did
return `rateLimit.cost: 1`. To verify live costs after reset, append
`rateLimit { cost remaining resetAt }` to a read-only query and inspect its
response together with `X-RateLimit-*`; do not infer cost from command count.

Intake now selects Status, issue identity/content and the board's **Linked pull
requests** field by name. Closing references on the issue are not a substitute:
non-closing board links also prevent duplicate implementation work. Both item
and PR-link connections paginate to completion and reject missing/repeated/cyclic
cursors. Board reads reject incomplete totals and more than 10,000 items.
Status transitions read only the specific item before and after the write, rather
than scanning the entire project twice. PR verification no longer downloads
comments/reviews/body; feedback and inline-thread readers retrieve those separately
when needed. Thread and reply pagination also detects cursor cycles.
Closed issues are excluded from the board snapshot before slot selection, avoiding
three repeated eligibility reads for a closed issue left in Ready for work.

## Live quota discrepancy

At **18:25:33 Perth**, REST `/rate_limit` returned GraphQL remaining **4556**,
used **444**, reset **1792407489**. At **18:25:34**, a live GraphQL viewer request
returned HTTP 200 with a `RATE_LIMIT` body, remaining **0**, used **5000**, reset
**1792407126** (**18:52:06 Perth**). These were fresh responses with different
GitHub request IDs. At **18:28:12**, a distinct query URL with
`Cache-Control: no-cache, no-store` still returned remaining **0** and that same
reset; its quota-only body reported used **5019** and cost **1**.
A viewer query with `rateLimit(dryRun:true)` at **18:35:31** was also rejected,
so a dry run could not obtain resource-query costs under this exhausted quota.

The discrepancy is therefore not explained by the usual REST-versus-GraphQL
resource distinction alone: the REST body itself advertised available GraphQL
points. The underlying server accounting/bucket discrepancy remains unproven.
No cached error file was found in the local gh cache during initial inspection;
there is no evidence establishing a local cache bug. Worklane does not add
`gh api --cache` or periodically query `/rate_limit`. The failing resource's
actual response headers and body govern cooldown, as GitHub recommends. Other
CLI sessions and agents on the same user quota can consume points concurrently.

## Coordination and recovery

Controller requests share a machine-local lock and SQLite cooldown at
`$XDG_STATE_HOME/worklane/_github/` (default `~/.local/state/worklane/_github/`).
This deliberately coordinates repositories conservatively without storing tokens
or assuming that different credentials have independent user quotas. Controllers
using different state roots, remote machines, browser sessions and agent-owned
subprocesses do not share that lock. They still consume the account's GitHub
budget; this is not a distributed quota reservation system.

Requests serialize through the lock. A quota failure records cooldown before
another controller request can run. `Retry-After` and a zero-remaining
`X-RateLimit-Reset` are minimum deadlines, with one second added past reset.
Without headers, delays grow 60, 120, 240 seconds up to one hour. A masked owner
error, or a CLI quota error lacking headers, permits one diagnostic GraphQL probe
to recover the underlying error/reset. This is not a recurring quota poll.
Success with zero remaining also publishes cooldown for subsequent calls.

Queue and feedback intake stop on the first quota failure. The remaining slots
and other controller threads respect the persisted deadline. A successful board
read alone does not reset exponential delay: the whole intake attempt must finish,
and it may clear only the exact failure generation it observed. A concurrent new
worker failure survives that clear. Continuous mode retries after cooldown; `--once`
exits after its batch and needs another invocation if quota interrupted the batch.

A claim interrupted before or after its status write is saved as resumable work,
so it cannot remain ACTIVE without a future. Completed structured build, review
and finalizer results are saved before GitHub verification/posting; quota recovery
reuses those results, rechecks the exact PR head and respects the current board
status. Idempotent comment markers avoid duplicate posts. Rework and explicitly
retried substantive blockers can start a new turn in the original role's session.
Human status changes, changed PR heads and unresolved review threads retain their
existing completion gates. Keep the state database, shared cooldown and worker
worktrees when recovering; do not delete them to work around quota exhaustion.
