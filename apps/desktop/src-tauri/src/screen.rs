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

fn err(code: &str, message: impl Into<String>) -> Value {
    json!({ "ok": false, "code": code, "message": message.into() })
}

fn classify_io(err: &std::io::Error) -> &'static str {
    use std::io::ErrorKind;
    match err.kind() {
        ErrorKind::NotFound => "NOT_FOUND",
        ErrorKind::PermissionDenied => "PERMISSION_DENIED",
        _ => "READ_ERROR",
    }
}

#[derive(Deserialize)]
struct ScreenSnapshotParams {
    // `format`: png or jpg. Defaults to png.
    #[serde(default = "default_format")]
    format: String,
}

fn default_format() -> String {
    "png".to_string()
}

pub async fn handle_screen_snapshot(params: &Value) -> Value {
    let p: ScreenSnapshotParams =
        serde_json::from_value(params.clone()).unwrap_or(ScreenSnapshotParams { format: "png".into() });
    let format = match p.format.to_ascii_lowercase().as_str() {
        "jpg" | "jpeg" => "jpg",
        _ => "png",
    };
    let tmp = match tempfile_path(format) {
        Some(p) => p,
        None => return err("READ_ERROR", "could not derive tempfile path"),
    };
    let status = match std::process::Command::new("screencapture")
        .args(["-x", "-t", format])
        .arg(&tmp)
        .status()
    {
        Ok(s) => s,
        Err(e) => return err("READ_ERROR", format!("screencapture: {}", e)),
    };
    if !status.success() {
        let _ = std::fs::remove_file(&tmp);
        return err("READ_ERROR", format!("screencapture exited {}", status));
    }
    let bytes = match std::fs::read(&tmp) {
        Ok(b) => b,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return err(classify_io(&e), format!("read snapshot: {}", e));
        }
    };
    let _ = std::fs::remove_file(&tmp);
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    let sha256_hex = format!("{:x}", hasher.finalize());
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    let mime = if format == "jpg" { "image/jpeg" } else { "image/png" };
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
    p.push(format!("isol8-snap-{}.{}", unique, ext));
    Some(p)
}
