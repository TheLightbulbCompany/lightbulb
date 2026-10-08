"""The baked managed overlay (/etc/hermes/config.yaml) wins over every profile's own config."""
import pathlib

import yaml

OVERLAY = pathlib.Path(__file__).resolve().parents[1] / "managed" / "config.yaml"


def test_the_file_mutation_verifier_footer_never_reaches_a_room():
    """Upstream turn_finalizer appends "⚠ File-mutation verifier: ... Write denied ...
    HERMES_WRITE_SAFE_ROOT" to the final reply when a write_file/patch failed. A room reply
    is customer-facing (leaked on prod 2026-09-29); the agent still sees the tool error."""
    assert yaml.safe_load(OVERLAY.read_text())["display"]["file_mutation_verifier"] is False


def test_recorded_sessions_get_no_auto_title_and_no_liveness_watchdog():
    """With a state.db Hermes titles every new session with an extra model call, and its turn
    watchdog hard-cancels a turn quiet for 600 s once it holds a turn lease."""
    overlay = yaml.safe_load(OVERLAY.read_text())
    assert overlay["auxiliary"]["title_generation"]["enabled"] is False
    assert overlay["agent"]["turn_liveness"]["timeout_s"] == 0


def test_a_room_transcript_compacts_under_the_gates_paid_request_bound():
    """Hermes's default (75% of a 200k window) is past the gate's 400 KB paid-mode 413."""
    compression = yaml.safe_load(OVERLAY.read_text())["compression"]
    assert compression["threshold_tokens"] == 60000
    # The no-LLM tool-result prune fires first, under the bridge's off-path compaction share.
    from bridge import turn
    assert compression["proactive_prune_tokens"] < turn.OFF_PATH_COMPACT_RATIO * compression["threshold_tokens"]


def test_compaction_summaries_think_lighter_on_whatever_model_the_profile_pins():
    """Effort only: the overlay names no provider or model, so a managed owner's summary stays on
    the gate's model, a ChatGPT owner's on openai-codex and a BYO owner's on their own key."""
    assert yaml.safe_load(OVERLAY.read_text())["auxiliary"]["compression"] == {"reasoning_effort": "low"}
