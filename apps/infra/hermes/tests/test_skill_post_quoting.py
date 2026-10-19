"""A post or mail body in double quotes loses every `$`: "$10-15" reaches the room as "0-15"."""

from pathlib import Path

SOURCES = [Path(__file__).resolve().parents[1] / "skills-src",
           Path(__file__).resolve().parents[3] / "backend/core/services"]


def test_no_example_pipes_a_double_quoted_body():
    offenders = [f"{p}:{n}" for root in SOURCES for p in root.rglob("*") if p.suffix in {".md", ".py"}
                 for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1)
                 if "printf '%s' \"" in line or "printf \\'%s\\' \"" in line]
    assert offenders == []
