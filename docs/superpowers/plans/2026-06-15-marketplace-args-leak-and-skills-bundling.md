# Marketplace args-secret leak + skills-bundling correctness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the marketplace publish path from leaking secrets passed in MCP server `args[]`, and make the publish-preview's skills step reflect reality (workspace skills auto-ship; only global skills are selectable additions).

**Architecture:** Two independent backend fixes in `apps/backend`. Fix 1 extends the existing scrub-then-gate for slice credentials to cover positional CLI args. Fix 2 reworks the publish-preview so it sources its skill lists correctly (the export already publishes the global MCP config, which is what every agent runs — no per-agent scoping exists). Pure-function changes with TDD; no infra, no deploy-path, no per-agent config reads.

**Tech Stack:** Python 3.12, pytest, FastAPI/Pydantic (backend), `uv` for deps.

**Spec:** `docs/superpowers/specs/2026-06-15-marketplace-args-leak-and-skills-bundling-design.md`

---

## Setup (once, before Task 1)

- [ ] **Install backend deps + confirm clean baseline**

Run:
```bash
cd apps/backend && uv sync
uv run pytest tests/unit/services/test_catalog_slice.py tests/unit/services/test_marketplace_safety.py tests/unit/services/test_agent_export.py -q
```
Expected: existing tests PASS (establishes a clean baseline before changes).

---

## Task 1: Scrub secrets in MCP server `args[]` (Fix 1, scrub layer)

The scrubber blanks secret-*named fields* (`apiKey`/`env`/`headers`/`webhookToken`) but never inspects `args`, so `npx -y instantly-mcp --api-key <KEY>` leaks. Add arg-flag scrubbing.

**Files:**
- Modify: `apps/backend/core/services/catalog_slice.py` (add `_SECRET_ARG_FLAGS` + `_scrub_arg_secrets` near `_SECRET_FIELD_NAMES:67`; call inside `_scrub_secret_fields:97-118`)
- Test: `apps/backend/tests/unit/services/test_catalog_slice.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/services/test_catalog_slice.py`:
```python
from core.services.catalog_slice import _scrub_secret_fields


def test_scrub_blanks_value_after_secret_flag():
    entry = {"command": "npx", "args": ["-y", "instantly-mcp", "--api-key", "REALSECRETVALUE123"]}
    out = _scrub_secret_fields(entry)
    assert out["args"] == ["-y", "instantly-mcp", "--api-key", ""]
    # original untouched (deep copy)
    assert entry["args"][-1] == "REALSECRETVALUE123"


def test_scrub_blanks_joined_secret_flag():
    entry = {"command": "x", "args": ["--token=abc123DEF456ghi789"]}
    assert _scrub_secret_fields(entry)["args"] == ["--token="]


def test_scrub_leaves_non_secret_flags_and_packages_untouched():
    entry = {"command": "npx", "args": ["-y", "@perplexity-ai/mcp-server", "--transport", "stdio"]}
    assert _scrub_secret_fields(entry)["args"] == ["-y", "@perplexity-ai/mcp-server", "--transport", "stdio"]


def test_scrub_still_blanks_named_fields():
    entry = {"apiKey": "k", "env": {"X": "y"}, "args": ["ok"]}
    out = _scrub_secret_fields(entry)
    assert out["apiKey"] == "" and out["env"] == {"X": ""} and out["args"] == ["ok"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice.py -k "scrub_blanks_value_after_secret_flag or scrub_blanks_joined or scrub_leaves_non_secret" -v`
Expected: FAIL — `args` still contains the secret (first two tests).

- [ ] **Step 3: Write minimal implementation**

In `catalog_slice.py`, immediately after `_SECRET_FIELD_NAMES = frozenset({...})` (line 67), add:
```python
# Secret CLI flags whose following value (or ``=``-joined value) is a credential.
# MCP servers commonly launch via ``npx -y <pkg> --api-key <KEY>``; the field-name
# scrub never sees these because the secret sits positionally in ``args``.
_SECRET_ARG_FLAGS = frozenset({"--api-key", "--apikey", "--key", "--token", "--secret", "--password"})


def _scrub_arg_secrets(args: list[Any]) -> list[Any]:
    """Copy of ``args`` with the value of any secret flag blanked.

    Separated ``["--api-key", "<VAL>"]`` -> ``"<VAL>"`` becomes ``""``.
    Joined ``["--api-key=<VAL>"]`` -> ``"--api-key="``. Flag tokens are kept so
    the buyer sees which credential to supply. Non-string elements pass through.
    """
    out: list[Any] = []
    blank_next = False
    for a in args:
        if blank_next:
            out.append("" if isinstance(a, str) else a)
            blank_next = False
            continue
        if isinstance(a, str):
            if a.lower() in _SECRET_ARG_FLAGS:
                out.append(a)
                blank_next = True
                continue
            eq = a.find("=")
            if eq != -1 and a[:eq].lower() in _SECRET_ARG_FLAGS:
                out.append(a[: eq + 1])
                continue
        out.append(a)
    return out
```

Then inside `_scrub_secret_fields`, just before `return result` (line 118), add:
```python
    args = result.get("args")
    if isinstance(args, list):
        result["args"] = _scrub_arg_secrets(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice.py -v`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/catalog_slice.py apps/backend/tests/unit/services/test_catalog_slice.py
git commit -m "fix(marketplace): scrub secrets passed as MCP args"
```

---

## Task 2: Block publish on credential-shaped `args[]` (Fix 1, gate layer)

The post-scrub gate (`assert_slice_scrubbed`) only inspects secret-*named* fields. Add an args-aware backstop: BLOCK on a non-empty value still following a secret flag, or a standalone credential-shaped token (provider prefix / high-entropy non-package).

**Files:**
- Modify: `apps/backend/core/services/marketplace_safety.py` (`_BLOCKING_CREDENTIAL_PATTERNS:264`; add helpers + `_walk_for_arg_secrets`; call inside `assert_slice_scrubbed:368-417`)
- Test: `apps/backend/tests/unit/services/test_marketplace_safety.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/services/test_marketplace_safety.py`:
```python
from core.services import marketplace_safety as ms


def _slice_with_args(args):
    return {"mcp": {"servers": {"s": {"command": "npx", "args": args}}}}


def test_gate_blocks_residual_value_after_secret_flag():
    flags = ms.assert_slice_scrubbed(_slice_with_args(["--api-key", "STILL-LEAKED-aaaaaaaa"]))
    assert ms.has_blocking_credential_findings(flags)
    assert any(f.pattern == "arg-secret-suspected" for f in flags)


def test_gate_blocks_standalone_provider_prefix_token():
    flags = ms.assert_slice_scrubbed(_slice_with_args(["sk-ant-0123456789ABCDEFabcdef"]))
    assert ms.has_blocking_credential_findings(flags)


def test_gate_blocks_high_entropy_standalone_token():
    flags = ms.assert_slice_scrubbed(_slice_with_args(["aZ09" * 12]))  # 48 chars, mixed
    assert ms.has_blocking_credential_findings(flags)


def test_gate_allows_npx_package_specs():
    flags = ms.assert_slice_scrubbed(_slice_with_args(["-y", "@perplexity-ai/mcp-server", "instantly-mcp"]))
    assert not ms.has_blocking_credential_findings(flags)


def test_gate_allows_properly_scrubbed_args():
    flags = ms.assert_slice_scrubbed(_slice_with_args(["-y", "instantly-mcp", "--api-key", ""]))
    assert not ms.has_blocking_credential_findings(flags)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_safety.py -k "gate_blocks or gate_allows" -v`
Expected: FAIL — `has_blocking_credential_findings` False for the leak cases (no args detection yet).

- [ ] **Step 3: Write minimal implementation**

In `marketplace_safety.py`, change `_BLOCKING_CREDENTIAL_PATTERNS` (line 264) to include the new pattern:
```python
_BLOCKING_CREDENTIAL_PATTERNS = frozenset(
    {"unscrubbed-credential", "oauth-client-secret", "arg-secret-suspected"}
)
```

Add near the other module constants (after `_CLIENT_SECRET_KEY_RE`):
```python
# Secret CLI flags whose following value is a credential (mirrors catalog_slice).
_SECRET_ARG_FLAGS = frozenset({"--api-key", "--apikey", "--key", "--token", "--secret", "--password"})

# High-confidence credential prefixes.
_PROVIDER_KEY_PREFIXES = (
    "sk-", "sk-ant-", "ghp_", "gho_", "ghs_", "github_pat_", "xoxb-", "xoxp-", "xapp-",
)
_AWS_KEY_RE = re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{16}$")
# npm package spec shape — NOT a secret (e.g. "@perplexity-ai/mcp-server", "instantly-mcp").
_NPM_PACKAGE_RE = re.compile(r"^@?[a-z0-9][a-z0-9._/-]*$")
# Generic high-entropy token: >=40 base64/hex-ish chars.
_HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def _looks_like_arg_secret(value: str) -> bool:
    """True for a standalone arg that looks like a credential, excluding npm
    package specs. Conservative: high-confidence prefixes, AWS keys, or a long
    high-entropy mixed-case+digit token."""
    if not isinstance(value, str) or value == "":
        return False
    if any(value.startswith(p) for p in _PROVIDER_KEY_PREFIXES):
        return True
    if _AWS_KEY_RE.match(value):
        return True
    if _NPM_PACKAGE_RE.match(value):
        return False
    return bool(
        _HIGH_ENTROPY_RE.match(value)
        and any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and any(c.isdigit() for c in value)
    )


def _walk_for_arg_secrets(slice_: dict) -> list[tuple[str, str]]:
    """(path, value) for credential-looking ``args`` across mcp.servers /
    skills.entries: a non-empty value after a secret flag, a non-empty
    ``--flag=value``, or a standalone credential-shaped token."""
    found: list[tuple[str, str]] = []
    for path, entry in _walk_slice_entries(slice_):
        args = entry.get("args")
        if not isinstance(args, list):
            continue
        expect_value_for_flag = False
        for i, a in enumerate(args):
            if not isinstance(a, str):
                expect_value_for_flag = False
                continue
            if expect_value_for_flag:
                if a != "":
                    found.append((f"{path}.args[{i}]", a))
                expect_value_for_flag = False
                continue
            if a.lower() in _SECRET_ARG_FLAGS:
                expect_value_for_flag = True
                continue
            eq = a.find("=")
            if eq != -1 and a[:eq].lower() in _SECRET_ARG_FLAGS:
                if a[eq + 1 :] != "":
                    found.append((f"{path}.args[{i}]", a[eq + 1 :]))
                continue
            if _looks_like_arg_secret(a):
                found.append((f"{path}.args[{i}]", a))
    return found
```

In `assert_slice_scrubbed`, after the `_walk_for_client_secret` loop and before `return flags` (line ~416), add:
```python
    for child_path, secret in _walk_for_arg_secrets(openclaw_slice):
        flags.append(
            SafetyFlag(
                pattern="arg-secret-suspected",
                severity="high",
                file=_SLICE_FILE,
                line=None,
                snippet=f"{child_path} = {_redact(secret)}",
            )
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_safety.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace_safety.py apps/backend/tests/unit/services/test_marketplace_safety.py
git commit -m "fix(marketplace): block publish on credential-shaped MCP args"
```

---

## Task 3: Publish-preview skills — auto-ship workspace, addable global (Fix 2)

`enumerate_for_preview` builds `available_skills` from the agent's global-config entry `skills` list (empty for real agents), misrepresenting what ships. Per intended behavior: workspace skills auto-ship (informational); only the container's global `skills.entries` are selectable additions.

**Files:**
- Modify: `apps/backend/core/services/agent_export.py` (`enumerate_for_preview:531-613`)
- Modify: `apps/backend/schemas/marketplace.py` (`ArtifactPreviewResponse:309-323`)
- Modify: `apps/backend/routers/marketplace_listings.py` (`my_agent_artifact_preview:704-755`, response build)
- Test: `apps/backend/tests/unit/services/test_agent_export.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/services/test_agent_export.py` (follow the file's existing fixture style for a fake workspace; if it stubs `get_workspace`, reuse that stub):
```python
from core.services import agent_export


def test_preview_workspace_skills_autoship_global_entries_addable(monkeypatch, tmp_path):
    # agent dir with two workspace skills
    agent_dir = tmp_path / "ws"
    (agent_dir / "skills" / "researcher").mkdir(parents=True)
    (agent_dir / "skills" / "writer").mkdir(parents=True)
    monkeypatch.setattr(agent_export, "_resolve_agent_dir", lambda *, seller_id, agent_id: agent_dir)

    config = {
        "agents": {"list": [{"id": "agent_x"}]},
        "skills": {"entries": {"image-lab": {"enabled": True}}},  # global, addable
    }

    class _WS:
        def read_openclaw_config(self, _):
            return config

    monkeypatch.setattr(agent_export, "get_workspace", lambda: _WS())

    out = agent_export.enumerate_for_preview(seller_id="u1", agent_id="agent_x")
    assert sorted(out["auto_ship_skills"]) == ["researcher", "writer"]
    assert [s["name"] for s in out["available_skills"]] == ["image-lab"]
    # workspace skills are NOT in the selectable add-list
    assert all(s["name"] not in ("researcher", "writer") for s in out["available_skills"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k "preview_workspace_skills_autoship" -v`
Expected: FAIL — `KeyError: 'auto_ship_skills'` and/or `available_skills` empty (sourced from agent entry).

- [ ] **Step 3: Write minimal implementation**

In `agent_export.py`, replace the skills block in `enumerate_for_preview` (lines 556-575) with:
```python
    workspace_skill_names = _enumerate_workspace_skill_names(agent_dir)

    # Workspace skills auto-ship via the tarball + buyer-side discovery — they are
    # informational, NOT a selectable bundle choice.
    auto_ship_skills = sorted(workspace_skill_names)

    # Addable skills = the container's GLOBAL skills.entries the buyer would not
    # otherwise have. There is no per-agent skill scoping, so the agent's config
    # entry is NOT a reliable source; the global skills.entries are. Bundled
    # skills already ship in the buyer's image, and workspace skills auto-ship
    # above, so neither belongs in the selectable add-list.
    skills_cfg = config.get("skills") if isinstance(config.get("skills"), dict) else {}
    entries_cfg = skills_cfg.get("entries") if isinstance(skills_cfg.get("entries"), dict) else {}
    addable_skill_names = sorted(entries_cfg.keys()) if isinstance(entries_cfg, dict) else []
    available_skills = [
        {
            "name": name,
            "bucket": classify_skill(name, config, workspace_skill_names=workspace_skill_names).value,
        }
        for name in addable_skill_names
    ]
```

Update the preview-slice build (line 599) to scan the addable skills, then add `auto_ship_skills` to the return dict:
```python
            selected_skills=addable_skill_names,
```
```python
    return {
        "agent_id": agent_id,
        "available_mcps": available_mcps,
        "available_skills": available_skills,
        "auto_ship_skills": auto_ship_skills,
        "persona_files": persona_files,
        "safety_flags": safety_flags,
    }
```

In `schemas/marketplace.py`, add to `ArtifactPreviewResponse` (after `available_skills`, line 320):
```python
    auto_ship_skills: list[str] = Field(default_factory=list)
```

In `routers/marketplace_listings.py`, in `my_agent_artifact_preview`'s response build (around line 746), add the field:
```python
        auto_ship_skills=preview["auto_ship_skills"],
```

Now update the THREE existing tests broken by the source change:

(a) Rewrite `test_enumerate_for_preview_classifies_each_skill` (`tests/unit/services/test_agent_export.py:632-656`) — `available_skills` is now sourced from global `skills.entries`, so `search` (allowBundled) and `custom-skill` (workspace) are no longer in it; `custom-skill` moves to `auto_ship_skills`:
```python
def test_enumerate_for_preview_classifies_each_skill(fake_efs, tmp_path):
    """available_skills lists the container's global skills.entries (the
    selectable additions); workspace skills auto-ship separately."""
    seller_id, agent_id, agent_dir = fake_efs
    user_config = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(user_config.read_text())
    cfg["skills"] = {"entries": {"jira": {"apiKey": "secret"}}, "allowBundled": ["search"]}
    user_config.write_text(json.dumps(cfg))
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "custom-skill.md").write_text("# custom skill\n", encoding="utf-8")

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    by_name = {s["name"]: s["bucket"] for s in preview["available_skills"]}
    assert by_name == {"jira": "entries"}                 # only global entries are addable
    assert "custom-skill" in preview["auto_ship_skills"]  # workspace skill auto-ships
    assert "search" not in by_name                        # bundled is not an addable selection
```

(b) In `tests/unit/routers/test_marketplace_listings.py`, add `"auto_ship_skills"` to the mocked `enumerate_for_preview` return dict at **both** `test_artifact_preview_returns_available_mcps_skills_and_flags` (line ~1261) and `test_artifact_preview_needs_config_patch_false_for_pure_workspace_listing` (line ~1307), e.g.:
```python
        "auto_ship_skills": ["my-skill"],
```
(without it the router's `preview["auto_ship_skills"]` raises `KeyError`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py tests/unit/routers/test_marketplace_listings.py -v`
Expected: PASS (new test + the three rewritten/updated tests + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/schemas/marketplace.py apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/services/test_agent_export.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "fix(marketplace): preview reflects auto-ship workspace skills + addable global skills"
```

---

## Final verification (after all tasks)

- [ ] **Run the full marketplace-touching unit suite**

Run:
```bash
cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice.py tests/unit/services/test_marketplace_safety.py tests/unit/services/test_agent_export.py tests/unit/routers/test_marketplace_listings.py -q
```
Expected: all PASS.

- [ ] **Lint**

Run: `cd apps/backend && uv run ruff check core/services/catalog_slice.py core/services/marketplace_safety.py core/services/agent_export.py && uv run ruff format --check core/services/catalog_slice.py core/services/marketplace_safety.py core/services/agent_export.py`
Expected: clean (the pre-commit hook also runs ruff format-check).

---

## Out of scope (do NOT implement here — documented in spec §3/§10)

- Managed-skill (`~/.openclaw/skills`) source packaging — phaseable follow-up; nothing to package today.
- Frontend rendering of `auto_ship_skills` vs. selectable `available_skills` — downstream frontend task; this plan delivers the corrected backend data.
- Buyer key-provisioning UX.
- Per-agent MCP scoping / "effective config" publishing — does not exist; export-reads-global is correct.

## Manual remediation (operator, not code — spec §9)

After deploy: rotate the Instantly API key; take down / republish the 7 leaking listings (echo, jarvis, lens, ora, pulse, stitch, thread).
