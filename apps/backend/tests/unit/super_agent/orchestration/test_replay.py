"""Every recorded history replays against today's workflow code (README: `workflow.patched` rule)."""

from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from core.temporal.registry import ALL_WORKFLOWS as WORKFLOWS

HISTORIES = Path(__file__).parent / "histories"


async def test_replay_recorded_histories():
    """Recorded by `scripts/temporal_record_histories.py` (O13): the TaskWorkflow happy path and a Router batch."""
    files = sorted(HISTORIES.glob("*.json"))
    assert [f.stem for f in files] == ["happy_path", "router_batch"]
    replayer = Replayer(workflows=WORKFLOWS)
    for f in files:
        await replayer.replay_workflow(WorkflowHistory.from_json(f.stem, f.read_text()))
