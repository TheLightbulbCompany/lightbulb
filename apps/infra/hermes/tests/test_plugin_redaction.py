"""Task 13: Tests for redaction pattern registration."""
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402


def test_the_pattern_matches_a_real_paperclip_key_shape():
    pattern = re.compile(lightbulb.REDACTION_PATTERNS[0])
    assert pattern.search("Authorization: pcp_9fK2mQ7xZa4bC1dE8gH") is not None
    assert pattern.search("pcp_short") is None


def test_the_pattern_satisfies_the_registrys_three_structural_rules():
    pattern = lightbulb.REDACTION_PATTERNS[0]
    assert re.compile(pattern)                    # compiles
    assert "|" not in pattern.split("[", 1)[0]    # no top-level alternation
    assert len(pattern.split("[", 1)[0]) >= 2     # >= 2 literal prefix chars


def test_registration_is_reported_and_a_failure_never_breaks_plugin_load():
    assert lightbulb._register_redaction(lambda patterns, source: len(patterns)) == 1
    assert lightbulb._register_redaction(lambda patterns, source: (_ for _ in ()).throw(RuntimeError())) == 0
