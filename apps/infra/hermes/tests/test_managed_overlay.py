"""The baked managed overlay (/etc/hermes/config.yaml) wins over every profile's own config."""
import pathlib

import yaml

OVERLAY = pathlib.Path(__file__).resolve().parents[1] / "managed" / "config.yaml"


def test_the_file_mutation_verifier_footer_never_reaches_a_room():
    """Upstream turn_finalizer appends "⚠ File-mutation verifier: ... Write denied ...
    HERMES_WRITE_SAFE_ROOT" to the final reply when a write_file/patch failed. A room reply
    is customer-facing (leaked on prod 2026-09-29); the agent still sees the tool error."""
    assert yaml.safe_load(OVERLAY.read_text())["display"]["file_mutation_verifier"] is False
