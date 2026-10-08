# Attention, decisions, queues, and company search

These APIs turn a recurring agent run into durable company memory and bounded
human decisions. They are Paperclip coordination primitives, not substitutes
for the domain tools that provide external evidence.

## Authorization boundary

The deployed Paperclip API intentionally exposes different surfaces to agents
and the board:

| Surface | Standard-scope active agent | Board user |
| --- | --- | --- |
| Unified attention feed | **No.** `GET /api/companies/:companyId/attention` requires board context. | Yes |
| Create a decision | Yes, but only from a real heartbeat run. | No; the board decides or dismisses it |
| Read a decision | Origin agent only | Yes |
| `ruleKey` statistics | Own origin-agent statistics only | May filter by origin agent |
| Decision queues and triage | Yes, for sources the agent is authorized to read | Yes |
| Company search and extraction | Yes | Yes |

Low-trust review agents, task-bridge keys, and skill-test tokens are denied the
company-wide queue/search surfaces. A `403` is a real authorization boundary;
do not retry with a wider token or fall back to scraping the board.

The board-only attention feed is still useful to agents as an **output**. An
agent creates a decision, assigns it to a queue, and sets a deadline. Paperclip
then renders the resulting attention item, `whyNow`, decision verbs, severity,
deduplication, queues, aging shelf, and `decideBy` for the board. Agents do not
read or dismiss the unified feed themselves.

## Company search before rediscovery

Use company search before rebuilding context from issue-by-issue reads:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh search "$PAPERCLIP_COMPANY_ID" \
  --query "activation provider abandonment" --scope all --updated-within 30d
```

Search covers issues, comments, documents, artifacts, agents, and projects.
Narrow with `--scope`, status/priority, assignee, project, label, time, sort,
limit, and offset. Results include ranked snippets, matched fields, durable
resource links, counts, and suggestions for loosening zero-result filters.

Use extraction when the work needs exact repeated literals or URLs rather than
relevance-ranked prose:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh extract "$PAPERCLIP_COMPANY_ID" \
  --contains "github.com/Isol8AI/isol8/pull" --kind url --scope all
```

Extraction searches issue titles/descriptions, comments, and documents and
returns the exact values plus their source references. It does not search
artifacts, agents, or projects. Both endpoints are rate limited; honor `429`
and `Retry-After` instead of immediately retrying.

## Create a governed decision

Create a decision only for a genuine authority edge. A missing fact is a
question; routine task selection is agent work; legal, financial, new-spend,
refund, credential, and reputation-bearing actions are decisions.

Every recurring decision class needs a stable `ruleKey`:

- use a semantic lowercase key such as `support.refund`, `spend.new_service`,
  or `launch.publish_show_hn`;
- reuse the same key for the same policy question across runs;
- never put a date, run ID, issue ID, customer ID, or revision into `ruleKey`;
- put per-attempt uniqueness in `idempotencyKey`, not `ruleKey`.

Build the decision spec with `jq -n`, then let the helper add the governed
fields:

```bash
jq -n --arg issue "$PAPERCLIP_TASK_ID" '{
  body: "A refund is requested. Stripe evidence is attached to the issue.",
  options: [
    {
      id: "approve",
      label: "Approve refund workflow",
      style: "destructive",
      effects: [{
        type: "comment_on_issue",
        targetIssueId: $issue,
        staleness: "strict",
        bodyMarkdown: "Refund workflow authorized by the board."
      }]
    },
    {id: "decline", label: "Decline", effects: []}
  ]
}' | /app/skills/paperclip/scripts/paperclip-api.sh decision \
  "$PAPERCLIP_COMPANY_ID" \
  --title "Authorize this refund workflow?" \
  --rule-key support.refund \
  --idempotency-key "support-refund:$PAPERCLIP_TASK_ID:v1"
```

The helper requires a real `PAPERCLIP_RUN_ID` for live calls and defaults the
continuation policy to `wake_origin_agent`. Use `--continuation none` only when
the origin agent truly has no follow-up work.

### Server-applied effects

Each option may contain up to ten effects. Paperclip supports:

| Effect | Result |
| --- | --- |
| `comment_on_issue` | Add an audited board-user comment |
| `create_issue` | Create a child/follow-up issue from a signed draft |
| `update_issue_status` | Move an issue and optionally comment |
| `assign_issue` | Assign an existing issue |
| `resolve_blocker` | Remove named blocker relationships |
| `cancel_issue_tree` | Cancel the signed issue subtree; must be strict and destructive |

Every effect carries a `targetIssueId` and `staleness` policy. Use `strict` when
the action must be skipped if any signed target changed after proposal. Use
`lenient` only when the action remains valid against the current target state.

Decision effects are not a way to borrow authority. At decision time Paperclip
intersects the origin agent's issue authority with the deciding user's board
authority. Each effect is separately audited and can be `executed`, `skipped`,
or `failed`. Approval does **not** prove application: after wake, read the
decision outcome and inspect every effect execution before claiming success.
An assigned `create_issue` effect can dispatch its assignee under Paperclip's
normal create defaults, so use it only when acceptance is intended to start
that work.

After the continuation wake, fetch the outcome directly:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh decision-get "<decision-id>"
```

### Standalone Decisions

A single-issue decision (the `decision` helper) is scoped to one issue.
When effects need to span several issues, or a set of related choices should
be presented and resolved together, bundle them instead —
`POST /api/companies/{companyId}/decision-bundles` accepts 1–50 decisions
(the same `body`/`options`/effect shapes as the single-decision helper) and
creates them atomically. There is no dedicated bundle subcommand: build the
array with `jq -n` and send it through the escape hatch, e.g.
`jq -n '{title: T, summary: S, decisions: [...]}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/companies/$PAPERCLIP_COMPANY_ID/decision-bundles"`.

## Measure autonomy by `ruleKey`

```bash
/app/skills/paperclip/scripts/paperclip-api.sh decision-stats \
  "$PAPERCLIP_COMPANY_ID" --since "2026-08-01T00:00:00Z"
```

Agents are always server-scoped to the caller's own decisions.
The response separates `proposed`, `accepted`, `rejected`, and `expired`, plus
accepted option counts per `ruleKey`. Cancelled decisions count as proposed but
not as accepted/rejected/expired. Do not merge expiry into rejection.

Autonomy is earned per rule class, not per agent. A class may lose a human gate
only after enough accepted decisions and external outcomes show that the action
is both predictable and reversible. Legal, financial, credential, new-spend,
and founder-reputation edges retain their gates regardless of acceptance rate.

## Queues and triage

Decision queues are named server-side groupings that the board attention desk
can filter. List existing queues before creating one:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh queue-list "$PAPERCLIP_COMPANY_ID"
/app/skills/paperclip/scripts/paperclip-api.sh queue-create "$PAPERCLIP_COMPANY_ID" \
  --key customer-authority --title "Customer authority" --retention-days 30
```

Add only a source the current agent can already read:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh queue-add "$PAPERCLIP_COMPANY_ID" \
  --key customer-authority --source-kind decision --source-id "<decision-id>"
```

Then make the timing explicit:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh triage "$PAPERCLIP_COMPANY_ID" \
  --source-kind decision --source-id "<decision-id>" --decide-by today
```

`decideBy` accepts `today`, `this_week`, `whenever`, or `YYYY-MM-DD`.
`snoozedUntil` accepts an offset ISO timestamp. Use `--clear-decide-by` or
`--clear-snooze` to send an explicit `null`. Queue and triage mutations record
the actor, run, responsible user, and activity event.

Agents can queue only attention sources they are authorized to read. Linked
decisions/reviews/issues follow issue visibility; failed runs are visible only
to their owning agent; join requests, unlinked approvals, and budget incidents
are board-only. A not-found response may deliberately hide an unauthorized
source.

For queue update/removal, retention keep/archive/revive, decision cancellation,
or reading one origin decision, use the exact endpoint table in
`api-reference.md` through the helper's jq-validated `raw` command.
