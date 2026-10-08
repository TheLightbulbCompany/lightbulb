"""Prove the Hermes seams a spoken turn's no-compaction path leans on exist in the pinned image.

The unit suite replaces Hermes, so a Hermes bump that renamed any of these would pass it while a
spoken turn went back to compacting inline (67-103 s, prod 10-07), lost its model-call deadline, or
the off-path compaction stopped committing."""

import inspect

import run_agent
from agent import agent_init, model_metadata, turn_context_compaction
from agent.conversation_compression import finalize_context_engine_compression_notification
from agent.conversation_compression_manual import CompressRequest, compress_now

init = inspect.signature(run_agent.AIAgent.__init__).parameters
# CALL_RUN_LIMITS and _construct_compaction_agent pass these by name.
assert {"skip_background_review", "skip_memory", "reasoning_config", "max_iterations"} <= set(init), sorted(init)
# _run_call flips the per-agent switch Hermes sets at init and every automatic trigger reads.
assert "agent.compression_enabled = cs.enabled" in inspect.getsource(agent_init)
assert "if not agent.compression_enabled" in inspect.getsource(turn_context_compaction._preflight_compression)
# _CallDeadline: re-issue the model request, then the hard stop (which a compaction honours too).
assert callable(getattr(run_agent.AIAgent, "redirect", None))
assert "hard_cancel" in inspect.signature(run_agent.AIAgent.interrupt).parameters
# compact_session: Hermes's manual /compress core and its notice contract.
assert {"system_message", "skip_without_window"} <= set(inspect.signature(compress_now).parameters)
assert "committed" in inspect.signature(finalize_context_engine_compression_notification).parameters
assert CompressRequest().partial is False and CompressRequest().preview is False
# _bounded_history's estimator.
assert model_metadata.estimate_messages_tokens_rough([{"role": "user", "content": "x" * 400}]) > 0
print("ok")
