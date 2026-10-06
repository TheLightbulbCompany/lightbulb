import pytest

from huddle_agent import huddle as h_mod


@pytest.fixture(autouse=True)
def _meeting_design(request, monkeypatch):
    """The tests written before hand first (a meeting voice that opens on a name) still run against that
    path while both exist; `test_hand_first.py` runs the meeting design that ships."""
    monkeypatch.setattr(h_mod, "HAND_FIRST", request.module.__name__ == "test_hand_first")
