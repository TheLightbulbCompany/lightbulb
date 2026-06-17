# Map 05 — Hermes Tools / Skills / Sandbox (pressure-test)

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
All file:line refs are in that tree. This map pressure-tests the spec's Q7
(skill install) and Q8 (workspace/sandbox) sections against the actual code.

---

## 1. Tool execution model (registry + dispatch)

- **Self-registering flat registry, no per-tool sandbox layer.** Every tool
  module calls `registry.register(name, toolset, schema, handler, ...)` at
  import time (`tools/registry.py:234-305`); `discover_builtin_tools` AST-scans
  `tools/*.py` for a top-level `registry.register(` call and imports the module
  (`registry.py:42-74`). Dispatch is a thin `entry.handler(args, **kwargs)` with
  a blanket try/except that JSON-wraps errors (`registry.py:390-416`). There is
  **no capability/permission layer between the model and a handler** — if a tool
  is in the active toolset, the model calls it directly. The only gate is
  `check_fn()` (TTL-cached 30s, `registry.py:126-141`), which tests *availability*
  (is Docker up? is the playwright binary present?), **not authorization**.

- **`terminal()` is the universal exec primitive and it is unsandboxed by
  default.** `TERMINAL_ENV` defaults to `"local"`
  (`tools/terminal_tool.py:633`, `:1013`, `:1138-1139`), which runs every
  command as a fresh `bash -c` **directly on the host** via
  `subprocess.Popen(..., preexec_fn=os.setsid)`
  (`tools/environments/local.py:462-528`). "The container is the security
  boundary" is the entire isolation story for the default config — the spec
  states this (line 141, line 159) and the code confirms it.

## 2. How skills are installed (network / git / http) + scanning + bypass

- **Three install paths, three postures (spec line 138 is accurate).**
  1. **Human CLI / `/skills` slash command** (`tools/skills_hub.py`): fetches
     over HTTP from GitHub's API (`httpx.get .../api.github.com/...`,
     `skills_hub.py:469,520-545,680-692`) — **never `git clone`**, pure
     Contents/Trees API + raw download — through a redirect-guarded, SSRF-checked
     `_guarded_http_get` (`skills_hub.py:126-159`, gated by
     `tools/url_safety.is_safe_url` + `website_policy.check_website_access`).
     Flow = `quarantine_bundle` → `scan_skill` → `should_allow_install` →
     confirm → `install_from_quarantine` (`skills_hub.py:2726-2809`).
  2. **Agent-runtime skill creation** (`tools/skill_manager_tool.py`, the
     `skill_manage` tool the *model* actually calls): create/edit/patch/
     write_file straight into `SKILLS_DIR`. **Scan is a NO-OP by default** —
     `_security_scan_skill` early-returns unless `skills.guard_agent_created`
     is on, which defaults **False** (`skill_manager_tool.py:78-102`, rationale
     at `:62-65`: "the agent can already execute the same code paths via
     terminal() with no gate").
  3. **Bundled / optional skills**: offline file copies from the in-image
     `skills/` dir into `~/.hermes/skills/` (`skills_tool.py:86-88`).

- **The scanner is real but not on the agent's path.** `tools/skills_guard.py`
  (~932 lines): regex `THREAT_PATTERNS` for exfil / cred-store reads /
  destructive ops / persistence / prompt-injection + jailbreak
  (`skills_guard.py:86-495`), invisible-unicode detection (`:584-597`),
  structural checks — symlink-escape, binary files, unexpected executables,
  oversized skills (`:738-855`). Trust matrix `INSTALL_POLICY`
  (`skills_guard.py:41-51`): community blocks on any caution/dangerous finding;
  `TRUSTED_REPOS` is a 3-entry hardcoded allowlist (`openai/skills`,
  `anthropics/skills`, `huggingface/skills`, `:39`).

- **Bypass surface (3 distinct ones, each weakens the spec's "pre-scanned"
  framing):**
  - `--force` overrides any block verdict (`skills_guard.py:664-668`). This is
    a **human-CLI flag only** — it is not a `skill_manage` tool argument, so the
    *model* cannot self-`--force`. The spec (line 140) is right that it exists
    but should note it is not agent-reachable.
  - The **agent's own creation path is opt-out** (scan off by default, above) —
    the model can write an arbitrary `scripts/x.sh` into a skill with zero scan.
  - **MCP config is trusted-by-default, never scanned** (spec line 141).

## 3. The sandbox / isolation model — and the deployment trap

- **Backends exist, but every non-`local` one needs an external binary or paid
  service the Isol8 Fargate task does not have.** `_create_environment`
  (`terminal_tool.py:1106-1245`) dispatches to: `local` (host),
  `docker` (`environments/docker.py` — needs a docker/podman CLI **and a
  running daemon**, hard-fails in `_ensure_docker_available`,
  `docker.py:208-249`), `singularity` (needs `apptainer`/`singularity` CLI +
  SIF build, `singularity.py:31-137`), `modal`/`daytona`/`vercel_sandbox`
  (external SaaS, API-keyed). **Inside a stock ECS Fargate container there is no
  Docker socket, no apptainer, no nested-virt** — so the only locally-viable
  backend is `local`, i.e. **no per-run and no per-agent isolation at all**;
  the cloud backends mean shipping every shell command to a *third paid sandbox
  provider*. The spec's "container is the only isolation boundary" (line 141) is
  correct but undersells this: the in-image alternative backends are effectively
  dead in Fargate, so there is no cheap path to harden it later.

- **Docker backend IS hardened — but only matters off-Fargate.** When usable it
  applies `--cap-drop ALL` + minimal cap-add, `--security-opt
  no-new-privileges`, `--pids-limit 256`, size-limited nosuid tmpfs
  (`docker.py:146-184`). Good posture, irrelevant to the proposed deployment.

- **No per-agent isolation within one container (confirms spec Q8, lines
  149-159).** cwd is the daemon-global `TERMINAL_CWD` set once at boot
  (`gateway/run.py:619-622`, default `$HOME`), read live per call; the only
  per-call override is the **model-chosen `workdir` tool arg** — agent-driven,
  not orchestrator-enforced. So Friday vs Atlas vs a marketplace agent share
  one filesystem, one env, one secret set; separation is *soft* (prompt +
  `workdir` convention), strictly weaker than OpenClaw's per-agent
  `AgentEntrySchema.workspace`.

## 4. Net read vs the spec

The spec's Q7/Q8 conclusions are substantially **correct** (rare for this
audit). Two things it under-weights: (a) the agent-created skill path is not
just "opt-out scanning" — combined with an unsandboxed `local` terminal it
means a marketplace/seeded agent can write-then-run arbitrary code with the
container as the *sole* boundary, identical blast radius to OpenClaw but with a
larger built-in tool surface (browser CDP, computer-use, code-exec, MCP); and
(b) the spec treats container-as-boundary as a parity statement, but the
in-image hardened backends (docker/singularity) cannot run in Fargate, so
"harden the sandbox later" is not a cheap config flip — it requires either
privileged/sidecar infra or outsourcing exec to Modal/Daytona/Vercel.

---

### Files read (for traceability)
`tools/registry.py`, `tools/terminal_tool.py` (factory + header),
`tools/environments/{base,local,docker,singularity}.py`,
`tools/skills_hub.py`, `tools/skills_guard.py`, `tools/skill_manager_tool.py`,
`tools/skills_tool.py`, `tools/skill_provenance.py`, `cli.py` (force grep).
