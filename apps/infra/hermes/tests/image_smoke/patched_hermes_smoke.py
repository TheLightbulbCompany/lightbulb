"""Build-time smoke for the Lightbulb patch series (Dockerfile, right after the series applies).

Not under tests/smoke/: infra-ci runs those against the UNPATCHED upstream base image. This one
runs inside the image build after hermes-patches/ is applied, and proves every patch is present
and inert by default (no checkpoint callback, no injected input, no refresh delegate)."""

import sys
from types import SimpleNamespace

sys.path.insert(0, "/opt/hermes")

from run_agent import AIAgent  # noqa: E402,F401  -- the patched tree still imports
from agent import lb_checkpoint, lb_inject  # noqa: E402
from agent.prompt_builder import STEER_MARKER_OPEN  # noqa: E402
from hermes_cli import auth_codex  # noqa: E402

# F1/F2: nothing installed -> proceed, commit is a no-op ack
bare = SimpleNamespace()
ref = lb_checkpoint.ToolRef("t", "x", {})
assert lb_checkpoint.before_tool(bare, ref).kind == "proceed"
assert lb_checkpoint.tool_committed(bare, ref, "r", None) is True
import agent.tool_executor as tool_executor  # noqa: E402
assert tool_executor.lb_checkpoint is lb_checkpoint, "F2 call sites missing"

# F3: a data row, never the steer marker, closed frame; inject refused with no open turn
row = lb_inject.input_row("job_completed", "hi")
assert row["display_kind"] == "lightbulb_input", row
assert STEER_MARKER_OPEN not in row["content"] and row["content"].endswith("[/Background input]"), row
assert lb_inject.inject_input(SimpleNamespace(), "mail", "x") is False

# F6: delegate unset -> upstream refresh path
assert auth_codex._REFRESH_DELEGATE is None and callable(auth_codex.set_refresh_delegate)

print("patched hermes ok")
