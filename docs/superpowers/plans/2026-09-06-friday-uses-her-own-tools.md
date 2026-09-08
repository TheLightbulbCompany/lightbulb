# Friday Uses Her Own Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Friday do work she is capable of doing herself — use the browser that already
runs in her container — instead of routing simple requests to `marketplace_search`.

**Architecture:** **This is a prompt fix. No tool configuration changes.** An earlier draft of
this plan blamed Tool Search for hiding the `browser` tool; that diagnosis was wrong and is
recorded below so nobody re-derives it. The cause is entirely in the five markdown files we
seed to each owner's EFS workspace. We fix those, heal already-provisioned Fridays through the
existing byte-exact migration rail, and cut AGENTS.md back under a truncation limit it is
currently over.

**Tech Stack:** Python 3 / FastAPI backend (`uv`, `pytest`), markdown identity files seeded to
EFS, one TypeScript OpenClaw tool plugin.

**Spec:** `docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md` — findings F1, F2,
F3, F4, and the H1 correction. Read "THE PLAN → OUTCOME 1" and each finding body first.

## Why this is not a tool-configuration problem — read before changing any config

An earlier draft proposed disabling Tool Search, on the theory that compaction hid `browser`
from Friday's tool list. **That was wrong**, and three checks against the tag disprove it:

1. **`marketplace_search` is compacted exactly like `browser`.** `isDirectVisibleCatalogTool`
   (`src/agents/tool-search-catalog.ts:243-252`) requires `classified.sourceName === "core"`.
   `marketplace_search` is a plugin tool (`apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.ts:51`),
   so it is compacted too. She reached it through the compacted catalog **in the same turn**
   she failed to reach `browser`. Compaction did not stop her finding a tool.
2. **`browser` was in the prompt by name.** The bounded capability directory is gated on
   `toolSearchDirectoryEnabled = toolSearchControlsEnabledForRun && toolSearch.catalogRegistered`
   (`src/agents/embedded-agent-runner/run/attempt.ts:342`) — **not** on mode. In our `tools`
   mode it renders, listing every catalog tool's name and a 180-char description
   (`src/agents/tool-search-directory.ts:157-197`), sorted by name with a prefix truncation, so
   `browser` survives near the front.
3. **Reaching a hidden tool is one call.** `tool_call` takes an exact result id **or name**
   (`src/agents/tool-search.ts:370`).

**Do not disable Tool Search.** `apps/backend/core/containers/config.py:2033-2039` records why
it is on: the catalog is unbounded at config-write time because an owner can connect an MCP
integration at any moment (`config.py:2580-2582`), and Meta's ads server alone measured **95
tools / ~172k tokens**. Upstream says direct exposure is right *for small catalogs*
(`docs/tools/tool-search.md:96-97, 123-125`); ours is not small. One integration would re-arm
a 172k-token prompt against a $3.00/day trial cap.

**Do not switch to `mode: "code"`** either — already litigated at `config.py:2049-2072` with a
live dev failure (repeated unrecoverable `ReferenceError: require is not defined`).

A separate, real improvement — `TOOL_SEARCH_MODE` `"tools"` → `"directory"`, which restores
upstream's capability-conditioned prompt sections that compaction suppresses — is **out of
scope here** and has its own plan. It is an improvement, not the fix.

## Global Constraints

- **Image tag we run:** `v2026.8.1-isol8.3`. Verify OpenClaw behaviour with
  `git show v2026.8.1-isol8.3:<path>` in `~/dev/agent-fork`. **Its working tree is a stale
  2026.7.2 — never read it.** Never `git checkout`/`switch` there.
- **`friday_identity.py` seeds write-if-missing** — a template edit reaches NEW provisions
  only. Every template change in this plan needs a matching heal migration (Task 6).
- **Heal migrations must be byte-exact and must never clobber evolved content.** An owner or
  Friday may have edited these files; a heading match alone is never permission to overwrite.
  Follow `_migrate_add_nightly_section` (`friday_identity.py:1045-1076`) exactly.
- **AGENTS.md has a hard 20,000-char ceiling** (`DEFAULT_BOOTSTRAP_MAX_CHARS`,
  `src/agents/embedded-agent-helpers/bootstrap.ts:89`) with a warning band at 17,000
  (`bootstrap-budget.ts:27,152`). **We are at 17,031 today**, before `{owner_display}`
  substitution. No task may grow it; Task 5 shrinks it.
  *(Note: the docstring at `types.agent-defaults.ts:187-189` claims a 150,000 total. The code
  says `DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 60_000` at `bootstrap.ts:90`. Trust the code.)*
- **USER.md is hard-capped at 4,000 chars regardless of config** (`bootstrap.ts:93`) — relevant
  only if anyone later enriches it.
- **`apps/infra/openclaw/skills/**` and `plugins/**` are IMG lane** — image rebuild, ECR push,
  then a human PR flipping `openclaw-version.json` `dev.tag`, then `prod.tag`. Slowest lane.
- **Backend tests:** `cd apps/backend && uv run pytest tests/ -v`. Targeted locally; CI runs full.
- No PR or issue references in code comments.

---

## File Structure

| file | responsibility | change |
|---|---|---|
| `apps/backend/core/services/friday_identity.py` | seeds SOUL/AGENTS/USER/IDENTITY/MEMORY + heal migrations | Tasks 1–5 templates, Task 6 migration |
| `apps/backend/tests/unit/services/test_friday_identity.py` | identity + migration tests | Tasks 1–6 |
| `apps/infra/openclaw/skills/lightbulb/SKILL.md` | the bundled platform skill | Task 7: frontmatter only |
| `apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.ts` | marketplace tool descriptions | Task 8 |

---

### Task 0: Prove the diagnosis before spending on it

The whole plan rests on "the prompt is the cause". One dev container settles it for ~30 minutes
of work, and if it fails, tasks 1–6 need rethinking rather than shipping.

**Files:** none — this is an experiment.

**Interfaces:** Consumes nothing. Produces a go/no-go for the rest of the plan.

- [ ] **Step 1: Change ONLY the browser line on a dev container**

Edit the SOUL browser bullet in `friday_identity.py` (Task 1 has the exact text), deploy to
dev, and reprovision one dev owner. **Change nothing else** — no tool config, no loop edit, no
skill change. In the browser console on a dev.flickmeon.ai page:

```javascript
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'DELETE', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'POST', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
```

- [ ] **Step 2: Ask her to browse**

In dev chat, verbatim: *"Go to flickmeon.ai and tell me what the pricing page says."*

- [ ] **Step 3: Record the outcome**

**She uses the browser** ⇒ causation confirmed with Tool Search still on and the catalog
untouched. Proceed, and note in the PR that the tool-config debate is closed for this class of
failure.
**She still runs `marketplace_search`** ⇒ the browser line was not sufficient. Proceed with
tasks 2–4 (the loop is the remaining suspect) but **do not** claim the cause is understood, and
re-run this experiment after Task 3.
**She does neither** (asks the owner to open it, or says she cannot) ⇒ stop and re-diagnose.

---

### Task 1: Tell Friday the truth about her browser

`config.py:2315-2322` sets `browser.enabled: true` and `defaultProfile: "openclaw"` — the
in-container headful Chromium on the Xvfb display, present on every container. The paired
desktop (`user` profile) is the optional upgrade. SOUL.md names only the non-default profile
and omits the default one, so "go to the browser" reads as a capability she may not have.

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (`_SOUL_TEMPLATE`, the
  `- **Browser** —` bullet, ~line 308)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_SOUL_BROWSER_LINE` and `_LEGACY_SOUL_BROWSER_LINE`, consumed by Task 6.

- [ ] **Step 1: Write the failing test**

```python
def test_soul_browser_line_states_the_container_browser_is_always_available():
    """SOUL must not gate the browser on desktop pairing.

    config.py sets browser.defaultProfile="openclaw" (in-container Chromium) on
    every container; the paired desktop is the optional upgrade. The old copy
    named only the non-default profile, so a browsing request read as a
    capability Friday might not have.
    """
    line = friday_identity._SOUL_BROWSER_LINE
    assert "always" in line.lower()
    assert "when their desktop app is paired" not in line
    assert "signed-in" in line  # the paired-desktop upgrade is still described


def test_rendered_soul_has_no_unsubstituted_placeholders():
    """str.format does not recurse into substituted values, so a placeholder
    inside an inlined constant reaches disk literally. Assert on the RENDERED
    file, not the constant — the constant cannot catch this."""
    soul = friday_identity._render_templates(_personal_profile())["SOUL.md"]
    assert "{" not in soul.replace("{{", "").replace("}}", ""), soul
```

`_personal_profile()` is the existing helper in this test file; if it is named differently,
use whatever the neighbouring tests already use to drive `_render_templates`.

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v -k browser_line`

Expected: FAIL — `AttributeError: ... has no attribute '_SOUL_BROWSER_LINE'`

- [ ] **Step 3: Extract the constant and rewrite the line**

Above `_SOUL_TEMPLATE` in `friday_identity.py`:

```python
# The browser bullet in SOUL.md, extracted so the heal migration can swap it
# byte-exactly in an already-seeded SOUL.md. The prior copy named ONLY the
# paired-desktop profile, which is the optional one: config.py sets
# browser.defaultProfile="openclaw" fleet-wide, so an in-container Chrome is
# always present. Naming only the gated profile made a browsing request read as
# a capability Friday might not have, which is what sent her to the marketplace.
# NOTE: deliberately contains NO {owner_display}. `str.format` does not format
# substituted values recursively, so a placeholder inside this constant would be
# written to disk literally. It also has to be byte-identical between the seed
# path and the heal migration (Task 6), and the migration has no owner display
# name to substitute — its signature is (workspace, owner_id, agent_uuid).
_SOUL_BROWSER_LINE = (
    "- **Browser** — you have a real Chrome running inside your own container, "
    "always available, no setup required. Use it to read pages, click, and fill "
    "forms. If your owner has paired their desktop app you can also drive their "
    "own signed-in Chrome — that is the upgrade for sites they are already "
    "logged into, and it is worth being careful with destructive actions there."
)

# Byte-exact prior copy. The migration replaces ONLY this; an edited line is
# owner/Friday-evolved content and is never touched.
_LEGACY_SOUL_BROWSER_LINE = (
    "- **Browser** — drive the user's signed-in Chrome via CDP (when their desktop\n"
    "  app is paired). You can read pages, click, fill forms — be careful with\n"
    "  destructive actions."
)
```

Replace the literal bullet in `_SOUL_TEMPLATE` with `{browser_line}`, then inline it at
**module level with `.replace()`, not via `.format()`** — the file already does exactly this
one line away:

```python
# Owner-agnostic, so substitute before .format() ever sees the template —
# .replace(), not .format(), because the template's real placeholders
# ({owner_display}) must survive until render time. Same idiom as
# _SELF_GROUNDING_SECTION at friday_identity.py:379.
_SOUL_TEMPLATE = _SOUL_TEMPLATE.replace("{browser_line}", _SOUL_BROWSER_LINE)
```

**Do NOT pass `browser_line=` to `.format(...)`.** `str.format` does not format substituted
values recursively, so any placeholder inside the substituted string would reach disk
literally. `_SOUL_BROWSER_LINE` carries none — see the note on the constant — but the
`.replace()` idiom is what keeps that true regardless.

- [ ] **Step 4: Run to verify**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`

Expected: PASS, including any existing test that renders SOUL.md.

- [ ] **Step 5: Verify the legacy constant is byte-exact against what shipped**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync && git show HEAD:apps/backend/core/services/friday_identity.py > /tmp/prior_identity.py
cd apps/backend && uv run python -c "
from core.services import friday_identity as f
prior = open('/tmp/prior_identity.py').read()
assert f._LEGACY_SOUL_BROWSER_LINE in prior, 'legacy constant is NOT byte-exact — the migration will silently no-op'
print('byte-exact confirmed')
"
```

Expected: `byte-exact confirmed`. If it fails, copy the exact prior text — Task 6 depends on it.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "fix(friday): the in-container browser is always available, not desktop-gated"
```

---

### Task 2: Give the orchestration loop a do-it-yourself terminal

The loop at `friday_identity.py:553` has **no** exit that isn't delegation. Step 1 says "if
it's a single piece, skip to step 4" — and step 4 is *Confirm the delegation plan*. The only
do-it-yourself permission in the whole file is buried at `:707-709`, in a "Key Endpoints"
appendix, scoped to *support* work ("drafting issue bodies, summarizing"), and it defers to
SOUL's capability list — which Task 1 just fixed.

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (`_AGENTS_TEMPLATE`, `## The
  orchestration loop` at ~553)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_AGENTS_DIY_GATE`, consumed by Task 6.

- [ ] **Step 1: Write the failing test**

```python
def test_orchestration_loop_has_a_do_it_yourself_terminal():
    """The loop must not be entered for work Friday can simply do.

    Without a step-0 exit, a one-shot request (browse a page, read a file, run a
    command) runs decompose -> roster -> "no agent qualified" -> marketplace_search.
    """
    gate = friday_identity._AGENTS_DIY_GATE
    assert gate.lstrip().startswith("0.")
    for trigger in ("browser", "fetch", "read a file", "run a command", "answer"):
        assert trigger in gate.lower(), f"missing trigger case: {trigger}"
    # It must forbid the loop, not merely permit doing the work.
    assert "do not decompose" in gate.lower()
    assert "marketplace" in gate.lower()
```

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v -k diy_terminal`

Expected: FAIL — `_AGENTS_DIY_GATE` does not exist.

- [ ] **Step 3: Add the gate**

Above `_AGENTS_TEMPLATE`:

```python
# Step 0 of the orchestration loop. The loop previously had no terminal that was
# not delegation: step 1's only escape ("single piece -> skip to step 4") lands on
# "confirm the delegation plan". A one-shot request therefore ran all the way to
# step 3 and out through marketplace_search. Kept as a constant so the heal
# migration can insert it into an already-seeded AGENTS.md.
_AGENTS_DIY_GATE = """\
0. **Can you just do it?** Before anything else: if your own tools finish this
   request, do it now and report back. Do not decompose it, do not check the
   roster, do not search the marketplace.
   This is the answer whenever the task is to look at a page in your browser,
   fetch a URL, read a file or write one in your workspace, run a command, or
   answer a question from what you already know. You have a browser, a shell, files and
   web fetch — work that needs one of those needs YOU, not a hire.
   Delegation is for work that needs a specialist's sustained attention over
   time, not for work that needs a tool call.
"""
```

Insert `{diy_gate}` at the top of the `## The orchestration loop` section body and pass
`diy_gate=_AGENTS_DIY_GATE` in `_AGENTS_TEMPLATE`'s existing `.format(...)` call.

- [ ] **Step 4: Run to verify**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`

Expected: PASS. A `KeyError` means a `.format(...)` call site was missed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "feat(friday): give the orchestration loop a do-it-yourself terminal"
```

---

### Task 3: Stop the two instructions that route her away from her own tools

Two lines still say "delegate" even with Task 2's gate in place:
`:631-634` — *"**Delegate specialist execution.** … If the primary deliverable belongs to a
specialist, delegate it — do not perform it yourself"* — a blanket ban on doing work; and
`:564-566` — step 3's *"If no agent is qualified, **check the marketplace first**"*, which
fires on a missing **tool** as readily as a missing **role**.

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (`_AGENTS_TEMPLATE`, ~564 and ~631)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the edited `_AGENTS_TEMPLATE` text asserted by Task 6's migration.

- [ ] **Step 1: Write the failing tests**

```python
def test_marketplace_check_is_scoped_to_a_missing_role():
    """Assert inside step 3's slice, so line-wrapping cannot make this vacuous.

    The prior draft asserted `"If no agent is qualified, **check the marketplace
    first**" not in t`, which passed BEFORE any edit — the live template wraps
    between "agent" and "is", so that exact string never appeared.
    """
    t = friday_identity._AGENTS_TEMPLATE
    step3 = t[t.index("3. **Identify or hire.") : t.index("4. **Confirm.")]
    # No leading "a": the replacement text wraps after "needs a".
    assert "role nobody on the roster fills" in step3
    assert "is qualified" not in step3


def test_delegation_boundary_is_scoped_to_sustained_work():
    t = friday_identity._AGENTS_TEMPLATE
    assert "sustained" in t
    assert "delegate it — do not perform it yourself" not in t
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v -k "missing_role or sustained"`

Expected: FAIL on both.

- [ ] **Step 3: Rewrite step 3's opening**

```
3. **Identify or hire.** For each piece, pick the right agent. If a piece needs a
   role nobody on the roster fills — an ongoing specialism such as bookkeeping or
   content marketing, not a tool you have not reached for yet — **check the
   marketplace first**:
```

- [ ] **Step 4: Rewrite the Boundaries bullet**

```
- **Delegate sustained specialist execution.** Your direct work is company
  control: choose priorities, synthesize evidence, resolve or reroute blockers,
  verify outcomes. When a deliverable needs a specialist's sustained attention
  over days, delegate it. This does NOT apply to a one-shot action you can
  complete in this turn — those you simply do, per step 0.
```

- [ ] **Step 5: Run to verify**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "fix(friday): scope delegation and the marketplace check to a missing role"
```

---

### Task 4: Re-run the experiment

Tasks 1–3 are the complete prompt fix. Re-run Task 0 before spending on the AGENTS.md diet.

- [ ] **Step 1:** Deploy to dev, reprovision, and ask the Task 0 question again.
- [ ] **Step 2:** **PASS** = she uses the browser and no `marketplace_search` call appears.
  **FAIL** = stop and re-diagnose before Task 5; the remaining tasks are budget hygiene and
  will not change behaviour on their own.

---

### Task 5: Cut AGENTS.md back under the truncation warning band

Measured today: **`_AGENTS_TEMPLATE` is 17,031 chars** — `friday_identity.py:855` already folds
`_NIGHTLY_WAKE_SECTION` and `_WEBCHAT_SECTION` into it, so do **not** add them again when
measuring. That is against a 17,000 warning band and a 20,000 hard ceiling, *before*
`{owner_display}` substitution. Tasks 1–3 add ~650 more. Crossing 20,000 truncates the file and
injects a notice telling Friday her own playbook was cut.

**Margin is thin, and `{owner_display}` appears 26 times.** An owner display name longer than
~15 characters grows the rendered file beyond the template length, so the test can pass while a
real container is over. Budget for that when deciding how much to cut.

The `## Paperclip reference` section (~line 682) is endpoint tables that **duplicate the
`paperclip` skill AGENTS.md already tells her to use** at `:466-468`. The skill is on disk, one
read away, and carries a 58KB `references/api-reference.md` besides.

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (`_AGENTS_TEMPLATE`, `## Paperclip
  reference` section)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: nothing. Produces: a smaller `_AGENTS_TEMPLATE`.

- [ ] **Step 1: Write the failing test**

```python
def test_agents_md_stays_under_the_bootstrap_warning_band():
    """AGENTS.md is seeded whole into every turn's context.

    OpenClaw truncates a bootstrap file at DEFAULT_BOOTSTRAP_MAX_CHARS = 20_000
    and warns from 85% of it (17_000). Truncation injects a notice telling the
    agent her own playbook was cut. Measure the ASSEMBLED file, not the template.
    """
    # friday_identity.py:855 ALREADY does
    #   _AGENTS_TEMPLATE = _AGENTS_TEMPLATE + _NIGHTLY_WAKE_SECTION + _WEBCHAT_SECTION
    # so adding those sections again here double-counts by 4,120 chars and the
    # assertion could never go green. Measure the template as it stands.
    assembled = friday_identity._AGENTS_TEMPLATE
    assert len(assembled) < 17_000, (
        f"AGENTS.md is {len(assembled)} chars; the bootstrap warning band is 17,000 "
        "and the hard ceiling is 20,000. Move reference material into the paperclip skill."
    )
```

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v -k warning_band`

Expected: FAIL, reporting a length above 17,000.

- [ ] **Step 3: Replace the section with a pointer**

Delete the `## Paperclip reference` body and leave:

```
## Paperclip reference

Every endpoint, payload and header rule lives in the `paperclip` skill on disk.
Read `/app/skills/paperclip/SKILL.md`, and its `references/api-reference.md` for
the full API. Do not work from memory for a payload shape — read it.
```

Keep in AGENTS.md: the orchestration loop, the hard rules (checkout/409, run-id discipline,
never create probe issues, the approval gate), triage, and the nightly/webchat sections. Those
are policy, not reference.

**Measured: the `## Paperclip reference` section is 1,177 chars. Replacing it with the ~230-char
pointer above lands the template at ~16,084 — under the band on the first cut.** No further
cutting is needed. If a later change pushes it back over, the next candidates are
`## Cross-agent data flow` and `## Plans and deliverables live in Paperclip, not files`, both of
which also duplicate skill material — but do not pre-emptively cut them.

- [ ] **Step 4: Run to verify**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "perf(friday): move the Paperclip endpoint tables out of AGENTS.md"
```

---

### Task 6: Heal already-provisioned Fridays

`pre_stage_friday_identity` seeds write-if-missing, so Tasks 1–5 reach NEW provisions only.
`_run_heal_migrations` (`:1329`) is the existing rail. Follow `_migrate_add_nightly_section`
(`:1045-1076`) exactly: anchor-guarded, idempotent, byte-exact, never clobbering evolved
content.

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (new `_migrate_self_service`,
  registered in `_run_heal_migrations`)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: `_SOUL_BROWSER_LINE`, `_LEGACY_SOUL_BROWSER_LINE` (Task 1); `_AGENTS_DIY_GATE` (Task 2).
- Produces: `_migrate_self_service(workspace, owner_id, agent_uuid) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def _ws_with(files: dict[str, str]) -> MagicMock:
    ws = MagicMock()
    ws.read_file.side_effect = lambda owner, path: files[path]
    ws.write_file.side_effect = lambda owner, path, content: files.__setitem__(path, content)
    return ws


def test_heal_replaces_the_legacy_browser_line():
    files = {"workspace-a/SOUL.md": "x\n" + friday_identity._LEGACY_SOUL_BROWSER_LINE + "\ny\n"}
    ws = _ws_with(files)
    assert friday_identity._migrate_self_service(ws, "u_1", "a") is True
    assert friday_identity._LEGACY_SOUL_BROWSER_LINE not in files["workspace-a/SOUL.md"]
    assert "always available" in files["workspace-a/SOUL.md"]


def test_heal_is_idempotent():
    files = {"workspace-a/SOUL.md": "x\n" + friday_identity._SOUL_BROWSER_LINE + "\n"}
    ws = _ws_with(files)
    assert friday_identity._migrate_self_service(ws, "u_1", "a") is False
    ws.write_file.assert_not_called()


def test_heal_never_clobbers_an_edited_browser_line():
    edited = "- **Browser** — my own notes. Do not touch.\n"
    files = {"workspace-a/SOUL.md": "x\n" + edited}
    ws = _ws_with(files)
    assert friday_identity._migrate_self_service(ws, "u_1", "a") is False
    assert files["workspace-a/SOUL.md"] == "x\n" + edited


def test_heal_inserts_the_gate_into_an_existing_agents_md():
    files = {
        "workspace-a/SOUL.md": friday_identity._SOUL_BROWSER_LINE,
        "workspace-a/AGENTS.md": "## The orchestration loop\n\n1. **Decompose.** ...\n",
    }
    ws = _ws_with(files)
    assert friday_identity._migrate_self_service(ws, "u_1", "a") is True
    assert friday_identity._AGENTS_DIY_GATE in files["workspace-a/AGENTS.md"]


def test_heal_survives_a_missing_file():
    ws = MagicMock()
    ws.read_file.side_effect = FileNotFoundError
    assert friday_identity._migrate_self_service(ws, "u_1", "a") is False
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v -k heal_`

Expected: FAIL — `_migrate_self_service` does not exist.

- [ ] **Step 3: Implement**

```python
def _migrate_self_service(workspace: Workspace, owner_id: str, agent_uuid: str) -> bool:
    """Heal an EXISTING Friday's browser line and orchestration-loop gate.

    SOUL.md and AGENTS.md are seeded write-if-missing, so already-provisioned
    Fridays keep the copy that routes a browsing request to marketplace_search.
    Surgical and idempotent, matching _migrate_add_nightly_section: replace only
    the byte-exact legacy text, insert the gate only when its heading exists and
    the gate does not, and otherwise leave the file untouched. An owner- or
    Friday-edited line is theirs. Returns True when anything was rewritten.
    """
    changed = False

    soul_path = f"workspace-{agent_uuid}/SOUL.md"
    try:
        soul = workspace.read_file(owner_id, soul_path)
    except Exception:  # noqa: BLE001 — no SOUL.md yet; the seed loop writes it
        soul = None
    if soul is not None and _SOUL_BROWSER_LINE not in soul and _LEGACY_SOUL_BROWSER_LINE in soul:
        workspace.write_file(
            owner_id, soul_path, soul.replace(_LEGACY_SOUL_BROWSER_LINE, _SOUL_BROWSER_LINE, 1)
        )
        changed = True

    agents_path = f"workspace-{agent_uuid}/AGENTS.md"
    try:
        agents = workspace.read_file(owner_id, agents_path)
    except Exception:  # noqa: BLE001 — no AGENTS.md yet; the seed loop writes it
        agents = None
    heading = "## The orchestration loop"
    if agents is not None and _AGENTS_DIY_GATE not in agents and heading in agents:
        workspace.write_file(
            owner_id,
            agents_path,
            agents.replace(heading, heading + "\n\n" + _AGENTS_DIY_GATE.rstrip("\n"), 1),
        )
        changed = True

    return changed
```

**`_SOUL_BROWSER_LINE` carries no `{owner_display}` — that is deliberate and load-bearing.**
Every migration signature is `(workspace, owner_id, agent_uuid)` (`:1045`, `:1078`, `:1132`,
`:1301`) and `_run_heal_migrations` (`:1329`) has no display name to pass, so a placeholder in
the constant would be written to a live owner's file verbatim across the whole fleet. Because
Task 1 inlines the same constant with `.replace()` at module level, the seeded bytes and the
healed bytes are identical — which is exactly what this migration's `_SOUL_BROWSER_LINE in soul`
idempotence check requires.

- [ ] **Step 4: Register it**

Add to the `migrations` tuple in `_run_heal_migrations`, after the nightly entry:

```python
        (
            _migrate_self_service,
            "friday_identity.self_service_migrated",
            "friday_identity.self_service_migrate_error",
            "healed browser line in SOUL.md and orchestration gate in AGENTS.md",
        ),
```

- [ ] **Step 5: Run the full identity suite**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "feat(friday): heal existing Fridays' browser line and loop gate"
```

---

### Task 7: Cut the `lightbulb` skill description to one sentence

Skill descriptions share a hard **18,000-char** budget (`DEFAULT_MAX_SKILLS_PROMPT_CHARS`,
`src/skills/loading/skill-prompt-limits.ts:10`). Upstream's 51 bundled skills render at ~11,713
chars; ours add 1,918, of which the `lightbulb` **description alone is 1,150** — 6.4% of the
whole fleet's budget. Add plugin skills and the catalog is at or over, at which point
`formatSkillsForPromptBounded` binary-searches **every** description shorter
(`skill-prompt-limits.ts:145-160`). One of the descriptions being squeezed is
`browser-automation`'s — the single best pointer to the browser anywhere in the prompt.

A description that fires on "almost any turn" also discriminates nothing, and firing it costs a
~58KB read ≈ 15k tokens against a $3.00/day cap.

**Files:**
- Modify: `apps/infra/openclaw/skills/lightbulb/SKILL.md` (frontmatter `description` only)

**Interfaces:** Consumes nothing. Produces a shorter always-on catalog entry.

- [ ] **Step 1: Measure today's rendered length**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync && python3 -c "
import re
for name in ('lightbulb','paperclip'):
    t = open(f'apps/infra/openclaw/skills/{name}/SKILL.md').read()
    fm = t.split('---')[1]
    d = re.search(r'description:\s*>?\s*\n?((?:\s+.*\n)+)', fm)
    print(name, 'description chars:', len(' '.join(d.group(1).split())) if d else 'n/a')
"
```

- [ ] **Step 2: Replace the description**

```yaml
description: >
  Lightbulb platform reference — what this platform is, its surfaces, plans and
  providers, integrations, the marketplace, and the only way to send email. Read
  before answering any question about what Lightbulb can do or how to build on it.
```

Leave `name:`, the H1, and the body untouched — the body split is a separate plan.

- [ ] **Step 3: Verify it is under 200 chars and still discriminates**

Re-run Step 1's command. Expected: under 200 chars. Sanity-check the wording still names the
email path, since the old description's one genuinely load-bearing claim was that Lightbulb
does not use Gmail or himalaya and looking for those finds nothing.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/openclaw/skills/lightbulb/SKILL.md
git commit -m "perf(skills): cut the lightbulb description from 1150 chars to one sentence"
```

---

### Task 8: Stop `marketplace_search` advertising itself as the answer to a missing tool

**Files:**
- Modify: `apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.ts`
- Test: `apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.test.ts`

**Interfaces:** Consumes nothing. Produces no signature change — description text only.

- [ ] **Step 1: Write the failing test**

`src/index.ts` has exactly one export — `export default defineToolPlugin({...})` — and the
existing `index.test.ts` imports only from `./env.js`, so there is no `tools` array a test can
reach. **Export the string first**, then assert on it:

```ts
import { describe, it, expect } from "vitest";
import { MARKETPLACE_SEARCH_DESCRIPTION as d } from "./index.js";

describe("marketplace_search", () => {
  it("is scoped to a missing role, not a missing tool", () => {
    expect(d).toContain("ongoing role");
    expect(d).toContain("Do NOT use this");
    expect(d).not.toContain("a capability nobody on the roster has");
  });
});
```

If importing `index.js` drags the heavy `openclaw` devDependency into the test run, put the
constant in `src/env.ts` (already imported by the existing test) and import it into `index.ts`.

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/infra/openclaw/plugins/lightbulb-marketplace && npm test`

Expected: FAIL on the `ongoing role` assertion. **If it fails with
`ReferenceError`/`SyntaxError` instead, the export in Step 3 is missing** — that is a broken
test, not a correct red.

- [ ] **Step 3: Export the constant and rewrite it**

```ts
export const MARKETPLACE_SEARCH_DESCRIPTION =
        "Search published Lightbulb marketplace agents the owner could add to their team. " +
        "Use ONLY when the work needs an ongoing role nobody on the roster fills — a " +
        "standing specialism like bookkeeping or content marketing. " +
        "Do NOT use this for a one-off task you can do yourself: you already have a " +
        "browser, a shell, files and web fetch, and reaching for those is always right " +
        "before recommending a hire. When a role IS missing, check here BEFORE asking " +
        "Atlas to build one — an existing agent is ready immediately and costs nothing to add.";
```

Then reference it in the tool definition: `description: MARKETPLACE_SEARCH_DESCRIPTION,`

- [ ] **Step 4: Run to verify**

Run: `cd apps/infra/openclaw/plugins/lightbulb-marketplace && npm test`

Expected: PASS.

- [ ] **Step 5: Rebuild dist/ — the image COPYs it, there is no build step in the Dockerfile**

```bash
cd apps/infra/openclaw/plugins/lightbulb-marketplace && npm run build
grep -c "ongoing role" dist/index.js
```

Expected: `1` or more. If `0`, the build did not run. The plugin's own `.gitignore` overrides
the repo-root ignore for `dist/`, so **`dist/` must be committed**.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/openclaw/plugins/lightbulb-marketplace/src apps/infra/openclaw/plugins/lightbulb-marketplace/dist
git commit -m "fix(marketplace): scope the search tool to a missing role, not a missing tool"
```

---

## Verification — the whole feature, on dev

- [ ] **Step 1: Ship to dev.** Merge (dev deploys on merge). For the IMG-lane pieces (Tasks 7
  and 8), wait for `build-openclaw-image.yml`, then land a PR flipping `openclaw-version.json`
  `dev.tag`. Watch both:
  `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`

- [ ] **Step 2: Reprovision a dev owner** (the DELETE/POST pair from Task 0).

- [ ] **Step 3: Confirm AGENTS.md is under the band on a real container**

```bash
aws ecs execute-command --profile isol8-admin --cluster <cluster> --task <task> \
  --container openclaw --interactive --command \
  "wc -c /mnt/efs/users/<owner>/workspace-<friday_uuid>/AGENTS.md"
```

Expected: under 17,000.

- [ ] **Step 4: The behavioural test — Claude-in-Chrome on dev, never the journey test**

Send Friday: *"Go to flickmeon.ai and tell me what the pricing page says."*
**PASS:** she uses the browser on the first turn and reports the page.
**FAIL:** any `marketplace_search` call, or a reply asking the owner to open it themselves.

- [ ] **Step 5: Confirm the heal on an EXISTING Friday.** Use a dev owner whose container
  predates this change and do **not** reprovision. Trigger a config rewrite:

```javascript
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'PATCH', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
```

Check CloudWatch for `friday_identity.self_service_migrated`, then re-run Step 4 against them.

- [ ] **Step 6: Regression — she must still delegate real work.** Send:
  *"I need someone to do our bookkeeping every month."*
  **PASS:** she checks the roster and recommends a hire or the marketplace. **FAIL:** she tries
  to do the bookkeeping herself — Task 2's gate is too broad and needs the "sustained
  attention" clause tightened.

- [ ] **Step 7: Promote to prod** only after 4, 5 and 6 pass. Prod is a manual dispatch:
  `gh workflow run deploy.yml --ref main`, plus a second PR flipping `prod.tag` for the image.

---

## Expected UX

**Before:** "Open flickmeon.ai and check the pricing page" → Friday decomposes the request,
lists the roster, finds nobody who does browsing, runs `marketplace_search`, and offers to hire
a web-research agent.

**After:** she opens the page and tells you what it says. Ask her for something that genuinely
needs a standing specialist and she still recommends one — that is the regression in Step 6.

Invisible but real: AGENTS.md drops back under the truncation band, so her playbook stops
being at risk of being cut mid-file; and ~950 chars come back to the shared skill-description
budget that `browser-automation`'s pointer competes in.

## Not in this plan

- **Splitting the two 118 KB SKILL.md bodies** — the larger token win, but a content refactor
  with real risk of dropping load-bearing instructions. Its own plan:
  `2026-09-06-split-oversized-skills.md`.
- **`skills.allowBundled`** — still worth doing, but its justification changed: it now buys
  headroom in the 18,000-char catalog budget rather than raw prompt tokens. Belongs with the
  skill split, and must land after the skill's `name:` is lowercased.
- **`TOOL_SEARCH_MODE` `"tools"` → `"directory"`** — restores upstream's capability-conditioned
  prompt sections that compaction suppresses. A genuine improvement, not this fix, and gated on
  a dev run because its main benefit depends on the provider emitting a tool name it was given
  no schema for.
- **Disabling Tool Search** — do not. See the section at the top of this plan.
