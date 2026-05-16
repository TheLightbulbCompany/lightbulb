"""Desktop pair / revoke business logic for the two-node architecture.

The desktop ships two openclaw nodes per Mac:
  - Tauri-mac process (clientId="openclaw-macos"): macOS-native commands
  - openclaw CLI subprocess (clientId="node-host"): cross-platform commands

Command sets MUST be disjoint so that openclaw's gateway routing is
unambiguous and a single-process compromise can't pivot to the other's
command surface. Backend derives these sets here — clients do NOT submit
them on /desktop/pair.
"""

DESKTOP_MAC_COMMANDS: tuple[str, ...] = (
    "system.notify",
    "screen.snapshot",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
)

DESKTOP_CLI_COMMANDS: tuple[str, ...] = (
    "system.run",
    "system.run.prepare",
    "system.which",
    "browser.proxy",
    "file.fetch",
    "file.write",
    "dir.list",
    "dir.fetch",
)


def assert_command_sets_disjoint(mac: list[str] | tuple[str, ...], cli: list[str] | tuple[str, ...]) -> None:
    overlap = set(mac) & set(cli)
    assert not overlap, f"Mac and CLI command sets must be disjoint; overlap: {overlap}"


# Module-load defensive assertion: protect against future drift in the constants above.
assert_command_sets_disjoint(DESKTOP_MAC_COMMANDS, DESKTOP_CLI_COMMANDS)
