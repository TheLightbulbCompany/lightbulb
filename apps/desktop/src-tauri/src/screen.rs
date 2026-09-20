//! macOS `screen.snapshot` node-host handler.
//!
//! Captures the current screen via the macOS `screencapture(1)` CLI,
//! reads the resulting file, hashes + base64-encodes it, and returns
//! the upstream-compatible payload shape:
//!
//! ```text
//! { ok: true, format, size, mimeType, base64, sha256 }
//! ```
//!
//! `screen.snapshot` stays Mac-native because `screencapture` is
//! macOS-only — the rest of the file/dir handlers live in the openclaw
//! subprocess.

use std::path::PathBuf;

use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::{io::AsyncReadExt, process::Command, sync::watch};

// The native node uses the raw relay; base64 plus protocol overhead must fit
// the broker's 1 MiB result budget. Oversized images fail instead of truncating.
const MAX_IMAGE_BYTES: usize = 512 * 1024;

pub fn permission() -> &'static str {
    #[cfg(target_os = "macos")]
    {
        #[link(name = "CoreGraphics", kind = "framework")]
        extern "C" {
            fn CGPreflightScreenCaptureAccess() -> bool;
        }
        if unsafe { CGPreflightScreenCaptureAccess() } {
            "granted"
        } else {
            "denied"
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        "unsupported"
    }
}

struct TempImage(PathBuf);
impl Drop for TempImage {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn capture(
    mut command: Command,
    path: PathBuf,
    mut cancel: watch::Receiver<bool>,
    timeout: std::time::Duration,
) -> Result<Vec<u8>, String> {
    let _cleanup = TempImage(path.clone());
    if *cancel.borrow() {
        return Err("capture cancelled".into());
    }
    let mut child = command
        .kill_on_drop(true)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    let status = tokio::select! {
        biased;
        _ = cancel.changed() => None,
        _ = tokio::time::sleep(timeout) => None,
        status = child.wait() => Some(status),
    };
    let Some(status) = status else {
        let _ = child.kill().await;
        let _ = child.wait().await;
        return Err("capture cancelled or timed out".into());
    };
    if !status.map_err(|e| e.to_string())?.success() {
        return Err("capture failed".into());
    }
    if *cancel.borrow() {
        return Err("capture cancelled".into());
    }
    let mut bytes = Vec::new();
    tokio::fs::File::open(&path)
        .await
        .map_err(|e| e.to_string())?
        .take((MAX_IMAGE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .await
        .map_err(|e| e.to_string())?;
    if bytes.len() > MAX_IMAGE_BYTES {
        return Err("snapshot exceeds transport limit".into());
    }
    Ok(bytes)
}

fn err(code: &str, message: impl Into<String>) -> Value {
    json!({ "ok": false, "code": code, "message": message.into() })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScreenSnapshotParams {
    // `format`: png or jpg. Defaults to png.
    #[serde(default = "default_format")]
    format: String,
}

fn default_format() -> String {
    "png".to_string()
}

pub async fn handle_screen_snapshot(params: &Value, cancel: watch::Receiver<bool>) -> Value {
    if permission() != "granted" {
        return err(
            "PERMISSION_DENIED",
            "Screen Recording permission is required",
        );
    }
    let p: ScreenSnapshotParams = match serde_json::from_value(params.clone()) {
        Ok(p) => p,
        Err(_) => return err("INVALID_PARAMS", "invalid snapshot parameters"),
    };
    let format = match p.format.to_ascii_lowercase().as_str() {
        "jpg" | "jpeg" => "jpg",
        "png" => "png",
        _ => return err("INVALID_PARAMS", "format must be png or jpg"),
    };
    let tmp = match tempfile_path(format) {
        Some(p) => p,
        None => return err("READ_ERROR", "could not derive tempfile path"),
    };
    let mut command = Command::new("/usr/sbin/screencapture");
    command.args(["-x", "-t", format]).arg(&tmp);
    let bytes = match capture(command, tmp, cancel, std::time::Duration::from_secs(10)).await {
        Ok(bytes) => bytes,
        Err(e) => return err("CAPTURE_FAILED", e),
    };
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    let sha256_hex = format!("{:x}", hasher.finalize());
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    let mime = if format == "jpg" {
        "image/jpeg"
    } else {
        "image/png"
    };
    json!({
        "ok": true,
        "format": format,
        "size": bytes.len(),
        "mimeType": mime,
        "base64": b64,
        "sha256": sha256_hex,
    })
}

fn tempfile_path(ext: &str) -> Option<PathBuf> {
    let mut p = std::env::temp_dir();
    let unique = uuid::Uuid::new_v4().to_string();
    p.push(format!("lightbulb-snap-{}.{}", unique, ext));
    Some(p)
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn cancellation_before_spawn_and_during_capture_reaps_and_cleans() {
        let dir = tempfile::tempdir().unwrap();
        let output = dir.path().join("capture.png");
        let (tx, rx) = tokio::sync::watch::channel(true);
        let mut command = tokio::process::Command::new("/bin/sh");
        command.args(["-c", "exit 99"]);
        assert!(capture(command, output.clone(), rx, Duration::from_secs(1))
            .await
            .is_err());
        assert!(!output.exists());
        tx.send_replace(false);
        let rx = tx.subscribe();
        let mut command = tokio::process::Command::new("/bin/sh");
        command.args(["-c", "while :; do :; done"]);
        let task = tokio::spawn(capture(command, output.clone(), rx, Duration::from_secs(1)));
        tokio::task::yield_now().await;
        tx.send(true).unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_secs(2), task)
                .await
                .unwrap()
                .unwrap()
                .is_err()
        );
        assert!(!output.exists());
    }

    #[tokio::test]
    async fn oversized_capture_is_rejected_and_removed() {
        let dir = tempfile::tempdir().unwrap();
        let output = dir.path().join("capture.png");
        std::fs::write(&output, vec![0u8; MAX_IMAGE_BYTES + 1]).unwrap();
        let (_tx, rx) = tokio::sync::watch::channel(false);
        let result = capture(
            tokio::process::Command::new("/usr/bin/true"),
            output.clone(),
            rx,
            Duration::from_secs(1),
        )
        .await;
        assert!(result.is_err());
        assert!(!output.exists());
    }

    #[tokio::test]
    async fn capture_timeout_reaps_the_actual_child_and_removes_partial_image() {
        let dir = tempfile::tempdir().unwrap();
        let pid_file = dir.path().join("pid");
        let output = dir.path().join("partial.png");
        std::fs::write(&output, "partial").unwrap();
        let mut command = Command::new("/bin/sh");
        command
            .env("CAPTURE_PID", &pid_file)
            .args(["-c", "echo $$ > \"$CAPTURE_PID\"; while :; do :; done"]);
        let (_tx, rx) = watch::channel(false);
        assert!(
            capture(command, output.clone(), rx, Duration::from_millis(100))
                .await
                .is_err()
        );
        let pid: i32 = std::fs::read_to_string(pid_file)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert_eq!(
            unsafe { libc::kill(pid, 0) },
            -1,
            "capture must reap before reporting timeout"
        );
        assert!(!output.exists());
    }
}
