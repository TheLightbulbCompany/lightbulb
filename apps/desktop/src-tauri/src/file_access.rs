//! User-managed allowlist of filesystem paths that openclaw agents can
//! read/write through file.fetch / file.write / dir.list / dir.fetch.
//!
//! The allowlist lives on disk at
//! `~/Library/Application Support/company.thelightbulb.desktop/file_access.json`.
//! On first launch we create `~/Lightbulb/` and add it to the allowlist so
//! agents have somewhere to read/write without the user thinking about
//! it; the user can add or remove paths via the Settings page (which
//! talks to this module through Tauri commands in `lib.rs`).
//!
//! Path-policy enforcement here is **client-side and authoritative**.
//! The backend's openclaw.json also carries an allowedPaths list as a
//! defense-in-depth layer, but the Rust handler is what actually
//! returns PERMISSION_DENIED. This keeps the user's chosen paths in
//! one place (their machine) and avoids a round-trip every time they
//! add or remove a folder.

use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use serde::{Deserialize, Serialize};

/// Current on-disk schema version. Bump when the JSON shape changes.
const STORE_VERSION: u32 = 1;

/// JSON shape persisted to ~/Library/Application Support.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct FileAccessStore {
    pub version: u32,
    pub allowed_paths: Vec<String>,
}

#[derive(Debug)]
pub enum FileAccessError {
    Io(std::io::Error),
    InvalidPath(String),
    OutsideAllowlist {
        canonical: PathBuf,
    },
    InvalidJson(serde_json::Error),
}

impl std::fmt::Display for FileAccessError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "io: {}", e),
            Self::InvalidPath(s) => write!(f, "invalid path: {}", s),
            Self::OutsideAllowlist { canonical } => write!(
                f,
                "path {} is outside the allowed folders; add it in Settings → Desktop → Approved folders",
                canonical.display()
            ),
            Self::InvalidJson(e) => write!(f, "json: {}", e),
        }
    }
}

impl From<std::io::Error> for FileAccessError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

impl From<serde_json::Error> for FileAccessError {
    fn from(e: serde_json::Error) -> Self {
        Self::InvalidJson(e)
    }
}

/// Returns the directory the store file lives in
/// (`~/Library/Application Support/company.thelightbulb.desktop/` on macOS —
/// the app's bundle identifier, matching where `app_data_dir()` puts
/// `pair_state.json`). Created if it doesn't exist yet. Defers to `std::env::var("HOME")` rather than the dirs
/// crate to keep dep surface tight.
fn store_dir() -> Result<PathBuf, FileAccessError> {
    let home = std::env::var("HOME")
        .map_err(|_| FileAccessError::InvalidPath("HOME not set".into()))?;
    let p = PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("company.thelightbulb.desktop");
    std::fs::create_dir_all(&p)?;
    Ok(p)
}

fn store_file() -> Result<PathBuf, FileAccessError> {
    Ok(store_dir()?.join("file_access.json"))
}

/// Default seed path. We mkdir it on first init so agents can immediately
/// drop files there; the user discovers it via the Settings page label
/// ("Default folder: ~/Lightbulb/").
pub fn default_seed_path() -> Result<PathBuf, FileAccessError> {
    let home = std::env::var("HOME")
        .map_err(|_| FileAccessError::InvalidPath("HOME not set".into()))?;
    Ok(PathBuf::from(home).join("Lightbulb"))
}

/// Resolve `~/foo` and relative paths to canonical absolute form. We
/// canonicalize so two equivalent path strings (with/without trailing
/// slash, symlink redirect, ..-traversal) collapse to one — and so the
/// `allowed_paths.contains_canonical()` check is robust against
/// adversarial inputs.
pub fn canonicalize(input: &str) -> Result<PathBuf, FileAccessError> {
    if input.is_empty() {
        return Err(FileAccessError::InvalidPath("empty".into()));
    }
    if input.contains('\0') {
        return Err(FileAccessError::InvalidPath("contains NUL".into()));
    }
    let expanded = if let Some(stripped) = input.strip_prefix("~/") {
        let home = std::env::var("HOME")
            .map_err(|_| FileAccessError::InvalidPath("HOME not set".into()))?;
        PathBuf::from(home).join(stripped)
    } else if input == "~" {
        let home = std::env::var("HOME")
            .map_err(|_| FileAccessError::InvalidPath("HOME not set".into()))?;
        PathBuf::from(home)
    } else {
        PathBuf::from(input)
    };
    std::fs::canonicalize(&expanded).map_err(FileAccessError::Io)
}

/// Returns true if `target` (which MUST already be canonical) sits
/// inside `prefix` (also canonical) or is exactly equal to it.
/// Component-by-component comparison so that `/Users/me/Iso` is NOT
/// treated as inside `/Users/me/Lightbulb`.
pub fn is_inside(target: &Path, prefix: &Path) -> bool {
    let mut t = target.components();
    for pc in prefix.components() {
        match t.next() {
            Some(tc) if tc == pc => continue,
            _ => return false,
        }
    }
    true
}

static STORE: OnceLock<Mutex<FileAccessStore>> = OnceLock::new();

/// Initializes the in-memory store from disk, creating the default
/// `~/Lightbulb/` seed entry on first run. Safe to call multiple times —
/// only the first call does I/O.
pub fn init() -> Result<(), FileAccessError> {
    if STORE.get().is_some() { return Ok(()); }
    let (initial, seed) = load_initial_store(&store_file()?)?;
    if STORE.set(Mutex::new(initial)).is_ok() && seed { ensure_default_seed()?; }
    Ok(())
}

fn load_initial_store(path: &Path) -> Result<(FileAccessStore, bool), FileAccessError> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok((FileAccessStore {
            version: STORE_VERSION,
            allowed_paths: Vec::new(),
        }, true)),
        Err(error) => return Err(error.into()),
    };
    Ok((serde_json::from_slice(&bytes)?, false))
}

fn save_to_disk(store: &FileAccessStore) -> Result<(), FileAccessError> {
    let p = store_file()?;
    let tmp = p.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(store)?;
    std::fs::write(&tmp, bytes)?;
    std::fs::rename(&tmp, &p)?;
    Ok(())
}

fn ensure_default_seed() -> Result<(), FileAccessError> {
    let seed = default_seed_path()?;
    std::fs::create_dir_all(&seed)?;
    let canonical_seed = std::fs::canonicalize(&seed)?;
    let store_arc = STORE.get().expect("init() before ensure_default_seed");
    let mut store = store_arc.lock().expect("store poisoned");
    let canonical_str = canonical_seed.to_string_lossy().to_string();
    if !store
        .allowed_paths
        .iter()
        .any(|p| Path::new(p) == canonical_seed.as_path())
    {
        store.allowed_paths.push(canonical_str);
        store.version = STORE_VERSION;
        save_to_disk(&store)?;
    }
    Ok(())
}

/// Public snapshot of the current allowlist. Used by the Tauri command
/// the Settings page invokes.
pub fn list_paths() -> Vec<String> {
    let Some(arc) = STORE.get() else {
        return Vec::new();
    };
    arc.lock().expect("store poisoned").allowed_paths.clone()
}

/// Current local standing consent; never seed or repair while answering a remote call.
pub fn execution_policy() -> Result<serde_json::Value, FileAccessError> {
    read_execution_policy(&store_file()?)
}

fn read_execution_policy(path: &Path) -> Result<serde_json::Value, FileAccessError> {
    use std::io::Read;
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    if !file.metadata()?.is_file() {
        return Err(FileAccessError::InvalidPath("policy must be a regular file".into()));
    }
    let mut bytes = Vec::new();
    file.take(65537).read_to_end(&mut bytes)?;
    if bytes.len() > 65536 {
        return Err(FileAccessError::InvalidPath("policy too large".into()));
    }
    policy_from_store(&serde_json::from_slice(&bytes)?)
}

fn policy_from_store(store: &FileAccessStore) -> Result<serde_json::Value, FileAccessError> {
    if store.version != STORE_VERSION || store.allowed_paths.is_empty() || store.allowed_paths.len() > 64 {
        return Err(FileAccessError::InvalidPath("no current folder consent".into()));
    }
    for path in &store.allowed_paths {
        let root = Path::new(path);
        if !root.is_absolute() || root.parent().is_none() || !root.is_dir()
            || std::fs::canonicalize(root)? != root {
            return Err(FileAccessError::InvalidPath("approved root changed".into()));
        }
    }
    Ok(serde_json::json!({"readRoots": store.allowed_paths, "writeRoots": store.allowed_paths, "denyRoots": []}))
}

/// Adds `path` to the allowlist (canonicalized). Returns the canonical
/// form on success. Idempotent.
pub fn add_path(input: &str) -> Result<String, FileAccessError> {
    let canonical = canonicalize(input)?;
    let canonical_str = canonical.to_string_lossy().to_string();
    let arc = STORE.get().ok_or(FileAccessError::InvalidPath("uninitialized store".into()))?;
    let mut store = arc.lock().expect("store poisoned");
    if !store
        .allowed_paths
        .iter()
        .any(|p| Path::new(p) == canonical.as_path())
    {
        store.allowed_paths.push(canonical_str.clone());
        store.version = STORE_VERSION;
        save_to_disk(&store)?;
        crate::log(&format!("[file-access] added {}", canonical_str));
    }
    Ok(canonical_str)
}

/// Removes `path` (matched by canonical equality). Returns true if
/// something was removed.
pub fn remove_path(input: &str) -> Result<bool, FileAccessError> {
    let canonical = canonicalize(input)?;
    let arc = STORE.get().ok_or(FileAccessError::InvalidPath("uninitialized store".into()))?;
    let mut store = arc.lock().expect("store poisoned");
    let before = store.allowed_paths.len();
    store
        .allowed_paths
        .retain(|p| Path::new(p) != canonical.as_path());
    let changed = store.allowed_paths.len() != before;
    if changed {
        save_to_disk(&store)?;
        crate::log(&format!("[file-access] removed {}", canonical.display()));
    }
    Ok(changed)
}

/// Ensures `target` (already canonicalized) sits inside at least one
/// allowed root. Returns Ok(()) on success or OutsideAllowlist with the
/// canonical target so the caller can include it in the error message.
pub fn ensure_inside_allowlist(target: &Path) -> Result<(), FileAccessError> {
    let arc = STORE.get().ok_or(FileAccessError::InvalidPath("uninitialized store".into()))?;
    let store = arc.lock().expect("store poisoned");
    for root in &store.allowed_paths {
        if is_inside(target, Path::new(root)) {
            return Ok(());
        }
    }
    Err(FileAccessError::OutsideAllowlist {
        canonical: target.to_path_buf(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_policy_uses_current_grants_and_rejects_swapped_roots() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("allowed");
        std::fs::create_dir(&root).unwrap();
        let canonical = std::fs::canonicalize(&root).unwrap().to_string_lossy().to_string();
        let store = FileAccessStore { version: 1, allowed_paths: vec![canonical.clone()] };
        let policy = policy_from_store(&store).unwrap();
        assert_eq!(policy["readRoots"], serde_json::json!([canonical]));
        assert_eq!(policy["writeRoots"], policy["readRoots"]);
        let policy_file = dir.path().join("policy.json");
        std::fs::write(&policy_file, serde_json::to_vec(&store).unwrap()).unwrap();
        assert_eq!(read_execution_policy(&policy_file).unwrap(), policy);
        std::fs::write(&policy_file, r#"{"version":1,"allowed_paths":[]}"#).unwrap();
        assert!(read_execution_policy(&policy_file).is_err());
        std::fs::write(&policy_file, "malformed").unwrap();
        assert!(read_execution_policy(&policy_file).is_err());
        assert!(policy_from_store(&FileAccessStore { version: 1, allowed_paths: vec![] }).is_err());
        std::fs::remove_dir(&root).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(dir.path(), &root).unwrap();
        assert!(policy_from_store(&store).is_err());
    }

    #[test]
    fn restart_does_not_restore_revoked_or_corrupt_folder_grants() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("file_access.json");
        assert!(load_initial_store(&path).unwrap().1);
        std::fs::write(&path, r#"{"version":1,"allowed_paths":[]}"#).unwrap();
        let (store, seed) = load_initial_store(&path).unwrap();
        assert!(!seed);
        assert!(store.allowed_paths.is_empty());
        std::fs::write(&path, "corrupt").unwrap();
        assert!(load_initial_store(&path).is_err());
    }

    #[test]
    fn is_inside_basics() {
        let root = PathBuf::from("/Users/me/Lightbulb");
        assert!(is_inside(&PathBuf::from("/Users/me/Lightbulb"), &root));
        assert!(is_inside(&PathBuf::from("/Users/me/Lightbulb/a"), &root));
        assert!(is_inside(&PathBuf::from("/Users/me/Lightbulb/a/b"), &root));
        // Sibling prefix doesn't match (the killer test — component-wise).
        assert!(!is_inside(&PathBuf::from("/Users/me/Iso"), &root));
        assert!(!is_inside(&PathBuf::from("/Users/me/Lightbulb2"), &root));
        // Completely outside.
        assert!(!is_inside(&PathBuf::from("/etc/passwd"), &root));
        // Empty target.
        assert!(!is_inside(&PathBuf::from(""), &root));
    }

    #[test]
    fn canonicalize_rejects_nul() {
        assert!(canonicalize("/tmp/with\0byte").is_err());
    }

    #[test]
    fn canonicalize_rejects_empty() {
        assert!(canonicalize("").is_err());
    }
}
