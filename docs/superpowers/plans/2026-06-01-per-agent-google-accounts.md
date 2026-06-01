# Per-agent Google accounts (sub-project C) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** an owner connects multiple Google accounts; each agent acts as its *own* account (`gog -a <its email>`) via its seeded identity, while unassigned agents fall back to a default. Each agent gets its own real Gmail/Calendar/Drive identity — **no per-agent env, no new infra**.

**Architecture:** gog is already multi-account (`gog auth tokens import` keys by email; `--account` selects at runtime). The keyring lives on the owner's EFS (`refresh_owner=actor`, nothing in Secrets Manager). **The backend runs gog ONLY for keyring writes** — connect (add an account) and disconnect (remove one account). Everything else is plain **records** (the existing `integrations` DDB table holds the connected-account inventory + the agent→email assignments under namespaced sort keys, maintained at connect/disconnect/assign) and **identity seeding** (each agent's identity says "run `gog -a <its email>`"). Runtime selection happens where gog actually runs — the agent in its container — never the backend.

**Tech Stack:** Python 3.12 / FastAPI; `gog` CLI (baked into the backend + container images); `core/services/credentials/materializers/gog_cli.py`; `friday_identity`/`atlas_seed` per-agent EFS file rail; Next.js for the UI.

---

## Background (grounded 2026-06-01)

- **`gog_cli.write`** (`materializers/gog_cli.py`) already adds an account per call — `gog auth tokens import` is keyed by the fetched email, and `gog auth credentials set` (the OAuth client) is idempotent. So two OAuth connects = two accounts in one keyring. **Connect is already additive; the blocker is elsewhere.**
- **`gog_cli.remove`** does `shutil.rmtree(.../gogcli)` — **nukes the entire keyring**. Disconnecting one account would delete all. **This is the #1 fix.**
- **google is `refresh_owner=actor`** → `_retains_secret` is False → **nothing stored in Secrets Manager**; the EFS keyring is the sole custodian. So the account inventory = the keyring (`gog auth list`), not the secret store.
- **`integrations_repo`** is keyed `(owner_id, provider)` — one `google` row = "Google connected at all". We do NOT add per-account rows there.
- **`gog auth alias`** (verify exact subcommands first) lets us map a stable name → email (`gog auth alias set <agent_id> <email>`). This is the assignment store — on EFS, survives reprovision with the keyring, queryable via `gog auth alias list`. **No DDB table, no CDK.**
- **Identity rail**: `friday_identity.pre_stage_friday_identity` / `atlas_seed` render templates and `workspace.write_file_if_missing(owner_id, "workspaces/{agent_id}/<file>", content)`. Each agent already owns `workspaces/{agent_id}`. The gog skill (`skills/gog/SKILL.md`) ships in the image and documents `--account`; we add the per-agent *which* account via identity.
- **The OAuth dance** (`oauth_dance.start/exchange`) keys state by `(owner_id, provider)`; `exchange` returns `(owner_id, provider, credential)` and the materializer derives the email from the Gmail profile. No agent dimension needed at connect (the email is the account; assignment is a separate step).

---

## Decisions (resolve at review)

- **D1 — default + per-agent override.** Unassigned agents use the owner's **default** Google account (the first connected — preserves today's single-account behavior, where all agents share one account). Assigned agents use their override. *(Confirm this is the model.)*
- **D2 — selection by convention, not isolation.** All of an owner's agents share the keyring, so an agent *could* pass another's `-a`. Acceptable for cooperating same-owner agents; hard isolation is out of scope.
- **D3 — backend gog is minimal (keyring writes only); inventory + assignments are records in the EXISTING `integrations` table** (no new table, no CDK). Single-table design: `owner_id` PK + SK `google#account#<email>` (inventory) / `google#assign#<agent_id>` (assignment → email). Safe — the only `list_for_owner` consumers ignore non-registry rows (connections-list iterates registry providers; reconcile skips unknown providers via the #793 orphan-row guard). The backend never runs `gog auth list`/`alias`.
- **D4 — first PR = backend core (Tasks 1–6); the polished tab UI (Task 7) can be the same PR or a follow-up.**

---

## File structure

| File | Change | Responsibility |
|------|--------|----------------|
| `core/services/credentials/materializers/gog_cli.py` | Modify | `remove` → per-account `gog auth remove <email>`; keep `write` additive. (The only backend gog calls.) |
| `core/repositories/google_accounts_repo.py` | **Create** | Record store over the EXISTING `integrations` table (no new table, no gog): `add_account`/`remove_account`/`list_accounts(owner)` (sk=`google#account#<email>`), `set_assignment`/`remove_assignment`/`list_assignments(owner)` (sk=`google#assign#<agent_id>`, attr `email`), `resolve_account_for_agent(owner, agent_id)` (assignment → else default). |
| `routers/connections.py` | Modify | Disconnect a *specific* Google account (`?account=<email>`); connect stays additive. |
| `routers/google_accounts.py` | **Create** | `GET /google/accounts` (list + assignments), `POST /google/accounts/{email}/assign` `{agent_id}`, `DELETE` the assignment. Org-admin-gated. |
| `core/services/friday_identity.py` + `atlas_seed/` | Modify | Seed each agent's Google-account file (the `gog -a <email>` rule) from its assignment; written (not write-if-missing) so reassignment updates it. |
| `core/containers/ecs_manager.py` (`write_user_configs`) | Modify | On provision, re-seed every agent's Google-account file from `list_assignments`. |
| `apps/frontend/.../IntegrationsPanel.tsx` (or a Google sub-component) | Modify | The Google row → list connected accounts + per-agent assignment UI. (Task 7.) |

---

## Task 1: Per-account gog disconnect (the #1 fix)

**Files:** `materializers/gog_cli.py`; test `tests/credentials/test_materializer_gog_cli.py`.

- [ ] **Step 1: Verify the exact gog command** — `gog auth --help` / docs: confirm the per-account removal subcommand (likely `gog auth remove <email>` or `gog auth tokens remove <email>`). Pin it.
- [ ] **Step 2: Failing test** — `remove(owner, "google", {"email": "a@x"})` with two accounts (`a@x`, `b@x`) in the keyring removes only `a@x`; `b@x` survives. (Mirror the existing gog_cli test harness — it monkeypatches `subprocess.run`/`owner_efs_root`.)
- [ ] **Step 3: Implement** — `remove` runs `gog auth remove <email>` (email from `credential["email"]`) with the owner's `XDG_CONFIG_HOME`, instead of `shutil.rmtree`. Keep a guarded full-cleanup only when no email is given (legacy disconnect-all).
- [ ] **Step 4: Run** — `uv run pytest tests/credentials/test_materializer_gog_cli.py -v` → PASS.
- [ ] **Step 5: Commit** — `fix(credentials): gog disconnect removes one account, not the whole keyring`.

## Task 2: The Google record store (existing `integrations` table, no gog, no new table)

**Files:** create `core/repositories/google_accounts_repo.py`; test `tests/repositories/test_google_accounts_repo.py`.

- [ ] **Grounding:** read `core/repositories/integrations_repo.py` — it uses the `integrations` table (`owner_id` PK, `provider` SK). The new repo writes to the SAME table with namespaced SKs, so no schema/CDK change.
- [ ] **Repo (no gog):** over the `integrations` table — `add_account(owner, email)` / `remove_account(owner, email)` / `list_accounts(owner)` (SK `google#account#<email>`, attr `connected_at`); `set_assignment(owner, agent_id, email)` / `remove_assignment(owner, agent_id)` / `list_assignments(owner)` (SK `google#assign#<agent_id>`, attr `email`); `resolve_account_for_agent(owner, agent_id)` → the assignment's email, else the default (oldest account by `connected_at`), else None. Query by `owner_id` and filter SK prefix. Mock DDB in tests (match the `integrations_repo` test idiom). Emails never contain `#`, so the SK parses cleanly.
- [ ] Commit — `feat(credentials): google account inventory + assignment records`.

## Task 3: Connect records the account (additive)

**Files:** `routers/connections.py` (the google OAuth callback path); test.

- [ ] On a successful Google connect, the materializer already imports the account into the keyring per-email (additive — a 2nd connect ADDS, doesn't overwrite). ADD: write the account record (`google_accounts_repo.add_account(owner, email)`) using the email the materializer derived. (Surface the email from the materializer/callback.) Test: two connects → two account records + two keyring accounts; the `integrations_repo` "google" row stays single (provider-level "connected").
- [ ] Commit — `feat(credentials): record each connected Google account`.

## Task 4: Per-account disconnect endpoint

**Files:** `routers/connections.py`; test `tests/unit/routers/test_connections.py`.

- [ ] `DELETE /connections/google?account=<email>` removes that one account: `gog auth remove <email>` (keyring) + `google_accounts_repo.remove_account` + remove any assignments pointing at it (and re-seed those agents back to default). Without `?account`: disconnect-all (now safe per Task 1). Org-admin gated. Test both.
- [ ] Commit — `feat(credentials): disconnect a single Google account by email`.

## Task 5: Assignment API

**Files:** Create `routers/google_accounts.py` (register in `main.py`); test.

- [ ] `GET /google/accounts` → `{accounts: [...], assignments: {agent_id: email}}` (from the repo). `POST /google/accounts/{email}/assign {agent_id}` → `set_assignment` + re-seed that agent's identity file (Task 6). `DELETE …/assign/{agent_id}` → `remove_assignment` + re-seed (back to default). Org-admin gated, owner-scoped (the email must be one of the owner's connected accounts).
- [ ] Commit — `feat(integrations): per-agent Google account assignment API`.

## Task 6: Identity seeding (the `gog -a <email>` rule)

**Files:** `core/services/friday_identity.py` (+ a shared seeder), `atlas_seed/`, `ecs_manager.write_user_configs`; test.

- [ ] A `seed_agent_google_identity(owner, agent_id, email)` that writes `workspaces/{agent_id}/GOOGLE.md` (or appends to the agent's SOUL) with: *"Your Google account is `<email>`. For ANY Google action (Gmail/Calendar/Drive) run `gog -a <email> …` — never bare `gog`, never another agent's account."* Use a plain `write_file` (updateable on reassignment), NOT write-if-missing.
- [ ] Wire it: Friday/Atlas seed call it with `resolve_account_for_agent`; `write_user_configs` re-seeds every agent from `list_assignments` on (re)provision; marketplace hire seeds the hired agent. Test the file content + that reassignment overwrites.
- [ ] Commit — `feat(credentials): seed each agent's Google account identity`.

## Task 7 (UI — same PR or follow-up): per-account + assignment UI

**Files:** `apps/frontend` Integrations tab Google row.

- [ ] The Google row expands to: connected accounts (from `GET /google/accounts`) with per-account disconnect, a "Connect another account" action (OAuth), and a per-agent dropdown to assign an account to each agent. Lint + build green.
- [ ] Commit — `feat(frontend): Google multi-account + per-agent assignment UI`.

---

## Final verification + live smoke
- `uv run pytest tests/credentials tests/services tests/unit/routers/test_connections.py -v`.
- **Live (the real test):** connect two Google accounts; assign account A→Friday, B→Atlas; in `/chat`, ask Friday to "check my latest email" → it runs `gog -a A …` and reads A's inbox; ask Atlas → reads B's. Disconnect A → B + Atlas unaffected (Task 1).

## Risks / notes
- **Verify the gog `auth remove` subcommand first** (Task 1 grounding) — the one backend gog call the plan adds; a wrong subcommand is a silent failure. Pin against `gog auth --help` on the pinned binary. (No `gog auth list`/`alias` — those are records now.)
- **Convention, not isolation** (D2) — document in the seeded identity ("never another agent's account").
- **Reprovision:** keyring + aliases live on EFS → survive a config-rewrite; a full EFS-wipe needs re-connect + re-assign (same as gog today). Re-seeding identity from `list_assignments` runs on every provision.
- **Google ToS/warming** — a real Google account per agent is the model (D1); throwaway accounts hammered by agents can be banned. An ops concern, not a code one.
