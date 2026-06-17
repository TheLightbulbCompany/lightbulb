# Marketplace Requirements Engine (Backend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the marketplace manifest accurately carry what each agent ships and needs, and gate publish/hire on it — the backend half of accurate publisher/buyer flows.

**Architecture:** A pure `requirements_scan()` reads an agent's effective config + workspace (declared-read ∪ body-parse over `.lobster` pipelines/scripts), classifies each dependency into `included|connect|byo|unavailable`, and emits a `Requirement[]`. `agent_export` freezes that into `manifest.requirements[]` + `manifest.requires.credentials[]` at artifact build. The publish path gates on `unavailable` rows; the existing hire gate (`assert_credentials_satisfied`) is now fed by the scanned `requires.credentials`. A backfill script re-scans live listings.

**Tech Stack:** Python 3.12, FastAPI, pytest, boto3 (S3), PyYAML, dataclasses. This is Plan 1 of 2; Plan 2 is the frontend Requirements Ledger consuming `manifest.requirements`.

## Global Constraints

- Scanner is **pure + side-effect-free**; reads already-resolved files only. It must **never raise** to its caller — on internal failure return `([], scan_incomplete=True)`.
- **Secret values never enter `requirements`** — only env-var *names*. The existing scrub/args-scrub/slice-leak gate stay authoritative.
- **Over-report, don't under-report** — a false positive is dismissable; a missed dep is the bug we are fixing.
- Vendored service-ID data is **Gitleaks-derived (MIT)**. Do **not** copy TruffleHog code (AGPL) — names only, as data.
- `gog`/Google classifies as **`unavailable` (gated)** per product decision; do not ungate.
- Follow existing module patterns: `frozenset` constants, `dataclasses.dataclass`, repositories for DDB, `core/services/` layout.
- Tests: targeted pytest during dev; **no full `pnpm test`**; full backend suite only at final verification.
- Commit after every green step. Work only on branch `marketplace-reqs-transparency` (worktree `.claude/worktrees/marketplace-reqs-transparency`). Do not push/PR without explicit approval.

---

## File Structure

- `core/services/marketplace/__init__.py` — new subpackage.
- `core/services/marketplace/requirements.py` — `Requirement` dataclass + `Disposition` + `requirements_scan()`.
- `core/services/marketplace/requirements_detect.py` — body-parse + declared-read detectors (raw hits).
- `core/services/marketplace/requirements_classify.py` — hit → `Requirement` classification + the registries.
- `core/services/marketplace/known_services.yaml` — Gitleaks-seeded host/env→service table (data).
- `core/services/agent_export.py` — call scanner at artifact build; write manifest blocks (modify).
- `core/services/marketplace_service.py` — DDB requirements summary; preview passthrough (modify).
- `routers/marketplace_listings.py` — publish gate + preview `requirements` (modify).
- `core/services/catalog_service.py` — confirm hire gate consumes scanned `requires.credentials` (modify/verify).
- `scripts/backfill_marketplace_requirements.py` — re-scan live listings (new).
- Tests under `apps/backend/tests/unit/services/marketplace/` + existing gate test files.

---

## Phase A — Scanner core (pure, fully unit-tested)

### Task A1: `Requirement` model + disposition enum

**Files:**
- Create: `apps/backend/core/services/marketplace/__init__.py` (empty)
- Create: `apps/backend/core/services/marketplace/requirements.py`
- Test: `apps/backend/tests/unit/services/marketplace/test_requirements_model.py`

**Interfaces:**
- Produces: `Disposition` (str enum: `INCLUDED, CONNECT, BYO, UNAVAILABLE`), `Requirement` dataclass with fields `key, display_name, kind, disposition, env, detected_via, bundled, required, note, reason`, and `Requirement.to_manifest() -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# test_requirements_model.py
from core.services.marketplace.requirements import Requirement, Disposition

def test_requirement_to_manifest_roundtrips_fields():
    r = Requirement(
        key="stripe", display_name="Stripe", kind="external_api",
        disposition=Disposition.BYO, env=["STRIPE_API_KEY"],
        detected_via=["pipeline:daily-feed:stripe-feed", "env:STRIPE_API_KEY"],
        bundled=False, required=True, note=None,
        reason="wiring mismatch: tab wires Stripe MCP/STRIPE_SECRET_KEY",
    )
    d = r.to_manifest()
    assert d["key"] == "stripe"
    assert d["disposition"] == "byo"
    assert d["env"] == ["STRIPE_API_KEY"]
    assert d["required"] is True
    assert "wiring mismatch" in d["reason"]

def test_disposition_values_are_stable_strings():
    assert Disposition.INCLUDED == "included"
    assert Disposition.UNAVAILABLE == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_model.py -v`
Expected: FAIL (ModuleNotFoundError: core.services.marketplace.requirements)

- [ ] **Step 3: Write minimal implementation**

```python
# core/services/marketplace/requirements.py
from __future__ import annotations
import dataclasses
from enum import Enum


class Disposition(str, Enum):
    INCLUDED = "included"      # ships in the box (bundled / keyless)
    CONNECT = "connect"        # supported in the integrations/Channels tab
    BYO = "byo"                # buyer brings their own key
    UNAVAILABLE = "unavailable"  # gated (gog) or phantom (no implementation)


@dataclasses.dataclass
class Requirement:
    key: str
    display_name: str
    kind: str  # tool|skill|mcp|cli|channel|external_api|bundled
    disposition: Disposition
    env: list[str] = dataclasses.field(default_factory=list)
    detected_via: list[str] = dataclasses.field(default_factory=list)
    bundled: bool = False
    required: bool = True
    note: str | None = None
    reason: str | None = None

    def to_manifest(self) -> dict:
        d = dataclasses.asdict(self)
        d["disposition"] = self.disposition.value
        return d
```

(Also create the empty `__init__.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_model.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace/__init__.py \
        apps/backend/core/services/marketplace/requirements.py \
        apps/backend/tests/unit/services/marketplace/test_requirements_model.py
git commit -m "feat(marketplace): Requirement model + disposition enum"
```

---

### Task A2: Body-parse detectors (tools, env vars, external hosts)

**Files:**
- Create: `apps/backend/core/services/marketplace/requirements_detect.py`
- Test: `apps/backend/tests/unit/services/marketplace/test_requirements_detect.py`

**Interfaces:**
- Produces: `detect_in_text(text: str, source: str) -> list[Hit]` and `Hit` dataclass `{kind: "tool"|"env"|"host", value: str, source: str}`; `detect_in_tree(agent_dir: Path) -> list[Hit]` walking `pipelines/*.lobster`, `scripts/*`, `*.md`.

- [ ] **Step 1: Write the failing test**

```python
# test_requirements_detect.py
from pathlib import Path
from core.services.marketplace.requirements_detect import detect_in_text, Hit

DAILY = """
  - id: stripe-feed
    command: >
      openclaw.invoke --tool stripe-api --action list-charges
  - id: bank-feed
    command: >
      exec --json --shell 'curl -s -X POST "https://production.plaid.com/transactions/get" -d "{\\"secret\\":\\"$PLAID_SECRET\\"}"'
"""

def test_detect_tool_invocations():
    hits = detect_in_text(DAILY, "pipeline:daily-feed")
    tools = {h.value for h in hits if h.kind == "tool"}
    assert "stripe-api" in tools

def test_detect_env_and_host():
    hits = detect_in_text(DAILY, "pipeline:daily-feed")
    assert any(h.kind == "env" and h.value == "PLAID_SECRET" for h in hits)
    assert any(h.kind == "host" and h.value == "production.plaid.com" for h in hits)

def test_ignores_lowercase_and_short_env():
    hits = detect_in_text("echo $x $AB $GOOD_KEY", "s")
    envs = {h.value for h in hits if h.kind == "env"}
    assert envs == {"GOOD_KEY"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_detect.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Write minimal implementation**

```python
# core/services/marketplace/requirements_detect.py
from __future__ import annotations
import dataclasses
import re
from pathlib import Path

# `--tool <name>` and `openclaw.invoke --tool <name>`; also `<name>.<action>` pipe heads.
_TOOL_FLAG = re.compile(r"--tool\s+([a-z0-9][a-z0-9._-]*)")
# $NAME or ${NAME}, uppercase + digits + underscore, length >= 3.
_ENV = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")
# scheme://host  -> capture host only.
_HOST = re.compile(r"https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")

_SCAN_GLOBS = ("pipelines/*.lobster", "pipelines/*.yaml", "pipelines/*.yml",
               "scripts/*", "*.md")


@dataclasses.dataclass(frozen=True)
class Hit:
    kind: str   # "tool" | "env" | "host"
    value: str
    source: str


def detect_in_text(text: str, source: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in _TOOL_FLAG.finditer(text):
        hits.append(Hit("tool", m.group(1), source))
    for m in _ENV.finditer(text):
        hits.append(Hit("env", m.group(1), source))
    for m in _HOST.finditer(text):
        hits.append(Hit("host", m.group(1), source))
    return hits


def detect_in_tree(agent_dir: Path) -> list[Hit]:
    hits: list[Hit] = []
    for pattern in _SCAN_GLOBS:
        for path in sorted(agent_dir.glob(pattern)):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits.extend(detect_in_text(text, source=path.name))
    return hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_detect.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace/requirements_detect.py \
        apps/backend/tests/unit/services/marketplace/test_requirements_detect.py
git commit -m "feat(marketplace): body-parse detectors for tools/env/hosts"
```

---

### Task A3: `known_services.yaml` + service-ID lookup (Gitleaks-seeded)

**Files:**
- Create: `apps/backend/core/services/marketplace/known_services.yaml`
- Modify: `apps/backend/core/services/marketplace/requirements_detect.py` (add `identify_service`)
- Test: `apps/backend/tests/unit/services/marketplace/test_known_services.py`

**Interfaces:**
- Produces: `identify_service(hit: Hit) -> tuple[str, str] | None` → `(canonical_key, display_name)` for `host`/`env` hits, via the table; `None` if unknown.

- [ ] **Step 1: Write the failing test**

```python
# test_known_services.py
from core.services.marketplace.requirements_detect import identify_service, Hit

def test_host_maps_to_service():
    assert identify_service(Hit("host", "production.plaid.com", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("host", "api.perplexity.ai", "s")) == ("perplexity", "Perplexity")
    assert identify_service(Hit("host", "api.stripe.com", "s")) == ("stripe", "Stripe")

def test_env_prefix_maps_to_service():
    assert identify_service(Hit("env", "PLAID_SECRET", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("env", "PERPLEXITY_API_KEY", "s")) == ("perplexity", "Perplexity")

def test_unknown_returns_none():
    assert identify_service(Hit("host", "example.invalid", "s")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_known_services.py -v`
Expected: FAIL (ImportError: identify_service)

- [ ] **Step 3: Write minimal implementation**

Create `known_services.yaml` (seed; extend from the Gitleaks MIT ruleset as data — names only):

```yaml
# Gitleaks-derived (MIT) service identification: canonical key -> matchers.
# host: domain suffixes; env: env-var name prefixes. Names only, no patterns copied.
stripe:      { display_name: Stripe,      hosts: [api.stripe.com],            env_prefixes: [STRIPE_] }
plaid:       { display_name: Plaid,       hosts: [plaid.com],                 env_prefixes: [PLAID_] }
perplexity:  { display_name: Perplexity,  hosts: [api.perplexity.ai],         env_prefixes: [PERPLEXITY_] }
quickbooks:  { display_name: QuickBooks,  hosts: [quickbooks.api.intuit.com], env_prefixes: [QBO_] }
xero:        { display_name: Xero,        hosts: [api.xero.com],              env_prefixes: [XERO_] }
intercom:    { display_name: Intercom,    hosts: [api.intercom.io],           env_prefixes: [INTERCOM_] }
zendesk:     { display_name: Zendesk,     hosts: [zendesk.com],               env_prefixes: [ZENDESK_] }
twilio:      { display_name: Twilio,      hosts: [api.twilio.com],            env_prefixes: [TWILIO_] }
```

Add to `requirements_detect.py`:

```python
import functools
import yaml

_TABLE_PATH = Path(__file__).with_name("known_services.yaml")


@functools.lru_cache(maxsize=1)
def _service_table() -> dict:
    with _TABLE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def identify_service(hit: Hit) -> tuple[str, str] | None:
    table = _service_table()
    if hit.kind == "host":
        for key, spec in table.items():
            for suffix in spec.get("hosts", []):
                if hit.value == suffix or hit.value.endswith("." + suffix):
                    return key, spec["display_name"]
    elif hit.kind == "env":
        for key, spec in table.items():
            for prefix in spec.get("env_prefixes", []):
                if hit.value.startswith(prefix):
                    return key, spec["display_name"]
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_known_services.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace/known_services.yaml \
        apps/backend/core/services/marketplace/requirements_detect.py \
        apps/backend/tests/unit/services/marketplace/test_known_services.py
git commit -m "feat(marketplace): Gitleaks-seeded service identification table"
```

---

### Task A4: Classification registry + `classify_hits`

**Files:**
- Create: `apps/backend/core/services/marketplace/requirements_classify.py`
- Test: `apps/backend/tests/unit/services/marketplace/test_requirements_classify.py`

**Interfaces:**
- Consumes: `Hit` (A2), `identify_service` (A3), `Requirement`/`Disposition` (A1), `providers.yaml` via `core.services.credentials.registry`.
- Produces: `classify_hits(hits: list[Hit], *, bundled_mcps: set[str], bundled_skills: set[str], config_skills: dict, config_mcps: dict) -> list[Requirement]`.
- Module constants: `BUNDLED_TOOLS`, `BUNDLED_SKILLS`, `CHANNELS`, `GATED_PROVIDERS` (`{"gog","google"}`), and `_supported_providers()` (keys from `providers.yaml`).

- [ ] **Step 1: Write the failing test**

```python
# test_requirements_classify.py
from core.services.marketplace.requirements_detect import Hit
from core.services.marketplace.requirements_classify import classify_hits
from core.services.marketplace.requirements import Disposition

def _by_key(reqs):
    return {r.key: r for r in reqs}

def test_bundled_tool_is_included():
    reqs = _by_key(classify_hits([Hit("tool", "llm-task", "p")],
                                 bundled_mcps=set(), bundled_skills=set(),
                                 config_skills={}, config_mcps={}))
    assert reqs["llm-task"].disposition == Disposition.INCLUDED

def test_gog_is_unavailable_gated():
    reqs = _by_key(classify_hits([Hit("tool", "gog", "p")],
                                 bundled_mcps=set(), bundled_skills=set(),
                                 config_skills={}, config_mcps={}))
    assert reqs["gog"].disposition == Disposition.UNAVAILABLE
    assert "Bedrock" in (reqs["gog"].reason or "")

def test_slack_tool_is_connect_channel():
    reqs = _by_key(classify_hits([Hit("tool", "slack", "p")],
                                 bundled_mcps=set(), bundled_skills=set(),
                                 config_skills={}, config_mcps={}))
    assert reqs["slack"].disposition == Disposition.CONNECT
    assert reqs["slack"].kind == "channel"

def test_unknown_external_api_is_byo():
    reqs = _by_key(classify_hits(
        [Hit("host", "production.plaid.com", "p"), Hit("env", "PLAID_SECRET", "p")],
        bundled_mcps=set(), bundled_skills=set(), config_skills={}, config_mcps={}))
    assert reqs["plaid"].disposition == Disposition.BYO
    assert "PLAID_SECRET" in reqs["plaid"].env

def test_phantom_skill_is_unavailable():
    # referenced as a tool, not bundled, not in config, not a known service/provider
    reqs = _by_key(classify_hits([Hit("tool", "stripe-api", "p")],
                                 bundled_mcps=set(), bundled_skills=set(),
                                 config_skills={}, config_mcps={}))
    assert reqs["stripe-api"].disposition == Disposition.UNAVAILABLE
    assert "implementation" in (reqs["stripe-api"].reason or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_classify.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Write minimal implementation**

```python
# core/services/marketplace/requirements_classify.py
from __future__ import annotations
import functools

from core.services.marketplace.requirements import Disposition, Requirement
from core.services.marketplace.requirements_detect import Hit, identify_service

# Core tools + plugins baked into the image (no upstream registry — we own this list).
BUNDLED_TOOLS = frozenset({
    "llm-task", "fast-io", "summarize", "lobster", "memory-core",
    "message", "capability-evolver",
})
BUNDLED_SKILLS = frozenset({"capability-evolver", "skill-vetter"})
CHANNELS = frozenset({"slack", "discord", "telegram", "whatsapp"})
GATED_PROVIDERS = frozenset({"gog", "google"})


@functools.lru_cache(maxsize=1)
def _supported_providers() -> dict:
    """Map of integration-tab provider key -> spec, from providers.yaml.
    Also index by the tool/actor key each provider wires."""
    from core.services.credentials import registry
    return registry.load_providers()  # {key: {actor, wiring, requires_bedrock, internal, ...}}


def _provider_for_tool(name: str) -> tuple[str, dict] | None:
    for key, spec in _supported_providers().items():
        wiring = spec.get("wiring", {}) or {}
        mcp = wiring.get("mcp_server", {}) or {}
        if key == name or mcp.get("key") == name:
            return key, spec
    return None


def classify_hits(hits, *, bundled_mcps, bundled_skills, config_skills, config_mcps) -> list[Requirement]:
    by_key: dict[str, Requirement] = {}

    def _ensure(key, display, kind, disposition, *, env=None, reason=None, bundled=False):
        r = by_key.get(key)
        if r is None:
            r = Requirement(key=key, display_name=display, kind=kind,
                            disposition=disposition, env=list(env or []),
                            reason=reason, bundled=bundled)
            by_key[key] = r
        else:
            for e in (env or []):
                if e not in r.env:
                    r.env.append(e)
        return r

    for hit in hits:
        if hit.kind == "tool":
            name = hit.value
            if name in BUNDLED_TOOLS:
                r = _ensure(name, name, "bundled", Disposition.INCLUDED)
            elif name in CHANNELS:
                r = _ensure(name, name.capitalize(), "channel", Disposition.CONNECT)
            elif name in GATED_PROVIDERS:
                r = _ensure(name, "Google Sheets/Drive", "cli", Disposition.UNAVAILABLE,
                            reason="internal + Bedrock-only on this platform")
            else:
                prov = _provider_for_tool(name)
                if prov:
                    key, spec = prov
                    disp = (Disposition.UNAVAILABLE
                            if spec.get("internal") or spec.get("requires_bedrock")
                            else Disposition.CONNECT)
                    r = _ensure(key, spec.get("display_name", key), spec.get("actor", "mcp"), disp,
                                reason=("internal + Bedrock-only on this platform"
                                        if disp == Disposition.UNAVAILABLE else None))
                elif name in config_skills or name in bundled_skills:
                    r = _ensure(name, name, "skill", Disposition.BYO)
                else:
                    # referenced but unresolvable -> phantom
                    r = _ensure(name, name, "skill", Disposition.UNAVAILABLE,
                                reason="no implementation found")
            r.detected_via.append(hit.source)
        else:  # env / host -> external API
            ident = identify_service(hit)
            if ident is None:
                continue
            key, display = ident
            prov = _supported_providers().get(key)
            if prov and not (prov.get("internal") or prov.get("requires_bedrock")):
                disp = Disposition.CONNECT
                reason = None
                # Stripe-style mismatch: provider wires an MCP key but the agent
                # references a raw env var the tab won't set.
                mcp_env = (((prov.get("wiring") or {}).get("mcp_server") or {}).get("env") or {})
                if hit.kind == "env" and hit.value not in mcp_env:
                    disp = Disposition.BYO
                    reason = (f"wiring mismatch: the {display} integration wires an MCP; "
                              f"this agent references {hit.value} directly")
            elif prov and (prov.get("internal") or prov.get("requires_bedrock")):
                disp, reason = Disposition.UNAVAILABLE, "internal + Bedrock-only on this platform"
            else:
                disp, reason = Disposition.BYO, None
            env = [hit.value] if hit.kind == "env" else []
            r = _ensure(key, display, "external_api", disp, env=env, reason=reason)
            r.detected_via.append(hit.source)

    # mark bundled wiring
    for r in by_key.values():
        if r.key in bundled_mcps or r.key in bundled_skills:
            r.bundled = True
    return list(by_key.values())
```

> Note: `registry.load_providers()` must exist. If `core.services.credentials.registry` exposes the parsed `providers.yaml` under a different name, adapt `_supported_providers()` to call it (verify in A4 Step 2 by reading `registry.py`). If no loader exists, add `load_providers()` to `registry.py` returning the parsed YAML dict and cover it with one test.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_classify.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace/requirements_classify.py \
        apps/backend/tests/unit/services/marketplace/test_requirements_classify.py
git commit -m "feat(marketplace): classify detected hits into requirement dispositions"
```

---

### Task A5: `requirements_scan()` orchestrator

**Files:**
- Modify: `apps/backend/core/services/marketplace/requirements.py` (add `requirements_scan`)
- Test: `apps/backend/tests/unit/services/marketplace/test_requirements_scan.py` (golden Tally/Vera fixtures)

**Interfaces:**
- Consumes: `detect_in_tree` (A2), `classify_hits` (A4).
- Produces: `requirements_scan(effective_config: dict, agent_dir: Path, *, bundled_mcps: set[str] | None = None, bundled_skills: set[str] | None = None) -> tuple[list[Requirement], bool]` — returns `(requirements, scan_incomplete)`. Never raises.

- [ ] **Step 1: Write the failing test**

```python
# test_requirements_scan.py
from pathlib import Path
from core.services.marketplace.requirements import requirements_scan, Disposition

def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")

def test_tally_shaped_pipeline_classifies_each_dependency(tmp_path):
    _write(tmp_path, "pipelines/daily-feed.lobster", """
      - command: openclaw.invoke --tool stripe-api --action list-charges
      - command: openclaw.invoke --tool gog --action sheets-write
      - command: openclaw.invoke --tool slack --action post
      - command: openclaw.invoke --tool llm-task --action json
      - command: exec --shell 'curl https://production.plaid.com/x -d "$PLAID_SECRET"'
      - command: exec --shell 'curl https://api.perplexity.ai/x -H "$PERPLEXITY_API_KEY"'
    """)
    reqs, incomplete = requirements_scan({}, tmp_path)
    assert incomplete is False
    d = {r.key: r.disposition for r in reqs}
    assert d["llm-task"] == Disposition.INCLUDED
    assert d["slack"] == Disposition.CONNECT
    assert d["gog"] == Disposition.UNAVAILABLE
    assert d["stripe-api"] == Disposition.UNAVAILABLE   # phantom skill
    assert d["plaid"] == Disposition.BYO
    assert d["perplexity"] == Disposition.BYO

def test_scan_never_raises_on_garbage(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "x.lobster").write_bytes(b"\xff\xfe not utf8 \x00")
    reqs, incomplete = requirements_scan({}, tmp_path)
    assert isinstance(reqs, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_scan.py -v`
Expected: FAIL (ImportError: requirements_scan)

- [ ] **Step 3: Write minimal implementation**

Append to `requirements.py`:

```python
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def requirements_scan(effective_config, agent_dir, *, bundled_mcps=None, bundled_skills=None):
    from core.services.marketplace.requirements_detect import detect_in_tree
    from core.services.marketplace.requirements_classify import classify_hits
    try:
        cfg = effective_config or {}
        config_skills = ((cfg.get("skills") or {}).get("entries") or {})
        config_mcps = ((cfg.get("mcp") or {}).get("servers") or {})
        hits = detect_in_tree(Path(agent_dir))
        reqs = classify_hits(
            hits,
            bundled_mcps=set(bundled_mcps or ()),
            bundled_skills=set(bundled_skills or ()),
            config_skills=config_skills,
            config_mcps=config_mcps,
        )
        # stable order: unavailable, byo, connect, included
        order = {"unavailable": 0, "byo": 1, "connect": 2, "included": 3}
        reqs.sort(key=lambda r: (order.get(r.disposition.value, 9), r.key))
        return reqs, False
    except Exception:  # scanner must never break publish
        logger.exception("requirements_scan failed for %s", agent_dir)
        return [], True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_scan.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace/requirements.py \
        apps/backend/tests/unit/services/marketplace/test_requirements_scan.py
git commit -m "feat(marketplace): requirements_scan orchestrator (never raises)"
```

---

## Phase B — Manifest population

### Task B1: Freeze requirements into the manifest at artifact build

**Files:**
- Modify: `apps/backend/core/services/agent_export.py` (`export_agent_from_efs`, after `manifest = {...}` ~line 473; uses existing `_resolve_agent_dir`, `selected_mcps`, `selected_skills`)
- Test: `apps/backend/tests/unit/services/test_agent_export.py` (add cases)

**Interfaces:**
- Consumes: `requirements_scan` (A5).
- Produces: manifest carries `requirements: list[dict]`, `requires: {credentials: [{provider, optional}]}`, `scan_incomplete: bool`.

- [ ] **Step 1: Write the failing test**

```python
def test_export_manifest_carries_requirements(monkeypatch, tally_agent_dir, seller_ctx):
    # tally_agent_dir fixture writes a pipelines/daily-feed.lobster calling
    # --tool stripe-api, --tool slack, curl plaid; see fixture.
    pkg = export_agent_from_efs(seller_id=seller_ctx.id, agent_id="tally")
    m = pkg.manifest
    keys = {r["key"]: r["disposition"] for r in m["requirements"]}
    assert keys["plaid"] == "byo"
    assert keys["slack"] == "connect"
    # requires.credentials lists connectable + byo registry providers, not bundled
    provs = {c["provider"] for c in m["requires"]["credentials"]}
    assert "slack" in provs
    assert "llm-task" not in provs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k requirements -v`
Expected: FAIL (KeyError: 'requirements')

- [ ] **Step 3: Write minimal implementation**

In `export_agent_from_efs`, after the existing `manifest = {...}` block:

```python
from core.services.marketplace.requirements import requirements_scan

agent_dir = _resolve_agent_dir(seller_id, agent_id)
# effective config: per-workspace if present (folds in the 2026-06-15 Fix 2),
# else the global config already read above as `config`.
effective_cfg = _summarize_openclaw_config(agent_dir) or config
reqs, scan_incomplete = requirements_scan(
    effective_cfg, agent_dir,
    bundled_mcps=set(selected_mcps or []),
    bundled_skills=set(selected_skills or []),
)
manifest["requirements"] = [r.to_manifest() for r in reqs]
manifest["scan_incomplete"] = scan_incomplete
# Feed the existing hire gate: connect + byo rows that resolve to a registry provider.
_CREDENTIAL_DISPOSITIONS = {"connect", "byo"}
manifest["requires"] = {
    "credentials": [
        {"provider": r.key, "optional": not r.required}
        for r in reqs
        if r.disposition.value in _CREDENTIAL_DISPOSITIONS and r.kind != "external_api" or
           (r.disposition.value == "connect")
    ]
}
```

> Refine the `requires.credentials` filter to: include a row when `disposition == "connect"` (registry/channel provider the buyer can connect). `byo`/`unavailable` are surfaced in `requirements` but must NOT enter `requires.credentials` (the hire gate can't verify them). Implement as:
> ```python
> manifest["requires"] = {"credentials": [
>     {"provider": r.key, "optional": not r.required}
>     for r in reqs if r.disposition.value == "connect"
> ]}
> ```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k requirements -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/tests/unit/services/test_agent_export.py
git commit -m "feat(marketplace): freeze scanned requirements into the manifest"
```

---

### Task B2: Preview endpoint returns requirements (live, for the wizard)

**Files:**
- Modify: `apps/backend/core/services/agent_export.py` (`enumerate_for_preview`)
- Modify: `apps/backend/core/services/marketplace_service.py` if the preview is shaped there
- Test: `apps/backend/tests/unit/services/test_agent_export.py`

**Interfaces:**
- Produces: preview dict gains `requirements: list[dict]` alongside `available_mcps`/`available_skills`/`safety_flags`.

- [ ] **Step 1: Write the failing test**

```python
def test_preview_includes_requirements(tally_agent_dir, seller_ctx):
    preview = enumerate_for_preview(seller_id=seller_ctx.id, agent_id="tally")
    assert "requirements" in preview
    assert any(r["key"] == "plaid" for r in preview["requirements"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k preview_includes -v`
Expected: FAIL (KeyError)

- [ ] **Step 3: Write minimal implementation**

In `enumerate_for_preview`, compute the same scan (no bundling selection yet — pass empty sets so everything classifies generically) and add to the returned dict:

```python
from core.services.marketplace.requirements import requirements_scan
agent_dir = _resolve_agent_dir(seller_id, agent_id)
effective_cfg = _summarize_openclaw_config(agent_dir) or config
reqs, _ = requirements_scan(effective_cfg, agent_dir)
preview["requirements"] = [r.to_manifest() for r in reqs]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k preview_includes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/tests/unit/services/test_agent_export.py
git commit -m "feat(marketplace): artifact preview returns live requirements"
```

---

### Task B3: DDB listing summary (counts + provider keys)

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py` (`_upload_artifact_to_s3` caller / row write)
- Test: `apps/backend/tests/unit/services/test_marketplace_service.py`

**Interfaces:**
- Produces: listing DDB row gains `requirements_summary: {included, connect, byo, unavailable}` (counts) + `requires_providers: [str]` (connect keys) for fast list render + publish gate.

- [ ] **Step 1: Write the failing test**

```python
def test_listing_row_carries_requirements_summary():
    manifest = {"requirements": [
        {"key": "plaid", "disposition": "byo"},
        {"key": "slack", "disposition": "connect"},
        {"key": "gog", "disposition": "unavailable"},
        {"key": "llm-task", "disposition": "included"},
    ]}
    summary = summarize_requirements(manifest)
    assert summary["counts"] == {"included": 1, "connect": 1, "byo": 1, "unavailable": 1}
    assert summary["requires_providers"] == ["slack"]
    assert summary["has_blocking"] is True   # an unavailable row exists
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py -k requirements_summary -v`
Expected: FAIL (ImportError: summarize_requirements)

- [ ] **Step 3: Write minimal implementation**

```python
# marketplace_service.py
def summarize_requirements(manifest: dict) -> dict:
    reqs = (manifest or {}).get("requirements") or []
    counts = {"included": 0, "connect": 0, "byo": 0, "unavailable": 0}
    providers = []
    for r in reqs:
        disp = r.get("disposition")
        if disp in counts:
            counts[disp] += 1
        if disp == "connect":
            providers.append(r["key"])
    return {
        "counts": counts,
        "requires_providers": sorted(set(providers)),
        "has_blocking": counts["unavailable"] > 0,
    }
```

Persist `summary` onto the listing row where the row is written after upload.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py -k requirements_summary -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/tests/unit/services/test_marketplace_service.py
git commit -m "feat(marketplace): persist requirements summary on listing row"
```

---

## Phase C — Gates

### Task C1: Publish gate — block/acknowledge on "won't run"

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py` (submit/publish handler)
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py`

**Interfaces:**
- Consumes: `summarize_requirements` (B3).
- Produces: submit returns 422 `{error: "requirements_blocking", unavailable: [...]}` unless the request carries `acknowledge_unavailable: true`.

- [ ] **Step 1: Write the failing test**

```python
def test_submit_blocks_on_unavailable_without_ack(client, draft_with_unavailable_req):
    resp = client.post(f"/api/v1/marketplace/listings/{draft_with_unavailable_req}/submit", json={})
    assert resp.status_code == 422
    assert resp.json()["error"] == "requirements_blocking"
    assert "gog" in resp.json()["unavailable"]

def test_submit_allowed_with_ack(client, draft_with_unavailable_req):
    resp = client.post(f"/api/v1/marketplace/listings/{draft_with_unavailable_req}/submit",
                       json={"acknowledge_unavailable": True})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_listings.py -k unavailable -v`
Expected: FAIL (currently 200 without ack)

- [ ] **Step 3: Write minimal implementation**

In the submit handler, before flipping draft→review:

```python
manifest = marketplace_service.read_manifest(listing_id)  # existing artifact manifest
summary = marketplace_service.summarize_requirements(manifest)
if summary["has_blocking"] and not body.acknowledge_unavailable:
    unavailable = [r["key"] for r in (manifest.get("requirements") or [])
                   if r.get("disposition") == "unavailable"]
    raise HTTPException(status_code=422, detail={
        "error": "requirements_blocking", "unavailable": unavailable,
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_listings.py -k unavailable -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "feat(marketplace): publish gate on won't-run requirements (acknowledge to override)"
```

---

### Task C2: Hire gate — fed by scanned requirements; warn rows surfaced

**Files:**
- Modify: `apps/backend/core/services/catalog_service.py` (`assert_credentials_satisfied` — verify it reads scanned `requires.credentials`; add warn-row passthrough)
- Test: `apps/backend/tests/credentials/test_marketplace_requires_credentials.py` (extend)

**Interfaces:**
- Consumes: `manifest.requires.credentials` (B1) + `manifest.requirements` (B1).
- Produces: `MissingCredentialsError.missing` (existing) blocks on unconnected `connect` providers; a new `warn_rows` (byo/unavailable) is returned/attached for the hire dialog, never blocking.

- [ ] **Step 1: Write the failing test**

```python
async def test_connect_provider_blocks_when_unconnected(monkeypatch):
    monkeypatch.setattr(catalog_service, "_connected_providers",
                        _async_return(set()))
    manifest = {"requires": {"credentials": [{"provider": "slack"}]}}
    with pytest.raises(catalog_service.MissingCredentialsError) as e:
        await catalog_service.assert_credentials_satisfied("owner_1", manifest, slice_=None)
    assert "slack" in e.value.missing

async def test_byo_rows_never_block_but_are_reported(monkeypatch):
    monkeypatch.setattr(catalog_service, "_connected_providers", _async_return(set()))
    manifest = {"requires": {"credentials": []},
                "requirements": [{"key": "plaid", "disposition": "byo"},
                                 {"key": "gog", "disposition": "unavailable"}]}
    # does not raise; warn rows available via helper
    await catalog_service.assert_credentials_satisfied("owner_1", manifest, slice_=None)
    warn = catalog_service.warn_rows(manifest)
    assert {w["key"] for w in warn} == {"plaid", "gog"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_marketplace_requires_credentials.py -k "byo_rows or connect_provider_blocks" -v`
Expected: FAIL (warn_rows missing)

- [ ] **Step 3: Write minimal implementation**

`assert_credentials_satisfied` already blocks on `requires.credentials` minus connected — confirm `connect` providers flow through (they do, since B1 writes them). Add:

```python
def warn_rows(manifest: dict) -> list[dict]:
    """byo + unavailable requirements the buyer should see but that never block."""
    return [r for r in (manifest or {}).get("requirements", [])
            if r.get("disposition") in ("byo", "unavailable")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/credentials/test_marketplace_requires_credentials.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/catalog_service.py apps/backend/tests/credentials/test_marketplace_requires_credentials.py
git commit -m "feat(marketplace): hire gate fed by scanned requirements + non-blocking warn rows"
```

---

## Phase D — Backfill live listings

### Task D1: Re-scan published listings into their manifests

**Files:**
- Create: `apps/backend/scripts/backfill_marketplace_requirements.py`
- Test: `apps/backend/tests/unit/scripts/test_backfill_marketplace_requirements.py`

**Interfaces:**
- Consumes: `requirements_scan` (A5), `summarize_requirements` (B3), S3 artifacts.
- Produces: writes a sidecar `requirements.json` next to the version's `manifest.json` in S3 + updates the DDB summary. Dry-run default; `--apply` to write. Idempotent.

- [ ] **Step 1: Write the failing test**

```python
def test_backfill_computes_requirements_for_tarball(tmp_path, tally_tarball_bytes):
    from scripts.backfill_marketplace_requirements import scan_tarball
    reqs = scan_tarball(tally_tarball_bytes)
    keys = {r["key"] for r in reqs}
    assert "plaid" in keys and "slack" in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_backfill_marketplace_requirements.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/backfill_marketplace_requirements.py
import io, tarfile, tempfile
from pathlib import Path
from core.services.marketplace.requirements import requirements_scan


def scan_tarball(tarball_bytes: bytes) -> list[dict]:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tf:
            tf.extractall(root, filter="data")
        # the agent tree is at the tar root (./pipelines, ./scripts, ./openclaw.json)
        cfg = {}
        cfg_path = root / "openclaw.json"
        if cfg_path.exists():
            import json
            cfg = json.loads(cfg_path.read_text())
        reqs, _ = requirements_scan(cfg, root)
        return [r.to_manifest() for r in reqs]

# main(): paginate listings table (status=published), for each fetch
# listings/{id}/v{n}/workspace.tar.gz, scan, write requirements.json sidecar +
# DDB summary when --apply. Dry-run prints the diff.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_backfill_marketplace_requirements.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/scripts/backfill_marketplace_requirements.py \
        apps/backend/tests/unit/scripts/test_backfill_marketplace_requirements.py
git commit -m "feat(marketplace): backfill requirements for live listings (dry-run default)"
```

---

## Final verification

- [ ] Run the whole marketplace backend suite: `cd apps/backend && uv run pytest tests/unit/services/marketplace tests/unit/services/test_agent_export.py tests/unit/routers/test_marketplace_listings.py tests/credentials/test_marketplace_requires_credentials.py -v`
- [ ] `uv run ruff format --check .` and `uv run ruff check .` clean.
- [ ] Dry-run the backfill against dev to eyeball Tally/Vera output (no `--apply`).

## Spec coverage check

- Scanner (declared-read ∪ body-parse) → A2/A3/A4/A5. ✓
- Classification (bundled/providers/channels/gog/byo + Stripe mismatch) → A4. ✓
- Manifest `requirements` + `requires.credentials` → B1. ✓
- Wizard live preview → B2. ✓ (frontend render = Plan 2)
- DDB summary → B3. ✓
- Publish gate → C1. ✓
- Hire gate (reuse + warn rows) → C2. ✓
- Backfill Tally/Vera → D1. ✓
- Effective-config dependency → B1/B2 use per-workspace config when present; body-parse works regardless. ✓
- **Frontend Requirements Ledger (two modes, personalization) → Plan 2 (separate).**
