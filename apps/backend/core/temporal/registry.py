"""Every workflow and activity the worker registers. A new module adds its `WORKFLOWS`/`ACTIVITIES`
here; `test_registry.py` fails on a duplicate name (contracts-v2 §13)."""

from core.services import watchdog
from core.temporal import (
    activities,
    automations,
    health,
    learning,
    rules_activities,
    task_questions,
    voice_usage_notices,
    watches,
    workflows,
)

ALL_WORKFLOWS = [
    *health.WORKFLOWS,
    *workflows.WORKFLOWS,
    *automations.WORKFLOWS,
    *learning.WORKFLOWS,
    *voice_usage_notices.WORKFLOWS,
]
ALL_ACTIVITIES = [
    *health.ACTIVITIES,
    *activities.ACTIVITIES,
    *task_questions.ACTIVITIES,
    *rules_activities.ACTIVITIES,
    *automations.ACTIVITIES,
    *learning.ACTIVITIES,
    *voice_usage_notices.ACTIVITIES,
    watchdog.reconcile_state,  # the watchdog's reconcile start reads task state on the main queue
    *watches.ACTIVITIES,
]
