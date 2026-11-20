"""Phone and Meetings live under ``core/voice``; no core module may lean on a router for them."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "core"
VOICE_ROUTERS = ("routers.voice", "routers.huddles", "routers.meetings")

#: The two router imports left in core/voice, both lazy and both for one reason: `end_call` and `start_work` start a
#: Worker task through `routers.internal_automations.start_voice_work`, which is not voice code. Remove an entry when
#: that path moves out of the routers; never add one.
KNOWN = {
    "voice/voice_calls.py: routers.voice_phone",  # sweep_phone_calls -> voice_phone.end_call
    "voice/phone/voice_booking.py: routers.voice_phone",  # _line_problem -> voice_phone.start_work
}


def _imports(path: Path) -> set[str]:
    """Every module `path` imports (top level or inside a function), `from routers import x` as `routers.x`."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "routers":
                names |= {f"routers.{alias.name}" for alias in node.names}
            else:
                names.add(node.module)
    return names


def _offenders(files: list[Path], bad) -> set[str]:
    return {f"{path.relative_to(CORE)}: {name}" for path in files for name in _imports(path) if bad(name)}


def test_core_voice_does_not_import_routers():
    files = sorted((CORE / "voice").rglob("*.py"))
    assert files, "core/voice is missing"
    found = _offenders(files, lambda name: name == "routers" or name.startswith("routers."))
    assert found == KNOWN


def test_core_services_and_temporal_do_not_import_voice_routers():
    files = sorted((CORE / "services").rglob("*.py")) + sorted((CORE / "temporal").rglob("*.py"))
    assert _offenders(files, lambda name: name.startswith(VOICE_ROUTERS)) == set()
