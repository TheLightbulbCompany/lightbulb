import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _BACKEND_ROOT / "scripts" / "vendor_openclaw_schema.sh"
_TAG = "v2026.8.1-isol8.3"
_UPSTREAM = "ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class VendorCase:
    root: Path
    fork: Path
    install_marker: Path
    artifacts: dict[Path, bytes]
    source_commit: str

    def run(self, upstream: str = _UPSTREAM, *, complete: bool = False) -> subprocess.CompletedProcess[str]:
        (self.root / "openclaw-version.json").write_text(json.dumps({"upstream": upstream}))
        env = os.environ.copy()
        env.update(
            {
                "FAKE_PNPM_EXIT": "0" if complete else "74",
                "INSTALL_MARKER": str(self.install_marker),
                "OPENCLAW_FORK": str(self.fork),
                "PATH": f"{self.root / 'bin'}{os.pathsep}{env['PATH']}",
            }
        )
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def commit_change(self, relative_path: str, contents: str) -> None:
        (self.fork / relative_path).write_text(contents)
        _git(self.fork, "add", relative_path)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.fork),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "change",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def assert_artifacts_unchanged(self) -> None:
        for path, contents in self.artifacts.items():
            assert path.read_bytes() == contents
        schema_dir = self.root / "apps" / "backend" / "tests" / "chaos" / "_schema"
        assert not (schema_dir / "PINNED_COMMIT").exists()


@pytest.fixture
def vendor_case(tmp_path: Path) -> VendorCase:
    fork = tmp_path / "fork"
    source = fork / "src" / "config" / "zod-schema.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const schema = true;\n")
    (fork / ".gitignore").write_text("src/config/generated.ts\n")
    _git(fork, "init", "-q")
    _git(fork, "add", ".gitignore", "src/config/zod-schema.ts")
    subprocess.run(
        [
            "git",
            "-C",
            str(fork),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(fork),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "tag",
            "-a",
            _TAG,
            "-m",
            "release",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    root = tmp_path / "invocation"
    schema_dir = root / "apps" / "backend" / "tests" / "chaos" / "_schema"
    json_schema = root / "apps" / "backend" / "core" / "services" / "openclaw_config.schema.json"
    schema_dir.mkdir(parents=True)
    json_schema.parent.mkdir(parents=True)
    artifact_contents = {
        schema_dir / "openclaw-config-schema.mjs": b"bundle sentinel\n",
        json_schema: b'{"sentinel": true}\n',
        schema_dir / "PINNED_TAG": b"tag sentinel\n",
    }
    for path, contents in artifact_contents.items():
        path.write_bytes(contents)

    bin_dir = root / "bin"
    bin_dir.mkdir()
    shims = {
        "pnpm": '#!/bin/sh\n: > "$INSTALL_MARKER"\nexit "$FAKE_PNPM_EXIT"\n',
        "npx": """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    --outfile=*) outfile=${arg#--outfile=} ;;
  esac
done
printf 'generated bundle\\n' > "$outfile"
""",
        "node": """#!/bin/sh
if [ "$1" = "-e" ]; then
  exit 0
fi
printf '%s\\n' '{"generated": true}'
""",
    }
    for name, contents in shims.items():
        path = bin_dir / name
        path.write_text(contents)
        path.chmod(0o755)

    return VendorCase(root, fork, root / "install.marker", artifact_contents, _git(fork, "rev-parse", "HEAD"))


def test_clean_annotated_tag_checkout_reaches_dependency_install(vendor_case: VendorCase):
    result = vendor_case.run()
    assert result.returncode == 74
    assert vendor_case.install_marker.exists()
    assert _TAG in result.stdout
    assert vendor_case.source_commit in result.stdout


def test_ignored_source_build_output_is_allowed(vendor_case: VendorCase):
    (vendor_case.fork / "src" / "config" / "generated.ts").write_text("generated\n")
    result = vendor_case.run()
    assert result.returncode == 74
    assert vendor_case.install_marker.exists()


def test_wrong_head_is_rejected_before_dependency_install(vendor_case: VendorCase):
    vendor_case.commit_change("src/config/zod-schema.ts", "export const changed = true;\n")
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "HEAD" in result.stderr
    vendor_case.assert_artifacts_unchanged()


def test_missing_expected_tag_is_rejected_before_dependency_install(vendor_case: VendorCase):
    _git(vendor_case.fork, "tag", "-d", _TAG)
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "missing" in result.stderr
    vendor_case.assert_artifacts_unchanged()


def test_unstaged_tracked_change_is_rejected_before_dependency_install(vendor_case: VendorCase):
    (vendor_case.fork / "src" / "config" / "zod-schema.ts").write_text("export const dirty = true;\n")
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "tracked modifications" in result.stderr
    vendor_case.assert_artifacts_unchanged()


def test_staged_tracked_change_is_rejected_before_dependency_install(vendor_case: VendorCase):
    path = vendor_case.fork / "src" / "config" / "zod-schema.ts"
    path.write_text("export const staged = true;\n")
    _git(vendor_case.fork, "add", "src/config/zod-schema.ts")
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "tracked modifications" in result.stderr
    vendor_case.assert_artifacts_unchanged()


def test_untracked_source_input_is_rejected_before_dependency_install(vendor_case: VendorCase):
    (vendor_case.fork / "src" / "config" / "untracked.ts").write_text("export const untracked = true;\n")
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "untracked" in result.stderr
    vendor_case.assert_artifacts_unchanged()


@pytest.mark.parametrize(
    "upstream",
    [
        "ghcr.io/thelightbulbcompany/agent:",
        "ghcr.io/thelightbulbcompany/agent:bad tag",
        "ghcr.io/thelightbulbcompany/agent:bad/tag",
        "ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3@sha256:deadbeef",
    ],
)
def test_malformed_or_digest_image_ref_is_rejected_before_dependency_install(vendor_case: VendorCase, upstream: str):
    result = vendor_case.run(upstream)
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "tagged Lightbulb fork image" in result.stderr
    vendor_case.assert_artifacts_unchanged()


def test_success_records_verified_commit(vendor_case: VendorCase):
    result = vendor_case.run(complete=True)
    assert result.returncode == 0, result.stderr
    schema_dir = vendor_case.root / "apps" / "backend" / "tests" / "chaos" / "_schema"
    assert (schema_dir / "PINNED_COMMIT").read_text() == f"{vendor_case.source_commit}\n"
    assert (schema_dir / "PINNED_TAG").read_text() == f"{_UPSTREAM}\n"
