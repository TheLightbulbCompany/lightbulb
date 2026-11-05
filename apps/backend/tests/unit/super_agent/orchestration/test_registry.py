"""contracts-v2 §13: workflow and activity names are unique across every registered module."""

from temporalio import activity, workflow

from core.services.watchdog import WATCHDOG_ACTIVITIES
from core.temporal.registry import ALL_ACTIVITIES, ALL_WORKFLOWS
from core.temporal.watchdog_workflow import WATCHDOG_WORKFLOWS


def test_no_duplicate_workflow_or_activity_names():
    """The watchdog queue registers its own lists; names stay unique across both queues."""
    wf_names = [workflow._Definition.must_from_class(w).name for w in [*ALL_WORKFLOWS, *WATCHDOG_WORKFLOWS]]
    act_names = [activity._Definition.must_from_callable(a).name for a in [*ALL_ACTIVITIES, *WATCHDOG_ACTIVITIES]]
    assert wf_names and act_names
    assert len(wf_names) == len(set(wf_names))
    assert len(act_names) == len(set(act_names))
