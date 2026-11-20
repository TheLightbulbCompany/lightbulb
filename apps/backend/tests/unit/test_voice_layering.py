"""Phone and Meetings live under ``core/voice``; no core module may lean on a router for them."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "core"
VOICE_ROUTERS = ("routers.voice", "routers.huddles", "routers.meetings")


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def test_core_voice_does_not_import_routers():
    files = sorted((CORE / "voice").rglob("*.py"))
    assert files, "core/voice is missing"
    bad = [
        f"{path.relative_to(CORE)}: {name}"
        for path in files
        for name in _imports(path)
        if name == "routers" or name.startswith("routers.")
    ]
    assert bad == []


def test_core_services_and_temporal_do_not_import_voice_routers():
    files = sorted((CORE / "services").rglob("*.py")) + sorted((CORE / "temporal").rglob("*.py"))
    bad = sorted(
        {
            f"{path.relative_to(CORE)}: {name}"
            for path in files
            for name in _imports(path)
            if any(name == r or name.startswith(r) for r in VOICE_ROUTERS)
        }
    )
    assert bad == []
