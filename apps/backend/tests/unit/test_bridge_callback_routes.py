"""Every bridge callback (``turn._post_backend(f"<path>")``) must resolve to a POST route under
``/api/v1/internal/messaging/``: the helper prefixes that, and a route mounted anywhere else is a
404 the bridge logs and never retries (the hold notices before #4335)."""

import re
from pathlib import Path

from starlette.routing import Match, Mount

from engine_main import app

BRIDGE = Path(__file__).resolve().parents[3] / "infra" / "hermes" / "bridge"
_CALL = re.compile(r'_post_backend\(\s*f(["\'])([^"\']+)\1')  # either quote style
# The network sidecar's hold notice (bridge/network.py): its route went with the network's server side, and
# the image still carries the caller until its next change. A 4xx there is logged and dropped, never retried.
SIDECAR_GONE = {"network/holds/{bridge.OWNER}"}


def test_every_bridge_callback_path_is_routed():
    raw_paths = {m[1] for src in BRIDGE.glob("*.py") for m in _CALL.findall(src.read_text())}
    assert raw_paths, f"no _post_backend calls found under {BRIDGE}"
    missing = []
    assert SIDECAR_GONE <= raw_paths, "the sidecar's hold notice is gone from the bridge: empty SIDECAR_GONE"
    for raw in sorted(raw_paths - SIDECAR_GONE):
        path = "/api/v1/internal/messaging/" + re.sub(r"\{[^}]*\}", "org_test", raw)
        scope = {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": []}
        # Routers are included lazily (``_IncludedRouter``); only the root Mount matches every path.
        if not any(r.matches(scope)[0] is Match.FULL for r in app.routes if not isinstance(r, Mount)):
            missing.append(path)
    assert not missing, f"the bridge posts to paths the engine service does not route: {missing}"
