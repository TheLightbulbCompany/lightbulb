"""Every workflow and activity the worker registers. A new module adds its `WORKFLOWS`/`ACTIVITIES`
here; `test_registry.py` fails on a duplicate name (contracts-v2 §13)."""

from core.temporal import health, task_questions, workflows

ALL_WORKFLOWS = [*health.WORKFLOWS, *workflows.WORKFLOWS]
ALL_ACTIVITIES = [*health.ACTIVITIES, *task_questions.ACTIVITIES]
