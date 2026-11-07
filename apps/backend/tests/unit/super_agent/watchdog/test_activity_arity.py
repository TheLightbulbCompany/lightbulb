"""Every `workflow.execute_activity` call passes exactly as many args as the registered Activity declares.
temporalio decodes args with the Activity's type hints only when the counts match
(`temporalio/worker/_activity.py`): one short and dataclass args arrive as plain dicts. That was the prod
watchdog_act `'dict' object has no attribute 'action'` on every tick (7 Nov 2026). The workflow tests use fake
Activities, so only this check sees the real signatures."""

import ast
import inspect
from pathlib import Path

from temporalio import activity

from core.services.watchdog import WATCHDOG_ACTIVITIES
from core.temporal.registry import ALL_ACTIVITIES

TEMPORAL_DIR = Path(__file__).resolve().parents[4] / "core" / "temporal"


def _calls():
    """(file:line, activity name, args passed) for each call whose name and args are literal."""
    for path in sorted(TEMPORAL_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if getattr(node.func, "id", None) == "_act" and not any(isinstance(a, ast.Starred) for a in node.args):
                if isinstance(node.args[0], ast.Constant):  # the `_act(name, *args)` wrappers
                    yield f"{path.name}:{node.lineno}", node.args[0].value, len(node.args) - 1
                continue
            if getattr(node.func, "attr", None) != "execute_activity":
                continue
            target = node.args[0]
            if isinstance(target, ast.Constant):
                name = target.value
            elif isinstance(target, (ast.Name, ast.Attribute)):
                name = target.id if isinstance(target, ast.Name) else target.attr
            else:
                continue
            args = next((k.value for k in node.keywords if k.arg == "args"), None)
            if args is None:
                n = len(node.args) - 1
            elif isinstance(args, (ast.List, ast.Tuple)):
                n = len(args.elts)
            else:
                continue  # computed args: not checkable here
            yield f"{path.name}:{node.lineno}", name, n


def test_every_activity_call_matches_the_real_signature():
    by_name = {}
    for fn in [*ALL_ACTIVITIES, *WATCHDOG_ACTIVITIES]:
        defn = activity._Definition.from_callable(fn)
        by_name[defn.name] = by_name[fn.__name__] = len(inspect.signature(fn).parameters)
    checked, bad = 0, []
    for where, name, n in _calls():
        if name not in by_name:
            continue
        checked += 1
        if by_name[name] != n:
            bad.append(f"{where} {name}: passes {n}, declares {by_name[name]}")
    assert checked > 20
    assert bad == []
