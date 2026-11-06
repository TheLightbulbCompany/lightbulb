"""The lightbulb skill routes document writing to references/documents.md, and the built tree ships it."""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
BUILD = REPO / "apps/infra/hermes/build-skills.sh"


def test_the_skill_routes_document_writing_to_the_documents_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/documents.md`" in line)
    assert row.startswith("|") and len(row) <= 200
    doc = (out / "lightbulb/references/documents.md").read_text()
    for phrase in ("Open with the answer", "Observed", "table", "who does what by when", "request_approval"):
        assert phrase in doc, phrase
    cvd = (out / "lightbulb/references/chat-vs-document.md").read_text()
    assert "references/documents.md" in cvd
    # Walk 18/19: a document is markdown unless the person asks for Word or PDF, and then once (never both).
    assert "Markdown (`.md`) unless the person asks for another format" in doc and "never the same piece twice" in doc
    assert "`.md` unless the person asked for another" in cvd and "`.docx`/`.pdf`)" not in cvd
    assert "documents.md" in (REPO / "apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md").read_text()
