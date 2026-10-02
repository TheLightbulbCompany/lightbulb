# Paperclip feature adoption plan

Date: 2026-10-02. Status: draft for owner review. Delete when shipped or abandoned.

Everything here was read from the deployed fork tag `v2026.916.1-isol8.3` and from this
repo, not from docs.paperclip.ing. The docs describe upstream's newest release and were
only used to find features. Items marked **verify in build** were reported by a read of
the fork and not re-checked by hand.

## The shape of the work

Teams is our Paperclip UI. Paperclip's own UI has no DNS record and customers never see
it. So every feature is: a server capability that is already running or one flag away,
plus a BFF route, plus a Teams surface, plus a line in the agent skill.

Three rails already exist and carry almost all of this plan:

| Rail | What it gives us | Where |
|---|---|---|
| Attention feed | "Needs you" in Inbox and Home, live refresh, badge count | `routers/teams/attention.py`, `teams/inbox/AttentionList.tsx` |
| Activity feed | Server-rendered rows with actions, on web and phone, no client work | `core/services/activity.py`, `GET /messaging/activity` |
| Issue properties rail | Per-issue rows: reviewers, approvers, monitor | `teams/issues/IssueProperties.tsx` |

No new panel is needed for phases 1 and 2. No new event bus, no new table.

## Design rule: port, do not design

Every surface below is a port of Paperclip's own component from the deployed fork tag
(`~/Desktop/paperclip-fork.nosync`, `v2026.916.1-isol8.3`, under `ui/src/`). We design
nothing new. Teams already holds 108 modules ported this way; follow the same convention:

- Keep upstream's markup, layout, states, copy and behaviour.
- Add the standard header: `// Ported from upstream Paperclip's X (paperclip/ui/src/...)
  (MIT, (c) 2025 Paperclip AI).`
- Swap the look for ours: shadcn semantic classes become our tokens from `globals.css`
  (`--fg`, `--fg-soft`, `--fg-mute`, `--plane-*`, `--edge`, `--alert`, `--live`). No
  colour literals; `brandTokens.test.ts` enforces it.
- Swap the plumbing for ours: TanStack Query becomes a `useTeamsApi` hook, `MarkdownBody`
  becomes `MarkdownView`, agent and status marks become `Identity` and `StatusIcon`,
  `@paperclipai/shared` types go in `teams/shared/types.ts`. `cn`, `Button`, `Input`,
  `Textarea` and lucide icons are already the same.
- Port only the files listed. Anything not listed is skipped on purpose.

| Feature | Port from `ui/src/` | Lines | Skip |
|---|---|---|---|
| Decisions card | `components/DecisionCard.tsx` | 627 | |
| Decisions data | `components/DecisionResolver.tsx`, `api/decisions.ts`, `api/decisionQueues.ts` as SWR hooks | 475 | |
| Decisions queue page | `pages/DecisionQueuePage.tsx`, `DecisionQueueRail`, `DecisionTriageStrip`, `DecisionDateChips`, `DecisionsToolbar`, `DecisionShelf` | 1,599 | |
| Watchdog | the watchdog row inside `components/issue-properties/IssueProperties.tsx` | part | |
| Work modes | `lib/work-mode-meta.ts` | 89 | |
| External objects | `ExternalObjectPill`, `ExternalObjectStatusIcon`, `ExternalObjectStatusSummary`, `issue-properties/external-object-rows.tsx`, `lib/external-objects.ts`, `lib/external-object-href.ts` | about 1,000 | |
| Cases | `pages/Cases.tsx`, `pages/CaseDetail.tsx`, `CaseActivityFeed`, `CaseAttachmentsGallery`, `CaseChildrenTree`, `CaseFieldsPanel`, `CaseIdentifierKey`, `CaseRevisionRail`, `IssueCasesPanel`, `api/cases.ts` | 3,512 | |
| Status cards | `pages/StatusCards/StatusCardTile.tsx`, `index.tsx`, `CreateStatusCardDialog.tsx` | about 700 | DetailDrawer, SettingsForm, archived rows |

## Phase 1: finish what is already running (no flags)

These features are on in the server today. Agents already use two of them. People cannot.

### 1. Decisions: make them answerable

**What it is.** An agent that hits an authority edge files a decision: a title, a body,
one to eight options, up to four inputs. Each option carries effects that Paperclip
executes itself when a person picks it: comment, set status, reassign, clear a blocker,
create a child issue, or cancel an issue tree. The agent is then woken with the outcome.

**Today.** Agents file them (the skill teaches it in full). Paperclip puts each open
decision in the attention feed. Teams shows it in Inbox and Home as a read-only row with
an "Open thread" link. Nobody can pick an option. Decisions expire after 7 days.

**Build.**

- Admin client: `get_decision`, `decide_decision`, `dismiss_decision`.
- BFF: `GET /teams/decisions/{id}`, `POST /teams/decisions/{id}/decide`,
  `POST /teams/decisions/{id}/dismiss`. Caller's own cookie, never the admin cookie.
  Effects run as the deciding user.
- No change to `attention.py`. Upstream's card fetches its own decision by id, so the
  port does the same and the feed stays a passthrough.
- Web: port `DecisionCard.tsx` and render it in `AttentionList.tsx` beside the approval and
  interaction branches. It already handles option buttons, the destructive style, inline
  inputs, the per-option effect summary and the result states (`succeeded`, `partial`,
  `failed`, skipped because the target changed). Port `DecisionResolver` as one SWR hook.
- Phone, second step after web ships: `render_decision` and `_run_decision_action` in
  `activity.py`, `"decision"` added to `ActivitySubtype` and the pinned order. The Activity
  clients are generic, so this needs no mobile UI. A decision with inputs opens the thread
  instead of offering one-tap actions.

**Not in this phase.** Push notifications. Paperclip does not forward `decision.*` to
plugins, so our notification forwarder never sees them. That needs a fork change and goes
with the next fork release (see phase 4). Until then a decision shows live in Inbox, Home
and the Activity badge, with no push.

**Then the full queue page (owner ruling 2026-10-02: keep it).** A new `decisions` Teams
panel ported from `DecisionQueuePage.tsx` with its rail, triage strip, date chips, toolbar
and shelf. It gives people named queues, snooze, decide-by dates and keep or archive.

- BFF: `GET /teams/decisions` (list with status, bundle, issue and agent filters), the
  queue routes (`decision-queues` list, create, patch, items add and remove, seed rules),
  triage (`decision-triage/{sourceKind}/{sourceId}` get and put) and retention (keep,
  archive, revive). Any non-viewer member may manage these upstream, so no extra role.
- Triage is keyed by the attention item's `sourceKind` and `sourceId`, not by decision id.
  The page therefore sorts every "needs you" kind, approvals and questions included.
- Panel registration: `PANEL_LOADERS` in `TeamsPanelRouter.tsx`, `TEAMS_LIST_KEYS` in
  `panelRegistry.ts`, and a row in `COMPANY_SECTIONS`.
- The page imports upstream's `AttentionQueueRow` and `IssueGroupHeader`. **Verify in
  build** whether we already ported them; if not they join the port list.

**Skipped.** Decision training examples. Not read in depth and no UI found for it.

**Gotchas.**
- An `assign_issue` or `create_issue` effect does not wake the new assignee. The skill
  already tells the origin agent to inspect every effect after its wake; add one line
  telling it to wake the assignee.
- The signing secret is already set in the stack. Without it every open decision would
  die on each task replacement.

### 2. Task watchdogs: let people arm them

**What it is.** A named agent that watches an issue and its whole subtree. When the
subtree goes quiet with no run queued, no wake pending and no retry scheduled, Paperclip
opens a child issue "Watchdog review for X", assigns it to the watchdog agent and wakes
it. One watchdog per issue. It is durable and is not used up by firing.

**Today.** The skill tells agents to arm one whenever they arm a monitor. Teams has no
route or UI for it. The watchdog on the run detail panel is a different feature, the
silent-run watchdog.

**Build.**

- Admin client and BFF: `PUT` and `DELETE /teams/issues/{id}/watchdog`, body
  `{agentId, instructions}`. No `GET`: upstream's UI reads `issue.watchdog` off the issue
  it already has. **Verify in build** that our issue detail passes that field through.
- Web: port upstream's watchdog row from its `IssueProperties.tsx` into ours, beside
  `MonitorRow`. Agent picker plus an instructions field, as upstream has it.
- Skill: a `watchdog` helper subcommand so agents stop using `raw PUT`.

**Cost.** One agent run each time the watched subtree stops in a new state. It is opt-in
per issue, so spend follows use.

**Verify in build.** How the "Watchdog review" child issue appears in our issue lists, and
whether it needs a label or filter so it does not read as noise.

### 3. Work modes: finish the half that exists

**What it is.** A per-issue field: `standard`, `ask` or `planning`. For our adapter it is
a prompt directive, not enforcement. Ask tells the agent to answer and write no code.
Planning tells it to write a plan document and nothing else. Planning has a real approval
loop: the agent files a confirmation on the `plan` document, a person accepts, and
Paperclip flips the issue back to `standard` and wakes the agent to execute.

**Today.** The web create dialog has the control and sends the field. Nothing shows the
mode after creation, nothing edits it, the phone sheet omits it, and the skill never
mentions it.

**Build.**

- Web: port `lib/work-mode-meta.ts` for the labels and icons, and use it for a mode badge
  and an editable row on issue detail. The field is accepted on PATCH. Point the existing
  create dialog at the same meta so the names match upstream.
- Phone: the control in `CreateIssueSheet.tsx`. The type already exists in `teamsData.ts`.
- Skill: a short reference. What each mode means, and the plan loop: write `plan`, file a
  `request_confirmation` on it, wait.

**Verify in build.** That the existing interaction card renders a plan approval well. The
feed marks it `detail.kind: "plan_approval"`.

### 4. Review and approval stages: close two gaps

**What it is.** Per-issue stages. When the working agent marks the issue done, Paperclip
moves it to `in_review`, assigns it to a stage participant and wakes them. After three
agent change-request rounds it hands the stage to a person.

**Today.** Teams has reviewer and approver rows on issues and a reviewer picker on
routines.

**Gaps.**

- A person approves by setting status to done with a comment, in one request, as the
  participant. There is no approve route.
- Any workspace owner who is not the active participant and changes the status clears the
  stage silently. Teams should warn before that happens.

**Verify in build.** What issue detail does today in both cases.

**Skipped.** Default stages for new issues. Paperclip has no company or project default at
this tag, so it would be our own BFF logic. Routines already stamp their policy onto the
issues they create, which covers recurring work. Add a workspace default when asked.

## Phase 2: flags

### How a flag gets set

Flags are one row for the whole instance. Every customer gets the change.

- The REST route is instance-admin only and writes an activity row into every company's
  feed, with the full settings object. Customers would see it.
- `PAPERCLIP_MANAGED_CONFIG` sets flags at boot but puts the instance in cloud-managed
  mode, which blocks company creation. Not usable.
- A direct update of the `instance_settings` row avoids both. The service re-reads the row
  on each use, with no cache.

**Build.** `apps/backend/scripts/set_paperclip_instance_flags.py`. Re-runnable, dry-run by
default, desired flags declared as a constant in the file so the repo records the intended
state. It merges into the `experimental` JSON over the RDS Data API, the same rail
`paperclip_user_delete.py` uses. Dev first, then prod.

The same script turns `enableNativeRunner` off. It defaults to on and we never use it.

### 5. External objects: live PR status on issues

**What it is.** Paperclip finds GitHub pull request and issue URLs in issue text, comments
and documents, resolves their state, and refreshes them every five minutes.

**Build.**

- Flag: `enableExternalObjects`.
- Admin client and BFF: list for an issue, refresh, and the batch summary route for lists.
- Web: port the pill, status icon, status summary and the issue-properties rows, with
  `lib/external-objects.ts` for the state logic. Pills on issue detail, the summary badge
  on list rows. Add `external_object.updated` to the Teams live event map.
- Skill: a short note, and assertions only against routes that exist.

**The real cost is the token.** The resolver reads a company secret named `GITHUB_TOKEN`.
Without one it calls GitHub unauthenticated from our shared egress address, which is rate
limited for the whole fleet at once, and private repos show "Requires auth". So this needs
a sync from the owner's GitHub connection into a Paperclip company secret. That sync is
the bulk of the work and is new plumbing.

### 6. Cases

**What it is.** A structured record with a type, a key, fields, documents, attachments and
links to issues. Upsert by type and key. Think a customer ticket, a lead or a deal that
several issues hang off.

**Build, in full (owner ruling 2026-10-02).**

- Flag `enableCases`, drop the "Disabled on this instance" line from the skill routing
  row, update the pinned test. Agents can then use cases at once.
- A new `cases` Teams panel ported from `pages/Cases.tsx`, and a `case` detail panel ported
  from `pages/CaseDetail.tsx` with its fields panel, children tree, activity feed,
  attachments gallery and revision rail. Registered like the decisions panel, plus a
  `DETAIL_LOADERS` entry.
- `IssueCasesPanel` on issue detail. Issue detail already returns `linkedCases`.
- BFF: cases list and upsert, get and patch, events, links, attachments, and the document
  routes (get, put, delete, lock, unlock, revisions, restore, annotations).
- Reuse, do not re-port: case documents use the same document, revision and annotation
  model as issue documents, which Teams already has. Upstream's page imports
  `IssueDocumentsSection` for exactly this.
- Cases emit no live events and no activity rows on create, update or link. The panels
  poll on focus and on an interval.

**Remove the Support placeholder in the same PR (owner ruling 2026-10-02).** Cases gets
its own sidebar row, named Cases, and Support goes:

- Delete `teams/panels/SupportPanel.tsx`, a 25-line "Soon" shell with no data behind it.
- Drop its `PANEL_LOADERS` entry, its title in `panelRegistry.ts`, and its row in
  `companySections.ts`.
- Add `"teams-support": "teams-cases"` to `RETIRED_PANEL_ALIASES`, so a saved layout or an
  old link lands on Cases instead of a missing panel.
- Update the two tests that list the key: `CompanyNav.test.tsx` and
  `commandPaletteActions.test.ts`.

What Support promised was a shared inbox of customer mail with drafted replies. That is
not built and this does not build it. If it comes back, a case per customer thread is the
natural shape.

## Phase 3: needs a decision first

### 7. Status cards

**What it is.** A standing question in plain language with a living answer. An agent
compiles a search over the company's work, then keeps a Markdown summary current on a
timer or when matching issues change.

**It can work for us.** A card can name any agent, so the built-in Summarizer that cannot
run on our adapter is not needed.

**What it takes.** Flag `enableStatusCards`, BFF routes, a Home section built from
upstream's `StatusCardTile` and create dialog, and a skill
reference carrying the search query schema, because the compile prompt points at a bundled
skill our agents do not have.

**Spend.** Default is a run every 15 minutes per card. The per-card daily token cap would
probably never trip for us, because run cost is recorded after the summary is written. So
we would bound it with manual or reactive mode and the updates-per-hour limit.

**Needs your call.** Whether this is worth standing agent spend on every plan.

### 8. Paperclip budgets: recommend not using them

Our bridge returns token counts to Paperclip at run end with no cost. Paperclip records an
unpriced row and budgets never move. We could send a cost, but Paperclip rounds each run to
whole cents, so a run under half a cent records zero. With our models most runs are that
small. The openrouter-gate is the fence and Metronome is the ledger. Leave Paperclip
budgets off. If per-agent spend in Teams is wanted, read it from our own usage counters.

## Phase 4: fork release

One fork release, bundling:

- Rebase onto upstream `v2026.1001.0`, 77 commits. Useful to us: finalized run failures no
  longer re-notify on reconnect, a Postgres recovery fix, and answers queuing during an
  active run (described for native turns, unconfirmed for our adapter).
- Renumber our migrations. Upstream took 0280 to 0283, the numbers ours use.
- Review the new "execution harnesses default to full auto" change before taking it.
- Forward `decision.created` through the notification plugin, so decisions can push.

Aurora snapshot before the prod promote, as with the last base move.

## Hygiene, independent of the above

- Set `PAPERCLIP_TELEMETRY_DISABLED=1` on the Paperclip task. Telemetry is on by default
  and the stack does not set it.
- Feedback traces a user agrees to share upload to `telemetry.paperclip.ing`. Check what
  our feedback vote path sends for the sharing preference.
- The proxy denylist does not cover budgets, secrets, company skills, export or agent
  permission edits. Harmless while the company host has no DNS. Close it before that host
  ever gets a record.
- The skill and a test assert flag values "in both envs" that nothing enforces. The flag
  script makes them true by construction.

## Not doing

File Viewer, Isolated Workspaces, Environments, Low-Trust Review, Conference Room Chat:
all act on a filesystem Paperclip itself controls. Ours is in the microVM. Summaries need
the built-in Summarizer. Paperclip connectors, tool gateway, team catalog and company
skill sync duplicate rails we already built. Pipelines were not traced.

## Order

1. Decisions answerable in Inbox. Agents are filing them into a void today.
2. Decisions queue page.
3. Watchdog row and work mode finish. Small, same files.
4. Review stage gaps.
5. Flag script, native runner off, telemetry off.
6. Cases: flag, skill line, both panels.
7. External objects with the token sync.
8. Decisions and cases in the Activity feed, for the phone app.
9. Fork release.
10. Status cards, if approved.
