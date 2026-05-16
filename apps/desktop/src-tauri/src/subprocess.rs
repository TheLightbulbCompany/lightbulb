//! Supervises the bundled `openclaw node run` subprocess.
//!
//! Spawns the launcher shim (which forwards to the real openclaw binary)
//! with the env vars + CLI args the host expects, plus writes the keypair
//! identity file to disk so openclaw can pick it up on startup.
//!
//! The identity file at `<state_dir>/identity/device.json` is not the
//! source of truth — that's the macOS Keychain (see `keychain.rs`,
//! Task 16). This file is rewritten on every spawn from the in-memory
//! keypair passed in, and chmod'd to 0o600 so other users on the host
//! can't read the private key.

use std::path::PathBuf;
use std::sync::Arc;
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

/// Handle to a running openclaw subprocess. Drop kills the child via
/// `kill_on_drop(true)` set at spawn time, but call `shutdown()` for an
/// orderly stop where possible.
pub struct SubprocessHandle {
    child: Arc<Mutex<Option<Child>>>,
}

/// Write the identity file and spawn the openclaw subprocess.
///
/// Args:
/// - `binary`: path to the launcher shim (typically the bundled
///   `openclaw-host` binary inside the Tauri app bundle).
/// - `proxy_port`: localhost port of the loopback WSS proxy (Task 18).
/// - `state_dir`: `OPENCLAW_STATE_DIR` — where openclaw stores its on-disk
///   state. The identity file is written under here.
/// - `private_key_pem` / `public_key_pem`: Ed25519 keypair from the
///   macOS Keychain (Task 16). Serialized into `identity/device.json`.
/// - `gateway_token`: `OPENCLAW_GATEWAY_TOKEN` — pre-shared token the
///   proxy validates the subprocess with before forwarding to the
///   real gateway.
pub async fn spawn(
    binary: PathBuf,
    proxy_port: u16,
    state_dir: PathBuf,
    private_key_pem: &str,
    public_key_pem: &str,
    gateway_token: &str,
) -> Result<SubprocessHandle, String> {
    // 1. Write OPENCLAW_STATE_DIR/identity/device.json
    let identity_dir = state_dir.join("identity");
    std::fs::create_dir_all(&identity_dir)
        .map_err(|e| format!("create identity dir: {e}"))?;
    let identity_path = identity_dir.join("device.json");

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let identity = serde_json::json!({
        "version": 1,
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

    // 2. Spawn the subprocess
    let port_str = proxy_port.to_string();
    let mut cmd = Command::new(&binary);
    cmd.env("OPENCLAW_STATE_DIR", &state_dir)
        .env("OPENCLAW_GATEWAY_TOKEN", gateway_token)
        .env("ISOL8_PROXY_PORT", &port_str)
        .args([
            "node",
            "run",
            "--host",
            "127.0.0.1",
            "--port",
            &port_str,
        ])
        .kill_on_drop(true);

    let child = cmd
        .spawn()
        .map_err(|e| format!("spawn subprocess: {e}"))?;

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
