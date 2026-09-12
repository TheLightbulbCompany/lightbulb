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


def test_bare_provider_token_flagged_high_in_tarball_scan():
    """Bare provider-prefixed tokens (no `key = "..."` assignment around them)
    must flag in the tarball content scan — pasting a Slack or OpenAI token
    into prose was invisible to hardcoded-secret's assignment shape."""
    for token in (
        b"here is my key sk-ant-api03-AbCdEf1234567890abcdef",
        b"export it: ghp_ABCDEFghijkl0123456789",
        b"bot token xoxb-1234567890-abcdefghij",
    ):
        flags = _scan(token)
        assert any(f.pattern == "provider-token" and f.severity == "high" for f in flags), token


def test_prose_containing_sk_dash_word_not_flagged():
    """`task-...` and short `sk-` fragments must not trip the token pattern."""
    flags = _scan(b"the task-management sk-etchpad is fine")
    assert not any(f.pattern == "provider-token" for f in flags)


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


# ---------------------------------------------------------------------------
# assert_slice_scrubbed — post-scrub credential-leak check on the PUBLISHED
# openclaw-slice.json (a separate S3 artifact the tarball scan never walks).
# ---------------------------------------------------------------------------


def test_slice_scrubbed_clean_slice_returns_no_flags():
    """A properly-scrubbed slice (env {API_KEY: ""}, apiKey "", headers {})
    has no surviving secrets → no flags."""
    slice_ = {
        "mcp": {"servers": {"resy": {"command": "node", "env": {"API_KEY": "", "RESY_PASSWORD": ""}}}},
        "skills": {"entries": {"jira": {"apiKey": "", "headers": {}}}, "allowBundled": ["search"]},
    }
    assert marketplace_safety.assert_slice_scrubbed(slice_) == []
    assert (
        marketplace_safety.has_blocking_credential_findings(marketplace_safety.assert_slice_scrubbed(slice_)) is False
    )


def test_slice_scrubbed_surviving_env_secret_flagged():
    """env.API_KEY still carrying a value → blocking flag."""
    slice_ = {"mcp": {"servers": {"resy": {"env": {"API_KEY": "sk_live_xxx"}}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "unscrubbed-credential" and f.severity == "high" for f in flags)
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_surviving_apikey_scalar_flagged():
    """A scalar apiKey field that wasn't blanked → blocking flag."""
    slice_ = {"skills": {"entries": {"jira": {"apiKey": "secret"}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "unscrubbed-credential" and f.severity == "high" for f in flags)
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_plugin_entry_secret_flagged():
    """plugins.entries carry the same apiKey/env-shaped config as MCP servers;
    the blocking gate must walk them too — a plugin credential used to sail
    through the slice gate entirely."""
    slice_ = {"plugins": {"entries": {"custom-notifier": {"env": {"API_KEY": "sk_live_xxx"}}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(
        f.pattern == "unscrubbed-credential" and "plugins.entries.custom-notifier" in f.snippet for f in flags
    ), flags
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_clean_plugin_entries_pass():
    slice_ = {"plugins": {"entries": {"memory": {"enabled": True}, "custom": {"apiKey": ""}}}}
    assert marketplace_safety.assert_slice_scrubbed(slice_) == []


def test_slice_scrubbed_nested_secret_caught():
    """The shallow one-level scrub only blanks the TOP level of env/headers.
    A secret nested DEEPER than one level (a dict value, or a list element)
    survives the scrub — the deep walk must still catch it.
    """
    # env value is a dict-of-dict: top-level key present (so scrub touched it)
    # but the secret sits one level deeper inside.
    slice_ = {
        "mcp": {
            "servers": {
                "x": {
                    "env": {"NESTED": {"inner": "super-secret-token"}},
                }
            }
        }
    }
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "unscrubbed-credential" and f.severity == "high" for f in flags), flags
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_nested_list_secret_caught():
    """A secret-shaped field whose value is a list with a non-empty string
    element survives a scrub that only empties list-typed fields to [] when
    the field is dict/list-typed at the top — here headers carries a list of
    strings one level down.
    """
    slice_ = {"skills": {"entries": {"jira": {"headers": {"Authorization": ["Bearer real-token"]}}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "unscrubbed-credential" for f in flags), flags
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_secret_key_nested_under_non_secret_object_caught():
    """A secret-NAMED key (apiKey/headers) nested under a NON-secret object
    (config/options) survives the shallow scrub AND a top-level-only field
    check — the recursive walk must find the secret-named key at any depth.
    """
    slice_ = {
        "mcp": {"servers": {"foo": {"config": {"apiKey": "sk_live_buried"}}}},
        "skills": {"entries": {"bar": {"options": {"headers": {"Authorization": "Bearer real"}}}}},
    }
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    snippets = [f.snippet for f in flags if f.pattern == "unscrubbed-credential"]
    assert any("config.apiKey" in s for s in snippets), flags
    assert any("options.headers" in s for s in snippets), flags
    assert marketplace_safety.has_blocking_credential_findings(flags) is True
    # Redaction holds even for the deeply-nested finds.
    blob = " ".join(f.snippet for f in flags)
    assert "sk_live_buried" not in blob and "Bearer real" not in blob


def test_slice_scrubbed_clean_nested_non_secret_object_passes():
    """A nested non-secret object with no surviving secret-named values is not
    flagged — guards the deeper walk against false positives (region is config,
    apiKey is properly blanked)."""
    slice_ = {"mcp": {"servers": {"foo": {"config": {"region": "us-east-1", "apiKey": ""}}}}}
    assert marketplace_safety.assert_slice_scrubbed(slice_) == []


def test_slice_scrubbed_oauth_client_secret_literal_flagged():
    """An OAuth client_secret literal buried anywhere in the slice → blocking
    flag. The scrub keys on apiKey/env/headers/webhookToken, so a config that
    stores client_secret under its own key rides straight through.
    """
    slice_ = {"mcp": {"servers": {"oauthy": {"oauth": {"client_id": "abc", "client_secret": "GOCSPX-xxx"}}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "oauth-client-secret" and f.severity == "high" for f in flags), flags
    assert marketplace_safety.has_blocking_credential_findings(flags) is True


def test_slice_scrubbed_camelcase_client_secret_flagged():
    """clientSecret (camelCase) is detected case-insensitively too."""
    slice_ = {"skills": {"entries": {"s": {"config": {"clientSecret": "shh"}}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "oauth-client-secret" for f in flags), flags


def test_slice_scrubbed_empty_client_secret_not_flagged():
    """A blanked clientSecret ("") is fine — only non-empty values leak."""
    slice_ = {"skills": {"entries": {"s": {"config": {"client_secret": ""}}}}}
    assert marketplace_safety.assert_slice_scrubbed(slice_) == []


def test_slice_scrubbed_snippet_redacts_secret():
    """The flag snippet must NOT contain the raw secret — it rides into the
    manifest / admin UI / logs. Redaction shows length + a 2-char prefix only.
    """
    secret = "sk_live_super_secret_value_1234567890"
    slice_ = {"mcp": {"servers": {"x": {"apiKey": secret}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert flags
    for f in flags:
        assert secret not in f.snippet, f"raw secret leaked into snippet: {f.snippet}"
        # Path is surfaced so a reviewer knows WHERE to look.
        assert "mcp.servers.x.apiKey" in f.snippet


def test_slice_scrubbed_oauth_snippet_redacts_secret():
    secret = "GOCSPX-do-not-leak-me"
    slice_ = {"mcp": {"servers": {"o": {"client_secret": secret}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert flags
    for f in flags:
        assert secret not in f.snippet, f"raw secret leaked into snippet: {f.snippet}"


def test_has_blocking_credential_findings_ignores_non_credential_flags():
    """Tarball flags (email-address, curl-bash, etc.) are NOT slice-credential
    leaks — has_blocking_credential_findings keys only off the new patterns.
    """
    flags = [
        marketplace_safety.SafetyFlag(
            pattern="email-address", severity="medium", file="USER.md", line=1, snippet="a@b.com"
        ),
        marketplace_safety.SafetyFlag(pattern="curl-bash", severity="high", file="x.sh", line=1, snippet="curl|bash"),
    ]
    assert marketplace_safety.has_blocking_credential_findings(flags) is False


def test_slice_scrubbed_uses_catalog_slice_field_names():
    """The field set is single-sourced from catalog_slice — webhookToken is in
    that set, so a surviving webhookToken must flag without re-listing fields
    here."""
    from core.services.catalog_slice import _SECRET_FIELD_NAMES

    assert "webhookToken" in _SECRET_FIELD_NAMES  # guard the source of truth
    slice_ = {"mcp": {"servers": {"hook": {"webhookToken": "whsec_live"}}}}
    flags = marketplace_safety.assert_slice_scrubbed(slice_)
    assert any(f.pattern == "unscrubbed-credential" for f in flags), flags


# ---------------------------------------------------------------------------
# assert_slice_scrubbed — credential-shaped MCP ``args[]`` gate. The scrub
# blanks the value AFTER a secret flag, but a residual value the scrub missed
# (or a bare positional credential token) must still block publish — without
# false-blocking normal ``npx -y <package>`` args.
# ---------------------------------------------------------------------------


def _slice_with_args(args):
    return {"mcp": {"servers": {"s": {"command": "npx", "args": args}}}}


def test_gate_blocks_residual_value_after_secret_flag():
    flags = marketplace_safety.assert_slice_scrubbed(_slice_with_args(["--api-key", "STILL-LEAKED-aaaaaaaa"]))
    assert marketplace_safety.has_blocking_credential_findings(flags)
    assert any(f.pattern == "arg-secret-suspected" for f in flags)


def test_gate_blocks_standalone_provider_prefix_token():
    flags = marketplace_safety.assert_slice_scrubbed(_slice_with_args(["sk-ant-0123456789ABCDEFabcdef"]))
    assert marketplace_safety.has_blocking_credential_findings(flags)


def test_gate_blocks_high_entropy_standalone_token():
    flags = marketplace_safety.assert_slice_scrubbed(_slice_with_args(["aZ09" * 12]))  # 48 chars, mixed
    assert marketplace_safety.has_blocking_credential_findings(flags)


def test_gate_allows_npx_package_specs():
    flags = marketplace_safety.assert_slice_scrubbed(
        _slice_with_args(["-y", "@perplexity-ai/mcp-server", "instantly-mcp"])
    )
    assert not marketplace_safety.has_blocking_credential_findings(flags)


def test_gate_allows_properly_scrubbed_args():
    flags = marketplace_safety.assert_slice_scrubbed(_slice_with_args(["-y", "instantly-mcp", "--api-key", ""]))
    assert not marketplace_safety.has_blocking_credential_findings(flags)


def test_secret_arg_flags_mirror_catalog_slice():
    """The gate's secret-flag set must stay identical to the scrub's, or the two
    layers would disagree on which flags to handle (scrub blanks, gate blocks)."""
    from core.services import catalog_slice

    assert marketplace_safety._SECRET_ARG_FLAGS == catalog_slice._SECRET_ARG_FLAGS


# ---------------------------------------------------------------------------
# Snippet stripping — the persistable / public-safe flag shape.
#
# ``snippet`` is 80 raw chars of the seller's source line, so it is by
# construction the field most likely to carry a credential. It may reach live
# authenticated surfaces (seller preview, admin review) but must never be
# persisted into a manifest, which the unauthenticated listing-detail route
# serves verbatim under a public cache header.
# ---------------------------------------------------------------------------


def test_flags_without_snippets_drops_only_the_snippet():
    flags = [{"pattern": "aws-secret-key", "severity": "high", "file": "M.md", "line": 3, "snippet": "AKIA…"}]
    assert marketplace_safety.flags_without_snippets(flags) == [
        {"pattern": "aws-secret-key", "severity": "high", "file": "M.md", "line": 3}
    ]


def test_manifest_without_snippets_strips_nested_flags():
    manifest = {
        "name": "Demo",
        "safety_flags": [
            {"pattern": "email-address", "file": "SOUL.md", "line": 19, "snippet": "alice@example.com"},
            {"pattern": "curl-bash", "file": "x.sh", "line": 1, "snippet": "curl u | bash"},
        ],
    }
    out = marketplace_safety.manifest_without_snippets(manifest)
    assert out["name"] == "Demo"
    assert all("snippet" not in f for f in out["safety_flags"])
    assert [f["pattern"] for f in out["safety_flags"]] == ["email-address", "curl-bash"]
    # Input is not mutated — the caller may be holding a cached search row.
    assert manifest["safety_flags"][0]["snippet"] == "alice@example.com"


def test_manifest_without_snippets_returns_input_when_nothing_to_strip():
    """Identity return lets read paths skip copying rows that are already clean."""
    for manifest in (None, "not-a-dict", {"name": "Demo"}, {"safety_flags": []}, {"safety_flags": [{"pattern": "e"}]}):
        assert marketplace_safety.manifest_without_snippets(manifest) is manifest
