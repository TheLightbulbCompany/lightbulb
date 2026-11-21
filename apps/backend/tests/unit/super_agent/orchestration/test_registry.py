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


#: The engine's Temporal surface, captured literally. The engine-service plan moves code around these workflows and
#: activities but never renames one, changes an argument or a `workflow.patched` id, or moves the task queue: a live
#: workflow replays against exactly this. Change it only in a PR that is about the Temporal surface itself.
TEMPORAL_SURFACE = {
    "workflows": [
        "GoogleWatchKeeperWorkflow",
        "HealthWorkflow",
        "LearningWorkflow",
        "RouterWorkflow",
        "ScheduleReconcileWorkflow",
        "TaskWorkflow",
        "TriggerFireWorkflow",
        "VoiceUsageNoticeWorkflow",
    ],
    "queue": "lightbulb",
    "patches": [
        "A-automation-run-v1",
        "O-cancel-questions-on-end-v1",
        "O-reconcile-cancel-questions-v1",
        "O-reopen-to-idle-v1",
        "c-router-waits-for-runtime-v1",
        "o-router-room-batch-v1",
    ],
    "activities": {
        "acquire_slot": "(ws: 'str', task_id: 'str') -> 'bool'",
        "apply_effect": "(ws: 'str', task_id: 'str', idem_key: 'str', answered_by: 'str') -> 'LandResult'",
        "apply_fallback": "(ws: 'str', trigger_ids: 'list[str]') -> 'None'",
        "begin_attempt": "(ws: 'str', task_id: 'str', input_trigger_ids: 'list[str]', started: 'bool' = False)"
        " -> 'AttemptRef'",
        "calendar_invites": "(ws: 'str', public_id: 'str') -> 'list[dict]'",
        "cancel_open_questions": "(ws: 'str', task_id: 'str', by: 'str', reason: 'str') -> 'list[str]'",
        "check_budget": "(ws: 'str', task_id: 'str') -> 'bool'",
        "check_voice_usage_notice": "(item: 'VoiceUsageNoticeInput') -> 'str'",
        "check_watch": "(ws: 'str', task_id: 'str', spec: 'WatchSpec') -> 'str'",
        "close_automation_run": "(ws: 'str', task_id: 'str', failed: 'bool') -> 'None'",
        "close_task_attempt": "(ws: 'str', task_id: 'str', specialist_id: 'str', attempt: 'int',"
        " run_id: 'str | None' = None) -> 'None'",
        "curate_specialist": "(ws: 'str', specialist_id: 'str') -> 'dict'",
        "deliver_input": "(ws: 'str', task_id: 'str', n: 'int', trigger_id: 'str') -> 'bool'",
        "end_attempt": "(ws: 'str', task_id: 'str', n: 'int', outcome: 'str', error: 'str | None') -> 'None'",
        "fire_automation": "(inp: 'FireInput', at_ms: 'int') -> 'str'",
        "mark_stop_requested": "(ws: 'str', task_id: 'str', by: 'str') -> 'None'",
        "mark_watch_broken": "(ws: 'str', public_id: 'str') -> 'None'",
        "merge_learning": "(ws: 'str', specialist_id: 'str', task_id: 'str', attempt: 'int') -> 'dict'",
        "note_answerers_unavailable": "(ws: 'str', task_id: 'str') -> 'None'",
        "open_asked_question": "(ws: 'str', task_id: 'str', ref: 'AskRef') -> 'QuestionRef'",
        "open_questions": "(ws: 'str', task_id: 'str') -> 'list[QuestionRef]'",
        "ping": "() -> str",
        "reconcile_attempt": "(ws: 'str', task_id: 'str', n: 'int') -> 'str'",
        "reconcile_schedules": "() -> 'dict'",
        "reconcile_state": "(ws: 'str', task_id: 'str') -> 'str'",
        "record_answer": "(ws: 'str', task_id: 'str', ref: 'AnswerRef') -> 'AnswerOut'",
        "record_match": "(ws: 'str', sched_id: 'str', version: 'int', thread_key: 'str', ref: 'dict[str, Any]',"
        " p: 'float | None', would_do: 'str', source: \"Literal['chat', 'webhook']\")"
        " -> \"Literal['observed', 'duplicate']\"",
        "relay_question": "(ws: 'str', task_id: 'str', qid: 'str') -> 'None'",
        "release_slot": "(ws: 'str', task_id: 'str') -> 'None'",
        "remind_question": "(ws: 'str', task_id: 'str', qid: 'str') -> 'None'",
        "renew_google_watch": "(ws: 'str', public_id: 'str') -> 'int | None'",
        "router_turn": "(inp: 'RouterTurnIn') -> 'RouterTurnOut'",
        "set_watch_until": "(ws: 'str', task_id: 'str', until: 'str | None') -> 'None'",
        "stop_attempt": "(ws: 'str', task_id: 'str', n: 'int') -> 'None'",
        "transition": "(ws: 'str', task_id: 'str', to: 'str', reason: 'str | None') -> 'None'",
        "watch_gave_up": "(ws: 'str', task_id: 'str', spec: 'WatchSpec') -> 'AskRef'",
        "watch_met": "(ws: 'str', task_id: 'str', spec: 'WatchSpec', n: 'int', result: 'str') -> 'str'",
        "worker_turn": "(inp: 'WorkerTurnIn') -> 'WorkerTurnOut'",
    },
}


def test_engine_temporal_surface_unchanged():
    import inspect

    from core.config import settings
    from core.temporal import automations, workflows

    surface = {
        "workflows": sorted(workflow._Definition.must_from_class(w).name for w in ALL_WORKFLOWS),
        "queue": settings.TEMPORAL_TASK_QUEUE,
        "patches": sorted({getattr(m, n) for m in (workflows, automations) for n in dir(m) if n.startswith("PATCH_")}),
        "activities": {
            activity._Definition.must_from_callable(a).name: str(inspect.signature(a))
            for a in sorted(ALL_ACTIVITIES, key=lambda a: activity._Definition.must_from_callable(a).name)
        },
    }
    assert surface == TEMPORAL_SURFACE
