//! Rust port of OpenClaw's file-transfer node-host handlers
//! (`extensions/file-transfer/src/node-host/*.ts` upstream).
//!
//! Implements the four `file.*`/`dir.*` node commands. The agent invokes
//! them via the gateway, which forwards as `node.invoke` over WS; our
//! `node_invoke::dispatch` routes here. Each handler:
//!
//! 1. Validates params (path is absolute, no NUL bytes, etc.)
//! 2. Canonicalizes — symlink traversal is denied unless followSymlinks=true
//! 3. **Enforces the client-side allowlist** (`file_access::ensure_inside_allowlist`)
//!    — this is the user-chosen safety boundary; anything outside their
//!    selected folders fails with `PERMISSION_DENIED`
//! 4. Performs the operation under the per-op size cap (16 MB ceiling
//!    matches upstream)
//!
//! All responses are JSON-shaped to match upstream so the agent-side
//! tool wrappers (file_fetch, dir_list, etc.) work without modification.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::file_access::{self, FileAccessError};

/// Matches upstream FILE_FETCH_HARD_MAX_BYTES / FILE_WRITE_HARD_MAX_BYTES.
const HARD_MAX_BYTES: usize = 16 * 1024 * 1024;
const FETCH_DEFAULT_MAX_BYTES: usize = 8 * 1024 * 1024;

/// Common error → JSON envelope. Codes mirror upstream so agent-side
/// error handling works unchanged.
fn err(code: &str, message: impl Into<String>) -> Value {
    json!({ "ok": false, "code": code, "message": message.into() })
}

fn err_with_canonical(code: &str, message: impl Into<String>, canonical: &Path) -> Value {
    json!({
        "ok": false,
        "code": code,
        "message": message.into(),
        "canonicalPath": canonical.to_string_lossy(),
    })
}

fn classify_io(err: &std::io::Error) -> &'static str {
    use std::io::ErrorKind;
    match err.kind() {
        ErrorKind::NotFound => "NOT_FOUND",
        ErrorKind::PermissionDenied => "PERMISSION_DENIED",
        _ => "READ_ERROR",
    }
}

/// Resolve `params.path` to a canonical, allowlist-checked PathBuf.
/// Returns the canonical path on success, or a JSON error envelope to
/// pass straight through to the caller.
fn resolve_and_check(
    raw: &Value,
    follow_symlinks: bool,
) -> Result<PathBuf, Value> {
    let path_str = raw.as_str().ok_or_else(|| err("INVALID_PATH", "path required"))?;
    if path_str.is_empty() {
        return Err(err("INVALID_PATH", "path required"));
    }
    if path_str.contains('\0') {
        return Err(err("INVALID_PATH", "path contains NUL byte"));
    }
    if !Path::new(path_str).is_absolute() {
        return Err(err("INVALID_PATH", "path must be absolute"));
    }

    let raw_path = PathBuf::from(path_str);
    let canonical = match std::fs::canonicalize(&raw_path) {
        Ok(p) => p,
        Err(e) => {
            let code = classify_io(&e);
            return Err(err(code, format!("realpath failed: {}", e)));
        }
    };

    // Symlink redirect refused by default (parity with upstream behavior).
    // Without this, ~/Downloads/evil-symlink → /etc would let an agent
    // read outside the user's chosen folder even with a tight allowlist.
    if !follow_symlinks && canonical != raw_path {
        return Err(err_with_canonical(
            "SYMLINK_REDIRECT",
            "path traverses a symlink; set followSymlinks=true on this call to allow",
            &canonical,
        ));
    }

    // Allowlist gate — the user-managed safety boundary.
    if let Err(FileAccessError::OutsideAllowlist { canonical: c }) =
        file_access::ensure_inside_allowlist(&canonical)
    {
        return Err(err_with_canonical(
            "PERMISSION_DENIED",
            "path is outside the user-allowed folders; add it in Settings → File access",
            &c,
        ));
    }
    Ok(canonical)
}

fn clamp_max_bytes(input: Option<u64>, default: usize) -> usize {
    match input {
        None => default,
        Some(n) if n == 0 => default,
        Some(n) => (n as usize).min(HARD_MAX_BYTES),
    }
}

/// `file -b --mime-type` shell-out; falls back to extension table.
/// Matches upstream's detectMimeType behavior.
fn detect_mime_type(path: &Path) -> String {
    if let Ok(output) = std::process::Command::new("file")
        .args(["-b", "--mime-type"])
        .arg(path)
        .output()
    {
        if output.status.success() {
            let s = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !s.is_empty() {
                return s;
            }
        }
    }
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_default();
    match ext.as_str() {
        "txt" => "text/plain",
        "json" => "application/json",
        "md" => "text/markdown",
        "html" | "htm" => "text/html",
        "css" => "text/css",
        "js" => "text/javascript",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "pdf" => "application/pdf",
        _ => "application/octet-stream",
    }
    .to_string()
}

// ============================================================================
// file.fetch
// ============================================================================

#[derive(Deserialize)]
struct FileFetchParams {
    path: Value,
    #[serde(rename = "maxBytes", default)]
    max_bytes: Option<u64>,
    #[serde(rename = "followSymlinks", default)]
    follow_symlinks: bool,
    #[serde(rename = "preflightOnly", default)]
    preflight_only: bool,
}

pub async fn handle_file_fetch(params: &Value) -> Value {
    let p: FileFetchParams = match serde_json::from_value(params.clone()) {
        Ok(p) => p,
        Err(e) => return err("INVALID_PATH", format!("invalid params: {}", e)),
    };
    let canonical = match resolve_and_check(&p.path, p.follow_symlinks) {
        Ok(c) => c,
        Err(v) => return v,
    };
    let max_bytes = clamp_max_bytes(p.max_bytes, FETCH_DEFAULT_MAX_BYTES);

    let meta = match std::fs::metadata(&canonical) {
        Ok(m) => m,
        Err(e) => return err_with_canonical(classify_io(&e), format!("stat: {}", e), &canonical),
    };
    if meta.is_dir() {
        return err_with_canonical("IS_DIRECTORY", "path is a directory", &canonical);
    }
    if !meta.is_file() {
        return err_with_canonical("READ_ERROR", "path is not a regular file", &canonical);
    }
    let size = meta.len() as usize;
    if size > max_bytes {
        return err_with_canonical(
            "FILE_TOO_LARGE",
            format!("file size {} exceeds limit {}", size, max_bytes),
            &canonical,
        );
    }

    if p.preflight_only {
        return json!({
            "ok": true,
            "path": canonical.to_string_lossy(),
            "size": size,
            "mimeType": "",
            "base64": "",
            "sha256": "",
            "preflightOnly": true,
        });
    }

    let buffer = match std::fs::read(&canonical) {
        Ok(b) => b,
        Err(e) => return err_with_canonical(classify_io(&e), format!("read: {}", e), &canonical),
    };
    if buffer.len() > max_bytes {
        return err_with_canonical(
            "FILE_TOO_LARGE",
            format!("read {} bytes exceeds limit {}", buffer.len(), max_bytes),
            &canonical,
        );
    }

    let mut hasher = Sha256::new();
    hasher.update(&buffer);
    let sha256_hex = format!("{:x}", hasher.finalize());
    let b64 = base64::engine::general_purpose::STANDARD.encode(&buffer);

    json!({
        "ok": true,
        "path": canonical.to_string_lossy(),
        "size": buffer.len(),
        "mimeType": detect_mime_type(&canonical),
        "base64": b64,
        "sha256": sha256_hex,
    })
}

// ============================================================================
// file.write
// ============================================================================

#[derive(Deserialize)]
struct FileWriteParams {
    path: Value,
    base64: String,
    #[serde(rename = "createDirs", default)]
    create_dirs: bool,
    #[serde(rename = "overwrite", default = "default_overwrite")]
    overwrite: bool,
    #[serde(rename = "followSymlinks", default)]
    follow_symlinks: bool,
}

fn default_overwrite() -> bool {
    true
}

pub async fn handle_file_write(params: &Value) -> Value {
    let p: FileWriteParams = match serde_json::from_value(params.clone()) {
        Ok(p) => p,
        Err(e) => return err("INVALID_PATH", format!("invalid params: {}", e)),
    };
    let path_str = match p.path.as_str() {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return err("INVALID_PATH", "path required"),
    };
    if path_str.contains('\0') {
        return err("INVALID_PATH", "path contains NUL byte");
    }
    if !Path::new(&path_str).is_absolute() {
        return err("INVALID_PATH", "path must be absolute");
    }

    let decoded = match base64::engine::general_purpose::STANDARD.decode(p.base64.as_bytes()) {
        Ok(d) => d,
        Err(e) => return err("INVALID_BASE64", format!("base64 decode: {}", e)),
    };
    if decoded.len() > HARD_MAX_BYTES {
        return err(
            "FILE_TOO_LARGE",
            format!("write size {} exceeds limit {}", decoded.len(), HARD_MAX_BYTES),
        );
    }

    let raw_path = PathBuf::from(&path_str);

    // Allowlist check on the PARENT directory (canonicalized). The file
    // itself may not exist yet, so we can't canonicalize it directly.
    let parent = raw_path
        .parent()
        .ok_or_else(|| ())
        .and_then(|p| if p.as_os_str().is_empty() { Err(()) } else { Ok(p.to_path_buf()) });
    let Ok(parent_path) = parent else {
        return err("INVALID_PATH", "could not derive parent directory");
    };

    if p.create_dirs {
        if let Err(e) = std::fs::create_dir_all(&parent_path) {
            return err(classify_io(&e), format!("mkdir -p {}: {}", parent_path.display(), e));
        }
    }

    let canonical_parent = match std::fs::canonicalize(&parent_path) {
        Ok(p) => p,
        Err(e) => return err(classify_io(&e), format!("parent realpath: {}", e)),
    };
    if !p.follow_symlinks && canonical_parent != parent_path {
        return err_with_canonical(
            "SYMLINK_REDIRECT",
            "parent traverses a symlink",
            &canonical_parent,
        );
    }
    if let Err(FileAccessError::OutsideAllowlist { canonical: c }) =
        file_access::ensure_inside_allowlist(&canonical_parent)
    {
        return err_with_canonical(
            "PERMISSION_DENIED",
            "parent directory is outside user-allowed folders",
            &c,
        );
    }

    let target = canonical_parent.join(
        raw_path
            .file_name()
            .ok_or_else(|| ())
            .map(|n| n.to_owned())
            .unwrap_or_default(),
    );

    if !p.overwrite && target.exists() {
        return err_with_canonical("FILE_EXISTS", "file already exists and overwrite=false", &target);
    }

    if let Err(e) = std::fs::write(&target, &decoded) {
        return err_with_canonical(classify_io(&e), format!("write: {}", e), &target);
    }

    let mut hasher = Sha256::new();
    hasher.update(&decoded);
    let sha256_hex = format!("{:x}", hasher.finalize());

    json!({
        "ok": true,
        "path": target.to_string_lossy(),
        "size": decoded.len(),
        "sha256": sha256_hex,
    })
}

// ============================================================================
// dir.list
// ============================================================================

#[derive(Deserialize)]
struct DirListParams {
    path: Value,
    #[serde(rename = "followSymlinks", default)]
    follow_symlinks: bool,
}

pub async fn handle_dir_list(params: &Value) -> Value {
    let p: DirListParams = match serde_json::from_value(params.clone()) {
        Ok(p) => p,
        Err(e) => return err("INVALID_PATH", format!("invalid params: {}", e)),
    };
    let canonical = match resolve_and_check(&p.path, p.follow_symlinks) {
        Ok(c) => c,
        Err(v) => return v,
    };
    let meta = match std::fs::metadata(&canonical) {
        Ok(m) => m,
        Err(e) => return err_with_canonical(classify_io(&e), format!("stat: {}", e), &canonical),
    };
    if !meta.is_dir() {
        return err_with_canonical("NOT_A_DIRECTORY", "path is not a directory", &canonical);
    }

    let mut entries = Vec::new();
    let iter = match std::fs::read_dir(&canonical) {
        Ok(it) => it,
        Err(e) => return err_with_canonical(classify_io(&e), format!("readdir: {}", e), &canonical),
    };
    for entry in iter.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        let meta = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        let kind = if meta.is_dir() {
            "directory"
        } else if meta.is_file() {
            "file"
        } else if meta.file_type().is_symlink() {
            "symlink"
        } else {
            "other"
        };
        entries.push(json!({
            "name": name,
            "path": path.to_string_lossy(),
            "kind": kind,
            "size": meta.len(),
        }));
    }
    json!({
        "ok": true,
        "path": canonical.to_string_lossy(),
        "entries": entries,
    })
}

// ============================================================================
// dir.fetch  (tar.gz the whole directory, return base64)
// ============================================================================

#[derive(Deserialize)]
struct DirFetchParams {
    path: Value,
    #[serde(rename = "maxBytes", default)]
    max_bytes: Option<u64>,
    #[serde(rename = "followSymlinks", default)]
    follow_symlinks: bool,
}

pub async fn handle_dir_fetch(params: &Value) -> Value {
    let p: DirFetchParams = match serde_json::from_value(params.clone()) {
        Ok(p) => p,
        Err(e) => return err("INVALID_PATH", format!("invalid params: {}", e)),
    };
    let canonical = match resolve_and_check(&p.path, p.follow_symlinks) {
        Ok(c) => c,
        Err(v) => return v,
    };
    let meta = match std::fs::metadata(&canonical) {
        Ok(m) => m,
        Err(e) => return err_with_canonical(classify_io(&e), format!("stat: {}", e), &canonical),
    };
    if !meta.is_dir() {
        return err_with_canonical("NOT_A_DIRECTORY", "path is not a directory", &canonical);
    }
    let max_bytes = clamp_max_bytes(p.max_bytes, HARD_MAX_BYTES);

    // tar.gz into memory. Cap during write — abort if we exceed the
    // budget rather than allocating GB on a misconfigured root.
    let buffer: Vec<u8> = Vec::new();
    let mut gz = flate2::write::GzEncoder::new(buffer, flate2::Compression::default());
    {
        let mut tar = tar::Builder::new(&mut gz);
        for entry in walkdir::WalkDir::new(&canonical) {
            let Ok(entry) = entry else { continue };
            let entry_path = entry.path();
            // Symlinks in the walk are skipped unless followSymlinks is on.
            if !p.follow_symlinks && entry.path_is_symlink() {
                continue;
            }
            // Disallow escapes via symlinks even with followSymlinks: any
            // resolved target outside the canonical root is skipped.
            if let Ok(rp) = std::fs::canonicalize(entry_path) {
                if !file_access::is_inside(&rp, &canonical) {
                    continue;
                }
            }
            let Ok(rel) = entry_path.strip_prefix(&canonical) else {
                continue;
            };
            if rel.as_os_str().is_empty() {
                continue;
            }
            if entry.file_type().is_dir() {
                let _ = tar.append_dir(rel, entry_path);
            } else if entry.file_type().is_file() {
                let Ok(mut f) = std::fs::File::open(entry_path) else { continue };
                let mut buf = Vec::new();
                if f.read_to_end(&mut buf).is_err() {
                    continue;
                }
                if buf.len() > max_bytes {
                    return err_with_canonical(
                        "FILE_TOO_LARGE",
                        format!("entry {} exceeds budget {}", rel.display(), max_bytes),
                        &canonical,
                    );
                }
                let mut header = tar::Header::new_gnu();
                if header.set_path(rel).is_err() {
                    continue;
                }
                header.set_size(buf.len() as u64);
                header.set_mode(0o644);
                header.set_cksum();
                if tar.append(&header, buf.as_slice()).is_err() {
                    continue;
                }
            }
        }
        if tar.finish().is_err() {
            return err_with_canonical("READ_ERROR", "tar finalize failed", &canonical);
        }
    }
    let gzipped = match gz.finish() {
        Ok(b) => b,
        Err(e) => return err_with_canonical("READ_ERROR", format!("gz: {}", e), &canonical),
    };
    if gzipped.len() > max_bytes {
        return err_with_canonical(
            "FILE_TOO_LARGE",
            format!("tar.gz size {} exceeds limit {}", gzipped.len(), max_bytes),
            &canonical,
        );
    }
    let mut hasher = Sha256::new();
    hasher.update(&gzipped);
    let sha256_hex = format!("{:x}", hasher.finalize());
    let b64 = base64::engine::general_purpose::STANDARD.encode(&gzipped);

    json!({
        "ok": true,
        "path": canonical.to_string_lossy(),
        "size": gzipped.len(),
        "mimeType": "application/gzip",
        "base64": b64,
        "sha256": sha256_hex,
    })
}

// ============================================================================
// screen.snapshot  (bonus easy command; shells out to macOS screencapture)
// ============================================================================

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

// Re-export the unused-write-target to satisfy `cargo check` in case
// new callers don't yet exist for tar's Builder::finish path above.
#[allow(dead_code)]
fn _unused_write_marker(w: &mut impl Write) -> std::io::Result<()> {
    w.write_all(b"")
}
