# Phase D Correction — Settings Consolidation + Sidebar Finalization

> Corrects PR #995 (which built a sidebar "System" section — forbidden by spec §3) by doing the *actual* spec Phase D: consolidate the config Control panels into `/settings`, leaving the sidebar work-only. Authority: `docs/superpowers/specs/2026-06-21-three-home-ia/design.md` §0/§3/§5/§5A/§8. Produced via a judged multi-agent design pass (3 proposals → scored → synthesized).

**Governing rule (§0 + §8):** *no surface is removed before its replacement is at parity.* Order: ADD Settings homes + verify → THEN remove SystemNav → THEN one-scroll fix → THEN dev-only flag gate. Increments 1–7 add only; nothing is deleted until Increment 8.

Repo: `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync`. Control panels: `apps/frontend/src/components/control/panels/`.

---

## Final design

### Sidebar — TALK + COMPANY only (NO System section; SystemNav is deleted)
```
TALK    My Agents {n} — roster rows (presence dot + open-work badge) → /chat/<agent>
COMPANY Dashboard, Inbox; Work: Issues, Routines, Goals; Company: Org chart, Skills†, Activity, Members
FOOTER  (pinned) ⚙ Settings → /settings ; Clerk account button
```
- **Costs CUT** (per the owner's explicit instruction — NOT a spec clause; spend lives in Settings → Usage). †`Skills` here = Paperclip company-skills roster, distinct from the engine-room `SkillsTab` (which moves to Settings).

### Settings nav — 6 intent-ordered groups (reuses the existing `NAV_SECTIONS` + `PANELS` pattern)
```
ACCOUNT            Profile (existing)
BILLING & USAGE    Billing (existing) · Usage  NEW← UsagePanel            [admin]
MODEL & PROVIDERS  Model & Providers  NEW← LLMPanel (+ inline CreditsSection)  [gw]
WIRING             Channels (existing) · Connections (existing href route) · MCP servers NEW← IntegrationsPanel [gw] · Webhooks (existing) · Desktop (existing)
ENGINE ROOM        Skills NEW← SkillsTab [gw] · Skill Proposals NEW← SkillProposalsPanel [gw] · Dreams/Memory NEW← DreamsPanel [gw] · Channel Status NEW← ChannelsStatusPanel [gw]
SELLER             Marketplace (existing)
```
`[admin]` = `ADMIN_ONLY_PANELS` + `useIsControlAdmin`; `[gw]` = needs `GatewayProvider`.
- **Connections vs MCP dedup:** `Connections` stays the OAuth-return **route** (`/settings/connections`, needs a real URL); `MCP servers` is the `mcp.servers` **tab** (`IntegrationsPanel`). One surface each — no double mount (§5).
- **Model & Providers** reuses `LLMPanel` *verbatim* (parity home). The §5A *atomic blend polish* is Phase E — homes reserved now, polish deferred.

### The load-bearing constraint — GatewayProvider in /settings
`/settings` is pure-REST today; six panels are gateway-bound (Skills, Skill Proposals, Dreams, Channel Status, MCP/IntegrationsPanel, the LLM health dot via `models.authStatus`). **Wrap `/settings/*` in the same `GatewayProvider` the chat surface uses** (in `app/settings/layout.tsx`, currently a `<>{children}</>` pass-through) so every panel mounts byte-identical to its Router form — *zero panel code changes*. This stands up a per-container WS in Settings (the real cost; parity-gated).

### One-scroll sidebar (fixes the missing-agents + double-scrollbar bug)
Header + New-chat + footer **pinned**; roster + CompanyNav + Gallery scroll as ONE `.sidebar-scroll { flex:1; min-height:0; overflow-y:auto }` region. Drop `min-h-0 overflow-y-auto` from CompanyNav's `<nav>` and change `.agent-list` from `flex:1; overflow-y:auto` → `overflow:visible`.

### Dev-only flag gate (closes the prod-leak the owner found)
In `lib/unifiedNav.ts`: `const IS_PROD = (process.env.NEXT_PUBLIC_API_URL ?? "").includes("//api.isol8.co");` Short-circuit `resolveUnifiedNav` → `false` if `IS_PROD`; guard `readUnifiedNav()`→`false` and `applyUnifiedNavParam()`→`null` so prod never reads/writes the localStorage key. `//api.isol8.co` excludes `api-dev.`/`api-staging.`/`localhost` (dev + Vercel previews keep full access).

---

## Increments (1–7 additive; 8–10 after the parity gate)

1. **Wrap `/settings` in `GatewayProvider`** — `app/settings/layout.tsx`. The enabler. Risk MEDIUM-HIGH (a 2nd WS opens in Settings). Must merge + verify live before any `[gw]` panel mounts.
2. **Settings nav scaffolding** — `app/settings/page.tsx`: expand `Panel` union + `NAV_SECTIONS` to the 6 groups; re-bucket the EXISTING panels only (no new components). Risk LOW.
3. **Settings → Usage** (`usage`, BILLING & USAGE) — register `UsagePanel`; admin-gate the nav row. Pure REST. Risk LOW.
4. **Settings → Model & Providers** (`providers`, [gw]) — register `LLMPanel` verbatim (embeds `CreditsSection`/`ModelRoutingCard`; no separate Credits row). Depends on #1. Risk MEDIUM.
5. **Settings → MCP servers + ENGINE ROOM** ([gw]) — register `IntegrationsPanel` (label "MCP servers", WIRING), `SkillsTab` (prop-less), `SkillProposalsPanel`, `DreamsPanel` (ENGINE ROOM). Depends on #1. Risk LOW-MEDIUM.
6. **Settings → Channel Status** (`channel-status`, [gw], ENGINE ROOM) — register `ChannelsStatusPanel` (key `channel-status` to avoid collision with WIRING→Channels). Depends on #1. Risk LOW.
7. **Parity verification gate** (NO removal) — live-verify each former panel renders + its RPC/REST writes its store, on dev with the flag on. §0/§8 checkpoint; Increment 8 may not proceed until this passes.
8. **Remove SystemNav** (flag-gated) — `ChatLayout.tsx`: delete both `{unifiedNav ? <SystemNav/> : null}` blocks + the import; delete `SystemNav.tsx`. After #7. Risk MEDIUM.
9. **One-scroll sidebar** — `ChatLayout.tsx`/`.css`/`CompanyNav.tsx` per the design. Risk MEDIUM.
10. **Dev-only prod flag gate** — `lib/unifiedNav.ts` per the design. Risk LOW.

**Insertion checklist per new Settings panel** (avoid the whitelist trap): (1) `Panel` union literal; (2) `NavItem` in the right group; (3) `NavIcon` case; (4) `PANELS` registration; (5) `initialPanel` deep-link whitelist; (6) `[gw]` needs #1 live, `[admin]` re-applies the gate.

## No-functionality-loss checklist
`overview`→COMPANY Dashboard `[verify]` · `agents`→roster+inspector · `sessions`→inspector `[verify]` · `usage`→Settings Usage(#3) · `llm`/`credits`→Settings Model & Providers(#4) · `integrations`→Settings MCP servers(#5) · `skills`→Settings Skills(#5) · `skill-proposals`→Settings(#5) · `dreams`→Settings Dreams(#5) · `channels`(status)→Settings Channel Status(#6) · `agent-inspector`→inspector. Already-in-Settings (re-bucketed only, #2): Profile, Billing, Channels, Connections, Webhooks, Desktop, Marketplace. **`ControlPanelRouter`/`ControlSidebar` are NOT deleted in Phase D** — they back the flag-off legacy shell; deletion deferred to the permanent flag-flip.

## Flagged decisions (defaulted; revisit only if rejected)
1. **Engine-room → Settings is a stated divergence from §5** (§5's engine-room home was the chat surface, which loses its panel host on switcher dissolution §8 → `/settings` is the surviving owner panel host; placed in a visually-separated ENGINE ROOM group). NOT justified by any "owner override" (no such clause exists — judge-verified).
2. **Costs CUT** — per the owner instruction, not the spec (§3 lists it). Spend → Settings Usage.
3. **ControlPanelRouter/Sidebar retained** through Phase D (flag-off prod still mounts them); deletion deferred to the flag-flip.
4. **Profile panel kept** as the registry fallback default (§5 hints at moving identity to the Clerk menu; non-blocking).
