"""Spec §3.6: a marketplace artifact must never carry `.projects/`, the Stripe
CLI config, or provider credentials. A hired agent arrives with no accounts."""

import io
import os
import tarfile

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.agent_export import _SKIP_DIR_NAMES, _build_tarball  # noqa: E402


@pytest.fixture
def dirty_workspace(tmp_path):
    ws = tmp_path / "workspace-agent_1"
    (ws / "skills").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("persona\n")
    (ws / "skills" / "sell.md").write_text("how to sell\n")
    # Everything below must be absent from the artifact.
    (ws / ".projects" / "vault").mkdir(parents=True)
    (ws / ".projects" / "vault" / "posthog.json").write_text('{"token":"phc_live_leak"}\n')
    (ws / ".projects" / "state.json").write_text('{"project":"p_1"}\n')
    (ws / ".stripe").mkdir()
    (ws / ".stripe" / "config.toml").write_text("[default]\napi_key = sk_live_leak\n")
    (ws / ".config" / "stripe").mkdir(parents=True)
    (ws / ".config" / "stripe" / "config.toml").write_text("[default]\napi_key = sk_live_leak\n")
    (ws / ".env").write_text("NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN=phc_live_leak\n")
    (ws / "posthog-credentials.json").write_text('{"token":"phc_live_leak"}\n')
    return ws


def _members(blob: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        return tf.getnames()


def _extracted_text(blob: bytes) -> str:
    """Decompress and concatenate every shipped file's actual bytes.

    Checking a plaintext substring against ``blob`` directly is a dead
    assertion: ``blob`` is gzip-compressed, and deflate output essentially
    never contains the plaintext substring whether or not it was ever
    tarred — so a leaked secret would pass silently. Extracting is the only
    place a content check proves anything.
    """
    chunks: list[bytes] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            extracted = tf.extractfile(member)
            if extracted is not None:
                chunks.append(extracted.read())
    return b"\n".join(chunks).decode("latin-1")


def test_tarball_omits_projects_stripe_and_credential_files(dirty_workspace):
    blob, contents, _pruned = _build_tarball(dirty_workspace)
    names = " ".join(_members(blob)) + " " + " ".join(contents)
    for leaked in (".projects", ".stripe", ".config", ".env", "posthog-credentials.json"):
        assert leaked not in names, f"{leaked} reached the artifact"
    extracted = _extracted_text(blob)
    assert "phc_live_leak" not in extracted
    assert "sk_live_leak" not in extracted
    # The real workspace still ships.
    assert any(n.endswith("AGENTS.md") for n in _members(blob))
    assert any(n.endswith("skills/sell.md") for n in _members(blob))


def test_explicit_export_paths_cannot_smuggle_the_projects_dir(dirty_workspace):
    """A seller naming `.projects` explicitly must still get nothing."""
    blob, contents, _ = _build_tarball(dirty_workspace, paths=[".projects", "AGENTS.md"])
    assert ".projects" not in " ".join(_members(blob) + contents)
    assert any(n.endswith("AGENTS.md") for n in _members(blob))


def test_projects_and_stripe_stay_named_backstops():
    """Spec §3.6: "nothing on this rail writes to EFS any more, and
    `.projects/` / `.stripe` stay in `_SKIP_DIR_NAMES` only as a named
    backstop" — for workspaces dirtied by the retired CLI path before it was
    removed, and for anything a seller copies in by hand."""
    assert ".projects" in _SKIP_DIR_NAMES
    assert ".stripe" in _SKIP_DIR_NAMES


def test_a_copied_non_dot_vault_is_still_caught(dirty_workspace):
    """A seller who copies the vault to a normal name defeats the dotfile rule.
    `projects-vault/` is not excluded by NAME — the credential-shape filename
    guard is what catches the payload."""
    copied = dirty_workspace / "projects-vault"
    copied.mkdir()
    (copied / "credentials.json").write_text('{"token":"phc_live_leak"}\n')
    blob, _contents, _ = _build_tarball(dirty_workspace)
    assert "phc_live_leak" not in _extracted_text(blob)
