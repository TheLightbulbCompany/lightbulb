import hashlib
from pathlib import Path

import pytest

import prompts

GOOD = b"def answer_prompt(): ...\n"


def test_verify_prompts_rejects_a_changed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "SOURCE_SHA256", hashlib.sha256(GOOD).hexdigest())
    assert prompts.ensure_exabase_prompts(tmp_path, fetch=lambda: GOOD).read_bytes() == GOOD
    tampered = bytearray(GOOD)
    tampered[0] ^= 1
    (tmp_path / prompts.CACHED_NAME).write_bytes(bytes(tampered))
    with pytest.raises(prompts.PromptsHashMismatch):
        prompts.ensure_exabase_prompts(tmp_path, fetch=lambda: GOOD)
    with pytest.raises(prompts.PromptsHashMismatch):
        prompts.ensure_exabase_prompts(tmp_path / "fresh", fetch=lambda: bytes(tampered))


def test_fork_pins_a_commit():
    import re
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert re.search(r"pastdotdev/benchmarks.*\b[0-9a-f]{40}\b", readme, re.S)
