# Integrations v2 — Control-dashboard tab + expanded providers + per-agent Google accounts

**Status:** design spec (2026-06-01). Decomposes into three independently-shippable sub-projects. Implementation plans (TDD) follow per sub-project once the open decisions are resolved.

**Builds on:** PR #793 (the `mcp_server` wiring rail + `reconcile_integrations` + the curated registry; Stripe is the first provider). That PR is the data-layer foundation; this spec is the UX + breadth + per-agent layer on top of it.

---

## Vision

One **Integrations** tab in the control dashboard where an owner signs into every service their agents can use — and, for Google, assigns a *distinct account per agent* so each agent acts as its own real email identity.

Three threads, folded together:
1. **The tab** — a single, prominent home for connecting integrations (control dashboard), reusing the existing connect UI.
2. **Breadth** — expand the curated registry beyond Stripe (GitHub, Slack, Notion, Linear, Sentry) so there's actually something to sign into.
3. **Per-agent Google accounts** — the gog multi-account capability, wired so Friday acts as `friday@…` and Atlas as `atlas@…`.

Decisions already made (prior discussion): keep `/settings/connections` *and* add the control-dashboard tab (two entry points, one backend); keep the raw "MCP Servers" panel separate; add providers now.

---

## Background (grounded)

- **The registry is the extension point.** A provider is a `providers.yaml` block; connect/reconcile/reprovision/marketplace-gate/disconnect/UI all iterate it generically. `mcp_server.env` is a source-map (`{VAR: input.token}`), so a new paste-key provider is pure data.
- **The connect UI already exists** (`/settings/connections`): `ConnectionsList.tsx`, `ConnectButton.tsx`, `useConnections.ts` — and per the frontend mapping they **drop into a control panel as-is** (no /settings-specific styling, admin-gating is server-side).
- **gog is multi-account by design** (same author as OpenClaw). Upstream `skills/gog/SKILL.md` ships in the image and documents `gog auth add`, `gog auth list`, `--account <email>`, `GOG_ACCOUNT`. Verified live on the pinned `gog v0.11.0`: two tokens in one keyring → two selectable accounts. Today Isol8 materializes exactly **one** Google account per owner (single `(owner_id, "google")` slot), shared by all agents.

---

## Sub-project A — Control-dashboard Integrations tab (frontend)

**Goal:** an "Integrations" tab in the control dashboard that lists every curated provider with connect/disconnect, reusing the existing components.

**Design:**
- Add `{ key: 'integrations', label: 'Integrations', icon: Plug }` to `NAV_ITEMS` (`ControlSidebar.tsx:31-40`).
- Add `integrations: IntegrationsPanel` to `PANELS` (`ControlPanelRouter.tsx:25-34`).
- Create `panels/IntegrationsPanel.tsx` that wraps `<ConnectionsList canManage={…} />` with a title/description; derive `canManage` from Clerk `useOrganization().membership` for UX (server enforces anyway).
- `useConnections` shares the `/connections` SWR cache with the settings page — connect on either refreshes both (a feature).

**The one real gotcha — OAuth in a non-route panel.** `startOAuth` does `window.location.href = authorize_url`, which navigates away; the control dashboard isn't a route, so the OAuth callback can't land back on the tab. Options:
- **A1 (simplest):** the tab handles **paste-token** providers inline; **OAuth** providers (Google) show a "Connect in Settings →" link that deep-links to `/settings/connections` (which already handles the redirect). 
- **A2:** make the OAuth callback redirect back to `/chat?panel=integrations` and re-pull status on mount.

Recommend **A1** for v1 (paste-token is the bulk of the new providers; Google's per-agent flow in Sub-project C needs its own richer UI anyway).

**Files:** create `IntegrationsPanel.tsx`; modify `ControlSidebar.tsx`, `ControlPanelRouter.tsx`. Frontend-only; ships independently of B and C. No backend change.

---

## Sub-project B — Expanded curated providers (backend)

**Goal:** add real providers so the tab is useful. Each is a `providers.yaml` `mcp_server` block (source-map env) + a `validate` block. They surface in the tab automatically.

**Pinned, web-verified (2026-06-01):**

| Provider | Package (local stdio) | Env (source) | Validate | Official? |
|---|---|---|---|---|
| **GitHub** | `docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server` ⚠️ **not npx** | `GITHUB_PERSONAL_ACCESS_TOKEN: input.token` | `GET api.github.com/user` Bearer | ✅ vendor |
| **Slack** | `npx -y slack-mcp-server@latest --transport stdio` | `SLACK_MCP_XOXP_TOKEN: input.token` | `POST slack.com/api/auth.test` (branch on JSON `ok`, not HTTP status) | community |
| **Notion** | `npx -y @notionhq/notion-mcp-server` | `NOTION_TOKEN: input.token` (`ntn_…`) | `GET api.notion.com/v1/users/me` (+ `Notion-Version`) | ✅ vendor (may sunset local → remote OAuth) |
| **Linear** | `npx -y @tacticlaunch/mcp-linear` | `LINEAR_API_TOKEN: input.token` (raw, **no Bearer**) | `POST api.linear.app/graphql` `{viewer{id}}` | community (official is remote-OAuth) |
| **Sentry** | `npx -y @sentry/mcp-server@latest` | `SENTRY_ACCESS_TOKEN: input.token` | `GET sentry.io/api/0/organizations/` Bearer | ✅ vendor (local marked "WIP") |

**Per-provider mechanics:** Slack registers read-only tools by default (write needs `SLACK_MCP_ADD_MESSAGE_TOOL` opt-in — good); Notion requires the user to share pages with the integration (a post-connect onboarding step); Linear sends the raw key (no `Bearer`) — the `validate` block already supports `auth_scheme: ""`.

**The GitHub wrinkle (open decision — see below).** GitHub's official server is a **Docker image / Go binary**, not npx, so it does **not** ride the EFS npm-cache path (PR #785). Options: (a) `docker run` invocation — needs Docker-in-container, likely a non-starter on Fargate; (b) **pre-bake the `github-mcp-server` binary into the extended OpenClaw image** and use `command: github-mcp-server, args: ["stdio"]`; (c) use the **remote** GitHub MCP (OAuth) — different rail; (d) **defer GitHub**. Recommend **(b)** (bake the binary) or **(d)** for v1.

**Files:** `providers.yaml` (one block each) + a registry test per provider. Builds on #793's `mcp_server` rail — zero new mechanism. For GitHub option (b): the extended OpenClaw `Dockerfile`.

---

## Sub-project C — Per-agent Google accounts (backend + frontend)

**Goal:** an owner connects N Google accounts; each agent is assigned one and acts as it (`gog -a <its email>`). Each agent gets its own real Gmail/Calendar/Drive identity. **No per-agent env** — it's account *selection* driven by the agent's seeded identity, not credential *isolation*.

**The model (two axes, kept separate):** identity = `--account`/`GOG_ACCOUNT`/alias (per-agent); storage = one owner keyring on EFS (per-user, already done). gog has no agent→account registry — *we* drive `--account` from each agent's identity.

**The four gaps + fixes:**
1. **Disconnect nukes the whole keyring (the #1 correctness bug).** `gog_cli.remove()` does `shutil.rmtree(.../gogcli)` → disconnecting one account deletes all. Fix: per-account removal (`gog auth remove <email>`), with the target email threaded through the credential.
2. **Single connection slot.** Connect is keyed `(owner_id, "google")` and overwrites. Add an account/agent dimension (e.g. `google:<email>` or a connection_id + an optional `?agent_id=` on authorize, threaded through `oauth_dance` state) so N accounts coexist. The materializer is already per-email-additive (`gog auth tokens import` keys by email) — the only blocker is the overwrite-on-connect / nuke-on-disconnect, not gog.
3. **Agent→account assignment.** A small map `(owner_id, agent_id) → google_account_email` (new tiny DDB table, or attributes on the Paperclip agent record seeded in `paperclip_provisioning.provision_org`).
4. **Identity + skill seeding.** Reuse the existing per-agent file rail (`friday_identity` / `atlas_seed` write into `workspaces/{agent_id}/`): seed each agent's SOUL with *"your Google account is X; always run `gog -a X`, never bare `gog`"* + a `gog SKILL.md` baking that rule. The agent self-selects each session.

**Reprovision:** already handled — gog is `refresh_owner=actor`, the keyring lives on EFS and survives, `reconcile_integrations` correctly skips it. Only the identity/skill re-seed runs at provision (existing `write_user_configs` path). Marketplace-hired agents need the same seed at deploy time.

**Frontend:** the Integrations tab's Google row becomes a **list of connected accounts + their agent assignments** (not one connected dot) — connect another account, assign account ↔ agent. This is the richer Google UI the tab needs.

**Honest caveats:**
- **Selection by convention, not isolation** — all of an owner's agents share the keyring, so a misbehaving agent *could* pass another's `-a`. Fine for cooperating same-owner agents; hard isolation needs per-agent keyrings (against gog's design, out of scope).
- **Each account is a real Google login** — its own OAuth consent, ideally a real Workspace seat (~$7/agent/mo). A throwaway Gmail hammered by an agent can be **banned** (Google ToS/warming) — an ops/product concern.
- Watch the `.dockerignore *.md` strip (bit Atlas's seeded `.md` before) for the seeded gog `SKILL.md` / identity files.

---

## Sequencing

1. **A (tab)** — frontend-only, ships now, immediate UX win. Paste-token providers usable; Google → "Connect in Settings" until C.
2. **B (providers)** — backend, parallel to A. Slack/Notion/Sentry are clean npx adds; resolve the GitHub packaging decision; they light up in the tab as they land.
3. **C (per-agent Google)** — the meaty new capability; backend + the richer Google UI in the tab. Do after A+B so the tab exists to host it.

A and B are quick wins on the #793 foundation. C is the differentiated feature.

---

## Open decisions (resolve before the per-sub-project plans)

- **D1 — GitHub packaging:** bake the Go binary into the extended image (b), or defer GitHub (d)? (npx-cache path doesn't fit it.)
- **D2 — Provider set for v1:** all five, or start with the clean official-ish npx ones (Slack/Notion/Sentry) + revisit Linear/GitHub? Note Notion/Sentry/Linear's *official* servers are remote-OAuth; the local ones are community or vendor-"WIP", so longevity is a watch-item.
- **D3 — Google per-agent model:** confirm "each agent = a real Google account (Workspace seat)" is the intended model, with the abuse/warming risk accepted. Keep the transactional-email-identity layer as a documented fallback, or drop it?
- **D4 — OAuth in the tab:** A1 ("Connect in Settings →" for OAuth providers) for v1, or build A2 (OAuth redirect back to the panel)?
- **D5 — write access:** default the new providers to read-only (Slack write tools off, GitHub `GITHUB_READ_ONLY`, Notion read-only capability) and expose write as an explicit opt-in?

---

## Risks

- **GitHub doesn't fit the npx-EFS-cache path** — its packaging is the one non-uniform provider; don't let it block the clean npx ones.
- **gog disconnect-nukes-all** must be fixed *before* any multi-account connect ships, or disconnecting one account silently wipes them all.
- **Convention-not-isolation** for per-agent Google — document it; it's acceptable for trusted same-owner agents, not a security boundary.
- **Local-vs-remote MCP drift** — Notion/Sentry/Linear are steering users to remote OAuth servers; the local paste-key servers we pin may be deprioritized upstream. Pin versions; plan for an eventual OAuth-MCP rail (which the registry's source-map env already half-supports via `credentials.access_token`).
- **No new cross-stack infra** for A/B/C except GitHub-option-(b) (image bake) and C's small DDB assignment table.
