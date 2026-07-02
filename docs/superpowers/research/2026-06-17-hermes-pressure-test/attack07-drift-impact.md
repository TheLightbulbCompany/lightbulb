# Attack 07 — Drift Impact (Hermes runtime option)

**Target claim (spec §10 Risk #1):** "Hermes release cadence — v0.10→v0.14 in 30
days. Pin + lag 1-2 releases; budget **1-2 days/bump** for the same audit pattern
as OpenClaw, including the `AUTH_STORE_VERSION` / event-schema checks."

**Verdict: NUANCED — leaning CRACKED on the per-bump *number*, CONFIRMED on the
*shape*.** The spec is right that pinning is the correct posture and that no
load-bearing claim is *structurally* destroyed by moving forward — but its
"1-2 days/bump" estimate is calibrated to the wrong unit of change. It treats a
Hermes "release" as an OpenClaw point-release (1-2 P0s), when a single Hermes
release in the audited window carried **1304 commits** and the v0.14.0 release
itself was **808 commits / 1393 files / 165k insertions** (`RELEASE_v0.14.0.md`).
Pinning a *recent* Hermes (the sensible choice) re-introduces exactly the
hard invalidation the spec's own basis dodges: mandatory `API_SERVER_KEY`.

All facts below read directly from `/Users/prasiddhaparthsarthy/Desktop/hermes-agent`
@ `4c46c35ed` (basis) vs `upstream/main` @ HEAD 2026-06-17.

---

- **The "1-2 days/bump" estimate is mis-unitized — CRACKED on the number.** Spec
  Risk #1 maps cadence to OpenClaw's audit pattern (point releases landing 1-2
  P0s, e.g. 5.12→5.28, 6.1→6.6 per project memory). But Hermes releases are
  *enormous*: `git rev-list v2026.5.16..v2026.5.28` = **1304 commits**;
  `v2026.5.29.2..v2026.6.5` = **895 commits**; `v2026.6.5..upstream/main` = **1141
  commits**; and `RELEASE_v0.14.0.md` self-reports **808 commits / 633 PRs / 1393
  files / 165,061 insertions / 12 P0 + 50 P1** *in one release* (v0.13→v0.14).
  An audit that found ONE boot-blocker per OpenClaw point-bump (memory
  `project_openclaw_bump_5.12_to_5.28_impact`) will face 50-P1-scale diffs per
  Hermes bump. "1-2 days" to diff the four load-bearing files across a 1000+
  commit release and re-run the auth/event-schema checks is optimistic by a
  factor of several; budget **3-5 days/bump minimum**, more on a release that
  touches the auth or config surfaces.

- **Pinning a recent Hermes RE-ARMS the one hard invalidation — CONFIRMED.** The
  spec's basis (`api_server.py:3434`) only refuses keyless binds when
  `is_network_accessible(self._host)`; loopback with no key is allow-all
  (`_check_auth:750-751`, "No key configured — allow all"). Upstream commit
  `1a9ef8314 fix(security): require API_SERVER_KEY before dispatching API server
  work` flips this: upstream `api_server.py:4223-4225` now does `if not
  self._api_key: ... "Refusing to start: API_SERVER_KEY is required for the API
  server"` — **mandatory on every bind, loopback included** (a sibling commit
  `989b950fb` also enforces non-loopback). The spec's own design §3 *already*
  binds on the container IP with `API_SERVER_KEY` set, so the architecture
  survives — but any informal "talk to a keyless loopback Hermes like the
  keyless OpenClaw gateway" mental model is dead the moment you pin >`v2026.5.28`.
  The per-container key-gen + Secrets-Manager + Bearer-injection plumbing is
  unavoidable, not optional, and the spec must treat it as *day-one scope*, not a
  drift contingency.

- **Config schema is a moving target but won't brick — CONFIRMED SOFT.**
  `_config_version` is **23** at basis (`config.py:1692`) → **29** upstream
  (`config.py:2572`): 6 forward migrations (e.g. `887295ba5` custom-provider
  model maps v11→v12). Unlike OpenClaw's strict-zod brick (memory
  `reference_openclaw_config_brick`), Hermes ships a forward `check_config_version`
  runner, so a basis-shaped hand-written `config.yaml` auto-upgrades on boot
  rather than crash-looping. The Isol8 pattern of hand-writing the runtime config
  (mirroring `openclaw.json`) is *fragile but safe* — the real cost is that the
  spec's "write a pinned config.yaml" surface drifts ~1.2 schema versions/month
  and each bump needs a re-verify that the generated shape still round-trips.

- **The load-bearing "no rewrite" claims genuinely survive forward — CONFIRMED.**
  This is the spec's saving grace and it checks out: (a) `AUTH_STORE_VERSION`
  stayed at **1** at both refs (`auth.py:63` basis, `auth.py:66` upstream) — the
  spec's per-bump lockstep check (§7) is real but cheap and currently a no-op; (b)
  SSE framing markers survive verbatim at upstream (`text/event-stream`, `data:
  [DONE]`, `event: hermes.tool.progress`, `run.completed` all present in
  `git show upstream/main:api_server.py`); (c) no `Last-Event-ID`/resume was
  added, so the spec's non-resumability assumption (Risk #4) holds even at HEAD;
  (d) all four cited files still exist with no renames. The drift is *additive
  and migratory*, not a structural rewrite — so the 10-15 eng-week build estimate
  isn't invalidated, only the *maintenance tail* is under-budgeted.

- **"Pin + lag 1-2 releases" is itself risky given release semantics — NUANCED.**
  Lagging 1-2 Hermes releases means sitting **1000-2600 commits** behind HEAD
  (1304 + 895 between recent tags). For a security-sensitive surface (the API
  server is the entire backend↔container control plane), deliberately lagging
  behind a repo that ships `fix(security)` commits at this rate
  (`1a9ef8314`, `989b950fb`, `a04854800`, `469cd16fe` all post-basis SSRF/
  timing/tar-traversal/key hardening) means knowingly running known-vuln runtime
  in beta. The spec inherits this tension and doesn't resolve it: you cannot both
  "lag 1-2 releases for stability" AND stay current on security fixes when each
  release is a 1000-commit superset. OpenClaw's point-release cadence lets you
  cherry-pick; Hermes's does not.

- **Bonus: the basis itself is mislabeled, inflating confidence in all line
  cites — CONFIRMED.** `git describe` on the basis = `v2026.5.16-201-g4c46c35ed`:
  HEAD is **+201 commits past** the actual v0.14.0 / `v2026.5.16` tag (basis date
  2026-05-17, one day *after* the v0.14.0 release). Every "line numbers match
  v0.14.0" claim in the spec is already off by 201 commits of churn before
  upstream drift even enters. Anyone re-pinning to the *real* `v2026.5.16` tag —
  or to any other tag — gets different offsets than every file:line the spec
  cites, so the spec's citations are a snapshot of an untagged intermediate, not
  a reproducible reference point. This compounds the maintenance burden: there is
  no clean tag the spec's evidence actually corresponds to.
