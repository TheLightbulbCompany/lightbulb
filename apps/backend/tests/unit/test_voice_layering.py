"""Phone and Meetings live under ``core/voice``; no core module may lean on a router for them."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "core"
VOICE_ROUTERS = ("routers.voice", "routers.huddles", "routers.meetings")

#: The router imports left in core/voice, all lazy. `voice_booking` and `voice_calls`: the phone's `start_work` and
#: `end_call` routes (a line problem's task, a stale call's end). `phone/tools.py`: the
#: front desk and transfer-call tools moved here from the engine's routes (engine-service Task 3), which made the same
#: lazy `routers.voice_phone` imports; they act through the phone's route functions (`start_call` / `end_call`
#: bookkeeping and the booking helpers) and go when those leave the router. Keyed by file and enclosing function, so a
#: new router import anywhere fails. Remove an entry when its path moves out of the routers.
KNOWN = [
    "voice/phone/tools.py: front_desk: routers.voice_phone",
    "voice/phone/tools.py: transfer_call: routers.voice_phone",
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


# --- the engine boundary (engine-service plan, Task 1) ----------------------------------------------------------------

BACKEND = CORE.parent
#: The engine: the Temporal Router and Workers, admission, tasks and questions, the conversation record and the agent
#: runtime plumbing, and its face to the products, `core/engine` (the contract, the hooks and the operations in
#: `core.engine.api`). That face is engine code (it imports no product) and the one part products may import.
ENGINE = [
    "core/engine",
    "core/temporal",
    "core/tasks_schema.py",
    *(
        f"core/services/{m}.py"
        for m in (
            "admission task_conversation questions task_asks tasks_access messaging openclaw_work jev "
            "runtime_runs runtime_transport container_rpc runtime_rpc"
        ).split()
    ),
    *(
        f"core/repositories/{m}.py"
        for m in "messaging_repo tasks_repo questions_repo effects_repo automations_repo".split()
    ),
    *(f"routers/{m}.py" for m in "runtime_internal internal_automations questions tasks messaging".split()),
]
#: Lightbulb Phone and Lightbulb Meetings.
PRODUCT = [
    "core/voice",
    *(
        f"routers/{m}.py"
        for m in "voice voice_phone voice_meet voice_owner huddles meetings meet_bot meet_audio".split()
    ),
]

#: Engine modules that import product code, as `file: function: module`: none since engine-service Task 3 (every
#: engine call into a product goes through `core.engine.hooks`). Never add one.
KNOWN_ENGINE_TO_PRODUCT: list[str] = []
#: The engine modules products may import: the contract, the operations and the hooks (to register theirs).
PRODUCT_MAY_IMPORT = {"core.engine.api", "core.engine.contract", "core.engine.hooks"}


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = BACKEND / p
        out += sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return out


def _module(path: Path) -> str:
    parts = list(path.relative_to(BACKEND).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _expanded(path: Path, package: str) -> list[tuple[str, list[str]]]:
    """(enclosing function, names) per import statement in `path`, a module of `package`: `from pkg import mod` read
    as `pkg.mod` for every name (a symbol's module still prefixes it), relative imports resolved against `package`,
    and `importlib.import_module("x")` / `__import__("x")` with a literal name as `x`."""
    found: list[tuple[str, list[str]]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            where = node.name
        if isinstance(node, ast.Import):
            found.append((where, [alias.name for alias in node.names]))
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = ".".join([*parts[: len(parts) - node.level + 1], node.module or ""] if node.level else [node.module])
            found.append((where, [f"{base.strip('.')}.{alias.name}" for alias in node.names]))
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("import_module", "__import__") and isinstance(node.args[0].value, str):
                found.append((where, [node.args[0].value]))
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(ast.parse(path.read_text(), filename=str(path)), "<module>")
    return found


def _crossing(sources: list[str], targets: list[str]) -> list[str]:
    """`file: function: module` for every import statement in `sources` of a module in `targets` (the longest module
    that names it). A second statement importing the same module in the same function is a second entry, `#2`."""
    mods = sorted({_module(f) for f in _files(targets)}, key=len, reverse=True)
    out: list[str] = []
    for path in _files(sources):
        mod = _module(path)
        package = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
        seen: dict[str, int] = {}
        for where, names in _expanded(path, package):
            hits = {m for name in names for m in mods[:] if name == m or name.startswith(m + ".")}
            hits = {h for h in hits if not any(o != h and o.startswith(h + ".") for o in hits)}  # the longest only
            for hit in sorted(hits):
                key = f"{path.relative_to(BACKEND)}: {where}: {hit}"
                seen[key] = seen.get(key, 0) + 1
                out.append(key if seen[key] == 1 else f"{key} #{seen[key]}")
    return sorted(out)


def test_the_boundary_scan_expands_from_imports(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        "from core.voice.meetings import meeting_notes\n"
        "def f():\n    from core.voice import rooms\n    from ..voice import rooms\n    __import__('core.voice.rooms')\n"
    )
    assert _expanded(src, "core.services") == [
        ("<module>", ["core.voice.meetings.meeting_notes"]),
        ("f", ["core.voice.rooms"]),
        ("f", ["core.voice.rooms"]),
        ("f", ["core.voice.rooms"]),
    ]


def test_engine_does_not_import_product():
    assert _crossing(ENGINE, PRODUCT) == KNOWN_ENGINE_TO_PRODUCT


def test_product_imports_only_the_contract():
    assert {hit.rsplit(": ", 1)[1].split(" #")[0] for hit in _crossing(PRODUCT, ENGINE)} == PRODUCT_MAY_IMPORT


# --- the one tenantless read --------------------------------------------------------------------------------------

#: The product functions that may call `api.find_conversation` (a conversation by id alone), as `file: function`.
#: Each is an entry point handed nothing but the conversation id, and each authenticates that conversation with a
#: credential the product stored on it before it trusts the tenant the lookup returns.
FIND_CONVERSATION_ENTRY_POINTS = [
    # the Recall audio socket's ticket: signed for its purpose, its nonce the meeting's booked `ticketNonce`
    "routers/meet_audio.py: _authorized",
    # the Recall bot page's ticket: signed, its nonce the meeting's booked `ticketNonce`
    "routers/meet_bot.py: session",
    # the Recall webhooks, signed with the workspace verification secret before the body is read; each then acts
    # only for the bot (and nonce) the meeting stored
    "routers/meetings.py: _chat",
    "routers/meetings.py: _on_status",
    "routers/meetings.py: _speaker",
    # the per-call token `start_call` minted and keeps on the call (`callToken`), sent on every per-call route
    "routers/voice_phone.py: _require_call_token",
]


def test_find_conversation_is_called_only_from_entry_points():
    """Per function: any use of the name (`api.find_conversation`, a bare `find_conversation`) inside a function
    names that function; importing the name itself, or a use outside any function, fails outright."""
    found: set[str] = set()
    for path in _files(PRODUCT):
        tree = ast.parse(path.read_text(), filename=str(path))
        where = str(path.relative_to(BACKEND))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == "find_conversation" for a in node.names):
                found.add(f"{where}: <import>")
        uses = [
            n
            for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr == "find_conversation")
            or (isinstance(n, ast.Name) and n.id == "find_conversation")
        ]
        in_functions = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = {id(n) for n in ast.walk(fn)}
                for n in uses:
                    if id(n) in inner:
                        found.add(f"{where}: {fn.name}")
                        in_functions.add(id(n))
        if any(id(n) not in in_functions for n in uses):
            found.add(f"{where}: <module>")
    assert sorted(found) == FIND_CONVERSATION_ENTRY_POINTS
