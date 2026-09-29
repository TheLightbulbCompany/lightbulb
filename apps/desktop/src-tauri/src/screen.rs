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
// the broker's 1 MiB result budget. Oversized images are normalized, never truncated.
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

/// Marker in app data: this install already raised the macOS prompt once.
const REQUESTED_MARKER: &str = "screen-access-requested";
static ASKED_THIS_LAUNCH: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// What to do when Screen is granted in `desktop_permissions.json` but macOS
/// has not allowed it. `CGRequestScreenCaptureAccess` returns false whether or
/// not it showed a prompt, and macOS shows it only while nobody has answered,
/// so the marker is how we know: first time ask, after that open the pane.
#[derive(Debug, PartialEq)]
enum Ask {
    None,
    Prompt,
    OpenSettings,
}

fn next_ask(has_access: bool, requested_before: bool) -> Ask {
    match (has_access, requested_before) {
        (true, _) => Ask::None,
        (false, false) => Ask::Prompt,
        (false, true) => Ask::OpenSettings,
    }
}

/// Screen was just granted (Settings switch turned on): ask macOS now.
pub fn request_access(app_data: &std::path::Path) {
    ASKED_THIS_LAUNCH.store(true, std::sync::atomic::Ordering::SeqCst);
    let marker = app_data.join(REQUESTED_MARKER);
    match next_ask(permission() == "granted", marker.exists()) {
        Ask::None => {}
        Ask::Prompt => {
            let _ = std::fs::create_dir_all(app_data);
            let _ = std::fs::write(&marker, b"");
            #[cfg(target_os = "macos")]
            {
                #[link(name = "CoreGraphics", kind = "framework")]
                extern "C" {
                    fn CGRequestScreenCaptureAccess() -> bool;
                }
                let granted = unsafe { CGRequestScreenCaptureAccess() };
                crate::log(&format!("[screen] requested screen recording access granted={granted}"));
            }
        }
        Ask::OpenSettings => {
            crate::log("[screen] screen recording still off; opening its Settings pane");
            let _ = std::process::Command::new("/usr/bin/open")
                .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
                .spawn();
        }
    }
}

/// Launch and every (re-)pairing: at most one ask per launch, never a loop.
pub fn request_access_once(app_data: &std::path::Path) {
    if !ASKED_THIS_LAUNCH.swap(true, std::sync::atomic::Ordering::SeqCst) {
        request_access(app_data);
    }
}

struct TempImage(PathBuf);
impl Drop for TempImage {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn capture(
    command: Command,
    path: PathBuf,
    mut cancel: watch::Receiver<bool>,
    timeout: std::time::Duration,
) -> Result<Vec<u8>, String> {
    let _cleanup = TempImage(path.clone());
    let normalized = path.with_extension("bounded.jpg");
    let _normalized_cleanup = TempImage(normalized.clone());
    let deadline = tokio::time::Instant::now() + timeout;
    run_capture_process(command, &mut cancel, deadline).await?;
    let mut selected = path.clone();
    if tokio::fs::metadata(&path)
        .await
        .map_err(|e| e.to_string())?
        .len()
        > MAX_IMAGE_BYTES as u64
    {
        // Keep desktop text readable at the first size that fits. JPEG handles
        // detailed/photo-heavy screens much better than shrinking a large PNG.
        for size in ["1600", "1200", "800"] {
            let mut normalize = Command::new("/usr/bin/sips");
            normalize
                .args([
                    "-Z",
                    size,
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "65",
                ])
                .arg(&path)
                .arg("--out")
                .arg(&normalized);
            run_capture_process(normalize, &mut cancel, deadline).await?;
            selected = normalized.clone();
            if tokio::fs::metadata(&selected)
                .await
                .map_err(|e| e.to_string())?
                .len()
                <= MAX_IMAGE_BYTES as u64
            {
                break;
            }
        }
    }
    if *cancel.borrow() || tokio::time::Instant::now() >= deadline {
        return Err("capture cancelled or timed out".into());
    }
    let mut bytes = Vec::new();
    tokio::fs::File::open(&selected)
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

async fn run_capture_process(
    mut command: Command,
    cancel: &mut watch::Receiver<bool>,
    deadline: tokio::time::Instant,
) -> Result<(), String> {
    if *cancel.borrow() {
        return Err("capture cancelled".into());
    }
    if tokio::time::Instant::now() >= deadline {
        return Err("capture timed out".into());
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
        _ = tokio::time::sleep_until(deadline) => None,
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
    Ok(())
}

fn snapshot_command(path: &std::path::Path, format: &str) -> Command {
    let mut command = Command::new("/usr/sbin/screencapture");
    // A single explicit display produces exactly the owned output path.
    command.args(["-x", "-D", "1", "-t", format]).arg(path);
    command
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
    let command = snapshot_command(&tmp, format);
    let bytes = match capture(command, tmp, cancel, std::time::Duration::from_secs(10)).await {
        Ok(bytes) => bytes,
        Err(e) => return err("CAPTURE_FAILED", e),
    };
    // Normalization may turn a requested PNG into JPEG; report the actual bytes.
    let format = if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        "jpg"
    } else {
        "png"
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

    #[test]
    fn screen_access_prompts_once_then_points_at_settings() {
        assert_eq!(next_ask(true, false), Ask::None);
        assert_eq!(next_ask(true, true), Ask::None);
        assert_eq!(next_ask(false, false), Ask::Prompt);
        assert_eq!(next_ask(false, true), Ask::OpenSettings);
    }

    #[test]
    fn snapshot_selects_exactly_the_primary_display() {
        let command = snapshot_command(std::path::Path::new("/tmp/owned.png"), "png");
        let args: Vec<_> = command
            .as_std()
            .get_args()
            .map(|a| a.to_str().unwrap())
            .collect();
        assert_eq!(args, ["-x", "-D", "1", "-t", "png", "/tmp/owned.png"]);
    }

    #[tokio::test]
    async fn retina_desktop_fixture_becomes_a_usable_bounded_image_and_cleans_outputs() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("desktop.bmp");
        let output = dir.path().join("capture.png");
        // 4K desktop: dark sidebar, fine horizontal text rows, and a large
        // detailed image panel. Runtime fixture avoids a checked-in megabyte blob.
        let (width, height) = (3840usize, 2160usize);
        let mut bmp = vec![0u8; 54 + width * height * 3];
        let len = bmp.len() as u32;
        bmp[..2].copy_from_slice(b"BM");
        for (offset, value) in [
            (2, len),
            (10, 54),
            (14, 40),
            (18, width as u32),
            (22, height as u32),
        ] {
            bmp[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        }
        bmp[26..28].copy_from_slice(&1u16.to_le_bytes());
        bmp[28..30].copy_from_slice(&24u16.to_le_bytes());
        let mut noise = 1u32;
        for y in 0..height {
            for x in 0..width {
                let pixel = 54 + (y * width + x) * 3;
                noise = noise.wrapping_mul(1664525).wrapping_add(1013904223);
                let rgb = if x < 640 {
                    [40, 40, 40]
                } else if x < 3200 && y < 1600 {
                    [noise as u8, (noise >> 8) as u8, (noise >> 16) as u8]
                } else if y % 32 < 3 {
                    [50, 50, 50]
                } else {
                    [245, 245, 245]
                };
                bmp[pixel..pixel + 3].copy_from_slice(&rgb);
            }
        }
        std::fs::write(&source, bmp).unwrap();
        assert!(std::process::Command::new("/usr/bin/sips")
            .args(["-s", "format", "png"])
            .arg(&source)
            .arg("--out")
            .arg(&output)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap()
            .success());
        assert!(std::fs::metadata(&output).unwrap().len() > MAX_IMAGE_BYTES as u64);
        let (_tx, rx) = watch::channel(false);
        let bytes = capture(
            Command::new("/usr/bin/true"),
            output.clone(),
            rx,
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        assert!(bytes.len() <= MAX_IMAGE_BYTES);
        assert!(
            bytes.starts_with(&[0xff, 0xd8, 0xff]),
            "oversized PNG falls back to JPEG"
        );
        assert!(!output.exists());
        assert!(!output.with_extension("bounded.jpg").exists());
        let normalized = dir.path().join("result.jpg");
        std::fs::write(&normalized, bytes).unwrap();
        let dimensions = std::process::Command::new("/usr/bin/sips")
            .args(["-g", "pixelWidth", "-g", "pixelHeight"])
            .arg(normalized)
            .output()
            .unwrap();
        let dimensions = String::from_utf8(dimensions.stdout).unwrap();
        assert!(dimensions.contains("pixelWidth: 1600"), "{dimensions}");
        assert!(dimensions.contains("pixelHeight: 900"), "{dimensions}");
    }

    #[tokio::test]
    async fn cancellation_before_spawn_and_during_capture_reaps_and_cleans() {
        let dir = tempfile::tempdir().unwrap();
        let output = dir.path().join("capture.png");
        let (tx, rx) = tokio::sync::watch::channel(true);
        std::fs::write(&output, "partial").unwrap();
        std::fs::write(output.with_extension("bounded.jpg"), "partial").unwrap();
        let mut command = tokio::process::Command::new("/bin/sh");
        command.args(["-c", "exit 99"]);
        assert!(capture(command, output.clone(), rx, Duration::from_secs(1))
            .await
            .is_err());
        assert!(!output.exists());
        assert!(!output.with_extension("bounded.jpg").exists());
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
        std::fs::write(output.with_extension("bounded.jpg"), "partial").unwrap();
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
        assert!(!output.with_extension("bounded.jpg").exists());
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
