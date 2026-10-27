//! Advisory local checkout evidence. No paths or file contents leave the desktop.
use super::*;
use sha2::{Digest, Sha256};
use tokio::io::AsyncReadExt;

const MAX_FILES: usize = 512;
const MAX_BYTES: u64 = 4 * 1024 * 1024;
const MAX_FILE: u64 = 1024 * 1024;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(super) struct State {
    root: Option<PathBuf>,
    digest: Option<String>,
    revision: u64,
    changed: bool,
    overlap: bool,
    known: bool,
}

impl State {
    pub(super) fn summary(&self) -> Value {
        json!({"revision":self.revision,"coverage":if self.known {"bounded"} else {"unknown"},
            "changed":self.changed,"overlap":self.overlap})
    }
    pub(super) fn root(&self) -> Option<&Path> {
        self.root.as_deref()
    }
    pub(super) fn observe(&mut self, result: Result<(PathBuf, String)>, overlap: bool) {
        self.overlap = overlap;
        match result {
            Ok((root, digest)) => {
                if self.digest.as_ref() != Some(&digest) || self.root.as_ref() != Some(&root) {
                    self.changed |= self.digest.is_some();
                    self.revision = self.revision.saturating_add(1);
                }
                self.root = Some(root);
                self.digest = Some(digest);
                self.known = true;
            }
            Err(_) => {
                self.known = false;
            }
        }
    }
    /// Empty when there is nothing to say: no change, no overlap, coverage known.
    pub(super) fn notice(&self) -> String {
        if !self.changed && !self.overlap && self.known {
            return String::new();
        }
        format!("[Lightbulb workspace advisory: {}{}{} Changes have external/unknown provenance. Re-read relevant files before continuing; this notice does not prove a refresh or prevent conflicts.]\n",
            if self.changed { "Checkout changes detected since the previous admitted notice. " } else { "" },
            if self.overlap { "Multiple shared sessions may overlap this checkout. " } else { "" },
            if self.known { "Ignored, sensitive, oversized and unsampled files are not covered." } else { "Workspace coverage is unknown." })
    }
    pub(super) fn acknowledge(&mut self, revision: u64) {
        if self.revision == revision {
            self.changed = false;
        }
    }
    pub(super) fn revision(&self) -> u64 {
        self.revision
    }
}

// Bounded stdout while the process runs (not an unbounded output() followed by a size check).
async fn output(program: &str, args: &[&str], cwd: &Path, limit: u64) -> Result<Vec<u8>> {
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        let mut child = tokio::process::Command::new(program)
            .args(args)
            .current_dir(cwd)
            .env_clear()
            .env("PATH", "/usr/bin:/bin:/usr/sbin")
            .env("HOME", "/nonexistent")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| "workspace unavailable")?;
        let mut bytes = vec![];
        child
            .stdout
            .take()
            .ok_or("workspace unavailable")?
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .await
            .map_err(|_| "workspace unavailable")?;
        if bytes.len() as u64 > limit {
            return Err("workspace observation bounded".into());
        }
        if !child
            .wait()
            .await
            .map_err(|_| "workspace unavailable")?
            .success()
        {
            return Err("workspace unavailable".into());
        }
        Ok(bytes)
    })
    .await
    .map_err(|_| "workspace observation timed out")?
}

pub(super) async fn git(cwd: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let mut fixed = vec![
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "diff.external=",
        "--no-optional-locks",
    ];
    fixed.extend(args);
    output("/usr/bin/git", &fixed, cwd, 65536).await
}

pub(super) async fn process_root(execution: &Process, home: &Path) -> Result<PathBuf> {
    if process(execution.pid)? != *execution {
        return Err("workspace process changed".into());
    }
    let bytes = output(
        "/usr/sbin/lsof",
        &["-a", "-p", &execution.pid.to_string(), "-d", "cwd", "-Fn"],
        Path::new("/"),
        8192,
    )
    .await?;
    if process(execution.pid)? != *execution {
        return Err("workspace process changed".into());
    }
    let value = std::str::from_utf8(&bytes).map_err(|_| "workspace unavailable")?;
    let paths: Vec<_> = value
        .lines()
        .filter_map(|line| line.strip_prefix('n'))
        .collect();
    if paths.len() != 1 {
        return Err("workspace unavailable".into());
    }
    let cwd = PathBuf::from(paths[0]);
    if cwd == home
        || cwd.starts_with(home.join(".codex"))
        || cwd.starts_with(home.join(".claude"))
        || cwd.starts_with(home.join(".openclaw"))
    {
        return Err("private workspace excluded".into());
    }
    validate_root(&cwd)?;
    Ok(cwd)
}

pub(super) async fn session_root(native: &NativeSession, home: &Path) -> Result<PathBuf> {
    if native.provider != "codex" || codex::daemon_program(home, &native.process).is_none() {
        return process_root(&native.process, home).await;
    }
    if process(native.process.pid)? != native.process {
        return Err("workspace process changed".into());
    }
    let cwd = codex::rollout_cwd(home, native)?;
    validate_root(&cwd)?;
    if process(native.process.pid)? != native.process {
        return Err("workspace process changed".into());
    }
    Ok(cwd)
}

fn validate_root(root: &Path) -> Result<()> {
    if !root.is_absolute()
        || root.parent().is_none()
        || fs::canonicalize(root).map_err(|_| "workspace unavailable")? != root
        || fs::symlink_metadata(root)
            .map_err(|_| "workspace unavailable")?
            .uid()
            != unsafe { libc::geteuid() }
    {
        return Err("unsafe workspace".into());
    }
    Ok(())
}

pub(super) async fn scan(cwd: &Path, home: &Path) -> Result<(PathBuf, String)> {
    validate_root(cwd)?;
    let root = PathBuf::from(
        String::from_utf8(git(cwd, &["rev-parse", "--show-toplevel"]).await?)
            .map_err(|_| "workspace unavailable")?
            .trim(),
    );
    validate_root(&root)?;
    if root == home
        || root.starts_with(home.join(".codex"))
        || root.starts_with(home.join(".claude"))
        || root.starts_with(home.join(".openclaw"))
    {
        return Err("private workspace excluded".into());
    }
    if !cwd.starts_with(&root) {
        return Err("workspace identity changed".into());
    }
    let mut hash = Sha256::new();
    hash.update(root.as_os_str().as_encoded_bytes());
    hash.update(
        git(
            cwd,
            &[
                "rev-parse",
                "--absolute-git-dir",
                "--git-common-dir",
                "HEAD",
            ],
        )
        .await?,
    );
    hash.update(git(cwd, &["ls-files", "--stage", "-z"]).await?);
    let files = git(
        &root,
        &[
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
    )
    .await?;
    // Up to 512 synchronous file reads: on the blocking pool, never on a runtime worker.
    tokio::task::spawn_blocking(move || hash_files(root, &files, hash))
        .await
        .map_err(|_| "workspace unavailable")?
}

fn hash_files(root: PathBuf, files: &[u8], mut hash: Sha256) -> Result<(PathBuf, String)> {
    let mut names: Vec<_> = files.split(|b| *b == 0).filter(|p| !p.is_empty()).collect();
    names.sort();
    names.dedup();
    let mut total = 0;
    for name in names.into_iter().take(MAX_FILES) {
        let name = std::str::from_utf8(name).map_err(|_| "workspace unavailable")?;
        let relative = Path::new(name);
        // Never read dotfiles, provider memory/auth or out-of-root paths, even if tracked.
        if relative.components().any(|p| !matches!(p, std::path::Component::Normal(n) if !n.to_string_lossy().starts_with('.')))
            || relative.components().any(|p| matches!(p.as_os_str().to_str(), Some("AGENTS.md" | "CLAUDE.md" | "MEMORY.md" | "auth.json" | "credentials.json"))) { continue; }
        let path = root.join(relative);
        hash.update(name.as_bytes());
        if !path.try_exists().map_err(|_| "workspace unavailable")? {
            hash.update(b"missing");
            continue;
        }
        let path = confined_path(&root, &path)?;
        let file = secure_file(&path)?;
        let before = file.metadata().map_err(|_| "workspace unavailable")?;
        if !before.is_file() || before.len() > MAX_FILE || total + before.len() > MAX_BYTES {
            hash.update(b"unsampled");
            continue;
        }
        let mut bytes = vec![];
        (&file)
            .take(MAX_FILE + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "workspace unavailable")?;
        let after = file.metadata().map_err(|_| "workspace unavailable")?;
        if bytes.len() as u64 != before.len()
            || before.mtime_nsec() != after.mtime_nsec()
            || before.mtime() != after.mtime()
        {
            return Err("workspace changed during observation".into());
        }
        total += bytes.len() as u64;
        hash.update(bytes);
    }
    Ok((root, format!("{:x}", hash.finalize())))
}

#[cfg(test)]
mod tests {
    use super::*;
    async fn scan(root: &Path) -> Result<(PathBuf, String)> {
        super::scan(root, Path::new("/nonexistent")).await
    }
    #[tokio::test]
    async fn local_sessions_workspace_rejects_tracked_fifo_without_blocking() {
        const CHILD_ROOT: &str = "LIGHTBULB_TEST_FIFO_ROOT";
        if let Some(root) = std::env::var_os(CHILD_ROOT) {
            assert!(scan(Path::new(&root)).await.is_err());
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q"]).await.unwrap();
        let path = root.join("tracked.txt");
        fs::write(&path, "ordinary file").unwrap();
        git(&root, &["add", "tracked.txt"]).await.unwrap();
        git(
            &root,
            &[
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        )
        .await
        .unwrap();
        assert!(scan(&root).await.is_ok());
        let mut text = String::new();
        secure_file(&path)
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        assert_eq!(text, "ordinary file");
        fs::remove_file(&path).unwrap();
        let name = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert_eq!(
            git(&root, &["ls-files", "--cached", "-z"]).await.unwrap(),
            b"tracked.txt\0"
        );
        // A blocking open defeats an async timer in the same runtime. Isolate the
        // real scan in a child test process, then kill AND reap it on RED timeout.
        let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "local_sessions::workspace::tests::local_sessions_workspace_rejects_tracked_fifo_without_blocking"])
            .env(CHILD_ROOT, &root).stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
            .kill_on_drop(true).spawn().unwrap();
        let completed = tokio::time::timeout(std::time::Duration::from_secs(5), child.wait()).await;
        let status = match completed {
            Ok(status) => Some(status.unwrap()),
            Err(_) => {
                child.kill().await.unwrap();
                child.wait().await.unwrap();
                None
            }
        };
        assert!(
            status.is_some_and(|s| s.success()),
            "tracked FIFO scan did not reject promptly; timed-out child was killed and reaped"
        );
    }
    #[tokio::test]
    async fn local_sessions_workspace_process_cwd_and_subprocess_output_are_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let mut child = tokio::process::Command::new("/bin/cat")
            .current_dir(&root)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let execution = process(child.id().unwrap()).unwrap();
        assert_eq!(
            process_root(&execution, Path::new("/nonexistent"))
                .await
                .unwrap(),
            root
        );
        assert!(process_root(&execution, &root).await.is_err());
        child.kill().await.unwrap();
        assert!(process_root(&execution, Path::new("/nonexistent"))
            .await
            .is_err());
        let started = std::time::Instant::now();
        assert!(output("/usr/bin/yes", &[], &root, 65536).await.is_err());
        assert!(started.elapsed() < std::time::Duration::from_secs(3));
    }
    #[tokio::test]
    async fn local_sessions_workspace_dirty_edits_stick_and_sensitive_symlinks_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        assert!(std::process::Command::new("/usr/bin/git")
            .args(["init", "-q"])
            .arg(&root)
            .status()
            .unwrap()
            .success());
        // Empty HEAD is unknown until an ordinary checkout exists.
        assert!(scan(&root).await.is_err());
        fs::write(root.join("work.txt"), "first").unwrap();
        assert!(std::process::Command::new("/usr/bin/git")
            .current_dir(&root)
            .args(["add", "work.txt"])
            .status()
            .unwrap()
            .success());
        assert!(std::process::Command::new("/usr/bin/git")
            .current_dir(&root)
            .args([
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "fixture"
            ])
            .status()
            .unwrap()
            .success());
        let mut state = State::default();
        state.observe(scan(&root).await, false);
        assert_eq!(state.summary()["changed"], false);
        fs::write(root.join("work.txt"), "dirty").unwrap();
        state.observe(scan(&root).await, true);
        let first = state.revision();
        fs::write(root.join("work.txt"), "dirty-again").unwrap();
        state.observe(scan(&root).await, true);
        assert!(state.revision() > first);
        state.observe(scan(&root).await, true);
        assert_eq!(state.summary()["changed"], true);
        state.acknowledge(first);
        assert_eq!(state.summary()["changed"], true);
        state.acknowledge(state.revision());
        assert_eq!(state.summary()["changed"], false);
        assert!(super::scan(&root, &root).await.is_err());
        let started = std::time::Instant::now();
        for index in 0..6 {
            fs::write(
                root.join(format!("bulk{index}")),
                vec![b'x'; MAX_FILE as usize],
            )
            .unwrap();
        }
        let capped = scan(&root).await.unwrap();
        fs::write(root.join("bulk5"), vec![b'y'; MAX_FILE as usize]).unwrap();
        assert_eq!(
            scan(&root).await.unwrap(),
            capped,
            "files beyond total byte ceiling are not read"
        );
        eprintln!("bounded workspace fixture: six 1MiB files, two scans in {:?}; each scan ceiling 4MiB/512 files", started.elapsed());
        fs::write(root.join(".env"), "not-read").unwrap();
        let before = scan(&root).await.unwrap();
        fs::write(root.join(".env"), "still-not-read").unwrap();
        assert_eq!(scan(&root).await.unwrap(), before);
        std::os::unix::fs::symlink("/etc/passwd", root.join("escape")).unwrap();
        assert!(scan(&root).await.is_err());
    }
}
