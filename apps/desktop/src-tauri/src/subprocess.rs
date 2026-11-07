//! Supervises the bundled `openclaw node run` subprocess.
//!
//! Spawns the launcher shim (which forwards to the real openclaw binary)
//! with the env vars + CLI args the host expects, plus writes the keypair
//! identity file to disk so openclaw can pick it up on startup.
//!
//! The identity file at `<state_dir>/identity/device.json` is not the
//! source of truth — that's the macOS Keychain (see `keychain.rs`). This
//! file is rewritten on every spawn from the in-memory keypair passed in,
//! and chmod'd to 0o600 so other users on the host can't read the private
//! key.
//!
//! On openclaw 8.1+ that rewrite is a one-time import, not the ongoing
//! source of truth: the node host migrates this file into its SQLite
//! `device_identities` row at startup, and thereafter the row wins. If the
//! rewritten file ever carried DIFFERENT key material than that row, the
//! migrator throws, `reportMigration` downgrades it to a stderr warning,
//! and the node silently connects under the OLD identity.
//!
//! ponytail: not reachable today — `keychain::load_or_generate` only mints
//! a key when none exists and `keychain::delete` has no callers, so the
//! keypair is stable for the life of the install. If key rotation or an
//! unpair-that-clears-the-Keychain is ever added, this becomes a real
//! silent-failure path and the fix is to stop writing this file once a
//! canonical SQLite identity row exists.

use ed25519_dalek::pkcs8::DecodePublicKey;
use ed25519_dalek::VerifyingKey;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

use crate::log;

/// Handle to a running openclaw subprocess. Drop kills the child via
/// `kill_on_drop(true)` set at spawn time, but call `shutdown()` for an
/// orderly stop where possible.
pub struct SubprocessHandle {
    child: Arc<Mutex<Option<Child>>>,
}

/// Remove the retired `node.json` node-host config so an 8.1+ sidecar can start.
///
/// openclaw 2026.5.28 persisted node-host config to `<state_dir>/node.json`.
/// 8.1 moved it into SQLite and now HARD-FAILS at startup while that file --
/// or an interrupted `.doctor-importing` claim -- is still present:
///
/// > retired node-host state remains at ...; run `openclaw doctor --fix`
///
/// Unlike the device identity, node-host config is NOT in the startup
/// migration set (`runStartupMigrations` covers device auth, device identity
/// and exec approvals only), so nothing self-heals it. The node host simply
/// exits on every launch until the file is gone.
///
/// We DELETE rather than migrate, because everything `node.json` carried is
/// either regenerated or re-supplied by us on the next spawn:
/// - `nodeId` is a random instance id, NOT the pairing key. The gateway keys
///   pairing on the device identity (`connectParams.device?.id`), which lives
///   in the Keychain and is written to `identity/device.json` below.
/// - `gateway` host/port/tls arrive as `--host`/`--port` from the launcher
///   shim on every launch.
/// - `displayName` is re-supplied by the runner's configure call.
///
/// Running `openclaw doctor --fix` instead would work, but it also drags in
/// the agent-DB migration ladder and its 300s budget for no benefit here --
/// and upstream's macOS app cannot be copied on this point, because it never
/// materializes a node-host state dir at all (it IS the node, and only spawns
/// the hidden `openclaw node worker`). We also have no CLI on the user's PATH
/// to tell them to run.
///
/// Unconditional and idempotent: 8.1+ never writes `node.json` back, so this
/// is a no-op on every launch after the first one following the version bump.
fn retire_legacy_node_host_config(state_dir: &std::path::Path) -> Result<(), String> {
    for stale in ["node.json", "node.json.doctor-importing"] {
        match std::fs::remove_file(state_dir.join(stale)) {
            Ok(()) => log(&format!("[subprocess] removed retired node-host state: {stale}")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("remove retired {stale}: {e}")),
        }
    }
    Ok(())
}

/// Write the identity file and spawn the openclaw subprocess.
///
/// Args:
/// - `binary`: path to the launcher shim (the bundled
///   `lightbulb-browser-service` resource in `Contents/Resources/bin/` —
///   see `lib.rs::resolve_node_host_binary`). The shim then locates the underlying
///   per-arch `openclaw-host` npm install on disk.
/// - `proxy_port`: localhost port of the loopback WSS proxy.
/// - `state_dir`: `OPENCLAW_STATE_DIR` — where openclaw stores its on-disk
///   state. The identity file is written under here.
/// - `private_key_pem` / `public_key_pem`: Ed25519 keypair from the
///   macOS Keychain. Serialized into `identity/device.json`.
/// - `gateway_token`: `OPENCLAW_GATEWAY_TOKEN` — the token the subprocess
///   presents in its own protocol frames. The loopback proxy relays frames
///   verbatim and never checks it; the proxy only refuses browser handshakes
///   (see `proxy::handle_connection`).
pub async fn spawn(
    binary: PathBuf,
    proxy_port: u16,
    state_dir: PathBuf,
    private_key_pem: &str,
    public_key_pem: &str,
    gateway_token: &str,
) -> Result<SubprocessHandle, String> {
    crate::local_mcp::restrict_node_integrations(&state_dir)?;
    // 0. Retire the pre-8.1 node-host config file.
    retire_legacy_node_host_config(&state_dir)?;

    // 1. Write OPENCLAW_STATE_DIR/identity/device.json
    let identity_dir = state_dir.join("identity");
    std::fs::create_dir_all(&identity_dir)
        .map_err(|e| format!("create identity dir: {e}"))?;
    let identity_path = identity_dir.join("device.json");

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);

    // Upstream openclaw's `normalizeStoredIdentity`
    // (src/infra/device-identity.ts:128-161) requires
    // {version, deviceId, publicKeyPem, privateKeyPem}. A missing deviceId
    // makes the entry `recognized-invalid` → openclaw regenerates a fresh
    // keypair and overwrites this file, discarding the Keychain-derived
    // identity that the backend just paired.
    //
    // deviceId = sha256(raw_ed25519_pubkey_32_bytes).hex — same SPKI-stripped
    // hash the backend's `core/services/desktop_pair.derive_device_id` uses,
    // so the value here matches the device-pairing record on the server.
    let pubkey = VerifyingKey::from_public_key_pem(public_key_pem)
        .map_err(|e| format!("parse public key pem: {e}"))?;
    let raw_pub_bytes = pubkey.to_bytes();
    let mut hasher = Sha256::new();
    hasher.update(raw_pub_bytes);
    let device_id = hex::encode(hasher.finalize());

    let identity = serde_json::json!({
        "version": 1,
        "deviceId": device_id,
        "publicKeyPem": public_key_pem,
        "privateKeyPem": private_key_pem,
        "createdAtMs": now_ms,
    });
    std::fs::write(&identity_path, identity.to_string())
        .map_err(|e| format!("write identity: {e}"))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            &identity_path,
            std::fs::Permissions::from_mode(0o600),
        )
        .map_err(|e| format!("chmod identity: {e}"))?;
    }

    // 2. Spawn the subprocess. The bundled launcher shim
    // (`apps/desktop/src-tauri/scripts/vendor-sidecars.sh::write_launcher`)
    // is self-contained — its `exec` line already invokes
    // `openclaw node run --host 127.0.0.1 --port "$LIGHTBULB_PROXY_PORT"`,
    // so we must NOT pass those args here. Passing them duplicates the
    // subcommand and openclaw's commander parser bails with
    // "option '--port <port>' argument missing" (the second `node`
    // gets misread as the first `--port`'s value).
    let port_str = proxy_port.to_string();
    let mut cmd = Command::new(&binary);
    cmd.env("OPENCLAW_STATE_DIR", &state_dir)
        // Do not inherit a path to the user's personal OpenClaw config.
        .env("OPENCLAW_CONFIG_PATH", crate::local_mcp::config_path(&state_dir))
        .env("OPENCLAW_OAUTH_DIR", state_dir.join("credentials"))
        .env("OPENCLAW_GATEWAY_TOKEN", gateway_token)
        .env("LIGHTBULB_PROXY_PORT", &port_str)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("spawn subprocess: {e}"))?;

    // Forward child stdio line-by-line to /tmp/lightbulb-desktop.log so
    // openclaw's `node host gateway connect failed: ...` panic/error
    // traces are visible. Without this, child stderr disappears into
    // the void (Tauri GUI apps have no terminal attached on macOS).
    //
    // The forwarding tasks naturally exit when the child closes its
    // stdio handles (EOF on read_line). No explicit shutdown needed.
    if let Some(stdout) = child.stdout.take() {
        tokio::spawn(async move {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                log(&format!("[cli-subprocess-stdout] {line}"));
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        tokio::spawn(async move {
            let mut reader = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                log(&format!("[cli-subprocess] {line}"));
            }
        });
    }

    Ok(SubprocessHandle {
        child: Arc::new(Mutex::new(Some(child))),
    })
}

impl SubprocessHandle {
    /// Kill the subprocess. Idempotent — safe to call multiple times.
    pub async fn shutdown(self) {
        let mut guard = self.child.lock().await;
        if let Some(mut child) = guard.take() {
            let _ = child.kill().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::retire_legacy_node_host_config;

    #[tokio::test]
    async fn spawn_loads_only_the_restricted_app_private_config() {
        use ed25519_dalek::pkcs8::{EncodePrivateKey, EncodePublicKey};
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("app-state");
        let binary = dir.path().join("check-config");
        std::fs::write(
            &binary,
            "#!/bin/sh\ntest \"$OPENCLAW_CONFIG_PATH\" = \"$OPENCLAW_STATE_DIR/openclaw.json\" && test \"$OPENCLAW_OAUTH_DIR\" = \"$OPENCLAW_STATE_DIR/credentials\"\n",
        ).unwrap();
        std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o700)).unwrap();
        let key = ed25519_dalek::SigningKey::generate(&mut rand::rngs::OsRng);
        let handle = super::spawn(
            binary,
            12345,
            state.clone(),
            key.to_pkcs8_pem(Default::default()).unwrap().as_str(),
            &key.verifying_key().to_public_key_pem(Default::default()).unwrap(),
            "synthetic-test-token",
        ).await.unwrap();
        let mut child = handle.child.lock().await;
        assert!(child.as_mut().unwrap().wait().await.unwrap().success());
        let config: serde_json::Value = serde_json::from_slice(
            &std::fs::read(state.join("openclaw.json")).unwrap(),
        ).unwrap();
        assert_eq!(
            config["plugins"]["entries"]["anthropic"]["config"]["sessionCatalog"]["enabled"],
            false,
        );
        assert_eq!(
            config["plugins"]["entries"]["ollama"]["config"]["nodeInference"]["enabled"],
            false,
        );
    }

    /// The 8.1 sidecar refuses to start while either legacy file is present,
    /// and nothing self-heals it — so both must go, and re-running must be safe.
    #[test]
    fn removes_legacy_node_host_state_and_is_idempotent() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path();
        std::fs::write(state.join("node.json"), r#"{"version":1}"#).unwrap();
        std::fs::write(state.join("node.json.doctor-importing"), "").unwrap();
        // A file we must not touch.
        std::fs::write(state.join("openclaw.json"), "{}").unwrap();

        retire_legacy_node_host_config(state).expect("first pass");
        assert!(!state.join("node.json").exists());
        assert!(!state.join("node.json.doctor-importing").exists());
        assert!(state.join("openclaw.json").exists(), "unrelated config must survive");

        // Idempotent: a clean state dir is not an error.
        retire_legacy_node_host_config(state).expect("second pass");
    }
}
