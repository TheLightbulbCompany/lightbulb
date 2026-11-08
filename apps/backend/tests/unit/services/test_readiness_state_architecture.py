"""INV-1 enforcement: ``readiness_state.compute_status`` is the SOLE
composer of readiness+onboarding truth from the three inputs (containers
row, billing-accounts row, paperclip-companies row). Gateway liveness was a
fourth until the AgentCore move: AWS parks an idle microVM invisibly and wakes
it on invoke, so a health probe answers "asleep", not "not ready" -- and
probing would wake the very box the poll must leave parked.

**Scope, honestly stated.** A literal "no module anywhere imports more
than one of the four store modules" import-linter rule was tried first (a
plain AST import-count scan across ``apps/backend``) and produces 21
pre-existing hits — ``ecs_manager.py``, ``account_wipe.py``,
``admin_service.py``, ``stripe_webhook_handlers.py``,
``paperclip_openclaw_pairing.py``, ``websocket_chat.py``, and more — every
one of them a legitimate, unrelated multi-repo MUTATION or admin/wipe path
(writing container rows, deleting Paperclip companies, resolving billing
for Stripe webhooks, etc.), not a second reader deriving a phase/status/
onboarding verdict. Retrofitting all of them onto the composer, or onto a
blanket allowlist that would need to grow with every future legitimate
multi-repo write path, is out of scope for this PR and would dilute the
signal this test exists to give.

So this test enforces INV-1 precisely where it was actually violated
before this PR, and where a regression would reintroduce the disease the
architecture doc names (readiness composed per-request from multiple
stores with no single owner):

1. ``core/services/readiness_state.py`` exists and is the only module
   defining a ``compute_status`` function anywhere under ``apps/backend``
   (a second same-named composer is exactly the failure mode INV-1
   forbids — a duplicate would silently fork the "what is this owner's
   readiness" answer).
2. ``routers/container.py`` — the ONE pre-COMPOSER offender (it directly
   imported ``paperclip_repo`` inside ``_resolve_teams_status`` alongside
   its existing ``container_repo`` use, composing two of the four stores
   itself) — no longer imports ``paperclip_repo`` or ``billing_repo``
   directly. It may still import ``container_repo`` (for provisioning
   mutations unrelated to readiness composition — auto-retry dispatch,
   stale-lease detection); it never composes gateway liveness itself
   either — the READINESS verdict itself comes from
   ``readiness_state.compute_status``.
3. ``core/services/readiness_state.py`` itself is verified to actually
   import all three stores (a sanity check that the composer is doing its
   job, not that it's vacuously "compliant" by reading nothing).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]

_STORE_MODULES = {
    "container_repo": "core.repositories.container_repo",
    "billing_repo": "core.repositories.billing_repo",
    "paperclip_repo": "core.repositories.workspace_repo",
}


def _imported_stores(path: Path) -> set[str]:
    """Which of the three store modules `path` imports, by any of the
    idioms this codebase actually uses (`import core.repositories.X`,
    `from core.repositories import X`, `from core.repositories.X import Y`).
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for key, mod in _STORE_MODULES.items():
                    if alias.name == mod or alias.name.startswith(mod + "."):
                        found.add(key)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for key, mod in _STORE_MODULES.items():
                if node.module == mod:
                    found.add(key)
                if node.module in ("core.repositories", "core.services"):
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" == mod:
                            found.add(key)
    return found


def test_readiness_state_module_exists_and_imports_all_three_stores():
    path = BACKEND_ROOT / "core" / "services" / "readiness_state.py"
    assert path.is_file(), "core/services/readiness_state.py must exist — it is the SOLE composer (INV-1)"
    stores = _imported_stores(path)
    assert stores == set(_STORE_MODULES), (
        "readiness_state.py must import all three readiness inputs "
        f"(container_repo, billing_repo, paperclip_repo); got {stores}"
    )


def test_compute_status_is_defined_in_exactly_one_module():
    """A second ``compute_status`` (or a same-shaped composer under a
    different name that ALSO reads >=2 of the three stores to derive a
    phase/lifecycle verdict) would fork the single readiness answer INV-1
    exists to prevent. This scans for the literal function name — a
    same-behavior duplicate under a different name would slip past a
    name-based scan, but would also be a much stranger thing for a future
    PR to introduce than simply reusing the existing one.
    """
    hits = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "compute_status":
                hits.append(str(path.relative_to(BACKEND_ROOT)))
    assert hits == ["core/services/readiness_state.py"], (
        f"compute_status must be defined in exactly readiness_state.py; found in {hits}"
    )


def test_container_router_no_longer_composes_paperclip_or_billing_directly():
    """The regression this PR fixes: routers/container.py used to import
    ``paperclip_repo`` directly (inside the now-deleted
    ``_resolve_teams_status``) alongside its own ``container_repo`` use —
    composing two of the three readiness stores itself, in the router, the
    exact thing INV-1 forbids. It must now source teams/onboarding state
    exclusively through ``readiness_state.compute_status``.

    ``container_repo`` is still allowed here — it backs provisioning
    MUTATIONS (auto-retry dispatch, stale-lease detection) that are not
    readiness composition. ``billing_repo`` must not appear directly
    either: subscription/provider gating in this router already goes
    through ``core.services.provision_gate`` (which owns its own
    billing_repo read for gate evaluation, a distinct concern from
    readiness composition) or through the composed contract.
    """
    path = BACKEND_ROOT / "routers" / "container.py"
    stores = _imported_stores(path)
    assert "paperclip_repo" not in stores, (
        "routers/container.py must not import paperclip_repo directly — "
        "read teams_state/onboarding via readiness_state.compute_status instead"
    )
    assert "billing_repo" not in stores, (
        "routers/container.py must not import billing_repo directly — "
        "provider/subscription facts come from provision_gate (gating) or "
        "readiness_state.compute_status (the composed contract), never a "
        "direct read in the router"
    )


def test_readiness_state_docstring_states_the_three_inputs():
    """Cheap documentation-drift guard: if someone adds a fourth input (or
    quietly drops one) to compute_status, the module docstring should say
    so — this just pins that the docstring currently enumerates all three.
    """
    src = (BACKEND_ROOT / "core" / "services" / "readiness_state.py").read_text()
    module_doc = ast.get_docstring(ast.parse(src)) or ""
    for needle in ("containers", "billing-accounts", "paperclip-companies"):
        assert needle in module_doc, textwrap.dedent(f"""
            readiness_state.py's module docstring should name "{needle}" as
            one of the three composed inputs — if this fails because the
            input set genuinely changed, update this test alongside it.
            """)
