"""AST guards (spec 11 §3.7): the watchdog never creates tasks or rooms, never routes, never stops work, and
starts a TaskWorkflow only to reconcile."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4]
MODULES = [
    "core/services/watchdog.py",
    "core/repositories/watchdog_repo.py",
    "core/temporal/watchdog_workflow.py",
    "scripts/watchdog_ctl.py",
]
FORBIDDEN = {"create_task", "put_task", "create_room", "post_room", "signal_router", "start_task", "update_task"}
STOPS = {"stop", "stop_run", "stop_attempt", "stop_task_session", "mark_stop_requested", "terminate", "cancel"}


def _calls():
    for rel in MODULES:
        path = BACKEND / rel
        if not path.exists():  # later lane-W tasks add these
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                yield rel, name, node


def test_never_creates_tasks_ast_guard():
    bad = [(rel, name) for rel, name, _ in _calls() if name in FORBIDDEN]
    assert bad == []
    starts = [node for _, name, node in _calls() if name == "start_task_workflow"]
    assert starts, "the reconcile start is expected in watchdog.py"
    for node in starts:
        reason = node.args[2] if len(node.args) > 2 else next(k.value for k in node.keywords if k.arg == "reason")
        assert isinstance(reason, ast.Constant) and reason.value == "reconcile"


def test_never_calls_stop_ast_guard():
    assert [(rel, name) for rel, name, _ in _calls() if name in STOPS] == []
