# Key endpoints (hot routes) and issue search

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

