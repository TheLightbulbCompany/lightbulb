"""Phone and Meetings live under ``core/voice``; no core module may lean on a router for them."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "core"
VOICE_ROUTERS = ("routers.voice", "routers.huddles", "routers.meetings")

#: The two router imports left in core/voice, both lazy and both for one reason: `end_call` and `start_work` start a
#: Worker task through `routers.internal_automations.start_voice_work`, which is not voice code. Keyed by file and
#: enclosing function, so a second router import anywhere fails. Remove an entry when that path moves out of the
#: routers; never add one.
KNOWN = [
    "voice/phone/voice_booking.py: _line_problem: routers.voice_phone",
    "voice/voice_calls.py: sweep_phone_calls: routers.voice_phone",
]


def _imports(path: Path) -> list[tuple[str, str]]:
    """(enclosing function, module) for every module `path` imports, top level ("<module>") or inside a function,
    `from routers import x` as `routers.x`, and `importlib.import_module("x")` with a literal name as `x`."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            where = node.name
        if isinstance(node, ast.Import):
            found.extend((where, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "routers":
                found.extend((where, f"routers.{alias.name}") for alias in node.names)
            else:
                found.append((where, node.module))
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "import_module" and isinstance(node.args[0].value, str):
                found.append((where, node.args[0].value))
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(ast.parse(path.read_text(), filename=str(path)), "<module>")
    return found


def _offenders(files: list[Path], bad) -> list[str]:
    return sorted(
        f"{path.relative_to(CORE)}: {where}: {name}" for path in files for where, name in _imports(path) if bad(name)
    )


def test_the_scan_sees_lazy_and_importlib_router_imports(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        "import importlib\n"
        "def a():\n    from routers import voice_phone\n"
        "def b():\n    importlib.import_module('routers.voice_meet')\n"
    )
    assert [n for n in _imports(src) if n[1].startswith("routers")] == [
        ("a", "routers.voice_phone"),
        ("b", "routers.voice_meet"),
    ]


def test_core_voice_does_not_import_routers():
    files = sorted((CORE / "voice").rglob("*.py"))
    assert files, "core/voice is missing"
    found = _offenders(files, lambda name: name == "routers" or name.startswith("routers."))
    assert found == KNOWN


def test_core_services_and_temporal_do_not_import_voice_routers():
    files = sorted((CORE / "services").rglob("*.py")) + sorted((CORE / "temporal").rglob("*.py"))
    assert _offenders(files, lambda name: name.startswith(VOICE_ROUTERS)) == []
