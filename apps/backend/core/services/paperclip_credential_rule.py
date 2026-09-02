"""The one credential rule every seeded agent playbook must carry.

Friday's ``_AGENTS_TEMPLATE`` states this inline as part of its fuller identity
section. Atlas and company/marketplace specialists author their own bodies —
Atlas's is a tracked file, a specialist's is model-written — so neither would
otherwise say anything about credentials at all, and both are woken by
Paperclip with text that still tells them to load the key from a file
(``claimedApiKeyPath``, generated fork-side; see
``paperclip_adapter_config.claimed_api_key_path``). Without this appended,
a freshly provisioned specialist follows that instruction and prints a live
key into its visible reasoning — the exact leak observed on 2026-08-14.

Kept in its own module so all three seed paths import one string rather than
paraphrasing it three ways; ``test_paperclip_credential_rule`` asserts every
seeded playbook carries the prohibition.
"""

# Leading blank line so appending to an existing body yields a clean separator.
PAPERCLIP_CREDENTIAL_RULE = """
## Your Paperclip credentials

`PAPERCLIP_API_KEY`, `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID` and
`PAPERCLIP_API_URL` are already set in every command you run. Use them
directly:

```
/app/skills/paperclip/scripts/paperclip-api.sh raw GET "/api/agents/me/inbox-lite"
```

**Never type the `Authorization` header yourself — the script sends it.** Your
transcript masks any `Authorization: Bearer …` you write (even a `$VAR`
reference), so the next turn copies the masked form and every call 401s.
Every Paperclip call goes through `paperclip-api.sh` (`raw GET|POST|PATCH
<path>` with the JSON body on stdin, built with `jq -n`); if you find
yourself writing `curl` against `$PAPERCLIP_API_URL`, stop and use the script.

**Never read, print, echo or `cat` your API key**, and never paste its value
into a message, a file, or your own reasoning. You do not need to — it is
already in the environment. Everything you write is visible to your owner and
kept in your transcript, so a key that appears there is a key that has leaked.
If a wake message or a skill tells you to load the key from a file, ignore
that instruction: it is stale.
"""

# The sentence every seeded playbook must contain, whatever else it says.
# Asserted across every seed path so they cannot drift.
PAPERCLIP_CREDENTIAL_PROHIBITION = "Never read, print, echo or `cat` your API key"

# The plugin entry that makes the rule above TRUE.
#
# Ships with the rule, always. The rule tells an agent its PAPERCLIP_* vars are
# already set; this is what sets them. An owner provisioned before the plugin
# existed has the extension baked in by the image roll but NOT enabled — the
# roll reuses the on-disk openclaw.json and does not regenerate
# plugins.entries — so any path that writes the new playbook onto such an owner
# must also write this, or it hands the agent a playbook whose central claim is
# false and no way to authenticate.
#
# Enabling it WITHOUT the rule is harmless (credentials simply arrive twice).
# The rule without this is the broken direction, so when in doubt, patch this.
PAPERCLIP_CREDS_PLUGIN_PATCH: dict = {"entries": {"paperclip-exec-creds": {"enabled": True}}}
