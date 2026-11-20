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


# --- the engine boundary (engine-service plan, Task 1) ----------------------------------------------------------------

BACKEND = CORE.parent
#: The engine: the Temporal Router and Workers, admission, tasks and questions, the conversation record and the agent
#: runtime plumbing. `core/engine` (the contract) is neither side: both import it.
ENGINE = [
    "core/temporal",
    "core/tasks_schema.py",
    *(
        f"core/services/{m}.py"
        for m in (
            "admission task_conversation questions task_asks tasks_access task_board messaging openclaw_work jev "
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

#: Engine modules that still import product code, as `file: function: module`. Each later task of the engine-service
#: plan moves call sites onto `core.engine.hooks` and deletes their entries; never add one.
KNOWN_ENGINE_TO_PRODUCT = [
    "core/services/messaging.py: <module>: core.voice.livekit_huddles",
    "core/services/messaging.py: create_agent_message: core.voice.meetings.meeting_invites",
    "core/services/messaging.py: create_agent_message: core.voice.phone.calls",
    "core/services/runtime_runs.py: _call: core.voice.meetings.meeting_context",
    "routers/messaging.py: <module>: core.voice.voice_calls",
    "routers/questions.py: _remember: core.voice.phone.calls",
    "routers/runtime_internal.py: business_facts: core.voice.phone.business_profile",
    "routers/runtime_internal.py: front_desk: core.voice.phone.business_profile",
    "routers/runtime_internal.py: front_desk: routers.voice_phone",
    "routers/runtime_internal.py: meeting_chat: core.voice.meetings.meeting_chat",
    "routers/runtime_internal.py: meeting_context: core.voice.meetings.meeting_context",
    "routers/runtime_internal.py: meeting_stop_transcribing: core.voice.meetings.meeting_chat",
    "routers/runtime_internal.py: transfer_call: routers.voice_phone",
]
#: Product modules that still import engine code other than the contract (`core.engine`). Only ever shrinks.
KNOWN_PRODUCT_TO_ENGINE = [
    "core/voice/livekit_huddles.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_chat.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_chat.py: <module>: core.repositories.tasks_repo",
    "core/voice/meetings/meeting_chat.py: <module>: core.services.task_conversation",
    "core/voice/meetings/meeting_context.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_context.py: <module>: core.repositories.tasks_repo",
    "core/voice/meetings/meeting_context.py: <module>: core.services.messaging",
    "core/voice/meetings/meeting_followups.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_followups.py: <module>: core.repositories.tasks_repo",
    "core/voice/meetings/meeting_followups.py: <module>: core.tasks_schema",
    "core/voice/meetings/meeting_followups.py: clarification: core.services.questions",
    "core/voice/meetings/meeting_followups.py: enqueue: core.services.admission",
    "core/voice/meetings/meeting_invites.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_invites.py: <module>: core.services.messaging",
    "core/voice/meetings/meeting_invites.py: _organizer_dm: core.services.task_conversation",
    "core/voice/meetings/meeting_notes.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/meeting_notes.py: <module>: core.repositories.tasks_repo",
    "core/voice/meetings/meeting_notes.py: <module>: core.services.admission",
    "core/voice/meetings/meeting_notes.py: <module>: core.services.messaging",
    "core/voice/meetings/meeting_notes.py: <module>: core.services.task_conversation",
    "core/voice/meetings/meeting_notes.py: <module>: core.tasks_schema",
    "core/voice/meetings/records.py: <module>: core.repositories.messaging_repo",
    "core/voice/meetings/records.py: <module>: core.repositories.tasks_repo",
    "core/voice/phone/business_profile.py: _build: core.repositories.messaging_repo",
    "core/voice/phone/business_profile.py: _upcoming: core.repositories.messaging_repo",
    "core/voice/phone/business_profile.py: caller_of: core.repositories.messaging_repo",
    "core/voice/phone/business_profile.py: facts_text: core.repositories.messaging_repo",
    "core/voice/phone/calls.py: <module>: core.repositories.messaging_repo",
    "core/voice/phone/calls.py: <module>: core.services.questions",
    "core/voice/phone/phone_pin.py: <module>: core.repositories.messaging_repo",
    "core/voice/phone/voice_booking.py: <module>: core.repositories.messaging_repo",
    "core/voice/phone/voice_booking.py: <module>: core.repositories.tasks_repo",
    "core/voice/phone/voice_onboarding.py: <module>: core.repositories.messaging_repo",
    "core/voice/phone/voice_outbound.py: <module>: core.repositories.messaging_repo",
    "core/voice/room_lines.py: <module>: core.repositories.messaging_repo",
    "core/voice/room_lines.py: agent_lines: core.repositories.questions_repo",
    "core/voice/rooms.py: <module>: core.repositories.messaging_repo",
    "core/voice/rooms.py: <module>: core.services.container_rpc",
    "core/voice/rooms.py: <module>: core.services.messaging",
    "core/voice/voice_calls.py: <module>: core.repositories.messaging_repo",
    "core/voice/voice_calls.py: <module>: core.services.messaging",
    "core/voice/worker_api.py: <module>: core.services.messaging",
    "routers/huddles.py: <module>: core.repositories.messaging_repo",
    "routers/huddles.py: <module>: core.services.messaging",
    "routers/meet_audio.py: <module>: core.repositories.messaging_repo",
    "routers/meet_bot.py: <module>: core.repositories.messaging_repo",
    "routers/meetings.py: <module>: core.repositories.messaging_repo",
    "routers/meetings.py: <module>: core.services.messaging",
    "routers/voice.py: <module>: core.repositories.messaging_repo",
    "routers/voice.py: <module>: core.services.task_conversation",
    "routers/voice_meet.py: <module>: core.repositories.messaging_repo",
    "routers/voice_meet.py: <module>: core.services.messaging",
    "routers/voice_meet.py: answer_said_question: routers.questions",
    "routers/voice_owner.py: <module>: core.repositories.messaging_repo",
    "routers/voice_owner.py: <module>: core.services.messaging",
    "routers/voice_phone.py: <module>: core.repositories.messaging_repo",
    "routers/voice_phone.py: <module>: core.repositories.tasks_repo",
    "routers/voice_phone.py: <module>: core.services.admission",
    "routers/voice_phone.py: <module>: core.services.messaging",
    "routers/voice_phone.py: <module>: core.services.task_conversation",
    "routers/voice_phone.py: <module>: core.tasks_schema",
    "routers/voice_phone.py: <module>: routers.internal_automations",
    "routers/voice_phone.py: callers_own_words: core.temporal.router_prompt",
]


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
    assert _crossing(PRODUCT, ENGINE) == KNOWN_PRODUCT_TO_ENGINE
