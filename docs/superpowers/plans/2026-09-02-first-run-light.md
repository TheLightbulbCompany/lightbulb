# Friday's First Run, Made Light — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut Friday's first run from ~30–55 tool calls / 47–71 model calls / up to the whole $3 trial cap down to a bounded run (target ≤ 15 tool calls, ≤ 20 model calls) while keeping the market brief and the integrations recommendation the owner asked to keep.

**Architecture:** The backend already knows the integrations catalog and the owner's Stripe state, so it renders both into `BRIEF.md` at brief fulfilment (deterministic, no model). The kickoff message is rewritten to bound the model's work (two searches, five bullets, three one-line moves, pick two–three integrations from the staged list, three-line email, one closing question) and to stop instructing plumbing (no catalog curl, no link minting, no browser tabs, no issue creation). Backend-only: rides a normal backend deploy; nothing under `apps/infra/openclaw/` changes, so no image build and no fleet reroll.

**Tech Stack:** Python 3 / FastAPI backend (`apps/backend`), pytest, ruff, `uv`. DynamoDB billing row via `core.repositories.billing_repo`. Credentials registry via `core.services.credentials.registry`.

**Spec:** `docs/superpowers/specs/2026-09-02-first-run-light-design.md`

## Global Constraints

- Backend only. **Do not modify anything under `apps/infra/openclaw/`** (a `SKILL.md` change triggers `build-openclaw-image.yml` and needs a 28-container reroll).
- **Keep the constant name `_INTEGRATIONS_STEP`** in `onboarding_brief.py` — the lightbulb skill's maintenance header anchors on it by name.
- The kickoff must never contain `Authorization`, `OPENCLAW_HOOK_TOKEN`, `bearer` (any case), the string `platform-info`, or `lightbulb-connect.sh`. It must contain `Integrations tab`, `BRIEF.md`, `three best next moves`, and `lightbulb-mail.sh send --owner`.
- The naming sentence (`_NAMING_STEP_SENTENCE`, unchanged) must appear before "three best next moves" in both kickoff paths.
- No new env vars, tables, or dependencies. Work in the worktree `.worktrees/first-run-light` on branch `feat/first-run-light` (base `origin/main` at `f2a42a73b`); never in `~/Desktop/isol8.nosync` itself (stale branch).
- Run backend commands from `apps/backend` with `uv run …`. Every commit message ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh
  ```

---

## File Structure

| file | responsibility after this plan |
|---|---|
| `apps/backend/core/services/onboarding_brief.py` | (existing) brief text, `BRIEF.md` rendering, kickoff message, first-run staging. **Adds:** `IntegrationRow`, `integration_rows_from_registry()`, `_stripe_line()`, `render_integrations_md()`; `stage_first_run_files` gains `integrations_md`; `_BRIEF_TEMPLATE` loses the person-research sentence; kickoff constants rewritten. |
| `apps/backend/tests/unit/services/test_onboarding_brief.py` | (existing) pins the kickoff and brief. **Adds** renderer + staging tests; **rewrites** the ~8 tests that pin the old kickoff. |
| `docs/superpowers/specs/2026-09-02-first-run-light-design.md` | the design this plan implements (deleted in the final task). |
| `docs/superpowers/plans/2026-09-02-first-run-light.md` | this plan (deleted in the final task). |

Nothing else changes. `apps/backend/routers/platform_info.py` and `core/services/credentials/registry.py` are read, not modified.

---

### Task 1: Render the integrations section deterministically

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` (add after `render_brief_md`, i.e. after line ~93)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py` (append)

**Interfaces:**
- Consumes: `core.services.credentials.registry.all_providers() -> dict[str, Provider]`, `registry.visible_in_env(p: Provider) -> bool`, `Provider.key / .display_name / .byo_required` (all existing).
- Produces:
  - `IntegrationRow(key: str, display_name: str, byo_required: bool, requestable: bool)` (frozen dataclass)
  - `integration_rows_from_registry() -> list[IntegrationRow]` (sorted by display name, case-insensitive)
  - `render_integrations_md(rows: Sequence[IntegrationRow], billing_row: Mapping[str, object]) -> str`
  - `_PAID_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing", "past_due"})`

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/services/test_onboarding_brief.py`:

```python
from core.services.onboarding_brief import (  # noqa: E402 — appended block
    IntegrationRow,
    integration_rows_from_registry,
    render_integrations_md,
)


def _rows():
    return [
        IntegrationRow(key="stripe", display_name="Stripe", byo_required=False, requestable=False),
        IntegrationRow(key="instagram", display_name="Instagram", byo_required=False, requestable=False),
        IntegrationRow(key="google", display_name="Google Workspace", byo_required=True, requestable=False),
        IntegrationRow(key="meta_ads", display_name="Meta Ads", byo_required=False, requestable=True),
    ]


def test_integrations_md_labels_each_provider_from_the_two_flags():
    md = render_integrations_md(_rows(), {})
    assert "## Integrations" in md
    assert "- Instagram (`instagram`): one-click" in md
    assert "- Google Workspace (`google`): needs you (bring your own app)" in md
    assert "- Meta Ads (`meta_ads`): by request" in md
    # The section tells Friday NOT to re-fetch or open sign-in pages on a first run.
    assert "never fetch this catalog" in md
    assert "Integrations tab" in md


def test_integrations_md_stripe_line_trial_says_it_comes_with_subscribing():
    md = render_integrations_md(_rows(), {"subscription_status": "trial_local"})
    assert "set up when the owner subscribes" in md
    assert "do not request it" in md


def test_integrations_md_stripe_line_paid_says_the_card_is_already_filed():
    for status in ("active", "trialing", "past_due"):
        md = render_integrations_md(_rows(), {"subscription_status": status})
        assert "already in the owner's Inbox" in md, status
        assert "Do not file another request" in md, status


def test_integrations_md_stripe_line_connected_wins_over_status():
    md = render_integrations_md(
        _rows(), {"subscription_status": "trial_local", "company_stripe_account_id": "acct_123"}
    )
    assert "connected" in md
    assert "do not request it" not in md


def test_integration_rows_come_from_the_live_registry_sorted():
    rows = integration_rows_from_registry()
    assert rows, "registry returned no providers"
    names = [r.display_name.lower() for r in rows]
    assert names == sorted(names)
    keys = {r.key for r in rows}
    assert "stripe" in keys  # the one every company needs
    assert all(isinstance(r.byo_required, bool) and isinstance(r.requestable, bool) for r in rows)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider -k "integrations_md or integration_rows"`
Expected: FAIL with `ImportError: cannot import name 'IntegrationRow'`.

- [ ] **Step 3: Write the implementation**

In `apps/backend/core/services/onboarding_brief.py`, add near the top imports (the file already imports from `dataclasses`? if not, add):

```python
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
```

Then add after `render_brief_md` (after its closing line, before `_MAX_GOAL_TITLE = 120`):

```python
# ---------------------------------------------------------------------------
# Integrations section of BRIEF.md — rendered by the BACKEND at brief fulfilment.
#
# The first run used to have Friday curl /platform-info, read the JSON, interpret
# `requestable`/`byo_required`, mint OAuth links and open browser tabs — 26–40
# shell commands per run, the bulk of a $3 trial cap, and the part that broke on
# 2026-08-30 (the hook-token redaction loop). Everything in that chain except
# "which of these serve the plan" is data the backend already holds. So the menu
# is staged into BRIEF.md as of provisioning (the same live registry
# /platform-info reads — nothing baked into the image), Friday makes the one
# judgment call, and the owner connects from the Integrations tab in their own
# browser (useConnections.ts -> /connections/{provider}/authorize). Later turns
# still read the live catalog per the skill; this covers the first run.
#
# Stripe is handled separately because it is move-independent: the company's own
# Stripe account is the eligibility gate for the whole Projects rail. The line is
# rendered from the billing row and Friday NEVER files the Stripe request on a
# first run — #2254 files it when a subscription goes live, and a trial owner
# cannot act on the card anyway.


@dataclass(frozen=True)
class IntegrationRow:
    key: str
    display_name: str
    byo_required: bool
    requestable: bool


def integration_rows_from_registry() -> list[IntegrationRow]:
    """Every connectable provider, with the same two flags /platform-info exposes.

    ``requestable`` mirrors routers/platform_info.py: ``not visible_in_env`` —
    the owner cannot START that connection themselves in this environment.
    """
    from core.services.credentials import registry

    rows = [
        IntegrationRow(
            key=p.key,
            display_name=p.display_name,
            byo_required=bool(p.byo_required),
            requestable=not registry.visible_in_env(p),
        )
        for p in registry.all_providers().values()
    ]
    return sorted(rows, key=lambda r: r.display_name.lower())


# Statuses under which #2254 has already filed the company-Stripe setup card
# (it fires on the trial_local -> active transition and on direct-to-paid entry;
# renewals and past-due keep the existing card).
_PAID_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing", "past_due"})


def _stripe_line(billing_row: Mapping[str, object]) -> str:
    if billing_row.get("company_stripe_account_id"):
        return (
            "- **Stripe (the company's own account): connected.** Payments and "
            "provisioning are unlocked; nothing to ask for."
        )
    if str(billing_row.get("subscription_status") or "") in _PAID_SUBSCRIPTION_STATUSES:
        return (
            "- **Stripe (the company's own account): required — a setup card is "
            "already in the owner's Inbox.** Do not file another request; say the "
            "card is waiting for them."
        )
    return (
        "- **Stripe (the company's own account): required** for payments and for "
        "provisioning hosting and analytics. It is set up when the owner subscribes "
        "— say so; do not request it."
    )


def render_integrations_md(rows: Sequence[IntegrationRow], billing_row: Mapping[str, object]) -> str:
    """The ``## Integrations`` section appended to BRIEF.md."""
    lines = [
        "",
        "## Integrations",
        "",
        "What can be connected here, as of provisioning. The owner connects from the",
        "Integrations tab in their own browser. On a first run you never fetch this",
        "catalog and never open sign-in pages — name the two or three the plan needs,",
        "one line each on what they unlock, and leave the connecting to them.",
        "",
        _stripe_line(billing_row),
        "",
    ]
    for r in rows:
        if r.byo_required:
            how = "needs you (bring your own app)"
        elif r.requestable:
            how = "by request"
        else:
            how = "one-click"
        lines.append(f"- {r.display_name} (`{r.key}`): {how}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider -k "integrations_md or integration_rows"`
Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

```bash
cd apps/backend && uv run ruff check core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py && uv run ruff format core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py
cd ../.. && git add apps/backend/core/services/onboarding_brief.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): render the integrations menu into BRIEF.md from the registry

The first run had Friday curl /platform-info, parse the flags, mint links and
open tabs — the bulk of a \$3 trial cap. The backend already knows the catalog
and the owner's Stripe state; render both into BRIEF.md at fulfilment. Stripe is
stated, never requested: #2254 files the card when a subscription goes live.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
```

---

### Task 2: Stage the section with BRIEF.md

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` — `stage_first_run_files` (line ~884) and its call in `_fulfil_pending_brief` (line ~713)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py` (append)

**Interfaces:**
- Consumes: `render_integrations_md`, `integration_rows_from_registry` (Task 1); `core.repositories.billing_repo.get_by_owner_id(owner_id) -> dict | None` (existing).
- Produces: `stage_first_run_files(*, workspace, owner_id, friday_agent_id, brief_md, integrations_md: str = "") -> None` — writes `brief_md + integrations_md` to `workspace-{friday_agent_id}/BRIEF.md`.

- [ ] **Step 1: Write the failing test**

```python
class _CapturingWorkspace:
    def __init__(self):
        self.writes: list[tuple[str, str, str]] = []

    def write_file(self, owner_id, rel_path, content):
        self.writes.append((owner_id, rel_path, content))


def test_stage_first_run_files_appends_the_integrations_section_to_brief_md():
    ws = _CapturingWorkspace()
    stage_first_run_files(
        workspace=ws,
        owner_id="user_1",
        friday_agent_id="friday-uuid",
        brief_md="# BRIEF.md — Onboarding brief\n",
        integrations_md="\n## Integrations\n\n- Stripe (`stripe`): one-click\n",
    )
    assert ws.writes == [
        (
            "user_1",
            "workspace-friday-uuid/BRIEF.md",
            "# BRIEF.md — Onboarding brief\n\n## Integrations\n\n- Stripe (`stripe`): one-click\n",
        )
    ]


def test_stage_first_run_files_without_a_section_is_unchanged():
    ws = _CapturingWorkspace()
    stage_first_run_files(workspace=ws, owner_id="u", friday_agent_id="f", brief_md="brief\n")
    assert ws.writes == [("u", "workspace-f/BRIEF.md", "brief\n")]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider -k "stage_first_run_files"`
Expected: FAIL with `TypeError: stage_first_run_files() got an unexpected keyword argument 'integrations_md'`.

- [ ] **Step 3: Implement**

Change the signature and body of `stage_first_run_files`:

```python
def stage_first_run_files(
    *,
    workspace: Workspace,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
    integrations_md: str = "",
) -> None:
    """Write BRIEF.md (brief + the staged integrations section), unconditionally.
    ...existing docstring...
    """
    base = f"workspace-{friday_agent_id}"
    workspace.write_file(owner_id, f"{base}/BRIEF.md", brief_md + integrations_md)
```

In `_fulfil_pending_brief`, immediately before the `stage_first_run_files(` call, add:

```python
        from core.repositories import billing_repo

        # Deterministic menu + Stripe status for the first run (see the
        # render_integrations_md header). A missing billing row renders the
        # trial wording, which is the safe default: "set up when you subscribe".
        billing_row = await billing_repo.get_by_owner_id(owner_id) or {}
        integrations_md = render_integrations_md(integration_rows_from_registry(), billing_row)
```

and pass `integrations_md=integrations_md,` into the `stage_first_run_files(` call.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider`
Expected: all passed (the two new + everything existing).

- [ ] **Step 5: Lint and commit**

```bash
cd apps/backend && uv run ruff check core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py && uv run ruff format core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py
cd ../.. && git add apps/backend/core/services/onboarding_brief.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): stage the integrations section alongside BRIEF.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
```

---

### Task 3: BRIEF.md stops asking for person-research on the with-brief path

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` — `_BRIEF_TEMPLATE` (lines 39–53)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py`

**Interfaces:** none new. `_NO_IDEA_BLOCK` (the "you pick" path) is unchanged — there, choosing the business is job zero and person-research earns its cost.

- [ ] **Step 1: Write the failing test**

```python
def test_brief_md_with_an_idea_does_not_ask_for_person_research():
    md = render_brief_md(path="new", user_input="handmade candles", detail=None, display_name="Leo", email="l@x.com")
    assert "Research the person" not in md
    assert "handmade candles" in md


def test_brief_md_you_pick_path_still_researches_the_person():
    md = render_brief_md(path="new", user_input=None, detail=None, display_name="Leo", email="l@x.com")
    assert "choosing the business is job zero" in md
    assert "public footprint" in md
```

- [ ] **Step 2: Run to verify the first fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider -k "brief_md_with_an_idea or you_pick_path_still"`
Expected: 1 failed (`"Research the person" not in md`), 1 passed.

- [ ] **Step 3: Implement**

Replace the last paragraph of `_BRIEF_TEMPLATE` (currently `Treat their words as the seed, not the ceiling — on the \`grow\` path the input is usually their website; fetch it first. Research the person as well as the business (their public presence tells you who you work for).`) with:

```python
Treat their words as the seed, not the ceiling — on the `grow` path the
input is usually their website; fetch it first. Keep the research to the
business and its market; the Integrations section below is already researched.
```

(Keep the `{idea_block}`/`{detail_line}`/name/email lines exactly as they are.)

- [ ] **Step 4: Run to verify both pass**

Run: same command. Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/onboarding_brief.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): BRIEF.md scopes first-run research to the business

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
```

---

### Task 4: Rewrite the kickoff to bound the work and stop instructing plumbing

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` — `FIRST_RUN_NO_BRIEF_MESSAGE` (~line 209), `_INTEGRATIONS_STEP` (~281), `_CLOSING_STEPS` (~310), `build_first_run_message` (~320)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py` — rewrite the tests at (current line numbers) 142, 161, 289, 303, 327, 337, 355, 368

**Interfaces:**
- Produces (constants, same names where anchored): `_NAMING_STEP_SENTENCE` (unchanged), `_RESEARCH_BOUND` (new), `_MOVES_STEP` (new), `_INTEGRATIONS_STEP` (rewritten, **name kept**), `_CLOSING_STEPS` (rewritten), `FIRST_RUN_NO_BRIEF_MESSAGE` (rewritten), `build_first_run_message(brief_text: str | None) -> str` (same signature).

- [ ] **Step 1: Rewrite the pinning tests first (they define the new contract)**

Replace the bodies of the eight tests (keep their names where useful; delete `test_kickoff_names_the_flags_that_gate_a_one_click_promise` outright — the backend renders the flags now):

```python
def test_first_run_message_with_brief_leads_with_the_users_ask():
    msg = build_first_run_message("a coffee subscription business")
    assert msg.startswith("I'm starting a coffee subscription business.")
    assert "Read BRIEF.md first" in msg
    assert "at most two web searches" in msg
    assert "five bullets" in msg
    assert "three best next moves" in msg
    assert "Do not create any issues yet" in msg
    assert "I'm watching" in msg


def test_first_run_message_no_brief_falls_back_to_the_you_pick_default():
    msg = build_first_run_message(None)
    assert msg.startswith(FIRST_RUN_NO_BRIEF_MESSAGE)
    assert "pick something promising" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert "three best next moves" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert "I'm watching" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert build_first_run_message("   ") == build_first_run_message(None)


@pytest.mark.parametrize("brief", ["a coffee subscription business", None])
def test_first_run_message_points_at_the_staged_integrations_list(brief):
    msg = build_first_run_message(brief)
    assert "Integrations list in BRIEF.md" in msg
    assert "two or three" in msg
    assert "Integrations tab" in msg
    # The plumbing is gone: no catalog fetch, no link minting, no browser.
    assert "platform-info" not in msg
    assert "lightbulb-connect.sh" not in msg
    assert "authorize" not in msg
    assert "Browser panel" not in msg
    # Stripe is stated by BRIEF.md, never requested by Friday on a first run.
    assert "nothing more about Stripe" in msg


def test_first_run_message_instructs_naming_and_email():
    msg = build_first_run_message("a coffee subscription business")
    assert "lightbulb-mail.sh send --owner" in msg
    assert "three lines" in msg
    assert "company's name" in msg


def test_no_brief_path_also_instructs_email():
    assert "lightbulb-mail.sh send --owner" in build_first_run_message(None)


def test_kickoff_says_the_name_becomes_the_address():
    msg = build_first_run_message("a coffee subscription business")
    assert "email address" in msg


def test_kickoff_names_the_company_before_the_board():
    for brief in ("a coffee subscription business", None):
        msg = build_first_run_message(brief)
        assert msg.index("company's name") < msg.index("three best next moves")


def test_kickoff_ends_by_asking_which_move_to_start():
    for brief in ("a coffee subscription business", None):
        assert build_first_run_message(brief).rstrip().endswith("asking me which move to start.")


def test_kickoff_never_inlines_an_authorization_header():
    """Unchanged rationale: OpenClaw redacts `Authorization: Bearer <x>` on the
    session-append path including a `$VAR` reference; an agent-typed curl works
    once and poisons the next turn (prod 2026-08-30)."""
    for brief in (None, "an online store that sells handmade candles"):
        msg = build_first_run_message(brief)
        assert "Authorization" not in msg
        assert "OPENCLAW_HOOK_TOKEN" not in msg
        assert "bearer" not in msg.lower()
        assert "Integrations tab" in msg
```

(Remove the module-level `CATALOG`/`SCRIPT` constants those tests used if nothing else references them; `grep -n "CATALOG\|^SCRIPT" tests/unit/services/test_onboarding_brief.py` to check.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider -k "first_run_message or kickoff"`
Expected: multiple FAIL (old text), `test_kickoff_never_inlines_an_authorization_header` still passes.

- [ ] **Step 3: Implement the new kickoff**

Replace `FIRST_RUN_NO_BRIEF_MESSAGE`, `_INTEGRATIONS_STEP`, `_CLOSING_STEPS` and `build_first_run_message` with:

```python
# The research is bounded in the MESSAGE, not hoped for from the playbook:
# two runs traced on 2026-09-01 spent 26–40 shell commands and 47–71 model
# calls on a first run — the whole $3 trial cap — most of it plumbing the
# backend already does (see render_integrations_md). Every bound below is a
# number the model can count.
_RESEARCH_BOUND = (
    "Read BRIEF.md first. Use at most two web searches, then give me the market "
    "in five bullets — no report. "
)

# "three best next moves" is pinned by tests and by the naming-order rule:
# the company is named BEFORE anything reaches the board.
_MOVES_STEP = (
    "Then propose my three best next moves, one line each. Do not create any "
    "issues yet — I'll tell you which to start. "
)

FIRST_RUN_NO_BRIEF_MESSAGE = (
    "I'm not sure what to build yet — read BRIEF.md and pick something promising "
    f"for me, then tell me what you chose and why. {_NAMING_STEP_SENTENCE} "
    f"{_RESEARCH_BOUND}{_MOVES_STEP}I'm watching."
)

# Name kept: the lightbulb skill's maintenance header anchors on
# `_INTEGRATIONS_STEP`. The catalog is now staged into BRIEF.md by the backend
# (render_integrations_md); Friday's only job here is the judgment call.
_INTEGRATIONS_STEP = (
    "\n\nFrom the Integrations list in BRIEF.md, name the two or three that serve "
    "those moves, one line each on what they unlock — I'll connect them from the "
    "Integrations tab. Don't fetch the catalog and don't open sign-in pages; the "
    "list is already there. Whatever BRIEF.md says about Stripe, repeat it and "
    "say nothing more about Stripe."
)

_CLOSING_STEPS = (
    "\n\nThen email me three lines in your own voice — what you found and the one "
    "move you'd start with:\n"
    "printf '%s' \"<your note>\" | /app/skills/lightbulb/scripts/lightbulb-mail.sh send --owner "
    '--subject "<subject>"\n'
    "That script is how you send email here — there is no Gmail, himalaya, or "
    "email channel to configure. Finish by asking me which move to start."
)


def build_first_run_message(brief_text: str | None) -> str:
    """Friday's first-run kickoff chat.send message — the VISIBLE first user
    message in her chat.

    Bounded on purpose (see _RESEARCH_BOUND). ``brief_text`` present → lead with
    the owner's ask; absent/blank → the "you pick" default. Both paths carry the
    same bounds, the same staged-integrations instruction, the same three-line
    email (first contact), and end with the same question.
    """
    brief = (brief_text or "").strip()
    if not brief:
        return FIRST_RUN_NO_BRIEF_MESSAGE + _INTEGRATIONS_STEP + _CLOSING_STEPS
    return (
        f"I'm starting {brief}. {_NAMING_STEP_SENTENCE} {_RESEARCH_BOUND}{_MOVES_STEP}I'm watching."
        + _INTEGRATIONS_STEP
        + _CLOSING_STEPS
    )
```

Delete the old `_RESEARCH…`-free with-brief string and the comment block above `_INTEGRATIONS_STEP` that described the redaction mechanism (it moved to `render_integrations_md`'s header); keep the `# NB: the kickoff must leave the three tasks in the BACKLOG …` comment above `FIRST_RUN_NO_BRIEF_MESSAGE`, updated to say the moves are now PROPOSED, not filed.

- [ ] **Step 4: Run the whole file, then the services suite**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_onboarding_brief.py -q -p no:cacheprovider`
Expected: all passed.
Run: `cd apps/backend && uv run pytest tests/unit/services -q -p no:cacheprovider`
Expected: all passed (this directory is where the last pinning-test miss was found; run it whole).

- [ ] **Step 5: Lint and commit**

```bash
cd apps/backend && uv run ruff check core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py && uv run ruff format core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py
cd ../.. && git add apps/backend/core/services/onboarding_brief.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): bound Friday's first run — two searches, five bullets, three moves, one question

Keeps the market brief and the integrations recommendation the owner asked
to keep; removes the plumbing (catalog curl, flag analysis, link minting,
browser tabs, issue creation) that cost 26–40 shell commands per run.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
```

---

### Task 5: Cross-file checks nothing else pinned the old kickoff

**Files:**
- Read-only: `apps/backend/tests/`, `apps/mobile/src`, `apps/frontend/src`

- [ ] **Step 1: Grep for the old strings**

Run from the worktree root:
```bash
git grep -n "Dig into the business\|three best next moves in the backlog\|connections/u/\$ISOL8_OWNER_ID\|platform-info\` — then" -- apps/backend/tests apps/frontend/src apps/mobile/src
```
Expected: no hits outside `test_onboarding_brief.py`. If any file pins the old text, update its assertion to the new text from Task 4 and include it in the commit below.

- [ ] **Step 2: Run the full backend unit tree once**

Run: `cd apps/backend && uv run pytest tests/unit -q -p no:cacheprovider`
Expected: all passed (a few skipped is normal).

- [ ] **Step 3: Commit any test fix-ups**

```bash
git add -A apps/backend/tests
git commit -m "test(onboarding): re-pin the first-run kickoff after the bounded rewrite

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
```
(Skip the commit if nothing changed.)

---

### Task 6: Measure on dev, open the PR, delete the docs

**Files:**
- Delete: `docs/superpowers/specs/2026-09-02-first-run-light-design.md`, `docs/superpowers/plans/2026-09-02-first-run-light.md` (CLAUDE.md: plans are disposable — delete in the PR that ships them)

- [ ] **Step 1: Push and open the PR (do not merge)**

```bash
git push -u origin feat/first-run-light
gh pr create --repo TheLightbulbCompany/lightbulb --base main --head feat/first-run-light \
  --title "feat(onboarding): Friday's first run, made light — staged integrations menu + bounded kickoff" \
  --body-file /path/to/body.md
```
Body: the spec's Problem table, the Design bullets, the Constraints, and a "Measurement" section left as `pending` until Step 3. End with:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh
```

- [ ] **Step 2: Reviews**

Dispatch an Opus review of the diff (read-only, findings with file:line, SHIP/BLOCK), then `Skill(code-review, args="feat/first-run-light high")`; if `/code-review`'s report does not arrive as a notification, pull it with `SendMessage(to="code-review", …)`. Fold confirmed findings into one amend; re-run Task 4 Step 4; force-push with `--force-with-lease`.

- [ ] **Step 3: Measure on dev (after merge → dev deploy)**

Merge only after reviews are SHIP and CI is green. Then wait for the dev deploy of the merge commit to complete (`gh run list --workflow deploy.yml --limit 3`) and confirm `lightbulb-dev-backend` advanced (`aws ecs describe-services --cluster lightbulb-dev-container --services lightbulb-dev-backend --query 'services[0].taskDefinition'`). Then, with the dev Clerk secret:

```bash
export AWS_PROFILE=isol8-admin
CK=$(aws secretsmanager get-secret-value --secret-id lightbulb/dev/clerk_secret_key --region us-east-1 --query SecretString --output text)
curl -sX POST https://api.clerk.com/v1/sign_in_tokens -H "Authorization: Bearer $CK" -H 'Content-Type: application/json' -d '{"user_id":"user_3IeOW2y8Y1o84mB2dnxZ54Ok9Xp"}'   # -> token (minutes TTL)
```
In a Claude-in-Chrome tab on `https://dev.flickmeon.ai/chat`:
```js
const si = await window.Clerk.client.signIn.create({strategy:'ticket', ticket:'<token>'});
await window.Clerk.setActive({session: si.createdSessionId});
const tok = await window.Clerk.session.getToken();
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision',{method:'DELETE',headers:{Authorization:'Bearer '+tok}}).then(r=>r.json());
// wait ~90s for the ECS service to go INACTIVE, then:
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision',{method:'POST',headers:{Authorization:'Bearer '+tok}}).then(r=>r.json());
```
Reload `/chat`, submit a real brief in the funnel (e.g. "an online store that sells handmade candles"), wait for `bootstrap_state=delivered` (`GET /api/v1/container/status` → `bootstrap.state`). Then measure the run window (UTC):

```bash
# tool calls by name (backend gateway events) — replace START/END epoch seconds
python3 scratchpad/q.py /ecs/lightbulb-dev START END 'filter @message like /user_3IeOW2y8Y1o84mB2dnxZ54Ok9Xp/ and @message like /stream=item phase=start/ | parse @message /name=(?<tool>[a-z_]+)/ | stats count() as n by tool | sort n desc' 20
# Bedrock invocations on the owner's AIP for that day (ModelId = the AIP id from lightbulb-dev-containers.bedrock_aip_arns)
aws cloudwatch get-metric-statistics --namespace AWS/Bedrock --metric-name Invocations --dimensions Name=ModelId,Value=<aip-id> --start-time <day>T00:00:00Z --end-time <day+1>T00:00:00Z --period 86400 --statistics Sum --region us-east-1
# spend
aws dynamodb get-item --table-name lightbulb-dev-billing-accounts --region us-east-1 --key '{"owner_id":{"S":"user_3IeOW2y8Y1o84mB2dnxZ54Ok9Xp"}}' --query 'Item.trial_daily_spend_microcents'
```
Record: tool calls by name, invocations, spend, and what the owner's chat shows (screenshot). Targets from the spec: ≤ 15 tool calls, ≤ 20 invocations, ≤ $0.60. Paste before/after into the PR body. If the target is missed, that is the signal for phase 2 (cheap tier / lobster) — do not widen this PR.

- [ ] **Step 4: Delete the docs and finish**

```bash
git rm docs/superpowers/specs/2026-09-02-first-run-light-design.md docs/superpowers/plans/2026-09-02-first-run-light.md
git commit -m "docs: delete the shipped first-run plan and spec (PR description is the record)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012AiR11BZ4xMht2PDVKQhPh"
git push
```
(If the docs are still needed for the review conversation, delete them in a follow-up commit right after merge instead — never leave them to accumulate.)

---

## Self-review

- **Spec coverage:** staged menu (T1+T2) ✓; Stripe line, never requested (T1) ✓; person-research dropped on with-brief path, kept on you-pick (T3) ✓; bounds — two searches, five bullets, three moves not filed, two–three integrations from the list, three-line email, closing question (T4) ✓; constraints — no `apps/infra/openclaw/**`, `_INTEGRATIONS_STEP` name kept, forbidden strings pinned (T4 tests) ✓; measurement + targets (T6) ✓; out-of-scope items named in the spec, not in tasks ✓.
- **Placeholder scan:** no TBD/TODO; every code step has code; the one "if any file pins the old text" branch in T5 gives the exact grep and the exact action.
- **Type consistency:** `IntegrationRow` fields (`key`, `display_name`, `byo_required`, `requestable`) match between T1 impl, T1 tests and T2; `render_integrations_md(rows, billing_row)` signature identical in T1 and T2; `stage_first_run_files(..., integrations_md: str = "")` identical in T2 impl, T2 test and the `_fulfil_pending_brief` call; kickoff strings asserted in T4 tests appear verbatim in T4 constants (`Read BRIEF.md first`, `at most two web searches`, `five bullets`, `three best next moves`, `Do not create any issues yet`, `Integrations list in BRIEF.md`, `two or three`, `Integrations tab`, `nothing more about Stripe`, `three lines`, `asking me which move to start.`, `company's name`, `email address`, `I'm watching`).
