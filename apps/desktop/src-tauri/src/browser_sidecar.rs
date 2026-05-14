//! Subprocess supervisor for the OpenClaw node-host running on the
//! user's Mac. The launcher at bin/isol8-browser-service-<triple>
//! invokes `node openclaw.mjs node run --port 18789`, which serves
//! the browser control HTTP API on 18789+2 = 18791 (per
//! src/config/port-defaults.ts:deriveDefaultBrowserControlPort in
//! openclaw@v2026.4.5). Port is deterministic so we don't parse stdout.

use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

/// Gateway port we pass to `openclaw node run --port`. Must match
/// what the launcher shim in scripts/vendor-sidecars.sh sets.
pub const GATEWAY_PORT: u16 = 18789;

/// Browser control HTTP port. OpenClaw derives this as
/// `gatewayPort + 2` in src/config/port-defaults.ts. Pinning the
/// gateway port pins this.
pub const BROWSER_CONTROL_PORT: u16 = GATEWAY_PORT + 2;

pub struct BrowserSidecar {
    binary: PathBuf,
    args: Vec<String>,
    child: Arc<Mutex<Option<Child>>>,
}

impl BrowserSidecar {
    pub fn new_for_test(binary: PathBuf, args: Vec<String>) -> Self {
        Self {
            binary,
            args,
            child: Arc::new(Mutex::new(None)),
        }
    }

    pub async fn start(&self) -> Result<(), String> {
        let mut guard = self.child.lock().await;
        // A stored Child is not proof of life — a crashed subprocess
        // leaves the handle `Some` even though the process is gone.
        // Probe try_wait() and clear the slot so the next block respawns.
        if let Some(existing) = guard.as_mut() {
            match existing.try_wait() {
                Ok(None) => return Ok(()), // still running
                Ok(Some(status)) => {
                    crate::log(&format!(
                        "[browser-sidecar] previous child exited ({status}); respawning"
                    ));
                    *guard = None;
                }
                Err(e) => {
                    crate::log(&format!(
                        "[browser-sidecar] try_wait failed ({e}); respawning"
                    ));
                    *guard = None;
                }
            }
        }
        let mut child = Command::new(&self.binary)
            .args(&self.args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| format!("spawn failed: {}", e))?;

        if let Some(out) = child.stdout.take() {
            tokio::spawn(async move {
                let reader = BufReader::new(out);
                let mut lines = reader.lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    crate::log(&format!("[browser-sidecar] {}", line));
                }
            });
        }
        if let Some(err) = child.stderr.take() {
            tokio::spawn(async move {
                let reader = BufReader::new(err);
                let mut lines = reader.lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    crate::log(&format!("[browser-sidecar err] {}", line));
                }
            });
        }

        *guard = Some(child);
        Ok(())
    }

    pub fn port(&self) -> u16 {
        BROWSER_CONTROL_PORT
    }
}

impl BrowserSidecar {
    /// Production constructor: resolves the externalBin sidecar path.
    ///
    /// Tauri's externalBin convention: configured as
    /// `bin/isol8-browser-service` in tauri.conf.json, the bundler
    /// finds `bin/isol8-browser-service-<target-triple>` at BUILD time,
    /// strips the triple suffix, and places the file in the .app at
    /// `Contents/MacOS/isol8-browser-service` (no suffix). So at
    /// RUNTIME the file we exec is the un-suffixed base name.
    ///
    /// Dev builds (`cargo tauri dev`) replicate the same layout —
    /// `target/debug/isol8-browser-service` next to the main binary.
    ///
    /// We previously appended the triple at runtime ("…-aarch64-apple-
    /// darwin") because the universal-apple-darwin build kept it; the
    /// per-arch matrix introduced in desktop-build.yml strips it via
    /// Tauri's standard externalBin handling.
    pub fn for_app(_app: &tauri::AppHandle) -> Result<Self, String> {
        let exe = std::env::current_exe()
            .map_err(|e| format!("current_exe: {}", e))?;
        let parent = exe
            .parent()
            .ok_or_else(|| "current_exe has no parent".to_string())?;
        let binary = parent.join("isol8-browser-service");
        if !binary.exists() {
            return Err(format!(
                "sidecar binary not found at {} (arch={})",
                binary.display(),
                std::env::consts::ARCH,
            ));
        }
        Ok(Self {
            binary,
            args: vec![],
            child: std::sync::Arc::new(tokio::sync::Mutex::new(None)),
        })
    }
}

/// Shared-state handle passed through Tauri's `.manage()`. The inner
/// Option is None until the first browser.proxy invoke spawns the
/// sidecar.
pub type BrowserSidecarHandle =
    std::sync::Arc<tokio::sync::RwLock<Option<BrowserSidecar>>>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_port_derives_from_gateway() {
        assert_eq!(BROWSER_CONTROL_PORT, GATEWAY_PORT + 2);
    }
}
