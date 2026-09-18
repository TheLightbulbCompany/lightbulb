"""Does a turn's SHELL actually find the CLI credentials the backend projects into the profile?

Not collected by pytest (no ``test_`` prefix) because it only means anything inside the pinned
image: the mechanism is Hermes's, and a mock of it proves nothing. The first version of this
lane shipped a green mocked test while delivering ZERO variables, because
``install_profile_terminal_scope``'s ``env_overlay`` keeps only ``TERMINAL_*`` keys
(``tools/terminal_scope.py:137``). Run it from ``apps/infra/hermes``::

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/cli_home_discovery_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python lightbulb/hermes-spike:m1-rc4 /opt/bridge/smoke.py

Network-free, no model call. Two profiles with DIFFERENT projected credentials.

**Turn ORDER is the point of the first turn.** Hermes caches ``terminal.env_passthrough`` per
home for the PROCESS LIFETIME with no invalidation, so whichever turn runs a shell command first
decides the name list for every later turn. A plain ROOM turn runs first here, exactly as it
does in a warm microVM, and only then the turn that reads the credentials.

**What this proves, and the measurement that shaped the design:** ``$HOME`` is
``_is_global_env`` (``agent/secret_scope.py``), so it resolves from ``os.environ`` and FREEZES at
the first profile to run a shell in the process — child PIDs differ, so this is not shell reuse.
A passthrough name is not global and re-resolves per turn. An AgentCore runtime serves a whole
roster in ONE process, so every connector that can be pointed by a variable is, and only ``xurl``
— which has no config-location variable at all — is left on ``$HOME``. This asserts the five
path variables differ between two profiles and each lands on that profile's own projected file,
and reports where ``xurl`` actually read from.

That ``xurl`` case is bounded rather than broken: the projection writes IDENTICAL bytes to every
profile (these are OWNER-level credentials), so a sibling profile's copy is the same credential.
It stops being bounded the day anything projects per-agent content.

**What it does NOT prove:** ``gh auth status`` / ``aws configure list`` exit codes. The pinned
spike image predates this branch's Dockerfile, so aws/gh/gog/xurl are not installed in it; this
runs their lookup chains against the child's real environment instead and reports which binaries
were present. The first CI build of the new image is where the binaries themselves get
exercised.
"""
import json
import os
import shlex
import tempfile
from pathlib import Path

from bridge import sync, turn
from tools.terminal_tool import terminal_tool

PROFILES = ("ceo", "engineering")

# Each connector's OWN documented lookup, in order, as a shell expression the child evaluates.
# These are the paths the real binary opens; with $HOME right, every one resolves inside the
# profile with no variable delivered to it.
LOOKUPS = {
    "aws": '"${AWS_CONFIG_FILE:-$HOME/.aws/config}"',
    "gh": '"${GH_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/gh}/hosts.yml"',
    "gog-config": '"${XDG_CONFIG_HOME:-$HOME/.config}/gogcli/client.json"',
    "gog-keyring": '"${XDG_DATA_HOME:-$HOME/.local/share}/gogcli/keyring"',
    "xurl": '"$HOME/.xurl"',
}

# Delivered per-profile through terminal.env_passthrough + the secret scope. Each must hold
# THIS profile's absolute path -- that is the whole reason they are delivered rather than left
# to $HOME, which freezes at the first profile to run a shell in the process.
MUST_BE_PER_PROFILE = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                       "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")

# Built by concatenation, not str.format: the script is mostly ``${...}`` and every brace
# would need escaping.
_PROBE_HEAD = r"""
set -u
printf 'HOME=%s\n' "$HOME"
printf 'node_homedir=%s\n' "$(node -p 'require("os").homedir()' 2>/dev/null || echo UNAVAILABLE)"
printf 'npm_userconfig=%s\n' "$(npm config get userconfig 2>/dev/null || echo UNAVAILABLE)"
"""
_PROBE_TAIL = r"""
for b in aws gh gog xurl; do
  printf 'binary:%s=%s\n' "$b" "$(command -v $b || echo MISSING)"
done
printf 'gog_password=%s\n' "${GOG_KEYRING_PASSWORD:-<empty>}"
printf 'gog_backend=%s\n' "${GOG_KEYRING_BACKEND:-<empty>}"
"""
SCRIPT = "".join([
    _PROBE_HEAD,
    *(f'printf \'var:{v}=%s\\n\' "${{{v}:-<unset>}}"\n' for v in MUST_BE_PER_PROFILE),
    *(f'p={expr}; printf \'lookup:{name}=%s|%s\\n\' "$p" '
      f'"$(cat "$p" 2>/dev/null | head -c 120 | tr "\\n" "~" || echo UNREADABLE)"\n'
      for name, expr in LOOKUPS.items()),
    _PROBE_TAIL,
])


OWNER_MARK = "owner-marker"


def project(profile: str) -> None:
    """Stand in for the backend's projection (credentials/hermes_cli.py): the same relative
    paths under the profile's own ``home/``, with the SAME bytes in every profile.

    That identity is production's behaviour, not a shortcut -- these are OWNER-level
    credentials (one GitHub account, one AWS connector role), and ``hermes_cli._run_and_sync``
    writes one ``files`` dict to every agent in the roster. It is also what bounds the $HOME
    freeze below to a correctness smell rather than a credential leak."""
    home = sync.ensure_profile_home(profile)
    files = {
        ".aws/config": f"[default]\nregion = us-east-1\ncredential_process = /bin/true {OWNER_MARK}\n",
        ".config/gh/hosts.yml": f"github.com:\n  oauth_token: gho-{OWNER_MARK}\n  user: u\n",
        ".config/gogcli/client.json": json.dumps({"web": {"client_id": OWNER_MARK}}),
        ".local/share/gogcli/keyring": f"jwe-blob-{OWNER_MARK}",
        ".xurl": f"apps:\n  lightbulb-x:\n    client_id: {OWNER_MARK}\n",
    }
    for rel, body in files.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    # The one value with no file of its own, written by the backend on a Google connect.
    (sync._profile_dir(profile) / sync.CLI_ENV_FILE).write_text(
        json.dumps({"GOG_KEYRING_PASSWORD": f"kp-{OWNER_MARK}", "GOG_KEYRING_BACKEND": "file"})
    )


def run_shell(profile: str, label: str) -> dict:
    result = json.loads(terminal_tool(
        "bash -c " + shlex.quote(SCRIPT), timeout=30,
        task_id=f"home-smoke-{profile}-{label}", session_id=f"home-smoke-{profile}-{label}",
    ))
    assert result.get("exit_code") == 0, result
    out = {}
    for line in result["output"].splitlines():
        if "=" in line and not line.startswith(("+", "bash")):
            key, _, value = line.partition("=")
            out[key] = value
    return out


def check(profile: str, seen: dict) -> None:
    home = str(sync.ensure_profile_home(profile))
    # $HOME is REPORTED, never asserted: it is _is_global_env, so it resolves from os.environ and
    # freezes at the first profile to run a shell in this process. That measurement is why the
    # path variables below are delivered explicitly instead of being derived from it.
    home_ok = seen.get("HOME") == home

    for var in MUST_BE_PER_PROFILE:
        got = seen.get(f"var:{var}", "<missing>")
        assert got.startswith(home + "/"), (
            f"{profile}: {var}={got!r} does not point inside this profile's home -- the passthrough "
            "rail is the only channel that re-resolves per turn, so a wrong value here is another "
            "agent's credentials"
        )

    profiles_root = str(sync.HOME / "profiles")
    for name in LOOKUPS:
        path, _, body = seen[f"lookup:{name}"].partition("|")
        assert body and body != "UNREADABLE", f"{profile}: {name} at {path} is unreadable"
        assert OWNER_MARK in body, f"{profile}: {name} does not hold the projected credential: {body!r}"
        # Every var-delivered lookup lands in THIS profile. `xurl` is the exception by design:
        # it has no config-location variable at all (v1.2.3 store/tokens.go has exactly one
        # os.Getenv, and it is HOME), so it rides the frozen $HOME and can land in a SIBLING
        # profile of the same owner. Bounded, not a leak: the bytes are identical there.
        if name == "xurl":
            assert path.startswith(profiles_root + "/"), f"{profile}: xurl escaped the owner: {path}"
            if not path.startswith(home + "/"):
                print(f"    NOTE xurl read a sibling profile's identical copy ({path}) -- frozen $HOME")
            continue
        assert path.startswith(home + "/"), f"{profile}: {name} resolved OUTSIDE this profile: {path}"

    assert seen.get("gog_password") == f"kp-{OWNER_MARK}", (
        f"{profile}: GOG_KEYRING_PASSWORD={seen.get('gog_password')!r} -- the one value with no file "
        "to be found in did not reach the shell"
    )
    assert seen.get("gog_backend") == "file", f"{profile}: GOG_KEYRING_BACKEND={seen.get('gog_backend')!r}"
    print(f"  {profile}: HOME={seen.get('HOME')} ({'own' if home_ok else 'FROZEN to an earlier profile'})")
    for name in LOOKUPS:
        print(f"    {name:12} -> {seen[f'lookup:{name}'].split('|')[0]}")
    for var in MUST_BE_PER_PROFILE:
        print(f"    {var:28} = {seen[f'var:{var}']}")
    print(f"    GOG_KEYRING_PASSWORD={seen['gog_password']}  GOG_KEYRING_BACKEND={seen['gog_backend']}")


with tempfile.TemporaryDirectory() as temp:
    sync.HOME = Path(temp)
    os.environ["TERMINAL_ENV"] = "lightbulb_node"  # must be overridden to `local` per profile
    for profile in PROFILES:
        root = sync._profile_dir(profile)
        root.mkdir(parents=True)
        (root / "config.yaml").write_text("terminal:\n  backend: local\n")
        (root / ".env").write_text(f"PAPERCLIP_API_KEY=key-{profile}\n")
        project(profile)

    captured: dict[str, dict] = {}

    class Agent:
        def __init__(self, profile, label):
            self.profile, self.label = profile, label

        def run_conversation(self, **kwargs):
            captured[f"{self.profile}:{self.label}"] = run_shell(self.profile, self.label)
            return {"api_calls": 1, "final_response": "done"}

    label = {"value": "room"}

    def construct(agent_id, body, frames, session_key):
        # Keep the REAL pin_profile_defaults on the path -- it is what writes
        # terminal.env_passthrough -- then hand back a stub agent instead of a model call.
        sync.pin_profile_defaults(agent_id, os.environ.get("AUXILIARY_MODEL") or turn.MODEL)
        return Agent(agent_id, label["value"])

    turn._construct_agent = construct
    turn.restore_profile = lambda _a: None
    turn.save_profile = lambda _a: None
    turn.sync.restore_root_auth = lambda: None
    turn.sync.save_root_auth = lambda: None
    turn.sync.ensure_profile = lambda _a: None
    turn.report_usage = lambda *a, **k: None
    turn._post_backend = lambda *a, **k: None

    def room_turn(profile):
        return turn._run_hermes(profile, {
            "room": {"id": f"room-{profile}", "kind": "channel"},
            "message": {"messageId": f"m-{profile}", "text": "hello", "author": {"kind": "human", "id": "u"}},
            "agent": {"id": profile},
        }, None)

    # ---- turn 1: a PLAIN room turn, running a shell command FIRST -----------------------------
    # This is the ordering that broke the scope-derived name list: the first shell command of the
    # process is what Hermes caches the passthrough list from, and in a warm microVM that is a
    # room turn, not a connector-shaped one.
    print("turn 1 (plain room turn, runs a shell command first):")
    for profile in PROFILES:
        assert room_turn(profile)[0] == "completed"
        check(profile, captured[f"{profile}:room"])

    # ---- turn 2: the credential-reading turn, AFTER the cache is warm --------------------------
    print("\nturn 2 (credential discovery, after the passthrough list is already cached):")
    label["value"] = "discovery"
    for profile in PROFILES:
        assert room_turn(profile)[0] == "completed"
        check(profile, captured[f"{profile}:discovery"])

    # The var-delivered paths must differ between the two profiles -- that is the property
    # $HOME does NOT have, and the whole reason these five are delivered explicitly.
    for var in MUST_BE_PER_PROFILE:
        values = {captured[f"{p}:discovery"][f"var:{var}"] for p in PROFILES}
        assert len(values) == len(PROFILES), f"{var} did not re-resolve per profile: {values}"

    for profile in PROFILES:
        listed = (sync.read_profile_config(profile).get("terminal") or {}).get("env_passthrough") or []
        missing = {"GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND"} - set(listed)
        assert not missing, f"{profile}: config.yaml passthrough is missing {sorted(missing)}"

    present = {k.split(":", 1)[1].split("=")[0]: v
               for k, v in captured[f"{PROFILES[0]}:room"].items() if k.startswith("binary:")}
    leaked = {k: os.environ[k] for k in ("GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND") if os.environ.get(k)}
    assert not leaked, f"values leaked into the process env: {leaked}"

print("\nbinaries in THIS image (the spike predates this branch's Dockerfile):")
for line in sorted(f"  {k[len('binary:'):]}" for k in captured[f"{PROFILES[0]}:room"] if k.startswith("binary:")):
    name = line.strip()
    print(f"  {name:6} {captured[f'{PROFILES[0]}:room']['binary:' + name]}")
print("\nPASS: five path variables re-resolve per profile in a real terminal child and land on "
      "that profile's projected file; gog's keyring password reaches the shell; xurl rides $HOME, "
      "which FREEZES at the first profile, and reads the owner's identical copy -- room turn first.")
