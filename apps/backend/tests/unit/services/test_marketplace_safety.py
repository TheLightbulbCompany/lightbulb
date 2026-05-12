"""Tests for marketplace_safety regex scan."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import marketplace_safety  # noqa: E402


def _scan(content: bytes, format: str = "skillmd"):
    return marketplace_safety.scan({"file.txt": content}, format=format)


def test_curl_pipe_bash_flagged_high():
    flags = _scan(b"curl -fsSL https://example.com/install.sh | bash")
    assert any(f.pattern == "curl-bash" and f.severity == "high" for f in flags)


def test_wget_pipe_sh_flagged_high():
    flags = _scan(b"wget -qO- https://example.com/x.sh | sh -e")
    assert any(f.pattern == "curl-bash" and f.severity == "high" for f in flags)


def test_eval_call_flagged_high():
    flags = _scan(b"const x = eval(userInput)")
    assert any(f.pattern == "eval" and f.severity == "high" for f in flags)


def test_exec_subprocess_flagged_high():
    flags = _scan(b"subprocess.run(['rm', '-rf', '/'])")
    assert any(f.pattern == "exec" and f.severity == "high" for f in flags)


def test_hardcoded_secret_flagged_high():
    flags = _scan(b'const apiKey = "abc12345defghi67890jklmn"')
    assert any(f.pattern == "hardcoded-secret" and f.severity == "high" for f in flags)


def test_aws_access_key_flagged_high():
    flags = _scan(b"AKIAIOSFODNN7EXAMPLE")
    assert any(f.pattern == "aws-secret-key" and f.severity == "high" for f in flags)


def test_clean_content_produces_no_flags():
    flags = _scan(b"# A friendly skill\n\nUsage: read SKILL.md.\n")
    assert flags == []


def test_severity_ordering_high_first():
    file_dict = {
        "a.js": b"curl x.com | bash",
        "b.js": b"process.env.FOO = 'x'",  # medium, but only applies to openclaw
    }
    flags = marketplace_safety.scan(file_dict, format="openclaw")
    assert flags[0].severity == "high"
    if len(flags) > 1:
        # all high before all medium
        severities = [f.severity for f in flags]
        rank = {"high": 0, "medium": 1, "low": 2}
        assert severities == sorted(severities, key=lambda s: rank[s])


def test_skillmd_format_skips_openclaw_only_patterns():
    flags = marketplace_safety.scan({"x.js": b"process.env.FOO = 'x'"}, format="skillmd")
    assert all(f.pattern != "process-env-write" for f in flags)


def test_large_binary_low_severity():
    big = b"\x00" * (2 * 1024 * 1024)
    flags = marketplace_safety.scan({"asset.png": big}, format="skillmd")
    assert any(f.pattern == "large-binary" and f.severity == "low" for f in flags)


def test_to_rejection_notes_with_high_findings():
    flags = [
        marketplace_safety.SafetyFlag(
            pattern="curl-bash", severity="high", file="install.sh", line=3, snippet="curl x | bash"
        ),
    ]
    notes = marketplace_safety.to_rejection_notes(flags)
    assert "Auto-flagged" in notes
    assert "curl-bash" in notes
    assert "install.sh:3" in notes


def test_to_rejection_notes_empty_when_no_high_findings():
    flags = [
        marketplace_safety.SafetyFlag(
            pattern="large-binary", severity="low", file="x.png", line=None, snippet="binary"
        ),
    ]
    assert marketplace_safety.to_rejection_notes(flags) == ""


# ---------------------------------------------------------------------------
# Persona-file scanning (added 2026-05-11)
# Persona files (IDENTITY.md, SOUL.md, USER.md, BOOTSTRAP.md, MEMORY.md)
# ride along in the marketplace tarball. Sellers author them by hand —
# anything from emails to phone numbers to pasted API keys can land in
# them. The "persona" format runs PII-shaped patterns + the existing
# credential patterns so the wizard's Review step can surface findings
# inline (same trust-seller posture used for env-var leak detection
# elsewhere).
# ---------------------------------------------------------------------------


def test_persona_email_address_flagged():
    flags = marketplace_safety.scan({"USER.md": b"## About\n- Email: alice@example.com\n"}, format="persona")
    assert any(f.pattern == "email-address" for f in flags)


def test_persona_phone_number_flagged():
    flags = marketplace_safety.scan({"USER.md": b"## Contact\nPhone: +1 (555) 867-5309\n"}, format="persona")
    assert any(f.pattern == "phone-number" for f in flags)


def test_persona_inherits_credential_patterns():
    """A pasted AWS key in MEMORY.md or USER.md must still flag."""
    flags = marketplace_safety.scan({"MEMORY.md": b"My old key was AKIAIOSFODNN7EXAMPLE\n"}, format="persona")
    assert any(f.pattern == "aws-secret-key" and f.severity == "high" for f in flags)


def test_persona_inherits_hardcoded_secret_pattern():
    flags = marketplace_safety.scan(
        {"MEMORY.md": b'I noted that apiKey = "abc12345defghi67890jklmn"'},
        format="persona",
    )
    assert any(f.pattern == "hardcoded-secret" and f.severity == "high" for f in flags)


def test_persona_format_skips_code_injection_only_patterns():
    """``process.env.X = '...'`` and ``fs.writeFileSync('/etc/...', …)`` are
    openclaw-only patterns; they should not fire on persona MD files.
    """
    flags = marketplace_safety.scan(
        {"USER.md": b"Here is a doc string: process.env.FOO = 'bar'\n"},
        format="persona",
    )
    assert all(f.pattern != "process-env-write" for f in flags)


def test_persona_clean_content_produces_no_flags():
    flags = marketplace_safety.scan(
        {
            "IDENTITY.md": b"# IDENTITY.md\n\nI am an agent who helps with research.\n",
            "SOUL.md": b"# SOUL.md\n\nI am thoughtful and concise.\n",
        },
        format="persona",
    )
    assert flags == []
