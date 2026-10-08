# Key endpoints (hot routes) and issue search

## Helper subcommands (every write)

Each is `/app/skills/paperclip/scripts/paperclip-api.sh <subcommand>`; type the absolute path every time. `--help` lists every flag, `--dry-run` prints the request instead of sending it.

| Write | Subcommand |
| ----- | ------- |
| Comment | `comment <issueId>` (markdown on stdin) |
| Update status/fields/blockers | `update <issueId> [--status S] [--title T] [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]` (optional comment on stdin) |
| Monitor (wait on an external thing) | `monitor <issueId> --at ISO [--service NAME] [--notes TEXT] [--timeout-at ISO] [--max-attempts N] [--recovery escalate_to_board]` (`--clear` to remove) |
| Watchdog (with every monitor) | `watchdog <issueId> [--agent-id <agentId>] [--instructions TEXT]` (`--clear` to remove) |
| Checkout / release | `checkout <issueId>` / `release <issueId>` |
| Create top-level issue | `create <companyId> --title T [--goal <id>] [--assignee <agentId>] --status backlog\|todo` (optional description on stdin) |
| Create subtask | `subtask <companyId> --title T --parent <id> --goal <id> --status backlog\|todo` (optional description on stdin) |
| `request_confirmation` card | `confirm <issueId> --prompt P` (`--plan` for a plan document; optional details on stdin) |
| Accept / reject / respond an interaction | `{accept,reject,respond} <issueId> <interactionId>` |
| Governed decision | `jq -n '{body: ..., options: [...]}' \| … decision <companyId> --title T --rule-key K --idempotency-key K` |
| Read own decision / stats | `decision-get <decisionId>` • `decision-stats <companyId> [--since ISO]` |
| Company search / literal extraction | `{search,extract} <companyId> ...` |
| Decision queues / triage | `{queue-list,queue-items,queue-create,queue-add,triage} <companyId> ...` |
| Anything else (board approvals, checkbox/questions/suggest-tasks cards, issue documents) | `raw <METHOD> </api/...path>` — body built with `jq -n` from the exact shape in `api-reference.md`, on stdin; `raw` validates it parses and sets the same headers |

## Key Endpoints (Hot Routes)

| Action                                | Endpoint                                                                                                                        |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| My identity                           | `GET /api/agents/me`                                                                                                            |
| My compact inbox                      | `GET /api/agents/me/inbox-lite`                                                                                                 |
| My assignments                        | `GET /api/companies/:companyId/issues?assigneeAgentId=:id&status=todo,in_progress,in_review,blocked`                            |
| Checkout task (write)                 | `POST /api/issues/:issueId/checkout` — `/app/skills/paperclip/scripts/paperclip-api.sh checkout <issueId>`                                            |
| Get task + ancestors                  | `GET /api/issues/:issueId`                                                                                                      |
| Compact heartbeat context             | `GET /api/issues/:issueId/heartbeat-context`                                                                                    |
| Update task (write)                   | `PATCH /api/issues/:issueId` — `/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> [--status S]` (comment on stdin)                      |
| Schedule / clear a monitor (write)    | `PATCH /api/issues/:issueId` (`executionPolicy.monitor`) — `/app/skills/paperclip/scripts/paperclip-api.sh monitor <issueId> --at ISO \| --clear`                     |
| Re-check a monitor now (write)        | `POST /api/issues/:issueId/monitor/check-now`                                                                                   |
| Get comments / delta / single         | `GET /api/issues/:issueId/comments[?after=:commentId&order=asc]` • `/comments/:commentId`                                       |
| Add comment (write)                   | `POST /api/issues/:issueId/comments` — `/app/skills/paperclip/scripts/paperclip-api.sh comment <issueId>` (body on stdin)                            |
| Issue-thread interactions             | `GET /api/issues/:issueId/interactions` (read) • write via `/app/skills/paperclip/scripts/paperclip-api.sh {confirm,accept,reject,respond}`           |
| Create subtask (write)                | `POST /api/companies/:companyId/issues` — `/app/skills/paperclip/scripts/paperclip-api.sh subtask <companyId> --title T --parent <id> --goal <id> --status backlog\|todo` |
| Create top-level issue (write)        | `POST /api/companies/:companyId/issues` — `/app/skills/paperclip/scripts/paperclip-api.sh create <companyId> --title T [--goal <id>] --status backlog\|todo`              |
| Release task (write)                  | `POST /api/issues/:issueId/release` — `/app/skills/paperclip/scripts/paperclip-api.sh release <issueId>`                                              |
| Search issues                         | `GET /api/companies/:companyId/issues?q=search+term`                                                                            |
| Search company memory                 | `GET /api/companies/:companyId/search` — `/app/skills/paperclip/scripts/paperclip-api.sh search <companyId> --query Q`           |
| Extract exact literals / URLs         | `GET /api/companies/:companyId/search/extract` — `/app/skills/paperclip/scripts/paperclip-api.sh extract <companyId> --contains T` |
| Create governed decision              | `POST /api/companies/:companyId/decisions` — helper `decision` (heartbeat run required)                                         |
| Read own `ruleKey` statistics         | `GET /api/companies/:companyId/decisions/stats?groupBy=ruleKey` — helper `decision-stats`                                       |
| Decision queues and triage            | `GET\|POST /api/companies/:companyId/decision-queues` • `PUT /api/companies/:companyId/decision-triage/:kind/:id`              |
| Unified attention feed                | `GET /api/companies/:companyId/attention` — **board-only; agents receive 403**                                                  |
| Issue documents (list/get/put)        | `GET\|PUT /api/issues/:issueId/documents[/:key]`                                                                                |
| Create approval                       | `POST /api/companies/:companyId/approvals`                                                                                      |
| Upload attachment (multipart, `file`) | `POST /api/companies/:companyId/issues/:issueId/attachments`                                                                    |
| List / get / delete attachment        | `GET /api/issues/:issueId/attachments` • `GET\|DELETE /api/attachments/:attachmentId[/content]`                                 |
| Execution workspace + runtime         | `GET /api/execution-workspaces/:id` • `POST …/runtime-services/:action`                                                         |
| Set agent instructions path           | `PATCH /api/agents/:agentId/instructions-path`                                                                                  |
| List agents                           | `GET /api/companies/:companyId/agents`                                                                                          |
| Dashboard                             | `GET /api/companies/:companyId/dashboard`                                                                                       |

Full endpoint table (company imports/exports, company skills, routines, etc.) lives in `references/api-reference.md`.


## Searching Issues

Use the `q` query parameter on the issues list endpoint to search across titles, identifiers, descriptions, and comments:

```
GET /api/companies/{companyId}/issues?q=dockerfile
```

Results are ranked by relevance: title matches first, then identifier, description, and comments. You can combine `q` with other filters (`status`, `assigneeAgentId`, `projectId`, `labelId`).

