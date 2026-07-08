# Isol8 Skills & Capabilities — Mechanism Map

**Status:** reference (understanding only — no target model / decision here)
**Date:** 2026-07-08
**Provenance:** Traced from source across the Isol8 repo, the Paperclip clone (`paperclip/`), and the
OpenClaw runtime (`openclaw/openclaw` GitHub + the pinned fork tag `v2026.6.10-isol8.2`). Two
adversarially-verified agent passes; the four residual facts were closed by hand. Citations are
`file:line` in this repo, `paperclip/…` in the clone, or `openclaw src/…` for runtime source.

---

## TL;DR

"An agent capability" in Isol8 is spread across **seven skill mechanisms + one connection (MCP) axis**,
and they do not coordinate. The unifying fact is how the OpenClaw **runtime** consumes skills:

> OpenClaw **discovers skills by filesystem scan** over a fixed 6-root precedence ladder and loads any
> `SKILL.md` it finds. `openclaw.json` **tunes** that loader (enable/apiKey/limits/allowlist) — it never
> enumerates the skills. Every other mechanism is just a different **writer** into one of those roots (or
> into the tuning config).

Isol8 sets exactly **one** skills key at provision: `skills.install.nodeManager="npm"`
(`apps/backend/core/containers/config.py:1178-1182`). Everything else is default.

---

## 1. The runtime substrate: filesystem-scan discovery

At session start the OpenClaw gateway snapshots skills by scanning a **6-root precedence ladder**
(`openclaw src/skills/loading/{bundled-dir,local-loader,config,source}.ts`; docs `docs/tools/skills.md`):

| # | Root | Source label | Who writes here |
|---|------|--------------|-----------------|
| 1 | `<workspace>/skills/` | `workspace` | marketplace WORKSPACE skills; workshop-applied skills |
| 2 | `<workspace>/.agents/skills/` | `workspace` | — |
| 3 | `~/.agents/skills/` | `workspace` | — |
| 4 | `~/.openclaw/skills/` (managed) | `managed` | `skill_cli` credential skills |
| 5 | `/app/skills/` (image) | `bundled` | image build (base fork + `paperclip`) |
| 6 | `skills.load.extraDirs[]` | `extra` | (Isol8 sets none) |

Roots **#1 and #4 are scanned by default** — no `skills.load.extraDirs` entry needed. This is load-bearing:
it's why marketplace and `skill_cli` skills actually load even though Isol8 never registers `extraDirs`.

`loadSkillsFromDirSafe` finds any `<name>/SKILL.md` up to 6 levels deep, `parseFrontmatter` reads
`name`+`description`, and `shouldIncludeSkill` filters an **already-discovered set** by
`skills.entries[name].enabled`, `allowBundled` (bundled source only), and
`metadata.openclaw.requires{bins,env,config}` gating. Eligible skills are compiled into a compact
`<available_skills>` prompt block (**name + description + location only**, ~24 tokens each); the body /
`references/` / `scripts/` are read lazily on invocation (progressive disclosure).

### The `openclaw.json` `skills` block (tuning only)

`allowBundled[]` · `load{extraDirs,watch}` · `install{nodeManager}` · `limits{maxSkillsInPrompt,
maxSkillsPromptChars,…}` · `workshop{autonomous,approvalPolicy,…}` · `entries{<name>:{enabled,apiKey,env,config}}`.
Plus per-agent `agents.list[].skills: string[]` and `agents.defaults.skills` — a **visibility allowlist**
(replaces, doesn't merge; explicitly *not* a shell-time authorization boundary). **Isol8 populates none of
the per-agent allowlists**, so every discovered/eligible skill is visible to every agent in the container.

---

## 2. The seven mechanisms + the MCP axis

| # | Mechanism | Actor | Writes | Reaches gateway agent? |
|---|-----------|-------|--------|------------------------|
| 0 | **Runtime loader** | OpenClaw runtime | — (scans 6 roots) | it *is* the runtime |
| 1 | **Bundled / baked skills** | image build | `/app/skills/<name>/` (root 5) | ✅ fleet-wide, no config needed |
| 2 | **SkillsTab** (control "Skills") | **human** | `skills.install` (npm on box); `skills.update`→`skills.entries[].enabled`; `create` flow | ✅ writable via RPC |
| 3 | **Skill Workshop** | **agent authors → human applies** | `<workspace>/skills/<key>/SKILL.md` (root 1) | ✅ (see §4) |
| 4 | **`skill_cli` credential materializer** | **backend, on integration connect** | `~/.openclaw/skills/<key>/SKILL.md` (root 4) + `skills.entries[key]={enabled,apiKey}` | ✅ — the skill↔integration bridge |
| 5 | **Marketplace deploy** | seller→admin→buyer | untar → `workspaces/{id}/skills/` (root 1) + deep-merge `skills.entries` + union `allowBundled` | ✅ direct to EFS+config |
| 6 | **Paperclip Company Skills** | human board/CEO **or agents** | `company_skills` (Aurora) + `desiredSkills` on agent row | ❌ **INERT for `openclaw_gateway`** |
| — | **MCP / Integrations** | human owner | `mcp.servers` in `openclaw.json` (global-only; nested inert) | ✅ — *connection* axis, not a skill |

Key file anchors:
- **Bundled:** `apps/infra/openclaw/Dockerfile:396` (`COPY skills/paperclip /app/skills/paperclip`); base fork bundled set below.
- **SkillsTab:** `apps/frontend/src/components/control/panels/SkillsTab.tsx` (`skills.status` read; `skills.install`/`skills.update`/`create` mutations over the raw gateway RPC).
- **Workshop:** `apps/frontend/src/components/control/panels/SkillProposalsPanel.tsx` (+ mounted in Settings); `skills.proposals.*` RPC; OpenClaw `src/skills/workshop/{service,store,scanner,policy}.ts`, `src/agents/tools/…skill-workshop-tool.ts`.
- **`skill_cli`:** `apps/backend/core/services/credentials/materializers/skill_cli.py:56-89`; provider registry `apps/backend/core/services/credentials/…/providers.yaml` (`materializer: skill_cli`, today: Vercel); UI pairing `apps/frontend/src/components/control/panels/skill-integrations.ts`.
- **Marketplace:** `apps/backend/core/services/agent_export.py` (`classify_skill`, `SkillBucket`), `catalog_service.py:249 deploy_from_artifact`, `config_patcher.py:463-583 apply_deploy_aux_mutations`.
- **Paperclip:** `paperclip/server/src/adapters/registry.ts:406-415` (`openclawGatewayAdapter`, `requiresMaterializedRuntimeSkills:false`, no `listSkills`/`syncSkills`); `paperclip/server/src/routes/agents.ts:1375-1389,1766-1780` (`buildUnsupportedSkillSnapshot`); Isol8 read-only proxy `apps/backend/routers/teams/skills.py`.
- **MCP:** `apps/backend/routers/integrations.py`; `config.py:1352-1372`; re-injected at provision via `core/containers/ecs_manager.py:2225 build_connected_integration_servers`.
- **RPC proxy (unfiltered):** `apps/backend/routers/websocket_chat.py:1525-1608` forwards every method except a few `agents.*`/session guards verbatim via `pool.send_rpc`. (The old CLAUDE.md names `routers/container_rpc.py` / `control_ui_proxy.py` no longer exist.)

---

## 3. Runtime-reach truth table

| Writer | Root / config it writes | Auto-loads? | Reprovision-durable? |
|--------|-------------------------|-------------|----------------------|
| Bundled image | `/app/skills` (root 5) | ✅ (image) | ✅ (rebaked each image) |
| SkillsTab install | npm install + `skills.entries[].enabled` | ✅ | ⚠️ entries dropped on full write |
| Workshop apply | `<workspace>/skills/` (root 1) | ✅ + hot-reload | ✅ file survives POST; ❌ on DELETE |
| `skill_cli` connect | `~/.openclaw/skills/` (root 4) + `skills.entries[key]` | ✅ | ❌ entries dropped, file orphaned (see §6d) |
| Marketplace deploy | `workspaces/{id}/skills/` + `skills.entries`/`allowBundled` | ✅ | ❌ entries/allowBundled dropped on full write |
| Paperclip `desiredSkills` | Aurora only | ❌ never reaches EFS | n/a (never materialized) |
| MCP connect | `mcp.servers` | ✅ | ✅ replayed at provision |

---

## 4. The Skill Workshop (agent self-improvement)

The only agent-authored, self-mutating rail. Lifecycle (`openclaw src/skills/workshop/`):

- **Author:** agent calls the built-in `skill_workshop` tool (`create|update|revise`) → a **pending** proposal
  (a draft in the workshop state dir, not yet a live skill). Authoring is **not** approval-gated.
- **Scan:** security scanner. Markdown body → `SKILL_CONTENT_RULES` (prompt-injection / pipe-to-shell /
  secret-exfil = **critical**; `rm -rf`, `chmod 777` = warn). Code/support files → `dangerous-exec`,
  `dynamic-code-exec`, `env-harvesting`, `exfiltration`, `obfuscated-code`. Fail → `quarantined`.
- **Statuses:** `pending → applied | rejected | quarantined | stale` (`stale` is auto-set drift detection).
- **Apply:** writes `<workspaceDir>/skills/<skillKey>/SKILL.md` via `writeWorkspaceSkill`, then
  `bumpSkillsSnapshotVersion` hot-reloads → **live immediately**. Only `WRITABLE_WORKSPACE_SOURCES`
  (`openclaw-workspace`, `agents-skills-project`) are targetable — bundled/marketplace/read-only cannot be
  modified. Apply/reject/quarantine are approval-gated (`approvalPolicy="pending"` default).
- **Isol8 wiring:** panel drives `skills.proposals.list/inspect/apply/reject` over the **unfiltered** gateway
  proxy — **no backend router, no policy layer**. Isol8 sets no `skills.workshop` config → OpenClaw defaults
  (approval=pending, autonomous **off**).

---

## 5. Why Paperclip Company Skills is inert here

Paperclip has a full governance model (app catalog + per-company `company_skills` library + per-agent
`desiredSkills` + RBAC), surfaced **read-only** in Teams (`routers/teams/skills.py`). But for
`openclaw_gateway` agents it reaches nothing:

- The adapter descriptor declares `requiresMaterializedRuntimeSkills:false` and has **no** `listSkills`/`syncSkills`
  (`paperclip/server/src/adapters/registry.ts:406-415`); there is no `openclaw-gateway/src/server/skills.ts`.
- `POST /agents/:id/skills/sync` dead-ends in `buildUnsupportedSkillSnapshot` → *"This adapter does not
  implement skill sync yet."* `desiredSkills` persists to `adapterConfig.paperclipSkillSync` in Aurora and stops there.
- **Only Isol8's backend can write EFS**, and it has **no bridge** from `company_skills` to `openclaw.json`/EFS
  (backend grep for `desiredSkills`/`paperclipSkillSync`/`company_skills` = 0 hits).
- The adapter is **invoke-only** (WebSocket `agent` + `agent.wait`; "cwd is the only persistence boundary — no
  git push from runtime").

Paperclip's local adapters (cursor/opencode/etc.) *do* materialize skills — by symlinking into the CLI's skills
home **on the Paperclip host**, never into Isol8 EFS. So the mechanism's runtime-reach is adapter-polymorphic;
it is simply dead on the one adapter Isol8 uses.

---

## 6. The four closed facts

**(a) Bundled skill set — CONFIRMED (high).** 58 skills at `/app/skills` = 57 from base fork
`v2026.6.10-isol8.2` + `paperclip`. Base set (57): `1password, apple-notes, apple-reminders, bear-notes,
blogwatcher, blucli, camsnap, canvas, clawhub, coding-agent, diagram-maker, discord, eightctl, gemini,
gh-issues, gifgrep, github, gog, goplaces, healthcheck, himalaya, imsg, mcporter, meme-maker, model-usage,
nano-pdf, node-connect, node-inspect-debugger, notion, obsidian, openai-whisper, openai-whisper-api, openhue,
oracle, ordercli, peekaboo, python-debugpy, sag, session-logs, sherpa-onnx-tts, skill-creator, slack,
songsee, sonoscli, spike, spotify-player, summarize, taskflow, taskflow-inbox-triage, things-mac, tmux,
trello, video-frames, voice-call, wacli, weather, xurl`. Runtime eligibility of many depends on binaries the
extended image bakes. (Fork's release Dockerfile does an unfiltered `COPY /app/skills`; image == source.)

**(b) Workshop tool enablement — CONFIRMED (high), at deployed tag.** Friday/Atlas/custom agents can author
proposals on demand: `createSkillWorkshopTool` registers unconditionally except in sandbox mode; Isol8 runs
`tools.profile="full"` + `tools.allow=["*"]`, `tools.deny=["canvas","nodes","cron"]` (workshop not denied),
no agent sandboxed. Off by default (but non-blocking): **autonomous auto-capture**
(`skills.workshop.autonomous.enabled=false`) and **auto-apply** (`approvalPolicy="pending"` gates apply/reject,
not create/update/revise).

**(c) Panel is Friday-only — RESOLVED.** `SkillProposalsPanel` sends **no `agentId`**; `send_rpc`
(`connection_pool.py:586`) injects none over the single per-owner operator connection. OpenClaw's
`resolveSkillsAgentWorkspace` therefore falls back to `resolveDefaultAgentId(cfg)`, and Isol8 marks the CEO
(**Friday**) as default (`config.py:580`: "role='ceo' is marked default"). **The panel shows only Friday's
proposals**; Atlas's and custom agents' proposals are unreachable through it.

**(d) Reprovision is not skill-durable — RESOLVED (robustness gap).**
- Full **POST** reprovision **overwrites** `openclaw.json` (`config.py:1477-1495` builds fresh + `write_text`) →
  `skills.entries` + `allowBundled` (`skill_cli` *and* marketplace) **dropped to base and not replayed**. By
  contrast MCP servers **are** replayed (`ecs_manager.py:2225`). No skills replay hook exists.
- **DELETE** wipes EFS entirely (`delete_user_dir` → `shutil.rmtree`, `workspace.py:574`) → DELETE+POST is a
  clean slate (no orphans, but all workspace/credential skills gone until reconnect/redeploy).
- **POST-only** (EFS preserved) **orphans** the files: e.g. `~/.openclaw/skills/vercel/SKILL.md` still loads by
  filesystem scan but with its `skills.entries.vercel.apiKey` gone → **present but unauthenticated/inert**.
- **PATCH** (`config_patcher` deep-merge) preserves `skills.entries` — the safe path.
- Same failure class as the documented "reprovision wipes channels."

---

## 7. Tensions for later reconciliation (diagnosis only — no solution)

1. **`skills.entries` is a shared, uncoordinated write target** — SkillsTab enable, `skill_cli` connect, and
   marketplace deploy all write it, and a full provision overwrites it. No single owner of "this agent's skills."
2. **The agent-governance lane doesn't function** — Paperclip Company Skills (the only catalog/RBAC/assignment
   model) is 100% inert for gateway agents. An "agents equip agents" story has no runtime path today.
3. **Skill authoring exists twice** — Workshop (scanner + approval + hot-reload) vs SkillsTab `create` /
   `skill-creator` bundled skill (ad-hoc) — different safety postures.
4. **Per-agent scoping is aspirational** — `agents.list[].skills` is never set → all skills globally visible to
   all agents; the Workshop panel is Friday-only. There's no real per-agent skill boundary.
5. **No reconciler treats skills as desired state** — the root cause behind #2 and #4d. MCP has a
   provision-time replay; skills have none.

---

## 8. Stale-doc corrections found during this pass

- RPC proxy is `routers/websocket_chat.py`, **not** the `routers/container_rpc.py` / `control_ui_proxy.py`
  named in CLAUDE.md (those files are gone).
- The control skills panel is `SkillsTab.tsx`, **not** `SkillsPanel.tsx`.
- There are **three** Isol8 backend skill-write paths (provision base, marketplace deploy, `skill_cli` connect),
  not "only marketplace."

## 9. Residual caveats

- (a) derived from fork source at the deployed tag, not `ls` inside the private running image (mechanically
  equivalent given the unfiltered `COPY`).
- (b)/(c) OpenClaw runtime lines fetched from `openclaw/openclaw@main`; the pinned fork may differ by a few
  lines but the mechanisms match.
