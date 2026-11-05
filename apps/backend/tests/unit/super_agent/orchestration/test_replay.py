"""Every recorded history replays against today's workflow code (README: `workflow.patched` rule)."""

from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from core.temporal.registry import ALL_WORKFLOWS as WORKFLOWS

HISTORIES = Path(__file__).parent / "histories"


async def test_replay_recorded_histories():
    files = sorted(HISTORIES.glob("*.json"))
    replayer = Replayer(workflows=WORKFLOWS)
    for f in files:
        await replayer.replay_workflow(WorkflowHistory.from_json(f.stem, f.read_text()))
