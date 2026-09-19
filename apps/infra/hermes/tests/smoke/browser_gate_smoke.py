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

**Verified 2026-09-19, an amendment to the original design assumption**: the ORIGINAL version of
this smoke also asserted the base image has NO Chromium without the sentinel (i.e. that the
sentinel is what flips tool advertisement on). That assertion is FALSE against the pinned base
image (digest in ``hermes-version.json#base.image``) -- it already bundles a Playwright
``chromium_headless_shell`` build under ``/opt/hermes/.playwright`` (``PLAYWRIGHT_BROWSERS_PATH``
points there), which alone satisfies ``_chromium_installed()``'s third branch
(``any(... _has_chromium_build(root) for root in _chromium_search_roots())``), with no sentinel
and no ``AGENT_BROWSER_PROVIDER`` set. Confirmed live:
``docker run --rm --entrypoint python3 <image> -c "import tools.browser_tool_install as m; print(m._chromium_search_roots())"``
lists ``/opt/hermes/.playwright``, and that directory contains ``chromium_headless_shell-1243``.
So ``check_browser_requirements()``'s final fallback branch (no provider, no CDP override, no
camofox, Browser-Use-CLI off) is ALREADY True in this base image, sentinel or not -- the "no
profile => no gate var => no Chromium => tools not advertised" chain design.md Sec2a describes
does not hold at the image layer alone for a profile-less workspace. Also: ``_chromium_installed``
memoizes its result on the origin module for the PROCESS LIFETIME (`` cached``), so a same-process
before/after comparison would silently read a stale cached value regardless -- not just wrong, but
untestable that way. **What this leaves gated is Task 4's job, not this image's**: whether
``AGENT_BROWSER_PROVIDER=agentcore`` is set is what determines whether an advertised call to
``browser_*`` actually reaches AgentCore versus the bundled local Chromium -- the image can no
longer be relied on to hide the tool for a profile-less owner, only to route a *used* tool
correctly. Flagged for the PR and for whoever picks up Task 4/6.
"""
import os
import sys

sys.path.insert(0, "/opt/hermes")
from tools.browser_tool_install import _chromium_installed  # noqa: E402

sentinel = "/tmp/_gate_probe"
open(sentinel, "w").close()
os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] = sentinel

assert _chromium_installed() is True, "the executable-path gate no longer satisfies _chromium_installed"

print("browser gate OK")
