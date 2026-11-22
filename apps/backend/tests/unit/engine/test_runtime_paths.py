"""The internal ALB sends the agent runtime's callbacks to two services by path alone
(`apps/infra/lib/engine-paths.json`, network-stack.ts): a path an engine pattern matches goes to the engine service
(`engine_main.app`), every other path to the backend (`main.app`). A callback sent to a service that does not serve it
is a 404 in a live agent turn, so every path the bridge and the plugin call is checked against both route tables."""

import json
import re
from pathlib import Path

import pytest
from starlette.routing import Match, Mount

INFRA = Path(__file__).resolve().parents[4] / "infra"
SOURCES = [INFRA / "hermes" / "bridge", INFRA / "hermes" / "plugins" / "lightbulb"]
ENGINE_PATTERNS = json.loads((INFRA / "lib" / "engine-paths.json").read_text())["paths"]

#: Callers still in the image whose server side is gone (the agent network's and A2A's routes): no service serves
#: them before or after this routing; each logs the 404 and drops it.
GONE = {
    "/api/v1/internal/network/org_test/runtime-proof",
    "/api/v1/internal/a2a/org_test/ask-session",
    "/api/v1/internal/a2a/org_test/session-reply",
    "/api/v1/internal/messaging/network/holds/org_test",
}

_LITERAL = re.compile(r"/api/v1/internal/[A-Za-z0-9_{}'\[\]./-]*")
_BRIDGE_HELPER = re.compile(r"""_(?:post_backend|get_backend_bytes)\(\s*f(?:"([^"]+)"|'([^']+)')""")  # .../messaging/
_VAR = re.compile(r"\{[^}]*\}")


def _engine_rule(path: str) -> bool:
    """ALB path-pattern matching: `*` any characters (slashes included), `?` one, case-sensitive, whole path."""
    return any(re.fullmatch(re.escape(p).replace(r"\*", ".*").replace(r"\?", "."), path) for p in ENGINE_PATTERNS)


def _serves(app, path: str) -> bool:
    """Some route of `app` has this path, whatever the method (the ALB never looks at the method)."""
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
        # Routers are included lazily (`_IncludedRouter`); only the root Mount matches every path.
        if any(r.matches(scope)[0] is Match.FULL for r in app.routes if not isinstance(r, Mount)):
            return True
    return False


def _route_paths(app) -> set[str]:
    """Every route path of `app`, hidden routes too (FastAPI includes routers lazily: their effective routes)."""
    out = set()
    for r in app.routes:
        for c in getattr(r, "effective_candidates", lambda r=r: [r])():
            if path := getattr(c, "path", None):
                out.add(path)
    return out


def _templates() -> set[str]:
    found = set()
    for src in (f for d in SOURCES for f in d.rglob("*.py") if "tests" not in f.parts):
        text = src.read_text()
        found |= {m.rstrip(".'") for m in _LITERAL.findall(text)}
        found |= {"/api/v1/internal/messaging/" + (a or b) for a, b in _BRIDGE_HELPER.findall(text)}
    return found


#: Every distinct callback path template the bridge and the plugin hold. A new or removed caller changes it: route the
#: new path (or drop the gone one), then set the count.
CALLBACK_TEMPLATES = 38


@pytest.fixture
def apps():
    """The two route tables. Importing `engine_main` registers the HTTP hooks process-wide; the hooks registered before
    are put back, so no later test in this worker calls the products over HTTP."""
    from core.engine import hooks

    before = hooks._registered
    import engine_main
    import main

    try:
        yield engine_main.app, main.app
    finally:
        hooks._registered = before


def test_engine_patterns_fit_one_alb_rule():
    assert 1 <= len(ENGINE_PATTERNS) <= 5 and len(set(ENGINE_PATTERNS)) == len(ENGINE_PATTERNS)
    assert all(p.startswith("/api/v1/internal/") and len(p) <= 128 for p in ENGINE_PATTERNS)


def test_every_runtime_callback_is_routed(apps):
    engine, backend = apps
    templates = _templates()
    assert len(templates) == CALLBACK_TEMPLATES, f"callbacks under {SOURCES} changed: {sorted(templates)}"
    wrong, seen = [], set()
    for template in sorted(templates):
        if template.endswith("{path}"):  # a helper's base: its callers name the rest
            prefix = _VAR.sub("org_test", template.removesuffix("{path}"))
            app = engine if _engine_rule(prefix + "x") else backend
            if not any(_VAR.sub("org_test", p).startswith(prefix) for p in _route_paths(app)):
                wrong.append(f"{template}: nothing under it on the service the ALB picks")
            continue
        path = _VAR.sub("org_test", template)
        seen.add(path)
        if path in GONE:
            assert not _serves(engine, path) and not _serves(backend, path), (
                f"{path} is served again: drop it from GONE"
            )
        elif _engine_rule(path):
            if not _serves(engine, path):
                wrong.append(f"{path}: the ALB sends it to the engine, which does not serve it")
        elif not _serves(backend, path):
            wrong.append(f"{path}: the ALB sends it to the backend, which does not serve it")
    assert GONE <= seen, f"a caller in GONE left the image: drop it ({sorted(GONE - seen)})"
    assert not wrong, "\n".join(wrong)


def test_no_product_route_under_engine_paths(apps):
    """A backend route under an engine pattern would be unreachable once the ALB sends that path to the engine."""
    engine, backend = apps
    stranded = sorted(
        p
        for p in _route_paths(backend)
        if _engine_rule(_VAR.sub("org_test", p)) and not _serves(engine, _VAR.sub("org_test", p))
    )
    assert not stranded, f"backend routes the engine does not serve, under an engine path: {stranded}"


def test_the_engine_callbacks_stay_on_the_backend(apps):
    """The engine's own calls into the products land on the backend's /api/v1/internal/engine/* through :80."""
    engine, backend = apps
    for path in (
        "/api/v1/internal/engine/context",
        "/api/v1/internal/engine/events",
        "/api/v1/internal/engine/tools/x",
    ):
        assert not _engine_rule(path) and _serves(backend, path) and not _serves(engine, path)
