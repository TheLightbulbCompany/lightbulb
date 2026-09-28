"""Prove the bridge hands AIAgent Hermes's CLI toolsets, not every toolset Hermes knows.

Left unset, AIAgent loads all toolsets: the desktop-GUI tools (desktop_ui/project) and the
opt-in video_analyze reached every agent, and agent.disabled_toolsets was never read."""

import tempfile
from pathlib import Path

import model_tools
import yaml

from bridge import sync, turn

GUI = {"desktop_preview", "drive_preview", "gui_tour", "read_terminal", "desktop_project", "show_tip"}

with tempfile.TemporaryDirectory() as root:
    sync.HOME = Path(root)
    sync._profile_dir("ceo").mkdir(parents=True)
    (sync._profile_dir("ceo") / "config.yaml").write_text(yaml.safe_dump({
        "agent": {"disabled_toolsets": ["cronjob", "tts", "video_gen"]},
        "mcp_servers": {"linear": {"url": "https://mcp.example/sse"}},
    }))
    with turn._hermes_home_scope("ceo"), turn._secret_scope("ceo"):  # as in a real turn
        enabled, disabled = turn.profile_toolsets()
        tools = {t["function"]["name"] for t in model_tools.get_tool_definitions(
            enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True, skip_tool_search_assembly=True)}
        everything = {t["function"]["name"] for t in model_tools.get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True)}

assert disabled == ["cronjob", "tts", "video_gen"], disabled
assert {"browser", "terminal", "file", "memory", "linear"} <= set(enabled), enabled
assert not {"desktop_ui", "project", "video"} & set(enabled), enabled
assert GUI & everything, "the unset default no longer loads the GUI tools; drop this smoke"
assert not GUI & tools and "video_analyze" not in tools, sorted(tools)
assert {"terminal", "read_file", "memory"} <= tools, sorted(tools)
print("ok", len(tools), "tools")
