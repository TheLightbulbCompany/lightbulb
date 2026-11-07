//! One-click packaging of the user's `~/.claude` (Claude Code) context for
//! the marketplace import wizard — the desktop replacement for the manual
//! `tar czf` terminal step.
//!
//! Only the allowlisted slice is ever read or packaged: the root `CLAUDE.md`,
//! `projects/<slug>/memory/**/*.md`, and `skills/**`. Credentials, settings,
//! transcripts, and runtime state are excluded by never matching the
//! allowlist — there is deliberately no denylist to keep in sync. The same
//! allowlist lives in the backend normalizer
//! (`core/services/claude_code_import.py`) by design: Rust keeps secrets off
//! the wire; the server remains the guarantee.

use std::path::{Path, PathBuf};

use base64::Engine as _;
use flate2::write::GzEncoder;
use flate2::Compression;
use walkdir::WalkDir;

/// Caps mirror the backend normalizer (skip the file / abort the export).
const MAX_MD_BYTES: u64 = 1024 * 1024; // memory files + CLAUDE.md
const MAX_SKILL_FILE_BYTES: u64 = 5 * 1024 * 1024;
const MAX_MEMORY_FILES: u32 = 2000;
const MAX_TOTAL_BYTES: u64 = 200 * 1024 * 1024;

#[derive(Debug, serde::Serialize)]
pub struct ClaudeExport {
    /// gzip'd tar of the allowlisted slice, entries prefixed `.claude/`.
    pub base64: String,
    pub file_count: u32,
    /// Uncompressed bytes packaged.
    pub total_bytes: u64,
}

// Async so the tree-read + gzip + base64 (up to 200 MiB) never runs on the
// main thread; spawn_blocking keeps the sync packaging off the async workers.
#[tauri::command]
pub async fn export_claude_context() -> Result<ClaudeExport, String> {
    let home = std::env::var("HOME").map_err(|_| "HOME not set".to_string())?;
    let export = tauri::async_runtime::spawn_blocking(move || {
        package(&PathBuf::from(home).join(".claude"))
    })
    .await
    .map_err(|e| format!("export task failed: {e}"))??;
    crate::log(&format!(
        "[claude-export] packaged {} files ({} bytes)",
        export.file_count, export.total_bytes
    ));
    Ok(export)
}

/// True for a path segment the skills walk never descends into or packages:
/// dotfiles/dotdirs (which covers `.git` and `.DS_Store`) plus dependency
/// junk.
fn is_skipped_segment(name: &str) -> bool {
    name.starts_with('.') || name == "node_modules" || name == "__pycache__"
}

fn append_file(
    tar: &mut tar::Builder<GzEncoder<Vec<u8>>>,
    path: &Path,
    name: &str,
) -> Result<u64, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("read {}: {e}", path.display()))?;
    let mut header = tar::Header::new_gnu();
    header.set_size(bytes.len() as u64);
    header.set_mode(0o644);
    header.set_mtime(0);
    header.set_cksum();
    tar.append_data(&mut header, name, bytes.as_slice())
        .map_err(|e| format!("archive {}: {e}", path.display()))?;
    Ok(bytes.len() as u64)
}

/// Package the allowlisted slice under `root` (the command passes
/// `$HOME/.claude`; tests pass a temp tree). Symlinks are never followed —
/// only `file_type().is_file()` entries are packaged.
fn package(root: &Path) -> Result<ClaudeExport, String> {
    if !root.is_dir() {
        return Err("No Claude Code data found at ~/.claude on this Mac.".into());
    }

    let mut tar = tar::Builder::new(GzEncoder::new(Vec::new(), Compression::default()));
    let mut file_count: u32 = 0;
    let mut total_bytes: u64 = 0;
    let mut memory_files: u32 = 0;

    fn add(
        tar: &mut tar::Builder<GzEncoder<Vec<u8>>>,
        path: &Path,
        name: &str,
        size: u64,
        file_count: &mut u32,
        total_bytes: &mut u64,
    ) -> Result<(), String> {
        if *total_bytes + size > MAX_TOTAL_BYTES {
            return Err("Your Claude Code context is over the 200 MB import limit.".into());
        }
        *total_bytes += append_file(tar, path, name)?;
        *file_count += 1;
        Ok(())
    }

    // Root CLAUDE.md (global memory). symlink_metadata so a symlinked
    // CLAUDE.md is skipped, never followed.
    let claude_md = root.join("CLAUDE.md");
    if let Ok(md) = std::fs::symlink_metadata(&claude_md) {
        if md.is_file() && md.len() <= MAX_MD_BYTES {
            add(
                &mut tar,
                &claude_md,
                ".claude/CLAUDE.md",
                md.len(),
                &mut file_count,
                &mut total_bytes,
            )?;
        }
    }

    // projects/<slug>/memory/**/*.md — the auto-memory collections.
    let projects = root.join("projects");
    if projects.is_dir() {
        for entry in WalkDir::new(&projects).follow_links(false).sort_by_file_name() {
            let Ok(entry) = entry else { continue };
            if !entry.file_type().is_file() {
                continue;
            }
            let rel = entry
                .path()
                .strip_prefix(&projects)
                .expect("walkdir stays under its root")
                .to_string_lossy()
                .into_owned();
            let parts: Vec<&str> = rel.split('/').collect();
            if parts.len() < 3 || parts[1] != "memory" {
                continue;
            }
            if !parts.last().expect("non-empty").to_ascii_lowercase().ends_with(".md") {
                continue;
            }
            let Ok(md) = entry.metadata() else { continue };
            if md.len() > MAX_MD_BYTES {
                continue;
            }
            memory_files += 1;
            if memory_files > MAX_MEMORY_FILES {
                return Err(format!(
                    "Claude Code auto-memory exceeds the import limit ({MAX_MEMORY_FILES} files)."
                ));
            }
            add(
                &mut tar,
                entry.path(),
                &format!(".claude/projects/{rel}"),
                md.len(),
                &mut file_count,
                &mut total_bytes,
            )?;
        }
    }

    // skills/<name>/** — regular files only, junk segments pruned.
    let skills = root.join("skills");
    if skills.is_dir() {
        let walk = WalkDir::new(&skills)
            .follow_links(false)
            .sort_by_file_name()
            .into_iter()
            .filter_entry(|e| {
                e.path() == skills || !is_skipped_segment(&e.file_name().to_string_lossy())
            });
        for entry in walk {
            let Ok(entry) = entry else { continue };
            if !entry.file_type().is_file() {
                continue;
            }
            let rel = entry
                .path()
                .strip_prefix(&skills)
                .expect("walkdir stays under its root")
                .to_string_lossy()
                .into_owned();
            if !rel.contains('/') {
                continue; // a stray file directly under skills/ is not a skill
            }
            let Ok(md) = entry.metadata() else { continue };
            if md.len() > MAX_SKILL_FILE_BYTES {
                continue;
            }
            add(
                &mut tar,
                entry.path(),
                &format!(".claude/skills/{rel}"),
                md.len(),
                &mut file_count,
                &mut total_bytes,
            )?;
        }
    }

    if file_count == 0 {
        return Err("Nothing to import: no memory, CLAUDE.md, or skills found under ~/.claude.".into());
    }
    // The backend's claude detection is anchored on project memory — a
    // memory-less tar routes to the backup path and fails with a confusing
    // backup-shaped error, so refuse it here with the real reason.
    if memory_files == 0 {
        return Err(
            "Nothing to import: no Claude Code project memory found under ~/.claude/projects. \
             Memory is what the import carries; CLAUDE.md and skills ride along with it."
                .into(),
        );
    }

    let gz = tar.into_inner().map_err(|e| format!("archive finalize: {e}"))?;
    let compressed = gz.finish().map_err(|e| format!("gzip finalize: {e}"))?;
    Ok(ClaudeExport {
        base64: base64::engine::general_purpose::STANDARD.encode(compressed),
        file_count,
        total_bytes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::io::Read;

    fn write(root: &Path, rel: &str, contents: &[u8]) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, contents).unwrap();
    }

    /// Decode the export back into {entry name → contents}.
    fn entries(export: &ClaudeExport) -> HashMap<String, Vec<u8>> {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&export.base64)
            .unwrap();
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(bytes.as_slice()));
        archive
            .entries()
            .unwrap()
            .map(|e| {
                let mut e = e.unwrap();
                let name = e.path().unwrap().to_string_lossy().into_owned();
                let mut buf = Vec::new();
                e.read_to_end(&mut buf).unwrap();
                (name, buf)
            })
            .collect()
    }

    #[test]
    fn packages_only_the_allowlisted_slice_with_dot_claude_prefix() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "CLAUDE.md", b"# global");
        write(root, "projects/-Users-me-proj/memory/MEMORY.md", b"fact");
        write(root, "projects/-Users-me-proj/memory/notes/deep.MD", b"deep");
        write(root, "skills/graphify/SKILL.md", b"skill");
        write(root, "skills/graphify/bin/helper", b"bin");
        // Never packaged: fails the allowlist, not a denylist.
        write(root, ".credentials.json", b"secret");
        write(root, "settings.json", b"{}");
        write(root, "settings.local.json", b"{}");
        write(root, "projects/-Users-me-proj/chat.jsonl", b"transcript");
        write(root, "projects/-Users-me-proj/memory/raw.txt", b"not md");
        write(root, "commands/deploy.md", b"cmd");
        write(root, "todos/x.json", b"todo");
        write(root, "skills/graphify/node_modules/pkg/index.js", b"dep");
        write(root, "skills/graphify/.git/HEAD", b"ref");
        write(root, "skills/graphify/.env", b"KEY=1");
        write(root, "skills/stray.md", b"not inside a skill dir");

        let export = package(root).unwrap();
        let e = entries(&export);
        assert_eq!(e[".claude/CLAUDE.md"], b"# global");
        assert_eq!(e[".claude/projects/-Users-me-proj/memory/MEMORY.md"], b"fact");
        assert_eq!(e[".claude/projects/-Users-me-proj/memory/notes/deep.MD"], b"deep");
        assert_eq!(e[".claude/skills/graphify/SKILL.md"], b"skill");
        assert_eq!(e[".claude/skills/graphify/bin/helper"], b"bin");
        assert_eq!(e.len(), 5);
        assert_eq!(export.file_count, 5);
        assert_eq!(
            export.total_bytes,
            e.values().map(|v| v.len() as u64).sum::<u64>()
        );
    }

    #[test]
    fn symlinks_are_skipped_never_followed() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("claude");
        let outside = dir.path().join("outside");
        write(&outside, "leak.md", b"outside content");
        write(&root, "projects/p/memory/real.md", b"real");
        std::os::unix::fs::symlink(outside.join("leak.md"), root.join("projects/p/memory/link.md"))
            .unwrap();
        std::fs::create_dir_all(root.join("skills")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("skills/evil")).unwrap();

        let export = package(&root).unwrap();
        let e = entries(&export);
        assert_eq!(e.len(), 1);
        assert!(e.contains_key(".claude/projects/p/memory/real.md"));
    }

    #[test]
    fn oversized_files_are_silently_dropped() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "projects/p/memory/small.md", b"ok");
        write(root, "projects/p/memory/big.md", &vec![b'x'; (MAX_MD_BYTES + 1) as usize]);
        write(root, "skills/s/SKILL.md", b"ok");
        write(root, "skills/s/big.bin", &vec![b'x'; (MAX_SKILL_FILE_BYTES + 1) as usize]);

        let export = package(root).unwrap();
        let e = entries(&export);
        assert_eq!(e.len(), 2);
        assert!(e.contains_key(".claude/projects/p/memory/small.md"));
        assert!(e.contains_key(".claude/skills/s/SKILL.md"));
    }

    #[test]
    fn empty_slice_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "settings.json", b"{}");
        let err = package(dir.path()).unwrap_err();
        assert!(err.contains("no memory, CLAUDE.md, or skills"), "{err}");
    }

    #[test]
    fn memory_less_tree_is_an_error() {
        // CLAUDE.md + skills but zero project memory would package fine and
        // then dead-end server-side on the backup path — refused up front.
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "CLAUDE.md", b"# global");
        write(dir.path(), "skills/graphify/SKILL.md", b"skill");
        let err = package(dir.path()).unwrap_err();
        assert!(err.contains("no Claude Code project memory"), "{err}");
    }

    #[test]
    fn missing_dir_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = package(&dir.path().join("nope")).unwrap_err();
        assert!(err.starts_with("No Claude Code data found"), "{err}");
    }
}
