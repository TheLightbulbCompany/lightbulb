"""lightbulb-post.sh: builds the out-of-turn post request with a deterministic event id.
A fake `curl` on PATH records the JSON body and answers 201, so no network is touched."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills-src/lightbulb/scripts/lightbulb-post.sh"
pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the script needs jq")


def _run(tmp_path, *args, body="Morning brief", receipt="404"):
    log = tmp_path / "curl.json"
    fake = tmp_path / "bin" / "curl"
    fake.parent.mkdir(exist_ok=True)
    # The payload follows -d; the URL is the last arg. Answer like the backend: JSON + code.
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'while [[ $# -gt 0 ]]; do case "$1" in -d) printf "%s" "$2" > "$LOG"; shift 2;; *) url="$1"; shift;; esac; done\n'
        # The receipt pre-check (GET .../receipts/<eventId>) answers with just the status code.
        'if [[ "$url" == */receipts/* ]]; then printf "%s" "$url" > "$LOG.receipt"; printf "%s" "$RECEIPT"; exit 0; fi\n'
        'printf "%s" "$url" > "$LOG.url"\n'
        "printf '{\"roomId\":\"r1\"}\\n201'\n"
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "LOG": str(log),
           "LIGHTBULB_AGENT_ID": "agent-1", "RECEIPT": receipt, "OPENCLAW_HOOK_TOKEN": "t", "ISOL8_API_URL": "https://api.x/",
           "ISOL8_OWNER_ID": "org_1"}
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=body, env=env, capture_output=True, text=True)
    sent = json.loads(log.read_text()) if log.exists() else None
    return proc, sent, (tmp_path / "curl.json.url").read_text() if sent else None


def test_default_posts_to_general_with_a_deterministic_agent_scoped_event_id(tmp_path):
    proc, sent, url = _run(tmp_path)
    assert proc.returncode == 0 and "Posted to room r1" in proc.stdout
    assert url == "https://api.x/api/v1/internal/messaging/agent-posts/org_1"
    assert set(sent) == {"eventId", "agentId", "text"} and sent["agentId"] == "agent-1"
    assert sent["eventId"].startswith("post:agent-1:")
    assert _run(tmp_path)[1]["eventId"] == sent["eventId"]  # a retry replays
    assert _run(tmp_path, "--handle", "ann")[1]["eventId"] != sent["eventId"]  # another target does not


def test_targets_and_event_id_override(tmp_path):
    _, sent, _ = _run(tmp_path, "--room", "r9", "--issue", "iss", "--event-id", "brief-2026-09-24")
    assert (sent["roomId"], sent["issueId"], sent["eventId"]) == ("r9", "iss", "post:agent-1:brief-2026-09-24")


def test_a_hash_room_is_sent_as_a_channel_name(tmp_path):
    _, sent, _ = _run(tmp_path, "--room", "#sales")
    assert sent["roomName"] == "#sales" and "roomId" not in sent


def test_refuses_two_targets_and_an_empty_body(tmp_path):
    assert _run(tmp_path, "--room", "r", "--user", "u")[0].returncode != 0
    assert _run(tmp_path, body="  \n")[0].returncode != 0


def test_an_already_posted_event_id_says_so_and_sends_nothing(tmp_path):
    proc, sent, _ = _run(tmp_path, "--event-id", "brief-2026-09-24", receipt="200")
    assert proc.returncode == 0 and proc.stdout.startswith("Already posted:") and sent is None
    receipt_url = (tmp_path / "curl.json.receipt").read_text()
    assert receipt_url == "https://api.x/api/v1/internal/messaging/agent-messages/org_1/receipts/post%3Aagent-1%3Abrief-2026-09-24"
    proc, sent, _ = _run(tmp_path, "--event-id", "brief-2026-09-24")  # 404: not posted yet
    assert proc.stdout.startswith("Posted to room r1") and sent["eventId"] == "post:agent-1:brief-2026-09-24"


def test_feed_posts_to_the_workspace_feed_with_a_reason(tmp_path):
    proc, sent, _ = _run(tmp_path, "--feed", "--why", "Money in is first in the brief.")
    assert proc.returncode == 0
    assert sent["feed"] is True and sent["postReason"] == "Money in is first in the brief."
    assert not {"roomId", "roomName", "userId", "handle"} & set(sent)
    assert _run(tmp_path)[1]["eventId"] != sent["eventId"]  # the feed is its own target


def test_why_rides_any_post_and_feed_is_one_target(tmp_path):
    _, sent, _ = _run(tmp_path, "--room", "r9", "--why", "A customer asked.")
    assert sent["postReason"] == "A customer asked." and "feed" not in sent
    assert _run(tmp_path, "--feed", "--room", "r9")[0].returncode != 0
    assert _run(tmp_path, "--why")[0].returncode != 0


def test_feed_without_why_is_refused_with_a_usage_message(tmp_path):
    proc, sent, _ = _run(tmp_path, "--feed")
    assert proc.returncode != 0 and sent is None and "--why" in proc.stderr


def test_a_changed_reason_is_a_new_post(tmp_path):
    first = _run(tmp_path, "--feed", "--why", "Money in.")[1]["eventId"]
    assert _run(tmp_path, "--feed", "--why", "Money in.")[1]["eventId"] == first  # a retry replays
    assert _run(tmp_path, "--feed", "--why", "A customer replied.")[1]["eventId"] != first
