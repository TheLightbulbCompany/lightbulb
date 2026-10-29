//! Pick up a teammate's Claude Code session on this Mac: their conversation, resumed,
//! and their working tree (unpushed commits, uncommitted edits, deletions, untracked
//! files) in a fresh git worktree beside the receiver's own checkout.
//!
//! The ticket comes from the web view, whose content is remote: every URL, id and
//! title in it is untrusted. URLs are checked against the owner-state bucket before
//! any request, every download is size- and sha256-verified, nothing is passed to a
//! shell, and the receiver's own checkout (index, branch, working tree) is never
//! touched -- the import happens in `git worktree add`.
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::Duration;
use tauri::Manager;

/// The per-kind caps the backend enforces on upload, enforced again here on the way in.
pub(crate) const PART_MAX: u64 = 64 * 1024 * 1024;
pub(crate) const CODE_MAX: u64 = 200 * 1024 * 1024;
pub(crate) const SIDECARS_MAX: u64 = 200 * 1024 * 1024;
const TOTAL_MAX: u64 = 1024 * 1024 * 1024;
/// Decompressed ceilings: a transcript's largest measured was 85 MB.
const TRANSCRIPT_RAW_MAX: u64 = 1024 * 1024 * 1024;
const SIDECARS_RAW_MAX: u64 = 1024 * 1024 * 1024;
const KEEP: Duration = Duration::from_secs(7 * 24 * 3600);

pub(crate) type Failure = Value;

fn fail(code: &str, message: &str) -> Failure {
    json!({"code": code, "message": message})
}

// ---- shared with the author side (local_sessions::snapshot) ----

/// Run `/usr/bin/git` with explicit argv, never a shell. Keeps the user's own HOME and
/// global config on purpose: their global excludes decide what `git add -A` leaves out,
/// and `fetch origin` needs their credentials. Hooks and fsmonitor are off; git never
/// prompts. Returns (exited 0, stdout).
pub(crate) async fn git_run(
    cwd: &Path,
    args: &[&str],
    index: Option<&Path>,
    timeout: Duration,
) -> Result<(bool, Vec<u8>), String> {
    let mut command = tokio::process::Command::new("/usr/bin/git");
    command
        .args([
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
        ])
        .args(args)
        .current_dir(cwd)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        .env_remove("GIT_INDEX_FILE")
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    if let Some(index) = index {
        command.env("GIT_INDEX_FILE", index);
    }
    let out = tokio::time::timeout(timeout, command.output())
        .await
        .map_err(|_| "git timed out".to_string())?
        .map_err(|_| "git unavailable".to_string())?;
    Ok((out.status.success(), out.stdout))
}

/// `/usr/bin/git` with none of the person's credentials, for what anyone may read: no global or
/// system config, no credential helper, no prompt, and none of the environment overrides that
/// would put config back (`git_run` keeps the user's config on purpose; this must not).
pub(crate) fn anonymous_git(cwd: &Path, args: &[&str]) -> tokio::process::Command {
    let mut command = tokio::process::Command::new("/usr/bin/git");
    command
        .args(["-c", "credential.helper="])
        .args(args)
        .current_dir(cwd)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ASKPASS", "/usr/bin/false")
        .env("SSH_ASKPASS", "/usr/bin/false")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    for key in [
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_SSL_NO_VERIFY",
        "GIT_PROXY_COMMAND",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
    ] {
        command.env_remove(key);
    }
    for (key, _) in std::env::vars_os() {
        if key
            .to_str()
            .is_some_and(|k| k.starts_with("GIT_CONFIG_KEY_") || k.starts_with("GIT_CONFIG_VALUE_"))
        {
            command.env_remove(key);
        }
    }
    command
}

pub(crate) async fn git(cwd: &Path, args: &[&str], index: Option<&Path>) -> Result<String, String> {
    match git_run(cwd, args, index, Duration::from_secs(300)).await? {
        (true, out) => Ok(String::from_utf8_lossy(&out).trim_end().to_string()),
        (false, _) => Err(format!("git {} failed", args.first().unwrap_or(&""))),
    }
}

/// `host/org/repo` from https, ssh:// and scp-form remotes, so two clones of one repo
/// match however each was cloned.
pub(crate) fn normalize_remote(raw: &str) -> Option<String> {
    let raw = raw.trim();
    let rest = match raw.split_once("://") {
        Some((_, rest)) => rest.to_string(),
        None => {
            // scp form: [user@]host:org/repo
            let (host, path) = raw.split_once(':')?;
            if host.contains('/') {
                return None;
            }
            format!("{host}/{path}")
        }
    };
    let (host, path) = rest.split_once('/')?;
    let host = host.rsplit('@').next()?;
    let host = host.split(':').next()?.to_ascii_lowercase();
    let path = path.trim_end_matches('/');
    let path = path
        .strip_suffix(".git")
        .unwrap_or(path)
        .trim_end_matches('/');
    (!host.is_empty() && !path.is_empty()).then(|| format!("{host}/{path}"))
}

/// A ticket's remote: the author's Mac already reduced it to `host/org/repo`, which
/// `normalize_remote` refuses (a bare path is a relative local origin there). Ticket-only.
fn ticket_remote(raw: &str) -> Option<String> {
    normalize_remote(raw).or_else(|| normalize_remote(&format!("https://{}", raw.trim())))
}

/// Presigned S3 URLs for the owner-state bucket, under the backend-only prefix. Anything
/// else in a ticket is refused before a single request is made.
pub(crate) fn allowed_url(raw: &str) -> bool {
    let Ok(url) = url::Url::parse(raw) else {
        return false;
    };
    url.scheme() == "https"
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.host_str().is_some_and(bucket_host)
        // Copies of shared sessions live under `session-copies/<copyId>/` (spec 2026-10-05).
        && (url.path().starts_with("/session-pickup/") || url.path().starts_with("/session-copies/"))
}

fn bucket_host(host: &str) -> bool {
    [
        "lightbulb-dev-openclaw-replicas.s3",
        "lightbulb-prod-openclaw-replicas.s3",
    ]
    .iter()
    .any(|bucket| {
        // `.s3.amazonaws.com`, `.s3.us-east-1.amazonaws.com`, `.s3-us-east-1.amazonaws.com`,
        // `.s3.dualstack.us-east-1.amazonaws.com` -- and nothing after `amazonaws.com`.
        host.strip_prefix(bucket)
            .and_then(|rest| rest.strip_suffix(".amazonaws.com"))
            .is_some_and(|region| {
                region.is_empty()
                    || ((region.starts_with('.') || region.starts_with('-'))
                        && region[1..].split('.').all(|label| {
                            !label.is_empty()
                                && label.bytes().all(|b| {
                                    b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'
                                })
                        }))
            })
    })
}

// ---- receiver ----

fn version(text: &str) -> Option<Vec<u64>> {
    text.split_whitespace()
        .next()?
        .split('.')
        .map(|p| p.parse().ok())
        .collect()
}

/// Strictly older, compared numerically component by component (`2.1.99 < 2.1.283`).
pub(crate) fn older(installed: &str, required: &str) -> bool {
    match (version(installed), version(required)) {
        (Some(have), Some(need)) => have < need,
        (None, Some(_)) => true,
        (_, None) => false,
    }
}

/// A teammate controls the session title: reduce it to `[a-z0-9-]` before it names a branch.
pub(crate) fn slug(title: &str) -> String {
    let mut out = String::new();
    for c in title.chars().flat_map(char::to_lowercase) {
        if out.len() >= 40 {
            break;
        }
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.is_empty() && !out.ends_with('-') {
            out.push('-');
        }
    }
    let out = out.trim_matches('-').to_string();
    if out.is_empty() {
        "session".into()
    } else {
        out
    }
}

fn safe_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

fn uuid_ok(id: &str) -> bool {
    uuid::Uuid::parse_str(id).is_ok()
}

pub(crate) fn hex_sha(s: &str) -> bool {
    matches!(s.len(), 40 | 64) && s.bytes().all(|b| b.is_ascii_hexdigit())
}

#[derive(Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Download {
    #[serde(default)]
    pub(crate) index: u64,
    pub(crate) url: String,
    pub(crate) bytes: u64,
    pub(crate) sha256: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Downloads {
    pub(crate) transcript: Vec<Download>,
    pub(crate) code: Option<Download>,
    pub(crate) sidecars: Option<Download>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ManifestCode {
    remote: String,
    snapshot: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Manifest {
    claude_version: String,
    conversation_id: String,
    #[serde(default)]
    cwd_relative: String,
    code: Option<ManifestCode>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct TicketSession {
    session_id: String,
    #[serde(default)]
    title: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Ticket {
    pickup_id: String,
    session: TicketSession,
    manifest: Manifest,
    downloads: Downloads,
}

fn parse_ticket(value: Value) -> Result<Ticket, Failure> {
    let invalid = |m: &str| fail("INVALID_TICKET", m);
    let ticket: Ticket =
        serde_json::from_value(value).map_err(|_| invalid("The pick-up ticket is malformed."))?;
    if !safe_id(&ticket.pickup_id)
        || !uuid_ok(&ticket.session.session_id)
        || !uuid_ok(&ticket.manifest.conversation_id)
    {
        return Err(invalid("The pick-up ticket is malformed."));
    }
    let d = &ticket.downloads;
    if d.transcript
        .iter()
        .chain(&d.code)
        .chain(&d.sidecars)
        .any(|p| !allowed_url(&p.url))
    {
        return Err(fail(
            "URL_REFUSED",
            "The ticket names a download this app will not fetch.",
        ));
    }
    if !downloads_ok(d)
        || d.code.is_some() != ticket.manifest.code.is_some()
        || ticket
            .manifest
            .code
            .as_ref()
            .is_some_and(|c| !hex_sha(&c.snapshot) || ticket_remote(&c.remote).is_none())
    {
        return Err(invalid("The pick-up ticket is malformed."));
    }
    Ok(ticket)
}

/// Every URL in the bucket's pick-up path, digests well formed, transcript non-empty and in
/// order, each part and the total under their caps. Shared by pick-up tickets and copy tickets.
pub(crate) fn downloads_ok(d: &Downloads) -> bool {
    let all: Vec<&Download> = d
        .transcript
        .iter()
        .chain(&d.code)
        .chain(&d.sidecars)
        .collect();
    let total = all.iter().fold(0u64, |sum, p| sum.saturating_add(p.bytes));
    all.iter().all(|p| {
        allowed_url(&p.url)
            && p.sha256.len() == 64
            && p.sha256.bytes().all(|b| b.is_ascii_hexdigit())
    }) && !d.transcript.is_empty()
        && d.transcript.iter().all(|p| p.bytes <= PART_MAX)
        && d.transcript
            .iter()
            .enumerate()
            .all(|(i, p)| p.index == i as u64)
        && d.code.as_ref().is_none_or(|c| c.bytes <= CODE_MAX)
        && d.sidecars.as_ref().is_none_or(|s| s.bytes <= SIDECARS_MAX)
        && total <= TOTAL_MAX
}

/// Size and sha256 of a file on disk against the ticket. Blocking: run off the runtime.
fn matches(path: &Path, part: &Download) -> bool {
    let Ok(mut file) = std::fs::File::open(path) else {
        return false;
    };
    if file.metadata().map(|m| m.len()).ok() != Some(part.bytes) {
        return false;
    }
    let mut hash = Sha256::new();
    std::io::copy(&mut file, &mut hash).is_ok()
        && hex::encode(hash.finalize()) == part.sha256.to_ascii_lowercase()
}

async fn download(client: &reqwest::Client, part: &Download, path: &Path) -> Result<(), Failure> {
    let (owned, check) = (path.to_path_buf(), part.clone());
    if tokio::task::spawn_blocking(move || matches(&owned, &check))
        .await
        .unwrap_or(false)
    {
        return Ok(());
    }
    let failed = || {
        fail(
            "DOWNLOAD_FAILED",
            "A part of the session could not be downloaded.",
        )
    };
    let mut response = client.get(&part.url).send().await.map_err(|_| failed())?;
    // Redirects are off, so a 3xx lands here too.
    if !response.status().is_success() {
        return Err(failed());
    }
    use tokio::io::AsyncWriteExt;
    let mut file = tokio::fs::File::create(path).await.map_err(|_| failed())?;
    let mut hash = Sha256::new();
    let mut seen = 0u64;
    while let Some(chunk) = response.chunk().await.map_err(|_| failed())? {
        seen += chunk.len() as u64;
        if seen > part.bytes {
            return Err(fail(
                "DOWNLOAD_MISMATCH",
                "A downloaded part did not match the ticket.",
            ));
        }
        hash.update(&chunk);
        file.write_all(&chunk).await.map_err(|_| failed())?;
    }
    file.flush().await.map_err(|_| failed())?;
    if seen != part.bytes || hex::encode(hash.finalize()) != part.sha256.to_ascii_lowercase() {
        return Err(fail(
            "DOWNLOAD_MISMATCH",
            "A downloaded part did not match the ticket.",
        ));
    }
    Ok(())
}

/// Extract a sidecar archive: relative paths only, no `..`, regular files and
/// directories only (no links or devices), and a ceiling on the total.
fn extract(archive: impl Read, dest: &Path, cap: u64) -> Result<(), String> {
    let mut archive = tar::Archive::new(archive);
    let mut total = 0u64;
    for entry in archive.entries().map_err(|_| "unreadable archive")? {
        let mut entry = entry.map_err(|_| "unreadable archive")?;
        let path = entry.path().map_err(|_| "unreadable archive")?.into_owned();
        if path.as_os_str().is_empty()
            || !path.components().all(|c| matches!(c, Component::Normal(_)))
        {
            return Err("archive path refused".into());
        }
        let target = dest.join(&path);
        match entry.header().entry_type() {
            tar::EntryType::Directory => {
                std::fs::create_dir_all(&target).map_err(|_| "extract failed")?;
            }
            tar::EntryType::Regular => {
                total += entry.header().size().map_err(|_| "unreadable archive")?;
                if total > cap {
                    return Err("archive too large".into());
                }
                if let Some(parent) = target.parent() {
                    std::fs::create_dir_all(parent).map_err(|_| "extract failed")?;
                }
                let mut out = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&target)
                    .map_err(|_| "extract failed")?;
                std::io::copy(&mut (&mut entry).take(cap), &mut out)
                    .map_err(|_| "extract failed")?;
            }
            _ => return Err("archive entry type refused".into()),
        }
    }
    Ok(())
}

/// Candidate clones: approved folders and their immediate children that are the top of a
/// git work tree whose `origin` normalises to `remote`.
async fn candidates(approved: &[PathBuf], remote: &str) -> Vec<PathBuf> {
    let mut dirs = vec![];
    for root in approved {
        dirs.push(root.clone());
        if let Ok(entries) = std::fs::read_dir(root) {
            dirs.extend(entries.flatten().map(|e| e.path()).filter(|p| p.is_dir()));
        }
    }
    let mut out: Vec<PathBuf> = vec![];
    for dir in dirs {
        let Ok(dir) = std::fs::canonicalize(&dir) else {
            continue;
        };
        let top = git(&dir, &["rev-parse", "--show-toplevel"], None).await;
        if !top.is_ok_and(|t| Path::new(&t) == dir) {
            continue;
        }
        let origin = git(&dir, &["remote", "get-url", "origin"], None).await;
        if origin.ok().and_then(|o| normalize_remote(&o)).as_deref() == Some(remote)
            && !out.contains(&dir)
        {
            out.push(dir);
        }
    }
    out
}

/// Lay the snapshot into a new worktree of `repo`. The snapshot commit's parent is the
/// author's real HEAD, so the worktree is created at `<ref>^` and the snapshot tree is
/// restored over it WITHOUT touching the index: their uncommitted edits stay uncommitted,
/// deletions stay deletions, new files stay untracked. Returns (branch, worktree).
///
/// Idempotent per pickup id: a retry after a failed launch reuses the worktree it made
/// (see `reusable`) and never restores over it again -- the person may already have
/// worked in it.
/// Keep `.claude/worktrees/` out of the repository's status: add it to `info/exclude` unless
/// something already ignores it.
pub(crate) async fn exclude_worktrees(repo: &Path) -> Result<(), String> {
    let unwritable = || "The repository's exclude file could not be written.".to_string();
    if git_run(
        repo,
        &["check-ignore", "-q", ".claude/worktrees"],
        None,
        Duration::from_secs(30),
    )
    .await
    .map_err(|_| "git is unavailable.".to_string())?
    .0
    {
        return Ok(());
    }
    let exclude = git(repo, &["rev-parse", "--git-path", "info/exclude"], None)
        .await
        .map_err(|_| "The repository could not be read.".to_string())?;
    let exclude = repo.join(exclude);
    if let Some(parent) = exclude.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&exclude)
        .map_err(|_| unwritable())?;
    writeln!(file, "\n.claude/worktrees/").map_err(|_| unwritable())
}

pub(crate) async fn import_code(
    repo: &Path,
    bundle: &Path,
    sid: &str,
    pickup_id: &str,
    title: &str,
    snapshot: &str,
) -> Result<(String, PathBuf), Failure> {
    let reference = format!("refs/lightbulb/pickup/{pickup_id}");
    let short: String = pickup_id
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .take(6)
        .collect::<String>()
        .to_ascii_lowercase();
    let name = format!("{}-{short}", slug(title));
    let branch = format!("pickup/{name}");
    let worktree = repo
        .join(".claude/worktrees")
        .join(format!("pickup-{name}"));
    if worktree.exists() {
        if reusable(repo, &worktree, &reference, snapshot).await {
            return Ok((branch, worktree));
        }
        return Err(
            json!({"code":"WORKTREE_EXISTS","message":"This session was already picked up here.",
            "worktree": worktree}),
        );
    }
    let bundle_arg = bundle.to_string_lossy();
    if git(repo, &["bundle", "verify", &bundle_arg], None)
        .await
        .is_err()
    {
        // Missing prerequisites: the receiver has not fetched what the author pushed.
        let _ = git_run(repo, &["fetch", "origin"], None, Duration::from_secs(120)).await;
        if git(repo, &["bundle", "verify", &bundle_arg], None)
            .await
            .is_err()
        {
            return Err(fail(
                "BASE_MISSING",
                "This clone is missing commits the session is based on. Fetch or pull, then try again.",
            ));
        }
    }
    let failed = |m: &str| fail("IMPORT_FAILED", m);
    git(
        repo,
        &[
            "fetch",
            &bundle_arg,
            &format!("refs/lightbulb/snap/{sid}:{reference}"),
        ],
        None,
    )
    .await
    .map_err(|_| failed("The session's changes could not be read."))?;
    if !resolves_to(repo, &reference, snapshot).await {
        return Err(failed("The session's changes did not match the ticket."));
    }
    exclude_worktrees(repo).await.map_err(|m| failed(&m))?;
    let tree = worktree.to_string_lossy();
    git(
        repo,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            &branch,
            &tree,
            &format!("{reference}^"),
        ],
        None,
    )
    .await
    .map_err(|_| failed("A worktree could not be created."))?;
    // From here on this attempt owns a worktree, a branch and a ref: a failure removes
    // all three, so a retry starts clean instead of reusing a half-made worktree.
    let restored = format!("{reference}-restored");
    let finished = async {
        #[cfg(test)]
        if FAIL_RESTORE.with(|f| f.get()) {
            return Err(());
        }
        git(
            &worktree,
            &[
                "restore",
                &format!("--source={reference}"),
                "--worktree",
                "--",
                ".",
            ],
            None,
        )
        .await
        .map_err(|_| ())?;
        // "Restored" is a recorded fact, and the reuse rule requires it.
        git(repo, &["update-ref", &restored, snapshot], None)
            .await
            .map_err(|_| ())
    }
    .await;
    if finished.is_err() {
        for args in [
            vec!["worktree", "remove", "--force", &*tree],
            vec!["branch", "-D", &branch],
            vec!["update-ref", "-d", &reference],
            vec!["update-ref", "-d", &restored],
        ] {
            if git(repo, &args, None).await.is_err() {
                crate::log(&format!(
                    "[session-pickup] cleanup step failed: git {}",
                    args[0]
                ));
            }
        }
        return Err(failed("The session's changes could not be applied."));
    }
    Ok((branch, worktree))
}

#[cfg(test)]
thread_local! {
    /// Makes the restore step fail, to test the cleanup after `worktree add`.
    static FAIL_RESTORE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

async fn resolves_to(repo: &Path, rev: &str, commit: &str) -> bool {
    git(
        repo,
        &["rev-parse", "--verify", "-q", &format!("{rev}^{{commit}}")],
        None,
    )
    .await
    .is_ok_and(|c| c.eq_ignore_ascii_case(commit))
}

/// An existing worktree is this pick-up's own, and safe to reuse, only when the pickup
/// ref AND its `-restored` marker both name the ticket's snapshot, git has the path
/// registered as a worktree of this repo, and its HEAD is still the snapshot's parent.
async fn reusable(repo: &Path, worktree: &Path, reference: &str, snapshot: &str) -> bool {
    if !resolves_to(repo, reference, snapshot).await
        || !resolves_to(repo, &format!("{reference}-restored"), snapshot).await
    {
        return false;
    }
    let listed = git(repo, &["worktree", "list", "--porcelain"], None)
        .await
        .unwrap_or_default();
    let real = std::fs::canonicalize(worktree).unwrap_or_else(|_| worktree.to_path_buf());
    if !listed
        .lines()
        .filter_map(|l| l.strip_prefix("worktree "))
        .any(|p| Path::new(p) == real || Path::new(p) == worktree)
    {
        return false;
    }
    match git(repo, &["rev-parse", &format!("{reference}^")], None).await {
        Ok(parent) => resolves_to(worktree, "HEAD", &parent).await,
        Err(_) => false,
    }
}

/// The exact argv the holder runs: a fixed binary, fixed flags, values as their own elements.
pub(crate) fn argv(claude: &Path, transcript: &Path, session: &str) -> Vec<String> {
    vec![
        claude.to_string_lossy().into_owned(),
        "--resume".into(),
        transcript.to_string_lossy().into_owned(),
        "--fork-session".into(),
        "--session-id".into(),
        session.into(),
        // Only the receiver's own user settings: a teammate's `.claude/settings*.json` in
        // the restored tree must not run hooks on this Mac before anyone has seen it.
        "--setting-sources".into(),
        "user".into(),
    ]
}

/// Where Claude starts: `<worktree>/<cwdRelative>` only if it resolves to a directory
/// still inside the worktree (a symlink in the author's tree must not lead out of it).
pub(crate) fn workdir(worktree: &Path, relative: &str) -> PathBuf {
    let root = std::fs::canonicalize(worktree).unwrap_or_else(|_| worktree.to_path_buf());
    let relative = Path::new(relative);
    if !relative
        .components()
        .all(|c| matches!(c, Component::Normal(_)))
    {
        return root;
    }
    std::fs::canonicalize(root.join(relative))
        .ok()
        .filter(|c| c.starts_with(&root) && c.is_dir())
        .unwrap_or(root)
}

pub(crate) async fn claude_version(program: &Path) -> Option<String> {
    let out = tokio::time::timeout(
        Duration::from_secs(10),
        tokio::process::Command::new(program)
            .arg("--version")
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .ok()?
    .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Pick-up folders older than seven days go at the start of the next pick-up.
pub(crate) fn prune(root: &Path) {
    for entry in std::fs::read_dir(root).into_iter().flatten().flatten() {
        let old = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.elapsed().ok())
            .is_some_and(|age| age > KEEP);
        if old {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}

struct Env {
    data: PathBuf,
    home: PathBuf,
    approved: Vec<PathBuf>,
    device_id: Option<String>,
}

async fn pick_up(ticket: Value, folder: Option<String>, env: &Env) -> Result<Value, Failure> {
    let ticket = parse_ticket(ticket)?;
    let manifest = &ticket.manifest;
    let claude = crate::local_sessions::provider_program(&env.home, "claude").ok_or_else(|| {
        fail(
            "CLAUDE_NOT_INSTALLED",
            "Claude Code is not installed on this Mac.",
        )
    })?;
    let installed = claude_version(&claude).await.unwrap_or_default();
    if older(&installed, &manifest.claude_version) {
        return Err(
            json!({"code":"CLAUDE_TOO_OLD","message":"Update Claude Code on this Mac to pick up this session.",
            "installed": version(&installed).map(|_| installed.split_whitespace().next().unwrap_or("").to_string()),
            "required": manifest.claude_version}),
        );
    }
    let chosen = match folder.as_deref() {
        Some(f) => Some(crate::file_access::canonicalize(f).map_err(|_| {
            fail(
                "FOLDER_NOT_APPROVED",
                "That folder is not available on this Mac.",
            )
        })?),
        None => None,
    };
    // Ok(repo) for a code pick-up, Err(folder) for a conversation-only one.
    let target: Result<PathBuf, PathBuf> = match &manifest.code {
        Some(code) => {
            let remote = ticket_remote(&code.remote).unwrap_or_default();
            let found = candidates(&env.approved, &remote).await;
            Ok(match (chosen, found.len()) {
                (_, 0) => {
                    return Err(
                        json!({"code":"REPO_NOT_FOUND","message":"No clone of this repository is in an approved folder.",
                        "remote": remote}),
                    )
                }
                (Some(c), _) if !found.contains(&c) => {
                    return Err(
                        json!({"code":"FOLDER_NOT_CANDIDATE","message":"That folder is not a clone of this repository.",
                        "candidates": found}),
                    )
                }
                (Some(c), _) => c,
                (None, 1) => found[0].clone(),
                (None, _) => {
                    return Err(
                        json!({"code":"REPO_AMBIGUOUS","message":"Several clones of this repository were found; choose one.",
                        "candidates": found}),
                    )
                }
            })
        }
        None => {
            let c = chosen.ok_or_else(|| {
                fail(
                    "FOLDER_REQUIRED",
                    "Choose a folder to continue this conversation in.",
                )
            })?;
            if !env.approved.contains(&c) {
                return Err(fail(
                    "FOLDER_NOT_APPROVED",
                    "That folder is not one of this Mac's approved folders.",
                ));
            }
            Err(c)
        }
    };
    let root = env.data.join("session-pickups");
    std::fs::create_dir_all(&root)
        .map_err(|_| fail("STORAGE_FAILED", "Desktop storage is unavailable."))?;
    prune(&root);
    // Kept across a retry with the same pickup id: parts already here are reused once
    // they pass their size and sha256 check again.
    let dir = root.join(&ticket.pickup_id);
    std::fs::create_dir_all(&dir)
        .map_err(|_| fail("STORAGE_FAILED", "Desktop storage is unavailable."))?;
    let result = async {
        let transcript = fetch_parts(&ticket, &dir).await?;
        let workdir = match &target {
            Ok(repo) => {
                let (branch, worktree) = import_code(
                    repo,
                    &dir.join("code.bundle"),
                    &ticket.session.session_id,
                    &ticket.pickup_id,
                    ticket.session.title.as_deref().unwrap_or(""),
                    manifest.code.as_ref().map_or("", |c| c.snapshot.as_str()),
                )
                .await?;
                (
                    workdir(&worktree, &manifest.cwd_relative),
                    Some((branch, worktree)),
                )
            }
            Err(folder) => (folder.clone(), None),
        };
        Ok::<_, Failure>((transcript, workdir))
    }
    .await;
    let (transcript, (cwd, code)) = match result {
        Ok(v) => v,
        Err(e) => {
            let _ = std::fs::remove_dir_all(&dir);
            return Err(e);
        }
    };
    let session = uuid::Uuid::new_v4().to_string();
    let argv = argv(&claude, &transcript, &session);
    crate::session_start::start_held(cwd, argv)
        .await
        .map_err(|e| fail("LAUNCH_FAILED", &format!("The session didn't start: {e}")))?;
    // Started with no window: tell the owner at this Mac (spec §5.2).
    let _ = crate::notify::show(
        format!(
            "Picked up {}",
            ticket
                .session
                .title
                .as_deref()
                .filter(|t| !t.is_empty())
                .unwrap_or("a session")
        ),
        Some("End it from its panel in Lightbulb.".into()),
        Some(format!("pickup-{session}")),
        None,
        None,
    )
    .await;
    let (branch, worktree) = code.unzip();
    Ok(json!({
        "ok": true,
        "branch": branch,
        "worktree": worktree,
        "conversationId": session,
        // The fingerprint the relay will register the resumed session under
        // (claude.rs: `~/.claude/projects`), so the backend can write its lineage.
        "profileFingerprint": hex::encode(Sha256::digest(env.home.join(".claude/projects").to_string_lossy().as_bytes())),
        "deviceId": env.device_id,
        "conversationOnly": target.is_err(),
        // Project and local Claude settings in the worktree were not loaded.
        "settings": "user",
    }))
}

/// Download and verify every part; returns the reassembled transcript's path.
async fn fetch_parts(ticket: &Ticket, dir: &Path) -> Result<PathBuf, Failure> {
    fetch_downloads(&ticket.downloads, &ticket.manifest.conversation_id, dir).await
}

/// Download and verify every part of `d`; returns the reassembled transcript's path.
pub(crate) async fn fetch_downloads(
    d: &Downloads,
    conversation: &str,
    dir: &Path,
) -> Result<PathBuf, Failure> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|_| fail("DOWNLOAD_FAILED", "The download could not start."))?;
    let mut parts = vec![];
    for part in &d.transcript {
        let path = dir.join(format!("t-{:06}.gz", part.index));
        download(&client, part, &path).await?;
        parts.push(path);
    }
    if let Some(code) = &d.code {
        download(&client, code, &dir.join("code.bundle")).await?;
    }
    let sidecars = match &d.sidecars {
        Some(side) => {
            let path = dir.join("sidecars.tgz");
            download(&client, side, &path).await?;
            Some(path)
        }
        None => None,
    };
    let (dir, conversation) = (dir.to_path_buf(), conversation.to_string());
    tokio::task::spawn_blocking(move || assemble(&dir, &conversation, &parts, sidecars.as_deref()))
        .await
        .map_err(|_| fail("STORAGE_FAILED", "Desktop storage is unavailable."))?
}

/// Gunzip the transcript parts into `<conversation>.jsonl` and unpack the sidecars beside
/// it. Blocking; rebuilt from the verified parts on every attempt.
fn assemble(
    dir: &Path,
    conversation: &str,
    parts: &[PathBuf],
    sidecars: Option<&Path>,
) -> Result<PathBuf, Failure> {
    let storage = || fail("STORAGE_FAILED", "Desktop storage is unavailable.");
    let transcript = dir.join(format!("{conversation}.jsonl"));
    let mut out = std::fs::File::create(&transcript).map_err(|_| storage())?;
    let mut raw = 0u64;
    for path in parts {
        let file = std::fs::File::open(path).map_err(|_| storage())?;
        raw += std::io::copy(
            &mut flate2::read::GzDecoder::new(file).take(TRANSCRIPT_RAW_MAX - raw + 1),
            &mut out,
        )
        .map_err(|_| fail("DOWNLOAD_MISMATCH", "A transcript part could not be read."))?;
        if raw > TRANSCRIPT_RAW_MAX {
            return Err(fail("TOO_LARGE", "The transcript is too large."));
        }
    }
    if let Some(path) = sidecars {
        let dest = dir.join(conversation);
        let _ = std::fs::remove_dir_all(&dest);
        std::fs::create_dir_all(&dest).map_err(|_| storage())?;
        let file = std::fs::File::open(path).map_err(|_| storage())?;
        extract(flate2::read::GzDecoder::new(file), &dest, SIDECARS_RAW_MAX).map_err(|_| {
            fail(
                "ARCHIVE_REFUSED",
                "The session's attachments could not be unpacked safely.",
            )
        })?;
    }
    Ok(transcript)
}

#[tauri::command]
pub async fn session_pickup(
    app: tauri::AppHandle,
    ticket: Value,
    folder: Option<String>,
) -> Result<Value, Value> {
    let data = app
        .path()
        .app_data_dir()
        .map_err(|_| fail("STORAGE_FAILED", "Desktop storage is unavailable."))?;
    let env = Env {
        device_id: crate::pair_state::load(&data).map(|p| p.device_id_mac),
        data,
        home: std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default(),
        approved: crate::file_access::list_paths()
            .iter()
            .filter_map(|p| crate::file_access::canonicalize(p).ok())
            .filter(|p| p.is_dir())
            .collect(),
    };
    let result = pick_up(ticket, folder, &env).await;
    match &result {
        Ok(v) => crate::log(&format!(
            "[session-pickup] launched (conversation only: {})",
            v["conversationOnly"]
        )),
        Err(e) => crate::log(&format!("[session-pickup] failed {}", e["code"])),
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn sh_git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("/usr/bin/git")
            .args([
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .current_dir(dir)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    fn status(dir: &Path) -> String {
        sh_git(dir, &["status", "--short", "--untracked-files=all"])
    }

    /// origin (bare) <- author clone, receiver clone. Author has an unpushed commit,
    /// an uncommitted edit, a deletion, an untracked file and an ignored `.env`.
    #[tokio::test]
    async fn session_pickup_capture_then_import_reproduces_the_authors_status() {
        let tmp = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(tmp.path()).unwrap();
        let origin = root.join("origin.git");
        sh_git(
            &root,
            &[
                "init",
                "-q",
                "--bare",
                "-b",
                "main",
                origin.to_str().unwrap(),
            ],
        );
        let seed = root.join("seed");
        sh_git(&root, &["init", "-q", "-b", "main", seed.to_str().unwrap()]);
        std::fs::write(seed.join(".gitignore"), ".env\n").unwrap();
        std::fs::write(seed.join("a.txt"), "a1\n").unwrap();
        std::fs::write(seed.join("gone.txt"), "bye\n").unwrap();
        std::fs::create_dir(seed.join("sub")).unwrap();
        std::fs::write(seed.join("sub/s.txt"), "s\n").unwrap();
        sh_git(&seed, &["add", "-A"]);
        sh_git(&seed, &["commit", "-qm", "base"]);
        sh_git(
            &seed,
            &["push", "-q", origin.to_str().unwrap(), "HEAD:main"],
        );
        let (author, receiver) = (root.join("author"), root.join("receiver"));
        // Both clones name the same hosted remote, spelled differently.
        for (clone, url) in [
            (&author, "git@github.com:Org/Repo.git"),
            (&receiver, "https://github.com/Org/Repo"),
        ] {
            sh_git(
                &root,
                &[
                    "clone",
                    "-q",
                    origin.to_str().unwrap(),
                    clone.to_str().unwrap(),
                ],
            );
            sh_git(clone, &["remote", "set-url", "origin", url]);
        }
        // Unpushed local commit.
        std::fs::write(author.join("local.txt"), "unpushed\n").unwrap();
        sh_git(&author, &["add", "local.txt"]);
        sh_git(&author, &["commit", "-qm", "local"]);
        // Uncommitted edit (one staged, to show the index is untouched), deletion, untracked, ignored.
        std::fs::write(author.join("a.txt"), "a2\n").unwrap();
        std::fs::write(author.join("sub/s.txt"), "s2\n").unwrap();
        sh_git(&author, &["add", "sub/s.txt"]);
        std::fs::remove_file(author.join("gone.txt")).unwrap();
        std::fs::write(author.join("new.txt"), "new\n").unwrap();
        std::fs::write(author.join(".env"), "SECRET=1\n").unwrap();
        let before = status(&author);
        let sid = "7d3f2a1e-1111-4222-8333-944455556666";
        let work = root.join("work");
        std::fs::create_dir(&work).unwrap();
        let captured = crate::local_sessions::snapshot::capture_code(&author, sid, &work, None)
            .await
            .unwrap();
        assert_eq!(
            status(&author),
            before,
            "capture must not touch the author's index or tree"
        );
        assert!(sh_git(&author, &["for-each-ref", "refs/lightbulb"]).is_empty());
        let crate::local_sessions::snapshot::CodeOutcome::Captured(code) = captured else {
            panic!("expected a bundle");
        };
        assert_eq!(code.remote, "github.com/Org/Repo");
        assert_eq!(code.branch.as_deref(), Some("main"));
        let bundle = root.join("snap.bundle");
        std::fs::write(&bundle, &code.bytes).unwrap();

        // The receiver has their own uncommitted work and stays on their own branch.
        std::fs::write(receiver.join("a.txt"), "receiver's own\n").unwrap();
        let receiver_before = status(&receiver);
        let snapshot = code.snapshot.clone();
        // A first attempt whose restore fails leaves no worktree, branch or ref behind.
        FAIL_RESTORE.with(|f| f.set(true));
        let failed = import_code(
            &receiver,
            &bundle,
            sid,
            "pk-abc123",
            "Fix the ../../ thing!",
            &snapshot,
        )
        .await;
        FAIL_RESTORE.with(|f| f.set(false));
        assert_eq!(failed.unwrap_err()["code"], "IMPORT_FAILED");
        assert!(!receiver
            .join(".claude/worktrees/pickup-fix-the-thing-pkabc1")
            .exists());
        assert!(!sh_git(&receiver, &["branch", "--list"]).contains("pickup/"));
        assert!(sh_git(&receiver, &["for-each-ref", "refs/lightbulb"]).is_empty());
        assert!(!sh_git(&receiver, &["worktree", "list", "--porcelain"]).contains("pickup-"));
        let (branch, worktree) = import_code(
            &receiver,
            &bundle,
            sid,
            "pk-abc123",
            "Fix the ../../ thing!",
            &snapshot,
        )
        .await
        .unwrap();
        assert_eq!(branch, "pickup/fix-the-thing-pkabc1");
        // Byte-identical, except that the author's staged edit arrives unstaged: the
        // receiver's index is deliberately never written.
        assert_eq!(
            status(&worktree),
            before.replace("M  sub/s.txt", " M sub/s.txt")
        );
        assert!(
            before.contains(" M a.txt")
                && before.contains(" D gone.txt")
                && before.contains("?? new.txt")
        );
        assert!(
            !worktree.join(".env").exists(),
            "ignored files never travel"
        );
        assert_eq!(
            std::fs::read_to_string(worktree.join("local.txt")).unwrap(),
            "unpushed\n"
        );
        assert_eq!(status(&receiver), receiver_before);
        assert_eq!(
            sh_git(&receiver, &["branch", "--show-current"]).trim(),
            "main"
        );
        // A retry with the same pickup id (after a failed launch) reuses that worktree and
        // leaves what the person already changed in it alone.
        std::fs::write(worktree.join("a.txt"), "typed after the first launch\n").unwrap();
        let worktrees = sh_git(&receiver, &["worktree", "list", "--porcelain"]);
        let branches = sh_git(&receiver, &["branch", "--list"]);
        let again = import_code(
            &receiver,
            &bundle,
            sid,
            "pk-abc123",
            "Fix the ../../ thing!",
            &snapshot,
        )
        .await
        .unwrap();
        assert_eq!(again, (branch.clone(), worktree.clone()));
        assert_eq!(
            std::fs::read_to_string(worktree.join("a.txt")).unwrap(),
            "typed after the first launch\n"
        );
        assert_eq!(
            sh_git(&receiver, &["worktree", "list", "--porcelain"]),
            worktrees
        );
        assert_eq!(sh_git(&receiver, &["branch", "--list"]), branches);
        // Any other snapshot for that path is refused, not reused.
        assert_eq!(
            import_code(
                &receiver,
                &bundle,
                sid,
                "pk-abc123",
                "Fix the ../../ thing!",
                &"0".repeat(40)
            )
            .await
            .unwrap_err()["code"],
            "WORKTREE_EXISTS"
        );
        // Without the restored marker the worktree is not trusted: refused, not reused.
        sh_git(
            &receiver,
            &[
                "update-ref",
                "-d",
                "refs/lightbulb/pickup/pk-abc123-restored",
            ],
        );
        assert_eq!(
            import_code(
                &receiver,
                &bundle,
                sid,
                "pk-abc123",
                "Fix the ../../ thing!",
                &snapshot
            )
            .await
            .unwrap_err()["code"],
            "WORKTREE_EXISTS"
        );
        assert!(std::fs::read_to_string(receiver.join(".git/info/exclude"))
            .unwrap()
            .contains(".claude/worktrees/"));
    }

    #[test]
    fn session_pickup_url_allow_list() {
        let good = "https://lightbulb-prod-openclaw-replicas.s3.amazonaws.com/session-pickup/org_1/s/x/t-000000.gz?X-Amz-Signature=abc&X-Amz-Expires=900";
        assert!(allowed_url(good));
        assert!(allowed_url(
            "https://lightbulb-dev-openclaw-replicas.s3.us-east-1.amazonaws.com/session-pickup/o/a"
        ));
        assert!(allowed_url(
            "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/session-copies/c/t-000000.gz"
        ));
        assert!(!allowed_url(
            "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/session-copies-x/c"
        ));
        assert!(!allowed_url(&good.replace("https", "http")));
        assert!(!allowed_url(
            "https://evil.s3.amazonaws.com/session-pickup/o/a"
        ));
        assert!(!allowed_url("https://lightbulb-prod-openclaw-replicas.s3.amazonaws.com.evil.example/session-pickup/o/a"));
        assert!(!allowed_url(
            "https://lightbulb-prod-openclaw-replicas.s3.amazonaws.com/org_1/hermes/a"
        ));
        assert!(!allowed_url(
            "https://lightbulb-prod-openclaw-replicas.s3.amazonaws.com:8443/session-pickup/a"
        ));
        assert!(!allowed_url(
            "https://user@lightbulb-prod-openclaw-replicas.s3.amazonaws.com/session-pickup/a"
        ));
    }

    fn tar_with(name: &[u8], kind: tar::EntryType) -> Vec<u8> {
        let mut header = tar::Header::new_old();
        header.as_old_mut().name[..name.len()].copy_from_slice(name);
        header.set_entry_type(kind);
        header.set_size(if kind == tar::EntryType::Regular {
            1
        } else {
            0
        });
        header.set_mode(0o644);
        if kind == tar::EntryType::Symlink {
            header.set_link_name("/etc/passwd").unwrap();
        }
        header.set_cksum();
        let mut builder = tar::Builder::new(Vec::new());
        builder
            .append(&header, &b"x"[..(header.size().unwrap() as usize)])
            .unwrap();
        builder.into_inner().unwrap()
    }

    #[test]
    fn session_pickup_tar_refuses_traversal_absolute_and_links() {
        let dir = tempfile::tempdir().unwrap();
        for (name, kind) in [
            (&b"../x"[..], tar::EntryType::Regular),
            (b"/abs", tar::EntryType::Regular),
            (b"link", tar::EntryType::Symlink),
            (b"hard", tar::EntryType::Link),
        ] {
            assert!(
                extract(&tar_with(name, kind)[..], dir.path(), 1024).is_err(),
                "{name:?}"
            );
        }
        assert!(!dir.path().parent().unwrap().join("x").exists());
        extract(
            &tar_with(b"subagents/ok.jsonl", tar::EntryType::Regular)[..],
            dir.path(),
            1024,
        )
        .unwrap();
        assert_eq!(
            std::fs::read(dir.path().join("subagents/ok.jsonl")).unwrap(),
            b"x"
        );
    }

    #[test]
    fn session_pickup_normalizes_remotes() {
        for raw in [
            "https://github.com/TheLightbulbCompany/lightbulb.git",
            "https://user@GitHub.com/TheLightbulbCompany/lightbulb/",
            "ssh://git@github.com:22/TheLightbulbCompany/lightbulb.git",
            "git@github.com:TheLightbulbCompany/lightbulb.git",
        ] {
            assert_eq!(
                normalize_remote(raw).as_deref(),
                Some("github.com/TheLightbulbCompany/lightbulb"),
                "{raw}"
            );
        }
        assert_eq!(normalize_remote("/local/path"), None);
        // A relative local origin is not a host path (Codex, #4383).
        assert_eq!(normalize_remote("team/project"), None);
        // What a pick-up ticket carries: the author's Mac already normalised it.
        for raw in [
            "github.com/TheLightbulbCompany/lightbulb",
            "https://github.com/TheLightbulbCompany/lightbulb",
        ] {
            assert_eq!(
                ticket_remote(raw).as_deref(),
                Some("github.com/TheLightbulbCompany/lightbulb"),
                "{raw}"
            );
        }
        assert_eq!(ticket_remote("/local/path"), None);
    }

    #[test]
    fn session_pickup_compares_versions_numerically() {
        assert!(older("2.1.99 (Claude Code)", "2.1.283"));
        assert!(!older("2.1.283 (Claude Code)", "2.1.283"));
        assert!(!older("2.2.0", "2.1.283"));
        assert!(older("", "2.1.283"));
        assert!(!older("2.1.0", "unknown"));
    }

    #[test]
    fn session_pickup_slugs_and_argv_never_reach_a_shell() {
        assert_eq!(slug("$(rm -rf ~); Ship it"), "rm-rf-ship-it");
        assert_eq!(slug("日本"), "session");
        assert!(slug(&"a".repeat(200)).len() <= 40);
        let argv = argv(Path::new("/bin/claude"), Path::new("/d/it's.jsonl"), "u-1");
        assert_eq!(
            argv,
            [
                "/bin/claude",
                "--resume",
                "/d/it's.jsonl",
                "--fork-session",
                "--session-id",
                "u-1",
                "--setting-sources",
                "user"
            ]
        );
        // A picked-up session runs in the classic renderer, as a new one does (owner ruling 2026-10-07).
        assert!(crate::holder::run::provider_environment(vec![], &argv).iter().any(|(k, _)| k == crate::holder::run::CLASSIC_RENDERER));
    }

    #[test]
    fn session_pickup_workdir_never_leaves_the_worktree() {
        let dir = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(dir.path()).unwrap();
        let (worktree, outside) = (root.join("w"), root.join("outside"));
        std::fs::create_dir_all(worktree.join("sub")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, worktree.join("evil")).unwrap();
        assert_eq!(workdir(&worktree, "sub"), worktree.join("sub"));
        assert_eq!(
            workdir(&worktree, "evil"),
            worktree,
            "a symlink out falls back to the root"
        );
        assert_eq!(workdir(&worktree, "../outside"), worktree);
        assert_eq!(workdir(&worktree, "missing"), worktree);
        assert_eq!(workdir(&worktree, ""), worktree);
    }

    #[test]
    fn session_pickup_ticket_refuses_foreign_urls_before_any_request() {
        let ticket = |url: &str| {
            json!({"pickupId":"p1","session":{"sessionId":"7d3f2a1e-1111-4222-8333-944455556666","title":"t"},
                "manifest":{"claudeVersion":"2.1.283","conversationId":"8d3f2a1e-1111-4222-8333-944455556666","cwdRelative":"","code":null},
                "downloads":{"transcript":[{"index":0,"url":url,"bytes":1,"sha256":"0".repeat(64)}],"code":null,"sidecars":null}})
        };
        assert_eq!(
            parse_ticket(ticket("https://example.com/session-pickup/x"))
                .err()
                .unwrap()["code"],
            "URL_REFUSED"
        );
        assert!(parse_ticket(ticket(
            "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/session-pickup/x"
        ))
        .is_ok());
    }
}
