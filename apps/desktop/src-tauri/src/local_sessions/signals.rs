//! What a session's folder and helpers say beyond its transcript (spec 2026-10-26 (f), gaps 7, 15,
//! 16): files changed since the session started, from git rather than edit events; the folder
//! branch's pull request, from `gh`; and how many of its subagents run. Git and `gh` run in spawned
//! tasks, never in the relay loop: `tick` starts them, the heartbeat reads their last answer.
use super::*;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// The relay lists `sessionSignals`: it reads `changes`, `agents` and `pr` on a heartbeat.
static READS: AtomicBool = AtomicBool::new(false);
/// A busy session's folder is read again after this long; an idle one only when a turn ends.
const BUSY_EVERY: Duration = Duration::from_secs(30);
/// A branch's PR is asked of GitHub at most this often (every session on the Mac shares the
/// person's one API budget).
const PR_EVERY: Duration = Duration::from_secs(60);
/// A subagent that wrote nothing for this long is not counted as running, whatever its file says
/// (one killed mid-call never writes its end).
const AGENT_QUIET: Duration = Duration::from_secs(600);
/// An idle session whose PR's checks are still running is read again this often, until they settle.
const PENDING_EVERY: Duration = Duration::from_secs(120);
/// A session first seen later than this after its process started may already have deleted files:
/// nothing counts as deleted before it (an over-count beats a deletion never shown).
const FIRST_SEEN_FRESH: Duration = Duration::from_secs(120);

pub(super) fn read_features(listed: &Value) {
    READS.store(listed.as_array().is_some_and(|f| f.iter().any(|x| x == "sessionSignals")), SeqCst);
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub(super) struct Changes {
    files: u64,
    additions: u64,
    deletions: u64,
}

#[derive(Default)]
struct Entry {
    changes: Option<Changes>,
    pr: Option<Value>,
    at: Option<Instant>,
    busy: bool,
    running: bool,
    /// A turn ended while a read was running: read once more after it.
    again: bool,
    /// Tracked files already deleted when this Mac first saw the session: deleted before it, not by
    /// it (`deleted_before`). None until that first look ends.
    gone: Option<BTreeSet<String>>,
    /// The last PR read failed (gh timed out, the network, an unreadable answer): `pr` is the last
    /// known one, and it is asked again on the slow cadence.
    pr_unknown: bool,
}

/// What a read said about the PR: Some(answer) replaces the last one (Some(None): no PR); None
/// (the read failed) keeps it and asks again later.
fn take_pr(entry: &mut Entry, read: Option<Option<Value>>) {
    entry.pr_unknown = read.is_none();
    if let Some(pr) = read {
        entry.pr = pr;
    }
}

/// Due for a read: never read, busy for 30 s, a turn just ended, or idle with its PR's checks
/// still running (else "checks running" would stand after CI finished) or its last PR read failed.
fn due(entry: &Entry, busy: bool, turn_ended: bool) -> bool {
    let checks_running = entry.pr.as_ref().is_some_and(|pr| pr["state"] == "open" && pr["checks"] == "pending");
    entry.at.is_none_or(|at| {
        (busy && at.elapsed() >= BUSY_EVERY) || ((checks_running || entry.pr_unknown) && at.elapsed() >= PENDING_EVERY)
    }) || turn_ended
        || entry.again
}

static ENTRIES: Mutex<BTreeMap<String, Entry>> = Mutex::new(BTreeMap::new());
/// `gh` answers by (repository, branch), so sessions in one checkout ask once.
static PRS: Mutex<BTreeMap<(PathBuf, String), (Instant, Option<Value>)>> = Mutex::new(BTreeMap::new());

/// From the relay loop, every lap: start a read for each live session that is due. Cheap: no I/O.
pub(super) fn tick(mirrors: &BTreeMap<String, Mirror>, home: &Path) {
    if !READS.load(SeqCst) {
        return;
    }
    let Ok(mut entries) = ENTRIES.lock() else { return };
    // Only a shared session's folder is read: nothing of one its person stopped sharing leaves the Mac.
    let readable = |m: &Mirror| m.ended_at.is_none() && !m.withheld();
    entries.retain(|key, _| mirrors.get(key).is_some_and(readable));
    for (key, m) in mirrors.iter().filter(|(_, m)| readable(*m)) {
        let entry = entries.entry(key.clone()).or_default();
        let busy = matches!(m.native.status.as_str(), "busy" | "waiting-approval");
        let turn_ended = entry.busy && !busy;
        entry.busy = busy;
        if entry.running {
            entry.again |= turn_ended;
            continue;
        }
        if !due(entry, busy, turn_ended) {
            continue;
        }
        entry.running = true;
        entry.again = false;
        let (key, native, home, gone) = (key.clone(), m.native.clone(), home.to_path_buf(), entry.gone.clone());
        let since = (native.process.birth_micros / 1_000_000) as i64;
        tokio::spawn(async move {
            let read = tokio::time::timeout(Duration::from_secs(30), async {
                let root = workspace::session_root(&native, &home).await;
                // The first look at a session is its first read, whatever else fails after: the files
                // already deleted then are the session's baseline, recorded once.
                let gone = match gone {
                    Some(gone) => gone,
                    None => match &root {
                        Ok(root) => deleted_before(root, since, epoch_now()).await,
                        Err(_) => BTreeSet::new(),
                    },
                };
                let Ok(root) = root else { return (None, None::<Option<Value>>, gone) };
                let changes = changes(&root, since, &gone).await.ok();
                let pr = match gh_program() {
                    Some(gh) => pr(&gh, &root, &home).await,
                    None => Some(None),
                };
                (changes, pr, gone)
            })
            .await
            // A look that stalled knows no baseline: count every deletion.
            .unwrap_or((None, None, BTreeSet::new()));
            if let Ok(mut entries) = ENTRIES.lock() {
                if let Some(entry) = entries.get_mut(&key) {
                    let (changes, pr, gone) = read;
                    entry.changes = changes;
                    take_pr(entry, pr);
                    if entry.gone.is_none() {
                        entry.gone = Some(gone);
                    }
                    entry.at = Some(Instant::now());
                    entry.running = false;
                }
            }
        });
    }
}

/// The signals on a heartbeat entry, for a relay that reads them. Absent is "none" or "unknown":
/// each frame says all of them.
pub(super) fn add(entry: &mut Value, key: &str, m: &Mirror) {
    add_if(READS.load(SeqCst), entry, key, m);
}

fn add_if(reads: bool, entry: &mut Value, key: &str, m: &Mirror) {
    if !reads || m.withheld() {
        return;
    }
    let agents = running_agents(m);
    if agents > 0 {
        entry["agents"] = json!(agents);
    }
    let Ok(entries) = ENTRIES.lock() else { return };
    let Some(read) = entries.get(key) else { return };
    if let Some(changes) = read.changes.as_ref().filter(|c| c.files > 0) {
        entry["changes"] = json!(changes);
    }
    if let Some(pr) = &read.pr {
        entry["pr"] = pr.clone();
    }
}

/// Codex on the shared daemon: its loaded child threads that are working (`codex::capture`).
/// Claude, and Codex without the daemon: the subagent transcripts this mirror tails that have not
/// ended and wrote something lately.
fn running_agents(m: &Mirror) -> u32 {
    if m.native.provider == "codex" && m.native.socket.is_some() {
        return m.native.attention.agents;
    }
    m.subagents
        .known
        .values()
        .filter(|s| !s.ended && !s.closed)
        .filter(|s| {
            s.path.as_ref().and_then(|p| fs::metadata(p).ok()).and_then(|meta| meta.modified().ok())
                .and_then(|t| t.elapsed().ok()).is_some_and(|age| age < AGENT_QUIET)
        })
        .count() as u32
}

/// What changed in the session's repository since the session started: the diff against the commit
/// HEAD was on then (the reflog), counting only files written since (an older dirty file is not the
/// session's), plus the untracked files it created. A tracked file that is gone counts unless it was
/// already gone when the session was first seen (`gone`). Err: not a repository, or too big to read.
// ponytail: a file dirty before the session and edited during it counts its whole diff; record a
// baseline tree at the start if that ever matters.
async fn changes(cwd: &Path, since: i64, gone: &BTreeSet<String>) -> Result<Changes> {
    let top = toplevel(cwd).await?;
    let mut changes = Changes::default();
    if let Some(base) = start_commit(&top, since).await {
        let numstat = workspace::git(&top, &["diff", "--numstat", "--no-renames", "-z", &base, "--"]).await?;
        for record in numstat.split(|b| *b == 0).filter(|r| !r.is_empty()) {
            let text = std::str::from_utf8(record).map_err(|_| "workspace unavailable")?;
            let mut fields = text.splitn(3, '\t');
            let (Some(added), Some(removed), Some(path)) = (fields.next(), fields.next(), fields.next()) else { continue };
            let file = top.join(path);
            if fs::symlink_metadata(&file).is_err() {
                if gone.contains(path) {
                    continue;
                }
            } else if !written_since(&file, since) {
                continue;
            }
            changes.files += 1;
            // A binary file says "-": it counts as a file, with no lines.
            changes.additions += added.parse::<u64>().unwrap_or(0);
            changes.deletions += removed.parse::<u64>().unwrap_or(0);
        }
    }
    let untracked = workspace::git(&top, &["ls-files", "--others", "--exclude-standard", "-z"]).await?;
    let new = tokio::task::spawn_blocking(move || new_files(&top, &untracked, since))
        .await
        .map_err(|_| "workspace unavailable")?;
    changes.files += new.0;
    changes.additions += new.1;
    Ok(changes)
}

async fn toplevel(cwd: &Path) -> Result<PathBuf> {
    Ok(PathBuf::from(
        String::from_utf8(workspace::git(cwd, &["rev-parse", "--show-toplevel"]).await?)
            .map_err(|_| "workspace unavailable")?
            .trim(),
    ))
}

fn epoch_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

/// The tracked files deleted (from the working tree or staged, repository-relative) when the
/// session is first seen at `now`. Empty when that is long after the session started (it may have
/// deleted them itself) or git cannot say: every deletion then counts.
async fn deleted_before(cwd: &Path, since: i64, now: i64) -> BTreeSet<String> {
    if now - since > FIRST_SEEN_FRESH.as_secs() as i64 {
        return BTreeSet::new();
    }
    let Ok(top) = toplevel(cwd).await else { return BTreeSet::new() };
    // Deleted in the working tree (`ls-files -d`) and deleted with `git rm` (staged, so no longer in
    // the index `ls-files` reads: the diff against HEAD names it).
    let Ok(unstaged) = workspace::git(&top, &["ls-files", "-d", "-z"]).await else { return BTreeSet::new() };
    let staged = workspace::git(&top, &["diff", "--name-only", "-z", "--no-renames", "--diff-filter=D", "HEAD", "--"]).await.unwrap_or_default();
    unstaged
        .split(|b| *b == 0)
        .chain(staged.split(|b| *b == 0))
        .filter(|n| !n.is_empty())
        .filter_map(|n| std::str::from_utf8(n).ok().map(str::to_owned))
        .collect()
}

/// `git hash-object -t tree /dev/null`.
const EMPTY_TREE: &str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";

/// The commit HEAD was on at `since`: the newest reflog entry no later than it, else the oldest one
/// (the repository's HEAD moved first after the session started), else HEAD now.
async fn start_commit(top: &Path, since: i64) -> Option<String> {
    let log = workspace::git(top, &["log", "-g", "--format=%H %gd %gs", "--date=unix", "-n", "500", "HEAD"]).await.ok();
    let entries: Vec<(&str, i64, &str)> = log
        .as_deref()
        .and_then(|bytes| std::str::from_utf8(bytes).ok())
        .into_iter()
        .flat_map(str::lines)
        .filter_map(|line| {
            let (hash, rest) = line.split_once(' ')?;
            let (selector, subject) = rest.split_once(' ').unwrap_or((rest, ""));
            let at = selector.strip_prefix("HEAD@{")?.strip_suffix('}')?.parse().ok()?;
            Some((hash, at, subject))
        })
        .collect();
    if let Some((hash, _, _)) = entries.iter().find(|(_, at, _)| *at <= since) {
        return Some((*hash).to_owned());
    }
    // The repository's first commit came after the session started: there was no commit then, and
    // everything since is the session's (git's empty tree).
    if let Some((hash, _, subject)) = entries.last() {
        return Some(if subject.starts_with("commit (initial)") { EMPTY_TREE.to_owned() } else { (*hash).to_owned() });
    }
    let head = workspace::git(top, &["rev-parse", "--verify", "-q", "HEAD"]).await.ok()?;
    Some(String::from_utf8(head).ok()?.trim().to_owned()).filter(|h| !h.is_empty())
}

/// A path written at or after `since`, or gone (a deletion is a change).
fn written_since(path: &Path, since: i64) -> bool {
    fs::symlink_metadata(path).map_or(true, |meta| meta.mtime() >= since)
}

/// Untracked files written since the session started, and their lines. Only regular files under
/// 1 MiB are read (never a FIFO or a link), 8 MiB in all.
fn new_files(top: &Path, listed: &[u8], since: i64) -> (u64, u64) {
    let (mut files, mut lines, mut read) = (0, 0, 0u64);
    for name in listed.split(|b| *b == 0).filter(|n| !n.is_empty()).take(512) {
        let path = top.join(std::ffi::OsStr::from_bytes(name));
        let Ok(meta) = fs::symlink_metadata(&path) else { continue };
        if meta.mtime() < since {
            continue;
        }
        files += 1;
        if meta.is_file() && meta.len() <= 1024 * 1024 && read + meta.len() <= 8 * 1024 * 1024 {
            read += meta.len();
            if let Ok(bytes) = fs::read(&path) {
                lines += bytecount(&bytes);
            }
        }
    }
    (files, lines)
}

fn bytecount(bytes: &[u8]) -> u64 {
    let newlines = bytes.iter().filter(|b| **b == b'\n').count() as u64;
    newlines + u64::from(bytes.last().is_some_and(|b| *b != b'\n'))
}

/// Homebrew's `gh`, when the person installed it.
fn gh_program() -> Option<PathBuf> {
    ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"].into_iter().map(PathBuf::from).find(|p| p.is_file())
}

/// The PR of the branch checked out in `root` (`gh pr view`), an answer cached a minute per branch;
/// it never prompts. Some(None): no PR, or a detached HEAD. None: it could not tell (gh signed out,
/// timed out, offline, an unreadable answer); the caller keeps what it knew.
async fn pr(gh: &Path, root: &Path, home: &Path) -> Option<Option<Value>> {
    let branch = String::from_utf8(workspace::git(root, &["rev-parse", "--abbrev-ref", "HEAD"]).await.ok()?).ok()?;
    let branch = branch.trim().to_owned();
    if branch.is_empty() || branch == "HEAD" {
        return Some(None);
    }
    let cache = (root.to_path_buf(), branch);
    if let Some((at, pr)) = PRS.lock().ok()?.get(&cache) {
        if at.elapsed() < PR_EVERY {
            return Some(pr.clone());
        }
    }
    let answer = gh_pr_view(gh, root, home).await?;
    if let Ok(mut prs) = PRS.lock() {
        prs.retain(|_, (at, _)| at.elapsed() < PR_EVERY);
        prs.insert(cache, (Instant::now(), answer.clone()));
    }
    Some(answer)
}

async fn gh_pr_view(gh: &Path, root: &Path, home: &Path) -> Option<Option<Value>> {
    let run = tokio::process::Command::new(gh)
        .args(["pr", "view", "--json", "number,state,url,statusCheckRollup"])
        .current_dir(root)
        .env_clear()
        .env("HOME", home)
        .env("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        .env("GH_PROMPT_DISABLED", "1")
        .env("GH_NO_UPDATE_NOTIFIER", "1")
        .env("NO_COLOR", "1")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .output();
    let out = tokio::time::timeout(Duration::from_secs(10), run).await.ok()?.ok()?;
    if !out.status.success() {
        // gh's own words for a branch with no PR; any other failure says nothing.
        let said = String::from_utf8_lossy(&out.stderr[..out.stderr.len().min(4096)]).to_lowercase();
        return said.contains("no pull requests found").then_some(None);
    }
    if out.stdout.len() > 512 * 1024 {
        return None;
    }
    Some(Some(pr_wire(&serde_json::from_slice(&out.stdout).ok()?)?))
}

/// `{number, state, url, checks?}`: open, merged or closed, and the checks' verdict (failing when
/// any failed, else pending while any runs, else passing; absent with none).
fn pr_wire(view: &Value) -> Option<Value> {
    let number = view["number"].as_u64().filter(|n| (1..100_000_000).contains(n))?;
    let url = view["url"].as_str().filter(|u| u.starts_with("https://") && u.len() <= 300)?;
    let state = match view["state"].as_str()? {
        "OPEN" => "open",
        "MERGED" => "merged",
        "CLOSED" => "closed",
        _ => return None,
    };
    let mut wire = json!({"number": number, "state": state, "url": url});
    let checks = view["statusCheckRollup"].as_array().filter(|c| !c.is_empty()).map(|checks| {
        let verdicts: Vec<_> = checks
            .iter()
            // A check run's conclusion, else a status context's state.
            .map(|c| c["conclusion"].as_str().filter(|s| !s.is_empty()).or(c["state"].as_str()).unwrap_or(""))
            .collect();
        if verdicts.iter().any(|v| matches!(*v, "FAILURE" | "ERROR" | "CANCELLED" | "TIMED_OUT" | "ACTION_REQUIRED" | "STARTUP_FAILURE")) {
            "failing"
        } else if verdicts.iter().all(|v| matches!(*v, "SUCCESS" | "NEUTRAL" | "SKIPPED")) {
            "passing"
        } else {
            "pending"
        }
    });
    if let Some(checks) = checks {
        wire["checks"] = json!(checks);
    }
    Some(wire)
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn git(root: &Path, args: &[&str]) {
        let mut all = vec!["-c", "user.name=Test", "-c", "user.email=test@example.invalid"];
        all.extend(args);
        workspace::git(root, &all).await.unwrap();
    }

    #[tokio::test]
    async fn local_sessions_signals_changes_count_what_the_session_wrote_in_git() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q"]).await;
        fs::write(root.join("old.txt"), "one\ntwo\nthree\n").unwrap();
        fs::write(root.join("dirty-before.txt"), "a\n").unwrap();
        git(&root, &["add", "."]).await;
        git(&root, &["commit", "-qm", "start"]).await;
        // Dirty before the session started: not the session's.
        fs::write(root.join("dirty-before.txt"), "a\nb\n").unwrap();
        fs::write(root.join("untracked-before.txt"), "x\n").unwrap();
        let old = std::time::SystemTime::now() - Duration::from_secs(3600);
        for name in ["dirty-before.txt", "untracked-before.txt"] {
            fs::File::options().write(true).open(root.join(name)).unwrap().set_modified(old).unwrap();
        }
        let since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64; // the start commit is no later than this
        assert_eq!(changes(&root, since, &BTreeSet::new()).await.unwrap(), Changes::default());
        // The session edits by shell (no edit event), commits, then creates a file. Its commit lands a
        // second after the start: the reflog counts in whole seconds.
        tokio::time::sleep(Duration::from_millis(1100)).await;
        fs::write(root.join("old.txt"), "one\n2\nthree\nfour\n").unwrap();
        git(&root, &["commit", "-qam", "session"]).await;
        fs::write(root.join("new.txt"), "n1\nn2\nn3").unwrap();
        assert_eq!(changes(&root, since, &BTreeSet::new()).await.unwrap(), Changes { files: 2, additions: 2 + 3, deletions: 1 });
        // Not a repository: no answer, the event count stands.
        let plain = tempfile::tempdir().unwrap();
        assert!(changes(&fs::canonicalize(plain.path()).unwrap(), since, &BTreeSet::new()).await.is_err());
    }

    #[tokio::test]
    async fn local_sessions_signals_a_file_deleted_before_the_session_is_not_its_change() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q"]).await;
        for name in ["before.txt", "during.txt"] {
            fs::write(root.join(name), "a\nb\n").unwrap();
        }
        git(&root, &["add", "."]).await;
        git(&root, &["commit", "-qm", "start"]).await;
        fs::remove_file(root.join("before.txt")).unwrap();
        let since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64; // the start commit is no later than this
        // Deleted before the session was first seen: the baseline, never its change.
        let gone = deleted_before(&root, since, since + 1).await;
        assert_eq!(gone, BTreeSet::from(["before.txt".to_owned()]));
        assert_eq!(changes(&root, since, &gone).await.unwrap(), Changes::default());
        // Deleted by the session after it was first seen but before any scan: it counts.
        fs::remove_file(root.join("during.txt")).unwrap();
        assert_eq!(changes(&root, since, &gone).await.unwrap(), Changes { files: 1, additions: 0, deletions: 2 });
        // First seen long after it started: it may have deleted them itself, so every deletion counts.
        assert_eq!(deleted_before(&root, since, since + 600).await, BTreeSet::new());
    }

    #[test]
    fn local_sessions_signals_an_idle_session_rereads_only_while_its_checks_run() {
        let old = Instant::now() - Duration::from_secs(150);
        let read = |pr: Option<Value>| Entry { at: Some(old), pr, ..Default::default() };
        let open = |checks: &str| Some(json!({"number": 1, "state": "open", "url": "https://x", "checks": checks}));
        assert!(due(&read(open("pending")), false, false), "checks running: read again");
        for settled in [open("passing"), open("failing"), Some(json!({"number": 1, "state": "merged", "url": "https://x", "checks": "pending"})), None] {
            assert!(!due(&read(settled), false, false), "settled, or no PR: an idle session is left alone");
        }
        let fresh = Entry { at: Some(Instant::now()), pr: open("pending"), ..Default::default() };
        assert!(!due(&fresh, false, false), "not before two minutes");
        assert!(due(&read(None), true, false), "busy: every 30 s");
        assert!(due(&read(None), false, true), "a turn ended");
    }

    #[test]
    fn local_sessions_signals_a_failed_pr_read_keeps_the_last_pr_and_asks_again() {
        let known = json!({"number": 7, "state": "open", "url": "https://x", "checks": "passing"});
        let mut entry = Entry { pr: Some(known.clone()), at: Some(Instant::now() - Duration::from_secs(150)), ..Default::default() };
        assert!(!due(&entry, false, false), "settled checks: an idle session is left alone");
        take_pr(&mut entry, None);
        assert_eq!(entry.pr, Some(known), "a failure does not erase the PR");
        assert!(due(&entry, false, false), "and is asked again on the slow cadence");
        take_pr(&mut entry, Some(None));
        assert_eq!(entry.pr, None, "gh said there is no PR");
        assert!(!due(&entry, false, false));
    }

    #[tokio::test]
    async fn local_sessions_signals_a_repo_with_no_commit_at_the_start_counts_everything() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q"]).await;
        let since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64 - 1;
        fs::write(root.join("first.txt"), "a\nb\n").unwrap();
        git(&root, &["add", "."]).await;
        git(&root, &["commit", "-qm", "first"]).await;
        assert_eq!(start_commit(&root, since).await.as_deref(), Some(EMPTY_TREE));
        assert_eq!(changes(&root, since, &BTreeSet::new()).await.unwrap(), Changes { files: 1, additions: 2, deletions: 0 });
    }

    #[tokio::test]
    async fn local_sessions_signals_a_deletion_staged_before_the_session_is_its_baseline() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q"]).await;
        for name in ["staged.txt", "worktree.txt"] {
            fs::write(root.join(name), "a\n").unwrap();
        }
        git(&root, &["add", "."]).await;
        git(&root, &["commit", "-qm", "start"]).await;
        git(&root, &["rm", "-q", "staged.txt"]).await;
        fs::remove_file(root.join("worktree.txt")).unwrap();
        let since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64; // the start commit is no later than this
        let gone = deleted_before(&root, since, since + 1).await;
        assert_eq!(gone, BTreeSet::from(["staged.txt".to_owned(), "worktree.txt".to_owned()]));
        assert_eq!(changes(&root, since, &gone).await.unwrap(), Changes::default());
    }

    #[test]
    fn local_sessions_signals_pr_says_state_and_the_checks_verdict() {
        let pr = |state: &str, checks: Value| pr_wire(&json!({"number": 123, "state": state, "url": "https://github.com/o/r/pull/123", "statusCheckRollup": checks}));
        let run = |conclusion: &str, status: &str| json!({"__typename":"CheckRun","conclusion":conclusion,"status":status});
        assert_eq!(pr("OPEN", json!([])), Some(json!({"number":123,"state":"open","url":"https://github.com/o/r/pull/123"})));
        assert_eq!(pr("OPEN", json!([run("SUCCESS", "COMPLETED"), run("SKIPPED", "COMPLETED")])).unwrap()["checks"], "passing");
        assert_eq!(pr("OPEN", json!([run("SUCCESS", "COMPLETED"), run("", "IN_PROGRESS")])).unwrap()["checks"], "pending");
        assert_eq!(pr("OPEN", json!([run("", "IN_PROGRESS"), {"__typename":"StatusContext","state":"FAILURE"}])).unwrap()["checks"], "failing");
        assert_eq!(pr("MERGED", Value::Null).unwrap()["state"], "merged");
        assert_eq!(pr("DRAFTISH", json!([])), None);
        assert_eq!(pr_wire(&json!({"number": 1, "state": "OPEN", "url": "javascript:alert(1)"})), None, "only a web link");
    }

    #[tokio::test]
    async fn local_sessions_signals_pr_reads_a_stubbed_gh_once_a_minute() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        git(&root, &["init", "-q", "-b", "feat/x"]).await;
        git(&root, &["commit", "-q", "--allow-empty", "-m", "start"]).await;
        let calls = root.join("calls");
        let gh = root.join("gh");
        fs::write(&gh, format!(
            "#!/bin/sh\necho x >> '{}'\n[ \"$GH_PROMPT_DISABLED\" = 1 ] || exit 9\necho '{{\"number\":7,\"state\":\"OPEN\",\"url\":\"https://github.com/o/r/pull/7\",\"statusCheckRollup\":[]}}'\n",
            calls.display()
        )).unwrap();
        fs::set_permissions(&gh, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        let want = Some(Some(json!({"number":7,"state":"open","url":"https://github.com/o/r/pull/7"})));
        assert_eq!(pr(&gh, &root, &root).await, want);
        assert_eq!(pr(&gh, &root, &root).await, want, "cached");
        assert_eq!(fs::read_to_string(&calls).unwrap().lines().count(), 1);
        // gh says the branch has no PR: that is an answer.
        let none = root.join("gh-none");
        fs::write(&none, "#!/bin/sh\necho 'no pull requests found for branch \"feat/y\"' >&2\nexit 1\n").unwrap();
        fs::set_permissions(&none, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        git(&root, &["checkout", "-q", "-b", "feat/y"]).await;
        assert_eq!(pr(&none, &root, &root).await, Some(None));
        // Any other failure (signed out, offline) is no answer, and is not cached.
        let failing = root.join("gh-fails");
        fs::write(&failing, "#!/bin/sh\necho 'error connecting to api.github.com' >&2\nexit 1\n").unwrap();
        fs::set_permissions(&failing, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        git(&root, &["checkout", "-q", "-b", "feat/z"]).await;
        assert_eq!(pr(&failing, &root, &root).await, None);
        assert_eq!(pr(&gh, &root, &root).await, want, "the failure was not cached");
    }

    #[test]
    fn local_sessions_signals_count_live_subagents_and_travel_only_to_a_relay_that_reads_them() {
        let dir = tempfile::tempdir().unwrap();
        let live = dir.path().join("agent-a.jsonl");
        fs::write(&live, "{\"isSidechain\":true}\n").unwrap();
        let mut m = super::super::transport::tests::test_mirror("s", "g");
        m.subagents.known.insert("a".into(), super::super::Subagent { path: Some(live.clone()), ..Default::default() });
        m.subagents.known.insert("b".into(), super::super::Subagent { path: Some(live.clone()), ended: true, ..Default::default() });
        m.subagents.known.insert("c".into(), super::super::Subagent { path: Some(live), closed: true, ..Default::default() });
        assert_eq!(running_agents(&m), 1);
        m.native.provider = "codex".into();
        m.native.socket = Some("/tmp/sock".into());
        m.native.attention.agents = 2;
        assert_eq!(running_agents(&m), 2, "the daemon's count, not the tailed files");
        let mut entry = json!({});
        add_if(false, &mut entry, "signals-test", &m);
        assert_eq!(entry, json!({}));
        ENTRIES.lock().unwrap().insert("signals-test".into(), Entry {
            changes: Some(Changes { files: 1, additions: 3, deletions: 0 }),
            pr: Some(json!({"number": 7})), ..Default::default()
        });
        add_if(true, &mut entry, "signals-test", &m);
        assert_eq!(entry, json!({"agents": 2, "changes": {"files": 1, "additions": 3, "deletions": 0}, "pr": {"number": 7}}));
        ENTRIES.lock().unwrap().remove("signals-test");
    }
}
