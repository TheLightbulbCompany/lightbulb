"""Per-turn context handoff, bridge -> plugin.

Hermes loads directory plugins as ``hermes_plugins.<slug>``, so a module global set by the
bridge is invisible here. The tool handler DOES receive ``session_id``, so the bridge drops
one JSON file per live turn under the profile home and deletes it afterwards. The directory
is in the profile sync's exclude list: the local-session JWT it carries never leaves the microVM.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DIRNAME = ".lightbulb-turn"


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _path(root, session_id: str) -> Path:
    return Path(root) / DIRNAME / f"{session_id.replace('/', '_')}.json"


def write(root, session_id: str, ctx: dict) -> None:
    target = _path(root, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # atomic: a reader never sees half a file
    tmp.write_text(json.dumps(ctx), encoding="utf-8")
    os.replace(tmp, target)


def read(session_id: str) -> dict | None:
    try:
        return json.loads(_path(_home(), session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear(root, session_id: str) -> None:
    _path(root, session_id).unlink(missing_ok=True)


# --- a task run's "end this turn waiting_approval" marker (Lane Q) ---------------------------------
# ask_person / request_approval on a task run can't change the run's outcome from inside a tool, so they
# leave this file; bridge/runs.py reads it when the turn ends and reports `waiting_approval`.


def _waiting_path(root, run_id: str) -> Path:
    from bridge.effects import waiting_marker_path  # one owner of the path: the bridge takes what this writes

    return waiting_marker_path(root, run_id)


def note_waiting(root, run_id: str, question_id: str) -> None:
    target = _waiting_path(root, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"questionId": question_id}), encoding="utf-8")


def read_waiting(root, run_id: str) -> str | None:
    try:
        return json.loads(_waiting_path(root, run_id).read_text(encoding="utf-8")).get("questionId")
    except (OSError, ValueError):
        return None


def clear_waiting(root, run_id: str) -> None:
    _waiting_path(root, run_id).unlink(missing_ok=True)
