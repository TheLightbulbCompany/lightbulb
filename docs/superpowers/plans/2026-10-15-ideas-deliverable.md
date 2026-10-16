# Ideas: deliverable — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An idea started with Let's do it ends done with proof, waiting on one clear ask, or stuck with a reason — never silently stopped.

**Architecture:** Each idea carries `needs` and `done_means`, rendered into its Work item. Tasks from ideas start in `todo` so Paperclip runs them; the backend resumes work when a needed service is connected. The bridge gives Work-started runs their DM room for sign-in asks. The agent's helper and instruction file get the flags and rules to file work that wakes, hand person-steps over, and prove done. Stripe Projects lands credentials on AgentCore.

**Tech Stack:** Python 3.12 (FastAPI backend, Hermes bridge), bash (`paperclip-api.sh`), YAML catalog, pytest; the Paperclip fork is used through `PaperclipAdminClient` only.

**Spec:** `docs/superpowers/specs/2026-10-15-ideas-deliverable-design.md`

## Global Constraints

- Work on a fresh worktree from `origin/main` under `lightbulb.nosync/.claude/worktrees/` (owner rule; never stack on another branch).
- No prices in the app; the person's DM message stays exactly `start_text()` as today (spec §2.1).
- Every catalog idea must have `needs` and `done_means`; the catalog fails loudly on a missing one (spec §3).
- `needs[].service` values are `providers.yaml` top-level keys or one of `mailbox`, `browser`, `meet`; a test enforces it (Task 1).
- No new approval card for a connect (owner, 2026-10-15); the connect wake is one issue comment.
- Backend tests: `cd apps/backend && uv run pytest -q <files>`; bridge tests: `cd apps/infra/hermes && python -m pytest -q <files>` (CI runs the full suite; locally run only the touched files).
- Commit after each task; one PR for the whole plan; merge on a clean Codex review.
- Never modify prod directly; the prod switch in Task 9 is flipped by the owner.

## Review Focus

1. An idea whose `needs` names a provider key that is not in the registry: the connect wake would never match. Task 1's `test_every_need_names_a_known_service` pins it.
2. A connect for a provider two open starts need: both get exactly one comment each, and a second connect of the same provider adds none. Task 4's idempotency test pins it.
3. A tracked idea's DM message must never start a second run beside Paperclip's; a quick idea's message must still start one. Task 3's two wake tests pin it.
4. A Paperclip run with no bound conversation still passes `None` as the room (a delegated task), so nothing regresses to posting in a wrong room. Task 5's second test pins it.
5. `land_credentials` with an empty `env` (a provider that returned no access configuration) must still write the pointer and not raise on `patch_profile_env_and_config` with nothing to merge. Task 8's empty-env test pins it.

---

### Task 1: `needs` and `done_means` on every idea

**Files:**
- Modify: `apps/backend/core/services/ideas.py:56-92` (the `Idea` model and `_NOT_SERVED`)
- Modify: `apps/backend/ideas/*.yaml` (all 35)
- Test: `apps/backend/tests/unit/services/test_ideas.py`

**Interfaces:**
- Produces: `Idea.needs: list[Need]` where `Need` has `service: str`, `access: Literal["read", "write"]`; `Idea.browser: list[str]`; `Idea.person: list[str]`; `Idea.done_means: str`. `PLATFORM_SERVICES = frozenset({"mailbox", "browser", "meet"})`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_ideas.py`:

```python
def test_every_idea_has_needs_and_done_means():
    for idea in ideas.catalog().values():
        assert idea.needs or idea.browser or idea.person, idea.id
        assert idea.done_means and len(idea.done_means) <= 200, idea.id
        for need in idea.needs:
            assert need.access in ("read", "write"), idea.id


def test_every_need_names_a_known_service():
    from core.services.credentials import registry

    for idea in ideas.catalog().values():
        for need in idea.needs:
            if need.service in ideas.PLATFORM_SERVICES:
                continue
            registry.get_provider(need.service)  # raises for an unknown key


@pytest.mark.asyncio
async def test_needs_and_done_means_are_not_served(paperclip):
    first = (await ideas.list_ideas(AUTH))["ideas"][0]
    assert "needs" not in first and "doneMeans" not in first and "person" not in first
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py -k "needs or done_means or known_service"`
Expected: FAIL (`Idea` has no attribute `needs`; validation error on load).

- [ ] **Step 3: Add the fields to the model**

In `apps/backend/core/services/ideas.py`, above `class Idea`:

```python
#: Services an idea uses that are not connectors: the team mailbox, the shared browser, Meet.
PLATFORM_SERVICES = frozenset({"mailbox", "browser", "meet"})


class Need(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(pattern=r"^[a-z0-9_]+$", max_length=40)
    access: Literal["read", "write"] = "read"
```

In `class Idea`, after `deliverables`:

```python
    #: What the idea uses (spec 2026-10-15 §2.1): connector keys from providers.yaml or a
    #: platform service, each read or write; websites worked in the shared browser; steps only
    #: a person can do. Rendered into the Work item, never into the DM message.
    needs: list[Need] = Field(default_factory=list)
    browser: list[str] = Field(default_factory=list)
    person: list[str] = Field(default_factory=list)
    #: One line a check can read; the agent proves it before `done`.
    done_means: str = Field(min_length=1, max_length=200)
```

Change `_NOT_SERVED` to:

```python
_NOT_SERVED = {"agency_price_low", "agency_price_high", "price_unit", "agency_turnaround",
               "needs", "browser", "person", "done_means"}
```

- [ ] **Step 4: Add the values to the 35 YAML files**

Run this once from the repo root (it inserts the four fields before `agency_price_low`, or at the end when there is no price):

```bash
cd apps/backend/ideas && uv run --with pyyaml python - <<'EOF'
import pathlib
N = {
 "accessibility-fixes": (["github:write"], ["the site"], ["Merge the pull request"],
   "The PR is merged and a re-scan of every page shows no remaining contrast, label, alt or keyboard errors."),
 "answer-leads-fast": (["google:write", "mailbox"], [], ["Approve the reply template"],
   "Every new lead email in the period got a reply within 15 minutes offering a call, and each is logged on the task."),
 "answer-support-email": (["google:write", "notion:read", "intercom:read"], [], ["Point at the help docs", "Approve the first replies"],
   "Every customer email in the period has a reply grounded in the help docs, and a list of doc gaps is on the task."),
 "books-cleanup": (["quickbooks:write", "google:write"], ["the bank's site"], ["Connect the bank feed", "Decide the flagged transactions"],
   "Six months of transactions are categorised and reconciled in QuickBooks, and the accountant has the package."),
 "cancel-or-renegotiate-bill": (["quickbooks:read", "stripe:read", "google:write"], ["the vendor's portal"], ["Approve the cancel or the new price", "Phone-only vendors"],
   "The vendor confirmed the cancellation or the new price in writing, linked on the task."),
 "chase-overdue-invoices": (["quickbooks:write", "stripe:read", "google:write"], [], ["Approve each reminder"],
   "Every overdue invoice has a reminder with a pay link sent after approval, each with its sent timestamp on the task."),
 "claim-refund": (["stripe:read", "quickbooks:read", "google:write"], ["the vendor's claim form"], ["Approve the claim text"],
   "The claim was sent with evidence and the vendor's answer, or the credit, is linked on the task."),
 "cold-email-engine": (["cloudflare:write", "apollo:read", "google:write"], ["the registrar"], ["Domain purchase and KYC", "Approve each first batch"],
   "Three warmed domains send a 3-step sequence to 200 fitting accounts and replies land in the inbox, with the list on the task."),
 "crm-setup": (["attio:write", "google:read", "apollo:read"], [], ["Approve the import"],
   "Every contact is in the CRM with no duplicates, the stages exist, and three workflows run, each linked on the task."),
 "deadline-sweep": (["vercel:read", "cloudflare:read", "aws:read", "google:read"], ["the registrar", "the state portal"], ["Sign in to the registrar and state portal", "Pay the fees"],
   "A dated list of every domain, SSL and annual-report deadline is on the task, and each renewal is confirmed or assigned to the person."),
 "dependency-prs": (["github:write"], [], ["Approve every merge"],
   "Every open dependency and security PR has a review comment and a safe/unsafe verdict, and the approved ones are merged."),
 "directory-listings": (["mailbox"], ["each directory", "Google Business Profile"], ["Approve the list and the text", "Type CAPTCHAs", "Google verification"],
   "Each directory on the approved list has a live listing URL on the task, and the Google Business Profile is claimed."),
 "email-flows": (["shopify:read", "cloudflare:write"], ["Klaviyo or Shopify Email"], ["Approve each email", "Approve go-live"],
   "Welcome, abandoned-cart and post-purchase flows are switched on and a test order triggers each, with screenshots on the task."),
 "google-ads-search": (["github:write"], ["Google Ads"], ["Billing and budget", "Press launch"],
   "Conversion tracking fires on the site, and three ad groups with ads are built and ready to launch, each linked on the task."),
 "hire-pipeline": (["google:write", "apollo:read"], ["the job boards"], ["Board accounts and payment", "Interviews and the decision"],
   "The job is posted, 50 candidates are listed with a note each, every applicant got an answer, and interviews are offered."),
 "inbox-triage": (["google:write"], [], ["Approve sends until you trust them"],
   "Every email from the period is labelled, routine ones are answered, and the needs-you list was posted in the DM."),
 "interview-scheduling": (["google:write"], [], ["Choose the times", "Approve rejections"],
   "Every candidate has an offered slot or a status reply, and the pipeline board matches the inbox."),
 "investor-update": (["stripe:read", "quickbooks:read", "google:write"], [], ["Approve the numbers and the text"],
   "The model is updated with numbers that tie to Stripe and QuickBooks, and the update was sent after approval, linked on the task."),
 "landing-page-checkout": (["github:write", "vercel:write", "stripe:write", "posthog:read", "cloudflare:write"], [], ["Approve the words", "Approve the DNS change and publish"],
   "The page is live on the domain, a test signup or checkout completes, and the event shows in analytics, linked on the task."),
 "launch-run": (["x:write", "google:write"], ["Product Hunt", "the directories", "the press wire"], ["Product Hunt account and CAPTCHA", "The wire fee", "Approve every public word"],
   "The Product Hunt page, the directory listings, the press release and the posts are live, each URL on the task."),
 "linkedin-ghostwriting": (["google:read"], ["LinkedIn"], ["Sign in to LinkedIn once", "Approve each post"],
   "Two or three approved posts are published each week, each URL on the task."),
 "local-listings-reviews": (["stripe:read", "shopify:read"], ["each directory", "Google Business Profile"], ["Record the verification video", "Approve the review email", "Type CAPTCHAs"],
   "40 listings are live, the Google profile is filled, and the review request went to the last 50 customers after approval."),
 "meeting-recaps": (["google:read", "meet"], [], ["A member invites agents@ to the meeting", "Approve the recap"],
   "Each meeting has a recap sent to attendees and its action items on the board, linked on the task."),
 "meta-ads-setup": (["meta_ads:write", "shopify:read", "github:write"], [], ["Meta Business verification", "Approve the budget"],
   "The Pixel and Conversions API fire on a test order, and the first campaigns exist in Ads Manager, linked on the task."),
 "monthly-receipts-package": (["google:write", "quickbooks:read"], ["vendor portals"], ["Approve the package"],
   "Every receipt for the month is in one folder, missing W-9s were requested, and the package was sent to the accountant."),
 "product-listings-rewrite": (["shopify:write", "parallel:read"], ["Seller Central"], ["Grant write scopes", "Approve the first 5, then the batch"],
   "The top 50 listings are rewritten and updated, with a before/after list on the task."),
 "sales-tax-registration": (["stripe:read", "shopify:read", "quickbooks:read"], ["each state portal"], ["Identity checks and signatures", "The fees"],
   "The nexus analysis is on the task, and each registration is confirmed by the state or assigned to the person."),
 "send-invoices-on-time": (["quickbooks:write", "stripe:read"], [], ["Approve the amounts"],
   "Every invoice due in the period was sent on its billing date after approval, each with its sent timestamp."),
 "seo-audit-fixes": (["github:write"], ["the site"], ["Merge the pull request"],
   "The audit is on the task, the PR with the fixes is merged, and the before/after numbers are posted."),
 "shopify-store-setup": (["shopify:write"], ["the Shopify admin"], ["App with write scopes", "Payments KYC", "Approve go-live"],
   "The store has the theme, 50 products, shipping, tax and policies set, and a test checkout completes."),
 "soc2-readiness": (["github:write", "aws:read", "google:write"], ["the compliance platform"], ["Platform signup and billing", "Choose the auditor", "Approve each policy"],
   "Every policy is written and approved, each control has a pass or a fix PR, and the gap list is on the task."),
 "stripe-subscriptions": (["github:write", "stripe:write"], [], ["Merge the pull request", "Switch on live keys"],
   "Plans, trials, upgrades, the portal and webhooks work in test mode with passing tests, and the PR is merged."),
 "three-quotes-book-vendor": (["parallel:read", "mailbox", "google:write"], [], ["Choose the vendor", "Approve the booking", "Phone-only vendors"],
   "Three quotes are compared on the task, and the chosen vendor confirmed the booking in writing."),
 "unused-software-seats": (["quickbooks:read", "stripe:read", "google:read"], ["each tool's admin"], ["Sign in to each tool", "Approve each removal"],
   "Every paid tool is listed with its unused seats, and each approved removal is confirmed with the new monthly cost."),
 "website-rebuild": (["github:write", "vercel:write", "cloudflare:write"], ["the old site"], ["Approve the words", "Approve the DNS change"],
   "The new site is live on the domain, every old URL redirects, and the before/after links are on the task."),
}
files = sorted(pathlib.Path('.').glob('*.yaml'))
assert {f.stem for f in files} == set(N), set(N) ^ {f.stem for f in files}
def q(s): return '"' + s.replace('"', '\\"') + '"'
for f in files:
    needs, browser, person, done = N[f.stem]
    block = "needs:\n" + "".join(f"  - {{service: {n.split(':')[0]}, access: {n.split(':')[1] if ':' in n else 'read'}}}\n" for n in needs)
    block += "browser: [" + ", ".join(q(b) for b in browser) + "]\n"
    block += "person:\n" + "".join(f"  - {q(p)}\n" for p in person)
    block += f"done_means: {q(done)}\n"
    lines = f.read_text().splitlines(keepends=True)
    i = next((n for n, l in enumerate(lines) if l.startswith("agency_price_low:")), len(lines))
    lines.insert(i, block)
    f.write_text("".join(lines))
print("ok", len(files))
EOF
```

If `test_every_need_names_a_known_service` fails on a key (the registry's keys may differ from `x`, `meta_ads`, `vercel`, `aws`), read the top-level keys of `apps/backend/core/services/credentials/providers.yaml` and fix the YAML to the registry's spelling. Do not add aliases in code.

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/ideas apps/backend/core/services/ideas.py apps/backend/tests/unit/services/test_ideas.py
git commit -m "ideas: every idea says what it needs and what done means"
```

---

### Task 2: Render Needs and Done means into the Work item

**Files:**
- Modify: `apps/backend/core/services/ideas.py:105-113` (`Job`), `:225-237` (`_job`), `:500-530` (`_start` bodies)
- Test: `apps/backend/tests/unit/services/test_ideas.py`

**Interfaces:**
- Consumes: `Idea.needs`, `.browser`, `.person`, `.done_means` from Task 1.
- Produces: `work_description(job: Job) -> str`; `Job.needs: list[Need]`, `Job.browser: list[str]`, `Job.person: list[str]`, `Job.done_means: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_a_task_idea_carries_needs_and_done_means_in_work(paperclip):
    await ideas.start(AUTH, "inbox-triage")
    body = paperclip.create_issue.await_args.kwargs["body"]
    assert "## Needs\n" in body["description"] and "- google (write)" in body["description"]
    assert "Approve sends until you trust them" in body["description"]
    assert body["description"].rstrip().endswith("## Done means\n" + ideas.catalog()["inbox-triage"].done_means)


@pytest.mark.asyncio
async def test_a_project_idea_carries_needs_and_done_means_in_work(paperclip):
    await ideas.start(AUTH, "launch-run")
    body = paperclip.create_project.await_args.kwargs["body"]
    assert "## Needs\n" in body["description"] and "Product Hunt" in body["description"]
    assert "## Done means" in body["description"]


def test_work_description_is_plain_for_a_suggestion_without_needs():
    job = ideas.Job("s-1", "Answer reviews", "Fourteen reviews wait.", ["Every review answered"], "task", {"id": "a", "name": "A"})
    text = ideas.work_description(job)
    assert text == "Fourteen reviews wait.\n\nWhat's included:\n- Every review answered"
```

Check the existing `start_text` tests still hold: the DM message must not change.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py -k "carries_needs or plain_for_a_suggestion"`
Expected: FAIL (`work_description` not defined; `Job` has no `needs`).

- [ ] **Step 3: Implement**

Change `Job`:

```python
@dataclass(frozen=True)
class Job:
    """What Start needs, from a catalog idea or an agent's suggestion."""

    id: str
    title: str
    why: str
    deliverables: list[str]
    size: str
    agent: dict
    needs: list[Need] = field(default_factory=list)
    browser: list[str] = field(default_factory=list)
    person: list[str] = field(default_factory=list)
    done_means: str | None = None
```

(`from dataclasses import dataclass, field`.) In `_job`, the catalog branch becomes:

```python
        return Job(idea.id, idea.title, idea.why, list(idea.deliverables), idea.size, agent,
                   list(idea.needs), list(idea.browser), list(idea.person), idea.done_means)
```

Add, next to `start_text`:

```python
def work_description(job: Job) -> str:
    """The Work item's text: the why, what's included, and (catalog ideas) what it needs and what
    done means. The agent reads these here; the DM message stays the plain prompt."""
    text = job.why + "\n\nWhat's included:\n" + "\n".join(f"- {d}" for d in job.deliverables)
    needs = [f"- {n.service} ({n.access})" for n in job.needs]
    needs += [f"- browser: {site}" for site in job.browser]
    needs += [f"- you: {step}" for step in job.person]
    if needs:
        text += "\n\n## Needs\n" + "\n".join(needs)
    if job.done_means:
        text += "\n\n## Done means\n" + job.done_means
    return text
```

In `_start`, the project body gets `"description": work_description(job),` after `"name"`, and the task body's `"description"` becomes `work_description(job)`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ideas.py apps/backend/tests/unit/services/test_ideas.py
git commit -m "ideas: the Work item carries Needs and Done means"
```

---

### Task 3: Tasks from ideas are created in `todo`; the DM message does not wake (owner ruling 2026-10-16)

**Files:**
- Modify: `apps/backend/core/services/ideas.py` (`_start`: the task body's status; a first task inside a project; `wake=False` on the DM message)
- Modify: `apps/backend/core/services/messaging.py:4022-4110` (`create_human_message` gains `wake: bool = True`)
- Test: `apps/backend/tests/unit/services/test_ideas.py`, and the messaging test file that covers `create_human_message` (find it with `git grep -l "create_human_message" apps/backend/tests`)

**Interfaces:**
- Consumes: `work_description(job)` from Task 2; `work_idempotency_key(...)` (`key`) already computed in `_start`.
- Produces: `create_human_message(auth, room_id, request, *, via=..., wake: bool = True)`; a project start's first task id stored on the start row as `firstIssueId`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_a_task_idea_is_created_in_todo_and_assigned(paperclip):
    await ideas.start(AUTH, "inbox-triage")
    body = paperclip.create_issue.await_args.kwargs["body"]
    assert body["status"] == "todo" and body["assigneeAgentId"] == STARTERS["customer_operations"]


@pytest.mark.asyncio
async def test_a_project_idea_gets_one_todo_task_inside_it(paperclip):
    start = await ideas.start(AUTH, "launch-run")
    body = paperclip.create_issue.await_args.kwargs["body"]
    assert body["projectId"] == start["projectId"] and body["status"] == "todo"
    assert body["title"] == ideas.catalog()["launch-run"].title
    assert body["assigneeAgentId"] == STARTERS["growth"]
    assert body["idempotencyKey"].endswith(":task") and "## Done means" in body["description"]
    row = (await messaging_repo.open_idea_starts(OWNER))[0]
    assert row["firstIssueId"] == "issue-1"


@pytest.mark.asyncio
async def test_the_dm_record_for_a_tracked_idea_does_not_wake_the_agent(paperclip, monkeypatch):
    calls = []
    real = ideas.messaging.create_human_message
    async def spy(*a, **kw):
        calls.append(kw.get("wake", True))
        return await real(*a, **kw)
    monkeypatch.setattr(ideas.messaging, "create_human_message", spy)
    await ideas.start(AUTH, "inbox-triage")
    assert calls == [False]


@pytest.mark.asyncio
async def test_a_quick_idea_still_wakes_the_agent_from_the_dm(paperclip, monkeypatch):
    calls = []
    real = ideas.messaging.create_human_message
    async def spy(*a, **kw):
        calls.append(kw.get("wake", True))
        return await real(*a, **kw)
    monkeypatch.setattr(ideas.messaging, "create_human_message", spy)
    await ideas.start(AUTH, QUICK_IDEA_ID)   # any catalog idea with size quick; if none exists, use a suggestion with size "quick" the way the existing suggestion tests do
    assert calls == [True]
```

In the messaging test file, add one test that `create_human_message(..., wake=False)` stores the message (it is listed by `messaging_repo.list_messages`) but does not call the dispatch step, and that the default still does. Find the dispatch step by reading `create_human_message`: it is the call that enqueues room work for the agent (grep `_dispatch_targets` and the enqueue it feeds in `messaging.py`); monkeypatch that function with an `AsyncMock` and assert `await_count` is 0 with `wake=False` and 1 with the default. Reuse that file's existing fixtures for a room with one agent member.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py -k "todo_and_assigned or todo_task_inside or does_not_wake or still_wakes"`
Expected: FAIL (status is backlog; no task inside the project; `wake` not accepted).

- [ ] **Step 3: Implement**

In `messaging.create_human_message`, add `wake: bool = True` to the keyword parameters, document it in the docstring ("False stores and shows the message but does not dispatch the agent: an Ideas record whose work Paperclip already started"), and wrap the dispatch step in `if wake:`. Nothing else in the function changes: the message is stored, published to the room's live channel, and counted as before.

In `ideas._start`:
- the task body: `"status": "todo"`, and replace the backlog comment with: `# todo: Paperclip starts the run at once, like any assigned task (owner, 2026-10-16). The DM message below is the person's record and does not wake.`
- after the project is created (inside `if not project_id:` is wrong — do it whenever the start row has no `firstIssueId`):

```python
            if not claim.get("firstIssueId"):
                first = await admin.create_issue(
                    session_cookie=ctx.session_cookie, company_id=ctx.company_id, body={
                        "title": job.title,
                        "description": work_description(job),
                        "status": "todo",
                        "assigneeAgentId": agent["id"],
                        "projectId": project_id,
                        "idempotencyKey": f"{key}:task",
                        "allowDuplicate": True,
                    })
                await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"firstIssueId": first["id"]})
```

- the DM message: `via="ideas", wake=tracked is None`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py tests/unit/routers/test_ideas_route.py <messaging test file>`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ideas.py apps/backend/core/services/messaging.py apps/backend/tests
git commit -m "ideas: tasks from ideas start in todo; the DM message is a record, not a wake"
```

---

### Task 4: Connecting a service resumes the work

**Files:**
- Modify: `apps/backend/core/services/ideas.py` (new `resume_after_connect`)
- Modify: `apps/backend/core/services/credentials/apply_connection.py:176-193` (`apply_connection`)
- Test: `apps/backend/tests/unit/services/test_ideas.py`, `apps/backend/tests/unit/services/credentials/test_apply_connection.py` (or the existing apply-connection test file; find it with `git grep -l "apply_connection" apps/backend/tests`)

**Interfaces:**
- Consumes: `Idea.needs` (Task 1), `messaging_repo.open_idea_starts`, `update_idea_start`.
- Produces: `async def resume_after_connect(owner_id: str, provider_key: str) -> int` (comments posted).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_connecting_a_needed_service_comments_on_the_waiting_task_once(paperclip):
    paperclip.add_issue_comment = AsyncMock()
    first = await ideas.start(AUTH, "inbox-triage")          # needs google
    second = await ideas.start(AUTH, "answer-leads-fast")     # needs google too
    await ideas.start(AUTH, "dependency-prs")                 # needs github only
    assert await ideas.resume_after_connect(OWNER, "google") == 2
    commented = {c.kwargs["issue_id"] for c in paperclip.add_issue_comment.await_args_list}
    assert commented == {first["issueId"], second["issueId"]}
    text = paperclip.add_issue_comment.await_args.kwargs["body"]["body"]
    assert text.startswith("Google") and "connected" in text
    assert await ideas.resume_after_connect(OWNER, "google") == 0   # idempotent per start


@pytest.mark.asyncio
async def test_connecting_a_needed_service_comments_on_a_projects_open_agent_tasks(paperclip):
    paperclip.add_issue_comment = AsyncMock()
    paperclip.list_issues = AsyncMock(return_value={"issues": [
        {"id": "t1", "status": "todo", "assigneeAgentId": "a1"},
        {"id": "t2", "status": "done", "assigneeAgentId": "a1"},
        {"id": "t3", "status": "todo", "assigneeAgentId": None},
    ]})
    await ideas.start(AUTH, "launch-run")                      # needs x
    assert await ideas.resume_after_connect(OWNER, "x") == 1
    assert paperclip.add_issue_comment.await_args.kwargs["issue_id"] == "t1"


@pytest.mark.asyncio
async def test_connecting_an_unneeded_service_does_nothing(paperclip):
    paperclip.add_issue_comment = AsyncMock()
    await ideas.start(AUTH, "inbox-triage")
    assert await ideas.resume_after_connect(OWNER, "linear") == 0
    paperclip.add_issue_comment.assert_not_awaited()
```

And in the apply-connection test file:

```python
@pytest.mark.asyncio
async def test_a_successful_connect_resumes_waiting_ideas(monkeypatch):
    from core.services.credentials import apply_connection as ac
    seen = []
    monkeypatch.setattr(ac, "_apply_connection_agentcore", AsyncMock(return_value="me@x.test"))
    monkeypatch.setattr(ac.ideas, "resume_after_connect", AsyncMock(side_effect=lambda o, p: seen.append((o, p)) or 1))
    assert await ac.apply_connection("owner-1", "google", {"token": "t"}) == "me@x.test"
    assert seen == [("owner-1", "google")]


@pytest.mark.asyncio
async def test_a_failed_resume_never_fails_the_connect(monkeypatch):
    from core.services.credentials import apply_connection as ac
    monkeypatch.setattr(ac, "_apply_connection_agentcore", AsyncMock(return_value=None))
    monkeypatch.setattr(ac.ideas, "resume_after_connect", AsyncMock(side_effect=RuntimeError("down")))
    assert await ac.apply_connection("owner-1", "google", {"token": "t"}) is None
```

(`google` is a non-`backend_only` provider; if the registry test double needs a provider object, follow the file's existing pattern for `registry.get_provider`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py -k resume_after_connect` and the apply-connection file.
Expected: FAIL (`resume_after_connect` not defined).

- [ ] **Step 3: Implement**

In `ideas.py`:

```python
async def resume_after_connect(owner_id: str, provider_key: str) -> int:
    """A service was connected: one comment on each open idea start that needs it, which wakes
    the assignee through Paperclip's own comment wake (spec 2026-10-15 §2.3). No approval card,
    no extra step. Idempotent per (start, provider). Returns the number of comments posted."""
    from core.services.credentials import registry
    from core.services.paperclip_admin_client import shared_admin_client

    waiting = []
    for row in await messaging_repo.open_idea_starts(owner_id):
        idea = catalog().get(row.get("ideaId", ""))
        if idea is None or provider_key in (row.get("resumed") or []):
            continue
        if any(n.service == provider_key for n in idea.needs):
            waiting.append(row)
    if not waiting:
        return 0
    try:
        label = registry.get_provider(provider_key).display_name
    except Exception:  # noqa: BLE001
        label = provider_key.replace("_", " ").title()
    text = f"{label} is connected. Carry on with this; ask again only if something else is missing."
    admin = shared_admin_client()
    posted = 0
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        cookie = await get_admin_session_cookie(http)
        ws = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
        for row in waiting:
            targets: list[str] = []
            if row.get("issueId"):
                targets = [row["issueId"]]
            elif row.get("projectId") and ws is not None:
                listed = await admin.list_issues(session_cookie=cookie, company_id=ws.company_id,
                                                 params={"projectId": row["projectId"], "status": "todo,in_progress,in_review,blocked"})
                targets = [i["id"] for i in listed.get("issues", []) if i.get("assigneeAgentId")]
            for issue_id in targets:
                await admin.add_issue_comment(session_cookie=cookie, issue_id=issue_id, body={"body": text})
                posted += 1
            await messaging_repo.update_idea_start(owner_id, row["ideaId"], row["userId"],
                                                   {"resumed": sorted({*(row.get("resumed") or []), provider_key})})
    return posted
```

In `apply_connection.py`, import `from core.services import ideas` at module level (check for an import cycle: `ideas` imports `core.services.messaging` lazily inside functions, so a top-level import of `ideas` from `credentials` is safe; if the test shows a cycle, import inside the function) and change `apply_connection`:

```python
    if p.wiring.backend_only:
        return None
    result = await _apply_connection_agentcore(owner_id, provider_key, p, credential)
    try:
        await ideas.resume_after_connect(owner_id, provider_key)
    except Exception:  # best-effort: a connect never fails because a nudge did
        logger.warning("ideas: resume after connect failed owner=%s provider=%s", owner_id, provider_key, exc_info=True)
    return result
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_ideas.py tests/unit/services/credentials`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ideas.py apps/backend/core/services/credentials/apply_connection.py apps/backend/tests
git commit -m "ideas: connecting a needed service resumes the waiting work"
```

---

### Task 5: A Work-started run in a bound DM can ask for a sign-in or a code there

**Files:**
- Modify: `apps/infra/hermes/bridge/turn.py:3770-3810` (`_run_native_paperclip`)
- Test: `apps/infra/hermes/tests/test_issue_conversation.py`

**Interfaces:**
- Consumes: `website_logins.prompt_scope(agent_id, room_id, thread_root, aborted)`; the bound session key shape `agent:<agent>:lightbulb:channel:<roomId>`.

- [ ] **Step 1: Write the failing tests**

Add to `test_issue_conversation.py`, after `test_a_turn_in_the_room_conversation_uses_its_lock_transcript_and_folder`:

```python
def _native_stubs(monkeypatch, seen):
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    monkeypatch.setattr(turn, "_room_history", lambda agent: ([], None))

    class Agent:
        model = ""
        session_input_tokens = session_output_tokens = session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    real_scope = turn.website_logins.prompt_scope
    monkeypatch.setattr(turn.website_logins, "prompt_scope",
                        lambda agent_id, room_id, thread_root, aborted: (seen.append((room_id, thread_root)), real_scope(agent_id, room_id, thread_root, aborted))[1])


def test_a_turn_in_the_bound_room_gives_the_sign_in_prompts_that_room(monkeypatch):
    seen = []
    _native_stubs(monkeypatch, seen)
    turn.register_paperclip_run(IDENTITY, REQUEST)
    assert turn._run_native_paperclip(IDENTITY, REQUEST, ROOM_KEY)[0] == "completed"
    assert seen == [(ROOM, None)]


def test_a_turn_in_the_issue_session_still_has_no_room_for_prompts(monkeypatch):
    seen = []
    _native_stubs(monkeypatch, seen)
    turn.register_paperclip_run(IDENTITY, REQUEST)
    assert turn._run_native_paperclip(IDENTITY, REQUEST, None)[0] == "completed"
    assert seen == [(None, None)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_issue_conversation.py -k "sign_in_prompts or no_room_for_prompts"`
Expected: the first FAILS (`seen == [(None, None)]`), the second passes.

- [ ] **Step 3: Implement**

In `_run_native_paperclip`, after `session_id = conversation or identity["sessionId"]`:

```python
    # A run in the room conversation that owns the issue can ask that room for a sign-in or a
    # code (spec 2026-10-15 §2.4); a run in the issue's own session has nobody to ask (no_room).
    room_id = conversation.rsplit(":", 1)[-1] if conversation and ROOM_SESSION_RE.match(conversation) else None
```

Change the two lines:

```python
    logins = website_logins.prompt_scope(agent_id, room_id, None, lambda: paperclip_run_aborted(run_id))
    ...
                     "runId": run_id, "sessionKey": session_id, "roomId": room_id,
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_issue_conversation.py tests/test_website_logins.py`
Expected: PASS. (If `tests/test_website_logins.py` is named differently, run the file that tests `website_logins`.)

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/bridge/turn.py apps/infra/hermes/tests/test_issue_conversation.py
git commit -m "bridge: a Work-started run in its bound DM asks that DM for sign-ins and codes"
```

---

### Task 6: Helper flags `--project` and `--assignee-user`

**Files:**
- Modify: `apps/infra/hermes/skills-src/paperclip/scripts/paperclip-api.sh:95-125` (usage), `:855-915` (`cmd_subtask`), `:917-972` (`cmd_create`)
- Test: `apps/infra/hermes/tests/test_issue_conversation.py` (reuse its `_helper` harness)

**Interfaces:**
- Produces: `create ... [--project <id>] [--assignee-user <userId>]`, `subtask ... [--project <id>] [--assignee-user <userId>]`; body fields `projectId`, `assigneeUserId`.

- [ ] **Step 1: Write the failing tests**

```python
def test_create_can_file_into_a_project_and_assign_a_person(tmp_path):
    calls = _helper(tmp_path, ["create", "company-1", "--title", "Sign the form", "--project", "proj-1",
                               "--assignee-user", "user-9", "--status", "todo"],
                    responses=[[201, json.dumps({"id": ISSUE})]])
    body = json.loads(calls[0]["body"])
    assert body["projectId"] == "proj-1" and body["assigneeUserId"] == "user-9"
    assert "assigneeAgentId" not in body and body["status"] == "todo"


def test_subtask_can_file_into_a_project(tmp_path):
    calls = _helper(tmp_path, ["subtask", "company-1", "--title", "One directory", "--parent", ISSUE,
                               "--project", "proj-1", "--assignee", AGENT, "--status", "todo"],
                    responses=[[201, json.dumps({"id": ISSUE})], [200, json.dumps({"id": ISSUE, "status": "todo"})]])
    assert json.loads(calls[0]["body"])["projectId"] == "proj-1"
```

Read `_helper`'s return value in the file (it returns the recorded calls list, or a tuple) and match it.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_issue_conversation.py -k "into_a_project"`
Expected: FAIL (`create: unknown argument: --project`).

- [ ] **Step 3: Implement**

In both `cmd_create` and `cmd_subtask`: add `local project="" assignee_user=""` to the locals, two cases to the parser:

```bash
      --project) project="${2:-}"; shift 2 ;;
      --assignee-user) assignee_user="${2:-}"; shift 2 ;;
```

two `--arg`s to the jq call: `--arg projectId "$project" --arg assigneeUserId "$assignee_user"`, and two lines in the jq expression:

```jq
      + (if $projectId == "" then {} else {projectId: $projectId} end)
      + (if $assigneeUserId == "" then {} else {assigneeUserId: $assigneeUserId} end)
```

A person's task never dispatches a run, so `resolve_create_status` must not force backlog for it: when `assignee_user` is set and `assignee` is empty, skip the `resolve_create_status` call and keep `status` as given (default `todo`):

```bash
  if [[ -n "$assignee" ]]; then
    status="$(resolve_create_status create "$status" "$assignee")"
  else
    status="${status:-todo}"
  fi
```

Update the usage text for `create` and `subtask` (lines 95-125): add `[--project <id>] [--assignee-user <userId>]` to the synopsis and `projectId?, assigneeUserId?` to the body line, plus one sentence under "Assigning work": "`--assignee-user` files a task for a person (it shows in their Inbox); it never starts a run."

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_issue_conversation.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/skills-src/paperclip/scripts/paperclip-api.sh apps/infra/hermes/tests/test_issue_conversation.py
git commit -m "paperclip helper: --project and --assignee-user on create and subtask"
```

---

### Task 7: The Ideas instruction: pre-flight, statuses, person steps, proof, limits

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/ideas.md` (rewrite; stays under 40 lines)
- Test: `apps/infra/hermes/tests/test_ideas_skill.py`

**Interfaces:**
- Consumes: Task 6 flags; the Work item sections "Needs" and "Done means" from Task 2.

- [ ] **Step 1: Update the test**

Replace the body of `test_the_skill_routes_ideas_to_a_short_reference` after the `row` assertions with:

```python
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    assert len(ideas.splitlines()) < 40
    assert "Do it like any request" in ideas
    # Pre-flight: needs against what is connected, one ask for everything missing.
    assert "Read the Work item's **Needs**" in ideas and "ask once, for all of it" in ideas
    assert "`connected_integrations`" in ideas
    # Statuses: project tasks filed in the project as todo; a task idea is checked out first.
    assert "`--project <id>` and `--status todo`" in ideas
    assert "one-line next action" in ideas
    # A step only a person can do is a task for the person, never a question in chat.
    assert "`--assignee-user`" in ideas and "blocked by that task" in ideas
    # Done means proof.
    assert "one line per deliverable with a link" in ideas and "**Done means**" in ideas
    # The box's limits.
    assert "30 minutes" in ideas and "50 a day" in ideas and "no phone" in ideas
    # Browser steps from a run with no room go back to the DM.
    assert "hand browser steps back to the DM" in ideas
    assert "sign-in (live browser only)" in ideas and "`request_approval`" in ideas
    assert "to the feed" in ideas
    assert "`suggest_idea` only when you notice a concrete, company-specific job" in ideas
    assert "not to chat" in ideas and "never on a schedule" in ideas
    assert "plan" not in ideas.lower().replace("plan this project", "") and "$HERMES_HOME/ideas" not in ideas
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "Let's do it!" in service and "Tracked in Work: [" in service
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_ideas_skill.py`
Expected: FAIL on the new phrases.

- [ ] **Step 3: Rewrite `references/ideas.md`**

```markdown
# Ideas

An idea arrives as the person's own message in your DM: the idea quoted, "Let's do it!",
and, for bigger jobs, "Tracked in Work: [name](link)". Do it like any request, and start now.

**First, the Work item.** Read the Work item's **Needs** and **Done means** before anything else.
Compare Needs with `connected_integrations` in `~/.openclaw/lightbulb-facts.json`. If anything is
missing, ask once, for all of it, in your reply: each service with its Connect link
(`lightbulb-connect.sh link <provider>`), and for a write you cannot get, what you will hand over as
text instead. Then keep doing every part that needs nothing. When the person connects a service,
Paperclip wakes you with a comment on the task; carry on.

- **The link is a task:** it is assigned to you and this run is it; work it and close it with proof.
- **The link is a project:** file every task inside it with `--project <id>` and `--status todo`,
  each assigned to yourself or a teammate (the id is the link's `detailId`). Work the first one now.
  You close the project only after checking each task's proof.
- **No link:** answer it in one go.
- After each unit of work (one invoice, one directory, one PR), leave a one-line next action as a
  comment on the task. Turns are 30 minutes: a next action is how the next run picks up.
- A step only a person can do (identity checks, signatures, a verification video, a phone-only
  vendor, a payment outside the rails) is a task for them: `create ... --assignee-user <userId>`,
  with the prepared text, number or amount in it, and your own task blocked by that task.
- Ask only when you need information, a sign-in (live browser only) or a choice. For a sign-in or
  a code from a run that has no DM (a task filed for you by a teammate), hand browser steps back
  to the DM: post what you need on the task and end the turn.
- Anything in the person's name (sending, publishing, paying, submitting) is an approval first
  (`request_approval`), as always.
- **Done means proof.** Before `done`, post one line per deliverable with a link to the thing that now
  exists, and check them against **Done means**. No proof: not done; say in the DM what is missing.
- Limits: the team mailbox sends 50 a day; there is no phone (prepare the script for the person).
- Post progress, and the finished result with proof, to the feed (`references/feed.md`).
- Promise the action, never the result.

## Adding an idea

Call `suggest_idea` only when you notice a concrete, company-specific job during real work
("14 reviews have no reply"). Never a generic or routine one, never on a schedule. It goes to
the Ideas list, not to chat: do not post it in the conversation.
```

- [ ] **Step 4: Run the test**

Run: `cd apps/infra/hermes && python -m pytest -q tests/test_ideas_skill.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/skills-src/lightbulb/references/ideas.md apps/infra/hermes/tests/test_ideas_skill.py
git commit -m "ideas skill: pre-flight on Needs, todo in the project, person-step tasks, proof, limits"
```

---

### Task 8: Stripe Projects lands credentials on AgentCore

**Files:**
- Modify: `apps/backend/core/services/provisioning_landing.py:52-69` (`land_credentials`)
- Modify: `apps/backend/tests/unit/services/test_agent_export_projects_exclusions.py:85-105` (the assertion that it refuses)
- Test: `apps/backend/tests/unit/services/test_provisioning_landing.py` (create)

**Interfaces:**
- Consumes: `integration_secret_store.put(owner_id, provider, credential) -> str` and `.get(owner_id, provider)`; `integrations_repo.projects_key(provider)`, `record_provider_resource(owner_id, provider, resource)`; `hermes_profiles.patch_profile_env_and_config(owner_id, agent_id, env_updates, config_patch)`; `hermes_cli._roster(owner_id) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
"""land_credentials on AgentCore: Secrets Manager, the pointer row, then every roster profile's .env."""
from unittest.mock import AsyncMock

import pytest

from core.services import provisioning_landing as pl

POINTER = {"resource_id": "res-1", "provider": "resend", "service": "email", "resource_name": "Acme mail",
           "status": "active", "env_keys": ["RESEND_API_KEY"], "dashboard_url": None, "level": "company",
           "workflow_id": "wf-1", "project_id": "proj-1"}


@pytest.fixture
def stores(monkeypatch):
    secret = {}
    monkeypatch.setattr(pl.integration_secret_store, "get", AsyncMock(side_effect=lambda o, k: secret.get((o, k))))
    monkeypatch.setattr(pl.integration_secret_store, "put",
                        AsyncMock(side_effect=lambda o, k, blob: secret.__setitem__((o, k), dict(blob)) or "arn"))
    pointer = AsyncMock(return_value=[POINTER])
    monkeypatch.setattr(pl.integrations_repo, "record_provider_resource", pointer)
    patched = []
    monkeypatch.setattr(pl, "_roster", AsyncMock(return_value=["agent-a", "agent-b"]))
    monkeypatch.setattr(pl, "_patch_profile_env", lambda o, a, env: patched.append((a, dict(env))))
    return secret, pointer, patched


@pytest.mark.asyncio
async def test_landing_writes_secret_pointer_and_every_profile_env(stores):
    secret, pointer, patched = stores
    await pl.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret[("o1", pl.integrations_repo.projects_key("resend"))] == {"RESEND_API_KEY": "re_123"}
    pointer.assert_awaited_once_with("o1", "resend", POINTER)
    assert patched == [("agent-a", {"RESEND_API_KEY": "re_123"}), ("agent-b", {"RESEND_API_KEY": "re_123"})]


@pytest.mark.asyncio
async def test_landing_merges_into_the_providers_existing_blob(stores):
    secret, _, _ = stores
    secret[("o1", pl.integrations_repo.projects_key("resend"))] = {"RESEND_OTHER": "x"}
    await pl.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret[("o1", pl.integrations_repo.projects_key("resend"))] == {"RESEND_OTHER": "x", "RESEND_API_KEY": "re_123"}


@pytest.mark.asyncio
async def test_landing_with_no_env_still_records_the_pointer_and_touches_no_profile(stores):
    secret, pointer, patched = stores
    await pl.land_credentials(owner_id="o1", resource={**POINTER, "env_keys": []}, env={})
    assert not secret and patched == [] and pointer.await_count == 1


@pytest.mark.asyncio
async def test_a_profile_write_failure_is_raised_after_the_secret_and_pointer_landed(stores, monkeypatch):
    secret, pointer, _ = stores
    monkeypatch.setattr(pl, "_patch_profile_env", lambda o, a, env: (_ for _ in ()).throw(FileNotFoundError(a)))
    with pytest.raises(FileNotFoundError):
        await pl.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret and pointer.await_count == 1
```

Change `test_agent_export_projects_exclusions.py:103` from asserting `"WorkflowRefused" in inspect.getsource(...)` to `assert "WorkflowRefused" not in inspect.getsource(provisioning_landing.land_credentials)` and update its docstring (lines 91-96) to say the landing writes to every profile's `.env`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_provisioning_landing.py tests/unit/services/test_agent_export_projects_exclusions.py`
Expected: FAIL (`WorkflowRefused` raised; `_roster`/`_patch_profile_env` missing).

- [ ] **Step 3: Implement**

Replace `land_credentials`:

```python
async def _roster(owner_id: str) -> list[str]:
    from core.services.credentials.hermes_cli import _roster as roster

    return await roster(owner_id)


def _patch_profile_env(owner_id: str, agent_id: str, env: dict[str, str]) -> None:
    from core.services.hermes_profiles import patch_profile_env_and_config

    patch_profile_env_and_config(owner_id, agent_id, env, {})


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    """Three writes, in the module docstring's order: the provider's blob in Secrets Manager
    (merged, so a second resource of the same provider keeps the first's keys), the pointer row,
    then the values into every roster profile's ``.env`` (spec 2026-10-15 §2.10), the same path a
    connector's secret takes. A profile write that fails raises AFTER the first two landed, so a
    retry finds the credential and the pointer and only has to deliver again."""
    provider = resource["provider"]
    if env:
        secret_key = integrations_repo.projects_key(provider)
        blob = dict(await integration_secret_store.get(owner_id, secret_key) or {})
        blob.update(env)
        await integration_secret_store.put(owner_id, secret_key, blob)
    await integrations_repo.record_provider_resource(owner_id, provider, resource)
    if not env:
        return
    for agent_id in await _roster(owner_id):
        await asyncio.to_thread(_patch_profile_env, owner_id, agent_id, env)
    logger.info("provisioning: landed %s/%s for owner=%s keys=%s", provider, resource.get("resource_id"), owner_id, sorted(env))
```

Add `import asyncio` and keep the existing imports of `integrations_repo` and `integration_secret_store`. Remove the `WorkflowRefused` import from this function. The bridge re-pulls a profile when its `.revision` changes, which `patch_profile_env_and_config` bumps; confirm with `grep -n "\.revision" apps/infra/hermes/bridge/sync.py` and note the line in the commit message.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest -q tests/unit/services/test_provisioning_landing.py tests/unit/services/test_agent_export_projects_exclusions.py tests/unit/services/test_provisioning_workflow.py`
Expected: PASS (if the workflow test file has another name, run the file that imports `provisioning_workflow`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/provisioning_landing.py apps/backend/tests/unit/services
git commit -m "stripe projects: land credentials into every agent profile on AgentCore"
```

---

### Task 9: The Projects API version and the prod switch

**Files:**
- Modify: `apps/backend/core/services/stripe_provisioning.py:45` (`PROVISIONING_VERSION`)
- Modify: `apps/infra/lib/stacks/service-stack.ts:1787-1789` (already reads `props.stripeProvisioningEnabled`); the stack's props construction in `apps/infra/bin/` (find with `grep -rn "new ServiceStack" apps/infra/bin`)
- Modify: `.github/workflows/deploy.yml` (the env block that already passes `OPENROUTER_MANAGED_MODEL_ID` from `vars.`)
- Test: `apps/infra/test/service-stack.test.ts:489`

**Interfaces:**
- Produces: CDK context/env `STRIPE_PROVISIONING_ENABLED` → `props.stripeProvisioningEnabled`; GitHub variable `STRIPE_PROVISIONING_ENABLED` (owner sets it to `true` for prod after a dev provision reaches `active`).

- [ ] **Step 1: Check the API version against Stripe's public Projects docs**

Run: `cd apps/backend && uv run python -c "import stripe; print(stripe.VERSION)"` and read https://docs.stripe.com/projects (the Provisioning API reference) for the `Stripe-Version` the public release expects. If the public release accepts a dated version (for example `2026-04-30.preview`), set `PROVISIONING_VERSION` to it; if it still documents `unsafe-development`, leave the constant and record the page's date in the module docstring. Write the finding in the commit message. On dev, with `STRIPE_PROVISIONING_ENABLED=true`, run one `catalog` call through the existing door (`GET /api/v1/provisioning/catalog` or the router that calls `stripe_provisioning.catalog`) and confirm HTTP 200.

- [ ] **Step 2: Write the failing infra test**

In `apps/infra/test/service-stack.test.ts`, next to the assertion at line 489, add a case that constructs the stack with `stripeProvisioningEnabled: true` and expects `Match.objectLike({ Name: "STRIPE_PROVISIONING_ENABLED", Value: "true" })`, and:

```ts
import { stripeProvisioningEnabled } from "../lib/stacks/service-stack";

it("reads the provisioning switch from the deploy environment", () => {
  expect(stripeProvisioningEnabled({ STRIPE_PROVISIONING_ENABLED: "true" })).toBe(true);
  expect(stripeProvisioningEnabled({ STRIPE_PROVISIONING_ENABLED: "false" })).toBe(false);
  expect(stripeProvisioningEnabled({})).toBe(false);
});
```

- [ ] **Step 3: Wire the switch**

In `apps/infra/lib/stacks/service-stack.ts`, next to the props interface:

```ts
/** The Stripe Projects rail master switch, from the deploy job's environment (a repo variable). */
export function stripeProvisioningEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.STRIPE_PROVISIONING_ENABLED === "true";
}
```

In the `bin` file that constructs `ServiceStack`, add `stripeProvisioningEnabled: stripeProvisioningEnabled()` to its props. In `deploy.yml`, add `STRIPE_PROVISIONING_ENABLED: ${{ vars.STRIPE_PROVISIONING_ENABLED }}` to the same `env:` block that passes `OPENROUTER_MANAGED_MODEL_ID` for both the dev and prod synth/deploy jobs. The variable is unset today, so nothing changes until the owner sets it.

- [ ] **Step 4: Run the infra tests**

Run: `cd apps/infra && npx vitest run test/service-stack.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/stripe_provisioning.py apps/infra .github/workflows/deploy.yml
git commit -m "stripe projects: public API version check; prod switch from a repo variable"
```

---

### Task 10: Dev proof and the ledger

**Files:**
- Modify: `.superpowers/sdd/2026-10-14-stage-1-deletions/progress.md` (ledger line)

- [ ] **Step 1: Open the PR and wait for Codex**

`gh pr create` with the spec path in the body. Merge only on a clean Codex review (owner rule). The merge deploys dev.

- [ ] **Step 2: Prove the mechanics on dev**

On the dev workspace, as the dev founder:
1. Start `inbox-triage` with Google disconnected. Expect: the agent's reply lists Google with a Connect link and does not pretend to read mail. Connect Google on Connections. Expect within 2 minutes: a comment "Google is connected…" on the task and a new run.
2. Start `inbox-triage`. Expect within 1 minute: one run from the `todo` task on the board, your message in the DM with the Work link, and no second run in the DM.
3. Start `launch-run`. Expect: one `todo` task inside the project carrying the idea, a run on it, and further tasks filed inside the project as `todo`.
4. From the DM, have the agent open a site that asks for a code on a task it checked out. Expect: the "go to the Browser panel" line in the DM.
5. With `STRIPE_PROVISIONING_ENABLED=true` on dev, ask the agent for a Resend account through the rail; approve the card. Expect: the workflow reaches `active` and `RESEND_API_KEY` is present in the agent's shell on the next turn (`printenv RESEND_API_KEY | cut -c1-4`).

Record each result (pass/fail, run ids) in the ledger. A failure is fixed before the prod dispatch.

- [ ] **Step 3: Prod**

Only after the owner says so: one `deploy.yml` dispatch; then the owner sets `STRIPE_PROVISIONING_ENABLED=true` when ready.
