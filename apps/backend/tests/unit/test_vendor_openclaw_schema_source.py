import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _BACKEND_ROOT / "scripts" / "vendor_openclaw_schema.sh"
_SCHEMA_ID = "2026.8.1-isol8.3"
_TAG = f"v{_SCHEMA_ID}"
_UPSTREAM = f"ghcr.io/thelightbulbcompany/agent:{_SCHEMA_ID}"
_PNPM_SPEC = (
    "pnpm@12.3.4+sha512."
    "961aa41fb077da3a04a441d9f8e15ebc0c96da8ef710b2eb67bf9ee7cb0610e"
    "abd48f1fd85f51cffe73846785fa0f87c56a3a872a1d893f8446741b5cce45457"
)


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
    esbuild_marker: Path
    artifacts: dict[Path, bytes]
    source_commit: str

    def run(
        self,
        upstream: str = _UPSTREAM,
        *,
        args: list[str] | None = None,
        build_exit: int = 0,
        node_version: str = "26.1.0",
        pnpm_version: str = "12.3.4",
        json_valid: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "openclaw-version.json").write_text(json.dumps({"upstream": upstream}))
        env = os.environ.copy()
        env.update(
            {
                "FAKE_BUILD_EXIT": str(build_exit),
                "FAKE_JSON_VALID": "1" if json_valid else "0",
                "FAKE_NODE_VERSION": node_version,
                "FAKE_PNPM_VERSION": pnpm_version,
                "INSTALL_MARKER": str(self.install_marker),
                "ESBUILD_MARKER": str(self.esbuild_marker),
                "OPENCLAW_FORK": str(self.fork),
                "PATH": f"{self.root / 'bin'}{os.pathsep}{env['PATH']}",
            }
        )
        return subprocess.run(
            ["bash", str(_SCRIPT), *(args or [])],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def explicit_args(self, **overrides: str) -> list[str]:
        values = {
            "source": str(self.fork),
            "tag": _TAG,
            "commit": self.source_commit,
            "schema-id": _SCHEMA_ID,
            **overrides,
        }
        return [part for key, value in values.items() for part in (f"--{key}", value)]

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

    def assert_legacy_artifacts_unchanged(self) -> None:
        for path, contents in self.artifacts.items():
            assert path.read_bytes() == contents
        schema_dir = self.root / "apps" / "backend" / "tests" / "chaos" / "_schema"
        assert not (schema_dir / "PINNED_COMMIT").exists()


@pytest.fixture
def vendor_case(tmp_path: Path) -> VendorCase:
    fork = tmp_path / "fork"
    source = fork / "src" / "config" / "zod-schema.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const OpenClawSchema = true;\n")
    (fork / "package.json").write_text(json.dumps({"packageManager": _PNPM_SPEC}))
    (fork / ".gitignore").write_text("node_modules/\nsrc/config/generated.ts\n")
    _git(fork, "init", "-q")
    _git(fork, "add", ".gitignore", "package.json", "src/config/zod-schema.ts")
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
    for consumer in ("gen_json_schema.mjs", "validate.mjs"):
        (schema_dir / consumer).write_text("// shimmed by fake node\n")
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
        "pnpm": """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\\n' "$FAKE_PNPM_VERSION"
  exit 0
fi
: > "$INSTALL_MARKER"
exit "$FAKE_BUILD_EXIT"
""",
        "node": """#!/bin/sh
if [ "$1" = "-p" ]; then
  printf '%s\\n' "$FAKE_NODE_VERSION"
  exit 0
fi
if [ "$1" = "-e" ]; then
  case "$2" in
    *process.versions.node.split*)
      case "$FAKE_NODE_VERSION" in
        24.1[6-9].*|24.[2-9][0-9].*|26.[1-9].*|26.[1-9][0-9].*|2[7-9].*|[3-9][0-9].*) exit 0 ;;
        *) exit 1 ;;
      esac
      ;;
    *packageManager*) printf '%s' 'pnpm@12.3.4+sha512.961aa41fb077da3a04a441d9f8e15ebc0c96da8ef710b2eb67bf9ee7cb0610eabd48f1fd85f51cffe73846785fa0f87c56a3a872a1d893f8446741b5cce45457'; exit 0 ;;
    *JSON.parse*) [ "$FAKE_JSON_VALID" = 1 ]; exit ;;
  esac
fi
if [ "$FAKE_JSON_VALID" = 1 ]; then
  printf '%s\\n' '{"generated":true}'
  exit 0
fi
printf '%s\\n' 'not-json'
exit 1
""",
    }
    for name, contents in shims.items():
        path = bin_dir / name
        path.write_text(contents)
        path.chmod(0o755)

    esbuild = fork / "node_modules" / ".bin" / "esbuild"
    esbuild.parent.mkdir(parents=True)
    esbuild.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$ESBUILD_MARKER"
for arg in "$@"; do
  case "$arg" in --outfile=*) outfile=${arg#--outfile=} ;; esac
done
printf '%s\\n' 'generated bundle' > "$outfile"
"""
    )
    esbuild.chmod(0o755)

    return VendorCase(
        root,
        fork,
        root / "install.marker",
        root / "esbuild.marker",
        artifact_contents,
        _git(fork, "rev-parse", "HEAD"),
    )


def test_clean_annotated_tag_checkout_reaches_dependency_install(vendor_case: VendorCase):
    result = vendor_case.run(build_exit=74)
    assert result.returncode == 74
    assert vendor_case.install_marker.exists()
    assert _TAG in result.stdout
    assert vendor_case.source_commit in result.stdout


def test_ignored_source_build_output_is_allowed(vendor_case: VendorCase):
    (vendor_case.fork / "src" / "config" / "generated.ts").write_text("generated\n")
    result = vendor_case.run(build_exit=74)
    assert result.returncode == 74
    assert vendor_case.install_marker.exists()


def test_lightweight_tag_is_rejected_before_dependency_install(vendor_case: VendorCase):
    _git(vendor_case.fork, "tag", "-d", _TAG)
    _git(vendor_case.fork, "tag", _TAG)
    result = vendor_case.run()
    assert result.returncode != 0
    assert "annotated" in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


def test_missing_expected_tag_is_rejected_before_dependency_install(vendor_case: VendorCase):
    _git(vendor_case.fork, "tag", "-d", _TAG)
    result = vendor_case.run()
    assert result.returncode != 0
    assert "missing" in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


def test_wrong_head_is_rejected_before_dependency_install(vendor_case: VendorCase):
    vendor_case.commit_change("src/config/zod-schema.ts", "export const changed = true;\n")
    result = vendor_case.run()
    assert result.returncode != 0
    assert not vendor_case.install_marker.exists()
    assert "HEAD" in result.stderr
    vendor_case.assert_legacy_artifacts_unchanged()


@pytest.mark.parametrize("tracked", [False, True], ids=["unstaged", "staged"])
def test_tracked_change_is_rejected_before_dependency_install(vendor_case: VendorCase, tracked: bool):
    path = vendor_case.fork / "src" / "config" / "zod-schema.ts"
    path.write_text("export const dirty = true;\n")
    if tracked:
        _git(vendor_case.fork, "add", "src/config/zod-schema.ts")
    result = vendor_case.run()
    assert result.returncode != 0
    assert "tracked modifications" in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


def test_untracked_source_input_is_rejected_before_dependency_install(vendor_case: VendorCase):
    (vendor_case.fork / "src" / "config" / "untracked.ts").write_text("export const untracked = true;\n")
    result = vendor_case.run()
    assert result.returncode != 0
    assert "untracked" in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


def test_untracked_package_manager_control_is_rejected_before_dependency_install(vendor_case: VendorCase):
    (vendor_case.fork / ".npmrc").write_text("node-linker=hoisted\n")
    result = vendor_case.run()
    assert result.returncode != 0
    assert "untracked" in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


@pytest.mark.parametrize(
    "upstream",
    [
        "ghcr.io/thelightbulbcompany/agent:",
        "ghcr.io/thelightbulbcompany/agent:bad tag",
        "ghcr.io/thelightbulbcompany/agent:bad/tag",
        "ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3@sha256:deadbeef",
    ],
)
def test_malformed_or_digest_image_ref_is_rejected(vendor_case: VendorCase, upstream: str):
    result = vendor_case.run(upstream)
    assert result.returncode != 0
    assert "tagged Lightbulb fork image" in result.stderr
    vendor_case.assert_legacy_artifacts_unchanged()


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"schema-id": "../escape"}, "invalid schema id"),
        ({"schema-id": "different"}, "exactly match"),
        ({"commit": "deadbeef"}, "full lowercase"),
        ({"commit": "0" * 40}, "peeled tag commit"),
    ],
)
def test_explicit_candidate_rejects_untrusted_identity(vendor_case: VendorCase, overrides: dict[str, str], error: str):
    result = vendor_case.run(args=vendor_case.explicit_args(**overrides))
    assert result.returncode != 0
    assert error in result.stderr
    assert not vendor_case.install_marker.exists()
    vendor_case.assert_legacy_artifacts_unchanged()


def test_explicit_candidate_requires_complete_provenance(vendor_case: VendorCase):
    result = vendor_case.run(args=["--source", str(vendor_case.fork)])
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not vendor_case.install_marker.exists()


def test_runtime_and_package_manager_pins_are_enforced(vendor_case: VendorCase):
    old_node = vendor_case.run(node_version="24.15.9")
    assert old_node.returncode != 0
    assert "unsupported" in old_node.stderr
    assert not vendor_case.install_marker.exists()

    wrong_pnpm = vendor_case.run(pnpm_version="9.15.0")
    assert wrong_pnpm.returncode != 0
    assert "requires pnpm 12.3.4" in wrong_pnpm.stderr
    assert not vendor_case.install_marker.exists()


def test_generation_failure_preserves_published_artifacts(vendor_case: VendorCase):
    result = vendor_case.run(json_valid=False)
    assert result.returncode != 0
    assert "published artifacts unchanged" in result.stderr
    vendor_case.assert_legacy_artifacts_unchanged()


def test_explicit_candidate_publishes_versioned_artifacts_and_provenance(vendor_case: VendorCase):
    result = vendor_case.run(args=vendor_case.explicit_args())
    assert result.returncode == 0, result.stderr
    vendor_case.assert_legacy_artifacts_unchanged()

    release = vendor_case.root / "apps" / "backend" / "tests" / "chaos" / "_schema" / "releases" / _SCHEMA_ID
    json_path = (
        vendor_case.root / "apps" / "backend" / "core" / "services" / "openclaw_config_schemas" / f"{_SCHEMA_ID}.json"
    )
    bundle = release / "openclaw-config-schema.mjs"
    provenance = json.loads((release / "PROVENANCE.json").read_text())
    assert provenance == {
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "commit": vendor_case.source_commit,
        "json_sha256": hashlib.sha256(json_path.read_bytes()).hexdigest(),
        "schema_id": _SCHEMA_ID,
        "tag": _TAG,
    }
    esbuild_args = vendor_case.esbuild_marker.read_text()
    assert "--target=node20" in esbuild_args
    assert "createRequire" in esbuild_args
    assert "npx" not in esbuild_args


def test_legacy_mode_remains_usable(vendor_case: VendorCase):
    result = vendor_case.run()
    assert result.returncode == 0, result.stderr
    schema_dir = vendor_case.root / "apps" / "backend" / "tests" / "chaos" / "_schema"
    assert (schema_dir / "PINNED_COMMIT").read_text() == f"{vendor_case.source_commit}\n"
    assert (schema_dir / "PINNED_TAG").read_text() == f"{_UPSTREAM}\n"
