"""Does the executable-path sentinel actually satisfy Hermes's own browser-advertise gate?

Not collected by pytest (no ``test_`` prefix) because it only means anything inside the pinned
image: the mechanism (``tools.browser_tool_install._chromium_installed``) ships in the base
Hermes image, and a mock of it proves nothing -- a mock would happily assert the sentinel works
even if Hermes's own gate check changed shape underneath it. Run it from ``apps/infra/hermes``::

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/browser_gate_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python "$(jq -r .base.image ../../../hermes-version.json)" /opt/bridge/smoke.py

Network-free, no model call. Pins the Hermes-side contract design.md Sec2a's sentinel depends on:
the gate is a bare ``os.path.isfile`` on ``AGENT_BROWSER_EXECUTABLE_PATH``
(``browser_tool_install.py:227-242``) with no real Chromium check, so a zero-byte sentinel file
satisfies it.

**Split by image (ruling R13, 2026-09-19).** ``hermes-image-smokes`` (#2760) always runs this
smoke against the pinned BASE image (``hermes-version.json#base.image``), never our built
extended one -- and the base image bundles a 269 MB Playwright ``chromium_headless_shell`` under
``PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright`` (its own local-automation fallback), which
alone satisfies ``_chromium_installed()`` with no sentinel and no ``AGENT_BROWSER_PROVIDER`` set
(confirmed live 2026-09-19 -- the base's own ``_chromium_search_roots()`` lists that path and it
contains ``chromium_headless_shell-1243``). So a "no Chromium without the sentinel" assertion can
NEVER pass here -- the base always has one, no matter what our Dockerfile does.

Ruling R13 ("browser means AgentCore Browser only, never a silent local Chromium in the microVM")
answers that by deleting the bundle from OUR image and asserting the negative half itself, at
BUILD TIME, against the image it actually applies to (``apps/infra/hermes/Dockerfile``, two
separate ``python3 -c`` processes -- ``_chromium_installed()`` memoizes per process, so a
same-process before/after would just read a stale cached value from the first call, not a fresh
answer). This smoke, run against the unmodified base, therefore asserts only:

1. the POSITIVE half -- the sentinel still satisfies ``_chromium_installed()`` (in a fresh
   subprocess, so no cache from this process leaks in), and
2. a canary -- the base still bundles Playwright. If upstream ever drops that bundle, the
   Dockerfile's ``rm -rf`` silently becomes a no-op and ruling R13 needs re-checking; this line is
   what catches that instead of nobody noticing.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = ("import sys; sys.path.insert(0,'/opt/hermes');"
         " from tools.browser_tool_install import _chromium_installed; print(_chromium_installed())")
MODE_PROBE = r'''
import os
import sys
from pathlib import Path

sys.path[:0] = ["/opt/bridge", "/opt/hermes"]
base = Path(os.environ["HERMES_HOME"])
profile = base / "profiles" / "probe"
profile.mkdir(parents=True)

from bridge import sync
sync.HOME = base
from hermes_constants import set_hermes_home_override
set_hermes_home_override(profile)

# The standalone probe has no turn-scoped secret scope. Disable the multiplexer guard so this
# checks only Browser Use's raw config selector, exactly as the first tool-catalog read does.
from agent.secret_scope import set_multiplex_active
set_multiplex_active(False)
from tools.browser_use_cli import is_browser_use_cli_mode
import yaml

assert is_browser_use_cli_mode() is True  # absent raw browser key + uvx => original bug
sync.normalize_profile_backends("probe")
config = yaml.safe_load((profile / "config.yaml").read_text())
assert config["browser"]["backend"] == "off"
assert is_browser_use_cli_mode() is False

(profile / "config.yaml").write_text(
    "browser:\n  backend: browser-use\n  cloud_provider: agentcore\n"
)
sync.normalize_profile_backends("probe")
config = yaml.safe_load((profile / "config.yaml").read_text())
assert config["browser"] == {"backend": "off", "cloud_provider": "agentcore", "allow_private_urls": True}
assert is_browser_use_cli_mode() is False
'''


def probe(env: dict) -> str:
    return subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env={**os.environ, **env}
    ).stdout.strip()


sentinel = "/tmp/_gate_probe"
open(sentinel, "w").close()

assert probe({"AGENT_BROWSER_EXECUTABLE_PATH": sentinel}) == "True", (
    "the executable-path gate stopped satisfying _chromium_installed"
)
assert os.path.isdir(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/hermes/.playwright")), (
    "the base no longer bundles Playwright -- the Dockerfile's rm is now a no-op, re-check ruling R13"
)

with tempfile.TemporaryDirectory() as home:
    Path(home, "config.yaml").write_text("browser:\n  backend: off\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", MODE_PROBE], capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": home},
    )
    assert result.returncode == 0, result.stderr

print("PASS: browser gate")
