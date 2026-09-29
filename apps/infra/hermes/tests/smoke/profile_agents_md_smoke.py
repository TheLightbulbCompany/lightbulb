"""The profile's AGENTS.md reaches the system message in the pinned image.

Not collected by pytest (no ``test_`` prefix): ``turn.with_profile_agents_md`` reuses Hermes's
private ``agent.prompt_builder._load_agents_md`` and falls back silently when it cannot import it,
so only the real image proves the loader is still there. Run from ``apps/infra/hermes``::

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/profile_agents_md_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python <hermes image> /opt/bridge/smoke.py
"""
import tempfile
from pathlib import Path

from bridge import sync, turn

with tempfile.TemporaryDirectory() as root:
    sync.HOME = Path(root)
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    (profile / "AGENTS.md").write_text("Wait for a clean review before merging.")
    out = turn.with_profile_agents_md("ceo", "room")
    assert out.startswith("room\n\n## AGENTS.md") and "Wait for a clean review" in out, out
print("ok: profile AGENTS.md reaches the system message")
