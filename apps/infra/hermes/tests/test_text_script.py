"""lightbulb-text.sh: the one text reply door. A fake `curl` on PATH records URL and body."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills-src/lightbulb/scripts/lightbulb-text.sh"
pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the script needs jq")


def _run(tmp_path, *args, body="Tuesday works", answer='{"status":"sent"}', code=200):
    log = tmp_path / "curl.json"
    fake = tmp_path / "bin" / "curl"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'while [[ $# -gt 0 ]]; do case "$1" in -d) printf "%s" "$2" > "$LOG"; shift 2;; '
        '-w|-X|-H) shift 2;; *) url="$1"; shift;; esac; done\n'
        'printf "%s" "$url" > "$LOG.url"\n'
        "printf '%s\\n%s' \"$ANSWER\" \"$CODE\"\n"
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "LOG": str(log), "ANSWER": answer, "CODE": str(code),
           "PAPERCLIP_AGENT_ID": "agent-1", "OPENCLAW_HOOK_TOKEN": "t",
           "ISOL8_API_URL": "https://api.x/", "ISOL8_OWNER_ID": "org_1"}
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=body, env=env, capture_output=True, text=True)
    sent = json.loads(log.read_text()) if log.exists() else None
    url = (tmp_path / "curl.json.url").read_text() if sent else None
    return proc, sent, url


def test_send_reply_posts_text_on_stdin(tmp_path):
    proc, sent, url = _run(tmp_path, "send", "--reply", "rk1")
    assert proc.returncode == 0 and proc.stdout.strip() == "Text sent."
    assert url == "https://api.x/api/v1/agent-text/u/org_1/send"
    assert sent == {"agent_id": "agent-1", "reply_key": "rk1", "text": "Tuesday works"}


def test_not_sent_statuses_exit_1(tmp_path):
    for status, msg in [("bad_reply_key", "Not sent: that Reply-Key is not a text you received."),
                        ("no_line", "Not sent: this workspace has no text number."),
                        ("disabled", "Not sent: texting is not configured."),
                        ("human_thread", "Not sent: a person has taken this thread over."),
                        ("send_failed", "Not sent: the carrier declined it; try again later."),
                        ("weird", "Not sent (status: weird).")]:
        proc, _, _ = _run(tmp_path, "send", "--reply", "rk1", answer=json.dumps({"status": status}))
        assert proc.returncode == 1 and msg in proc.stderr


def test_set_business_phone_posts_phone(tmp_path):
    proc, sent, url = _run(tmp_path, "set-business-phone", "+15551234567", answer='{"status":"ok"}')
    assert proc.returncode == 0
    assert url == "https://api.x/api/v1/agent-text/u/org_1/business-phone"
    assert sent == {"phone": "+15551234567"}


def test_bad_phone_is_explained(tmp_path):
    proc, _, _ = _run(tmp_path, "set-business-phone", "555", answer='{"status":"bad_phone"}', code=400)
    assert proc.returncode == 1 and "Not saved: give the number as +1XXXXXXXXXX." in proc.stderr


def test_an_http_error_without_a_status_shows_the_answer(tmp_path):
    proc, _, _ = _run(tmp_path, "set-business-phone", "+15551234567", answer='{"detail":"no_line"}', code=404)
    assert proc.returncode == 1 and 'HTTP 404: {"detail":"no_line"}' in proc.stderr


def test_send_without_reply_exits_2(tmp_path):
    proc, sent, _ = _run(tmp_path, "send")
    assert proc.returncode == 2 and sent is None
