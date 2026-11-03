use super::*;
use futures_util::{SinkExt, StreamExt};
use std::os::unix::fs::FileTypeExt;
use tokio_tungstenite::{client_async, tungstenite::Message, WebSocketStream};

/// Rollout sources we mirror: the terminal CLI, and the ChatGPT / Codex
/// Desktop app's bundled codex (which writes `source: "vscode"` into the
/// same `~/.codex`). The VS Code *extension* writes the identical `source`
/// value, so `vscode` alone is not enough to admit a rollout — see
/// `ACCEPTED_APP_ORIGINATORS`. `exec` and subagent rollouts stay out either
/// way.
const ACCEPTED_SOURCES: [&str; 2] = ["cli", "vscode"];

/// The Desktop app's originators: `codex_work_desktop` is what newer app builds
/// write for a conversation. Newer terminal TUIs also write `source: "vscode"`,
/// with `originator: "codex-tui"`; see `accepted_app_source`. These values keep
/// the VS Code extension's rollouts out of this mirror.
const ACCEPTED_APP_ORIGINATORS: [&str; 2] = ["Codex Desktop", "codex_work_desktop"];

fn app_originator(payload: &Value) -> bool {
    payload["originator"].as_str().is_some_and(|o| ACCEPTED_APP_ORIGINATORS.contains(&o))
}

/// Runtimes whose *paginated* rollout grammar this parser was verified
/// against. A `"legacy"` rollout carries none of the `item_completed` rows
/// this parser reads — text lives in bare `user_message`/`agent_message`
/// event_msgs and tool calls in `response_item` rows — so it is refused
/// outright rather than mirrored blank; see `accepted_history_mode` below.
// Exact builds with observed supported history grammar and native queue delivery.
const ACCEPTED_VERSIONS: [&str; 6] = ["0.149.0", "0.153.4", "0.155.0-alpha.2.6", "0.157.1", "0.159.2", "0.159.3"];
const CONTROL_DAEMON_VERSION: &str = "0.159.3";
const DESKTOP_RESOURCES: &str = "/Applications/ChatGPT.app/Contents/Resources";

fn accepted_version(version: &str) -> bool {
    ACCEPTED_VERSIONS.contains(&version) || at_least(version, [0, 159, 2])
}

/// A stable `a.b.c` release at or above `floor`. Codex's managed daemon updates itself
/// (0.160.0 while the CLI is 0.159.3); the 0.159 grammar is the floor, not the only build.
fn at_least(version: &str, floor: [u32; 3]) -> bool {
    let parts: Vec<u32> = version.split('.').map_while(|p| p.parse().ok()).collect();
    parts.len() == 3 && version.split('.').count() == 3 && parts.as_slice() >= floor.as_slice()
}

/// `vscode` rollouts must name the Desktop app or the verified 0.157.1 TUI;
/// `cli` rollouts carry no originator requirement.
fn accepted_app_source(source: &str, payload: &Value) -> bool {
    if source != "vscode" {
        return true;
    }
    app_originator(payload)
        || (payload["originator"] == "codex-tui"
            && payload["cli_version"].as_str().is_some_and(|v| v == "0.157.1" || at_least(v, [0, 159, 2])))
}

/// The ChatGPT app ships its bundled codex as a prerelease (`0.162.0-alpha.17.2`).
/// Accepted only for that bundle, whose file `capture` identifies by path and inode
/// before running it for its version: the bundle is the app's own, so its
/// prerelease is the shipped app. A terminal or daemon prerelease stays refused, and
/// so does any suffix other than `alpha.N[.N]` (`0.160.1-custom`).
fn desktop_prerelease(version: &str) -> bool {
    let Some((core, pre)) = version.split_once('-') else { return false };
    let Some(numbers) = pre.strip_prefix("alpha.") else { return false };
    let parts: Vec<&str> = numbers.split('.').collect();
    at_least(core, [0, 159, 2])
        && (1..=2).contains(&parts.len())
        && parts.iter().all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
}

fn desktop_program_in(resources: &Path, running: Option<&Path>) -> Option<PathBuf> {
    ["codex-cli/CodexCLI.app/Contents/MacOS/codex", "codex"]
        .into_iter()
        .map(|relative| resources.join(relative))
        .find(|path| {
            running.is_none_or(|expected| expected == path)
                && fs::symlink_metadata(path).is_ok_and(|meta| meta.is_file())
        })
}

fn desktop_program() -> Option<PathBuf> {
    desktop_program_in(Path::new(DESKTOP_RESOURCES), None)
}

/// Recent TUIs delegate rollout writing to a separately installed app server, which updates
/// itself. Any release under the managed daemon's releases directory counts: one of this user's
/// regular files, nobody else can write it, reached without a symlink. Returns its path and the
/// version its release directory names, so it is never executed to learn it.
pub(super) fn daemon_release(home: &Path, execution: &Process) -> Option<(PathBuf, String)> {
    let root = home.join(".codex/packages/app-server-daemon/releases");
    let release = execution.executable.strip_prefix(&root).ok()?;
    let triple = if cfg!(target_arch = "aarch64") { "aarch64" } else { "x86_64" };
    let mut parts = release.components();
    let dir = parts.next()?.as_os_str().to_str()?;
    let version = dir.strip_suffix(&format!("-{triple}-apple-darwin"))?;
    if parts.as_path() != Path::new("bin/codex") || !at_least(version, [0, 159, 2]) {
        return None;
    }
    let path = confined_path(home, &execution.executable).ok()?;
    let meta = fs::symlink_metadata(&path).ok()?;
    (meta.is_file() && meta.mode() & 0o022 == 0).then(|| (path, version.to_owned()))
}

/// The process can serve more than one conversation at once, so one of them cannot be told from
/// another by process: the Codex app's own, Codex's shared app-server daemon, or the ChatGPT app's
/// bundled codex.
pub(super) fn serves_many(session: &NativeSession) -> bool {
    serves_many_in(session, std::env::var_os("HOME").as_deref().map(Path::new))
}

/// A REFUSAL check, so deliberately loose: no mode, symlink or version validation, which would
/// let an odd install read as a single-conversation TUI. Any app bundle, or any path under the
/// managed daemon's directory, counts.
fn serves_many_in(session: &NativeSession, home: Option<&Path>) -> bool {
    let exe = &session.process.executable;
    session.app
        || home.is_none_or(|home| exe.starts_with(home.join(".codex/packages/app-server-daemon")))
        || exe.components().any(|part| part.as_os_str().to_str().is_some_and(|name| name.ends_with(".app")))
}

pub(super) fn daemon_program(home: &Path, execution: &Process) -> Option<PathBuf> {
    daemon_release(home, execution).map(|(path, _)| path)
}

/// Only the managed daemon (`daemon_release`), never a global Codex socket or another app-server
/// process. Its own lsof entry must name one user-owned socket. Any verified release: liveness reads
/// it on every build; steer and interrupt stay on `CONTROL_DAEMON_VERSION` (`control`).
async fn control_socket(home: &Path, execution: &Process) -> Option<(PathBuf, u64)> {
    daemon_release(home, execution)?;
    let uid = unsafe { libc::geteuid() };
    let dir = fs::canonicalize("/tmp").ok()?.join(format!("codex-daemon-{uid}"));
    let pid = execution.pid.to_string();
    let entries = native_output(Path::new("/usr/sbin/lsof"), &["-n", "-a", "-p", &pid, "-U", "-Fn"]).await.ok()?;
    let found = socket_from_lsof(&entries, &dir, uid)?;
    if process(execution.pid).ok()? != *execution { return None; }
    Some(found)
}

fn socket_from_lsof(entries: &str, dir: &Path, uid: u32) -> Option<(PathBuf, u64)> {
    let mut found = None;
    for path in entries.lines().filter_map(|line| line.strip_prefix('n')).map(PathBuf::from) {
        if !path.starts_with(&dir) { continue; }
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else { return None; };
        if name.len() != 64 || !name.bytes().all(|c| c.is_ascii_hexdigit()) { continue; }
        let meta = fs::symlink_metadata(&path).ok()?;
        if !meta.file_type().is_socket() || meta.uid() != uid { return None; }
        let candidate = (path, meta.ino());
        if found.as_ref().is_some_and(|previous| previous != &candidate) { return None; }
        found = Some(candidate);
    }
    found
}

/// A managed daemon serves many checkout folders. Its process cwd cannot
/// describe one thread; a resumed turn may also change the header's cwd.
pub(super) fn rollout_cwd(home: &Path, native: &NativeSession) -> Result<PathBuf> {
    let root = home.join(".codex/sessions");
    let path = confined_path(&root, native.transcript.as_deref().ok_or("rollout unavailable")?)?;
    let mut line = String::new();
    BufReader::new(secure_file(&path)?.take(65536))
        .read_line(&mut line)
        .map_err(|_| "rollout metadata unavailable")?;
    let row: Value = serde_json::from_str(&line).map_err(|_| "rollout metadata unavailable")?;
    if row["type"] != "session_meta" || row["payload"]["id"] != native.conversation {
        return Err("rollout identity changed".into());
    }
    let (_, _, _, turn_cwd) = latest_turn(&path)?;
    turn_cwd
        .unwrap_or_else(|| row["payload"]["cwd"].as_str().map(PathBuf::from))
        .ok_or("rollout cwd unavailable".into())
}

pub(super) fn program(home: &Path) -> Option<PathBuf> {
    provider_program(home, "codex").or_else(desktop_program)
}

/// `vscode` (app) rollouts must be explicitly paginated (fail closed); `cli`
/// rollouts keep the historic rule — absent means accepted, only the literal
/// `"legacy"` is refused — since 0.149.0 CLI rollouts never carried the
/// field. One helper for both `from_rollout` and `normalize` so the two
/// cannot drift.
fn accepted_history_mode(source: &str, payload: &Value) -> bool {
    if source == "vscode" {
        payload["history_mode"] == "paginated"
    } else {
        payload["history_mode"] != "legacy"
    }
}

fn from_rollout(root: &Path, process: Process, path: &Path, version: &str) -> Result<NativeSession> {
    from_writer(root, process, path, version, false)
}

/// `desktop_bundle`: the writer is the ChatGPT app's own bundled codex (see `desktop_prerelease`).
fn from_writer(
    root: &Path,
    process: Process,
    path: &Path,
    version: &str,
    desktop_bundle: bool,
) -> Result<NativeSession> {
    let path = confined_path(root, path)?;
    let mut reader = BufReader::new(secure_file(&path)?.take(65536));
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .map_err(|_| "rollout unavailable")?;
    let row: Value = serde_json::from_str(&line).map_err(|_| "rollout metadata unavailable")?;
    let source = row["payload"]["source"].as_str().unwrap_or("");
    if row["type"] != "session_meta" || !ACCEPTED_SOURCES.contains(&source) {
        return Err("not a Codex CLI or app rollout".into());
    }
    if !accepted_app_source(source, &row["payload"]) {
        return Err("not the Codex desktop app".into());
    }
    if !accepted_history_mode(source, &row["payload"]) {
        return Err("legacy rollout grammar is not mirrored".into());
    }
    let conversation = row["payload"]["id"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or("invalid native identity")?;
    let reported = row["payload"]["cli_version"]
        .as_str()
        .ok_or("missing runtime version")?;
    // An upgrade resumes the same rollout and keeps its creation-time
    // cli_version. The writer is verified separately against the exact installed
    // bundle inode; a supported paginated header is not its current runtime version.
    let app_header = source == "vscode"
        && app_originator(&row["payload"])
        && row["payload"]["history_mode"] == "paginated";
    let desktop_upgrade = app_header
        && (at_least(version, [0, 159, 2]) || (desktop_bundle && desktop_prerelease(version)));
    let cli_upgrade = row["payload"]["history_mode"] == "paginated"
        && (source == "cli" || row["payload"]["originator"] == "codex-tui")
        && at_least(version, [0, 159, 2]);
    // A conversation the bundle started carries the bundle's prerelease in its header too.
    let header = accepted_version(reported) || (desktop_bundle && app_header && desktop_prerelease(reported));
    let verified = header && (desktop_upgrade || cli_upgrade || (accepted_version(version) && reported == version));
    let (status, active_turn, title) = tail_state(&path);
    let title = root.parent().and_then(|codex| thread_name(&codex.join("session_index.jsonl"), conversation)).or(title);
    Ok(NativeSession {
        provider: "codex".into(),
        conversation: conversation.into(),
        profile: fingerprint(&root.to_string_lossy()),
        process,
        transcript: Some(path),
        socket: None,
        socket_inode: None,
        version: if verified { version } else { reported }.into(),
        restriction: (!verified).then(|| "runtime-unverified".into()),
        status: status.into(),
        title,
        // The backend's SessionRegistration caps cwd_name at 128 characters.
        cwd_name: row["payload"]["cwd"]
            .as_str()
            .and_then(|c| Path::new(c).file_name())
            .map(|n| n.to_string_lossy().chars().take(128).collect()),
        active_turn,
        last_assistant_message: None,
        holder: None,
        direct_holder: false,
        job: None,
        // A fork's header names the thread it came from (`forked_from_id`, 0.164.0), so its lineage
        // outlives the daemon that ran it; the app-server's `forkedFromId` says the same while loaded.
        forked_from: row["payload"]["forked_from_id"].as_str().filter(|id| !id.is_empty() && id.len() <= 128).map(str::to_owned),
        forked_by_person: None,
        aliases: vec![],
        // The header says who wrote it: the app's own conversations, never a TUI (the app's
        // bundled CLI run in a terminal writes `cli`).
        app: source == "vscode" && app_originator(&row["payload"]),
        attention: Default::default(),
    })
}

/// Like `open_files`, but keeps only paths the process holds open for
/// writing (lsof access mode `w` or `u` -- read-write; `r` is read-only).
/// Codex-only: a process merely reading another thread's rollout -- the
/// app-server loading a thread's history, the CLI resume picker browsing
/// past sessions -- must never be mistaken for that thread's owner, so
/// `capture` cannot use the shared `open_files` (which Claude's capture
/// keeps using unchanged).
async fn open_write_files(pid: u32) -> Result<Vec<PathBuf>> {
    let mut mode = None;
    Ok(native_output(
        Path::new("/usr/sbin/lsof"),
        &["-n", "-a", "-p", &pid.to_string(), "-Fan"],
    )
    .await?
    .lines()
    .filter_map(|line| {
        if line.starts_with('f') {
            mode = None;
            None
        } else if let Some(a) = line.strip_prefix('a') {
            mode = a.chars().next();
            None
        } else {
            line.strip_prefix('n')
                .filter(|p| p.starts_with('/') && matches!(mode, Some('w') | Some('u')))
        }
    })
    .map(PathBuf::from)
    .collect())
}

/// The thread to join for a live terminal: one whose rollout Codex's shared app-server daemon
/// holds (`daemon_release`). `codex resume <thread>` is then one more client of it
/// (`holder::manager::attach_argv`). Not the desktop app's own app server, and not a `--no-daemon`
/// TUI (a held session): neither is on the shared daemon.
fn attach_thread(home: &Path, execution: &Process, conversation: &str) -> Option<String> {
    (daemon_release(home, execution).is_some() && crate::holder::manager::attach_provider(conversation) == Some("codex"))
        .then(|| conversation.to_owned())
}

/// A single Codex process can host more than one thread at once (the ChatGPT
/// / Codex Desktop app runs one `codex app-server` pid for every open
/// conversation), each with its own rollout file open. Mirror every rollout
/// `from_rollout` accepts and that the process currently holds open for
/// writing; a rollout it refuses (exec, subagent, legacy grammar, other
/// originator) is skipped rather than failing the whole process, unless none
/// is accepted, in which case the first refusal's error is returned.
pub(super) async fn capture(home: &Path, program: &Path, pid: u32) -> Result<Vec<NativeSession>> {
    let execution = process(pid)?;
    let root = home.join(".codex/sessions");
    confined_path(home, &root)?;
    let mut paths: Vec<_> = open_write_files(pid)
        .await?
        .into_iter()
        .filter(|p| {
            p.starts_with(&root)
                && p.file_name()
                    .is_some_and(|n| n.to_string_lossy().starts_with("rollout-"))
                && p.extension().is_some_and(|e| e == "jsonl")
        })
        .collect();
    // The shared daemon says which threads it runs and what each is doing (spec 2026-10-26 (b)):
    // it lets an idle thread's rollout go while the thread stays loaded, so the open files alone
    // lost every idle thread. A listing that fails says nothing: the open files still count.
    let control = control_socket(home, &execution).await;
    let threads = match &control {
        Some((socket, _)) => loaded_threads(socket).await.ok(),
        None => None,
    };
    if daemon_release(home, &execution).is_some() {
        remember_loaded(&execution, threads.as_deref());
    }
    paths.extend(threads.iter().flatten().filter_map(|thread| thread_rollout(&root, thread)));
    if paths.is_empty() {
        return Err("native rollout is not available yet".into());
    }
    // A writer and a reader (or two writers) on the same rollout must still
    // yield one session.
    paths.sort();
    paths.dedup();
    let desktop_bundle = daemon_release(home, &execution).is_none()
        && desktop_program_in(Path::new(DESKTOP_RESOURCES), Some(&execution.executable)).is_some();
    let version = match daemon_release(home, &execution) {
        Some((path, version)) => named_release_version(&execution, &path, &version).await,
        None => {
            let installed = desktop_program_in(Path::new(DESKTOP_RESOURCES), Some(&execution.executable))
                .unwrap_or_else(|| program.to_path_buf());
            runtime_version(&execution, &installed, "codex").await
        }
    }
    .unwrap_or_default();
    let mut found = Vec::with_capacity(paths.len());
    let mut refusal = None;
    for path in &paths {
        match from_writer(&root, execution.clone(), path, &version, desktop_bundle) {
            Ok(mut session) => {
                if let Some((path, inode)) = &control {
                    session.socket = Some(path.clone());
                    session.socket_inode = Some(*inode);
                }
                session.job = attach_thread(home, &execution, &session.conversation);
                let thread = threads.iter().flatten().find(|t| t["id"] == session.conversation.as_str());
                // A subagent's rollout is refused above (its source is not a user's); one the
                // listing names is still its parent's, never a row (Codex desktop nests it).
                if thread.is_some_and(subagent) {
                    refusal.get_or_insert_with(|| "a subagent thread is its parent's".to_owned());
                    continue;
                }
                if let Some(thread) = thread {
                    apply_thread(&mut session, thread);
                }
                session.attention.agents = working_children(threads.iter().flatten(), &session.conversation);
                if let (Some((socket, _)), Some(thread)) = (&control, thread) {
                    let polled = match waiting_on_approval(thread) {
                        true => Some(open_approval(socket, &session.conversation).await),
                        false => None,
                    };
                    update_approval(&session.conversation, polled);
                }
                found.push(session);
            },
            Err(e) => {
                refusal.get_or_insert(e);
            }
        };
    }
    if process(pid)? != execution {
        return Err("process changed during discovery".into());
    }
    if found.is_empty() {
        // paths was non-empty and found is empty, so every path hit the Err
        // arm above, and each one sets refusal -- it is always Some here.
        return Err(refusal.expect("a refusal was recorded for every refused path"));
    }
    Ok(found)
}

pub(super) async fn discover(home: &Path) -> Result<Vec<NativeSession>> {
    let Some(program) = program(home) else {
        return Ok(vec![]);
    };
    let processes = native_output(Path::new("/bin/ps"), &["-axo", "pid=,comm="]).await?;
    let mut found = vec![];
    for row in processes.lines() {
        let mut fields = row.trim().splitn(2, char::is_whitespace);
        let Some(pid) = fields.next().and_then(|p| p.parse().ok()) else {
            continue;
        };
        let Some(name) = fields.next() else {
            continue;
        };
        if !name.contains("/codex") {
            continue;
        }
        if let Ok(sessions) = capture(home, &program, pid).await {
            found.extend(sessions);
        }
    }
    Ok(found)
}

fn queued_id(output: &str, thread: &str) -> Option<String> {
    let (id, target) = output
        .trim()
        .strip_prefix("Queued message ")?
        .strip_suffix('.')?
        .split_once(" for thread ")?;
    (target == thread && uuid::Uuid::parse_str(id).is_ok()).then(|| id.into())
}

pub(super) async fn send(native: &NativeSession, text: &str) -> Result<Option<String>> {
    let output = native_output(
        &native.process.executable,
        &["queue", "--thread", &native.conversation, "--message", text],
    )
    .await?;
    if process(native.process.pid)? != native.process {
        return Ok(None);
    }
    Ok(queued_id(&output, &native.conversation))
}

/// A live control is never retried by the journal after the daemon sees it.
/// `Ok(None)` is an explicit app-server refusal; transport ambiguity is `Err`.
pub(super) async fn control(home: &Path, native: &NativeSession, kind: &str, turn: &str, text: Option<&str>) -> Result<Option<String>> {
    let (Some(socket), Some(inode), Some(rollout)) = (&native.socket, native.socket_inode, &native.transcript) else {
        return Ok(None);
    };
    if native.provider != "codex" || native.version != CONTROL_DAEMON_VERSION
        || !matches!(kind, "steer" | "interrupt") || turn.is_empty()
        || process(native.process.pid)? != native.process
        || control_socket(home, &native.process).await.as_ref() != Some(&(socket.clone(), inode))
    {
        return Ok(None);
    }
    let (status, active, _, _) = latest_turn(rollout)?;
    if status != "busy" || active.as_deref() != Some(turn) { return Ok(None); }
    let request = if kind == "steer" {
        json!({"id":2,"method":"turn/steer","params":{"threadId":native.conversation,
            "expectedTurnId":turn,"input":[{"type":"text","text":text.ok_or("steer text missing")?}]}})
    } else {
        json!({"id":2,"method":"turn/interrupt","params":{"threadId":native.conversation,"turnId":turn}})
    };
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        let response = control_rpc(socket, &request).await?;
        if process(native.process.pid)? != native.process
            || fs::symlink_metadata(socket).map_err(|_| "control socket changed")?.ino() != inode
        {
            return Err("control runtime changed".into());
        }
        if response.get("error").is_some() { return Ok(None); }
        let accepted = if kind == "steer" { response["result"]["turnId"].as_str() == Some(turn) }
            else { response["result"].as_object().is_some_and(|result| result.is_empty()) };
        Ok(accepted.then(|| turn.to_owned()))
    }).await.map_err(|_| "control response timed out".to_owned())?
}

async fn control_rpc(socket: &Path, request: &Value) -> Result<Value> {
    let mut conn = match connect(socket).await? {
        Ok(conn) => conn,
        Err(refused) => return Ok(refused),
    };
    conn.send(Message::Text(request.to_string().into())).await.map_err(|_| "control request failed")?;
    rpc_response(&mut conn, 2).await
}

/// The name this Mac's app-server client gives. Codex stamps the FIRST client's name into every
/// thread its daemon creates after that (`session_meta.originator`, observed on 0.164.0): a daemon
/// whose first client was this one marked the person's own TUI threads as ours, which
/// `accepted_app_source` then refuses. It reads as the TUI the daemon serves, so the stamp stays true.
const CLIENT_NAME: &str = "codex-tui";

type Conn = WebSocketStream<tokio::net::UnixStream>;

/// What every daemon door says when the daemon cannot be reached or does not finish `initialize`
/// in time: a start or resume then falls back to a private `--no-daemon` writer.
pub(crate) const UNAVAILABLE: &str = "Codex's shared daemon did not answer";
/// A stalled app-server must not hold a start, a resume or the watcher forever.
const HANDSHAKE: std::time::Duration = std::time::Duration::from_secs(5);

/// An initialized app-server connection, or the server's refusal of `initialize` (Err inside Ok).
/// Err(UNAVAILABLE) when the socket or the handshake fails or takes longer than HANDSHAKE.
async fn connect(socket: &Path) -> Result<std::result::Result<Conn, Value>> {
    tokio::time::timeout(HANDSHAKE, async {
        let stream = tokio::net::UnixStream::connect(socket).await.ok()?;
        let (mut conn, _) = client_async("ws://localhost/", stream).await.ok()?;
        let init = json!({"id":1,"method":"initialize","params":{"clientInfo":{"name":CLIENT_NAME,"title":"Lightbulb","version":"0.1.0"}}});
        conn.send(Message::Text(init.to_string().into())).await.ok()?;
        let initialized = rpc_response(&mut conn, 1).await.ok()?;
        if initialized.get("error").is_some() { return Some(Err(initialized)); }
        conn.send(Message::Text(r#"{"method":"initialized","params":{}}"#.into())).await.ok()?;
        Some(Ok(conn))
    })
    .await
    .ok()
    .flatten()
    .ok_or_else(|| UNAVAILABLE.to_owned())
}

async fn connected(socket: &Path) -> Result<Conn> {
    connect(socket).await?.map_err(|_| UNAVAILABLE.into())
}

/// One request's `result`; an error answer is Err.
async fn call(conn: &mut Conn, id: u64, method: &str, params: Value) -> Result<Value> {
    let request = json!({"id": id, "method": method, "params": params});
    conn.send(Message::Text(request.to_string().into())).await.map_err(|_| "control request failed")?;
    let mut response = rpc_response(conn, id).await?;
    if response.get("error").is_some() { return Err(format!("{method} refused")); }
    Ok(response["result"].take())
}

/// Notifications for every thread reach every client (`thread/status/changed`, `thread/started`,
/// observed on 0.164.0), so a response can sit behind many of them; an oversized one is skipped.
async fn rpc_response(conn: &mut Conn, id: u64) -> Result<Value> {
    for _ in 0..512 {
        let Some(frame) = conn.next().await else { break; };
        let Message::Text(text) = frame.map_err(|_| "control response unavailable")? else { continue; };
        if text.len() > 1024 * 1024 { continue; }
        let row: Value = serde_json::from_str(&text).map_err(|_| "control response invalid")?;
        // A server request (an approval) carries its own id and a method: never our response.
        if row["id"].as_u64() == Some(id) && row.get("method").is_none() { return Ok(row); }
    }
    Err("control response unavailable".into())
}

/// Every thread the daemon has loaded (`thread/loaded/list`), as `thread/read` describes it.
async fn loaded_threads(socket: &Path) -> Result<Vec<Value>> {
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        let mut conn = connected(socket).await?;
        let mut ids = vec![];
        let mut cursor = Value::Null;
        for page in 0..8 {
            let listed = call(&mut conn, 10 + page, "thread/loaded/list", json!({"cursor": cursor})).await?;
            ids.extend(listed["data"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned));
            cursor = listed["nextCursor"].clone();
            if cursor.is_null() || ids.len() >= 512 { break; }
        }
        let mut threads = vec![];
        for (n, id) in ids.iter().take(512).enumerate() {
            // A thread unloaded between the two calls is simply not running.
            if let Ok(mut read) = call(&mut conn, 100 + n as u64, "thread/read", json!({"threadId": id})).await {
                threads.push(read["thread"].take());
            }
        }
        Ok(threads)
    })
    .await
    .map_err(|_| "control response timed out".to_owned())?
}

/// A subagent thread belongs to its parent's row (Codex desktop shows it nested), never its own.
fn subagent(thread: &Value) -> bool {
    !thread["parentThreadId"].is_null() || thread["source"].get("subAgent").is_some()
}

/// How many loaded threads `parent` spawned are working now (`parentThreadId`): the row's "2 agents
/// running". Only direct children: a grandchild is its own parent's.
fn working_children<'a>(threads: impl Iterator<Item = &'a Value>, parent: &str) -> u32 {
    threads
        .filter(|t| t["parentThreadId"] == parent && matches!(thread_status(&t["status"]), Some("busy" | "waiting-approval")))
        .count() as u32
}

/// The app-server's `ThreadStatus` in the mirror's words (the state table, spec 2026-10-26 (e)):
/// working is busy; waiting on an approval or on an answer is waiting on its person; idle (its last
/// turn done) and a system error are idle. `notLoaded` runs nowhere: None, the row is stopped.
fn thread_status(status: &Value) -> Option<&'static str> {
    match status["type"].as_str()? {
        "active" if status["activeFlags"].as_array().is_some_and(|flags| !flags.is_empty()) => Some("waiting-approval"),
        "active" => Some("busy"),
        "idle" | "systemError" => Some("idle"),
        _ => None,
    }
}

/// The rollout of a loaded thread that is a row: a person's thread, written to disk, running.
fn thread_rollout(root: &Path, thread: &Value) -> Option<PathBuf> {
    if subagent(thread) || thread["ephemeral"] == true || thread_status(&thread["status"]).is_none() {
        return None;
    }
    let path = PathBuf::from(thread["path"].as_str()?);
    (path.starts_with(root) && path.is_file()).then_some(path)
}

/// What the daemon says outranks what the rollout's tail suggests: its status, and the thread a
/// fork came from (`forkedFromId`), which the rollout does not carry.
fn apply_thread(session: &mut NativeSession, thread: &Value) {
    if let Some(status) = thread_status(&thread["status"]) {
        session.status = status.into();
    }
    if let Some(origin) = thread["forkedFromId"].as_str().filter(|id| !id.is_empty() && id.len() <= 128) {
        session.forked_from = Some(origin.to_owned());
    }
}

/// The threads the shared daemon had loaded at its last listing, by daemon process.
static LOADED: std::sync::Mutex<Option<(Process, std::collections::BTreeSet<String>)>> = std::sync::Mutex::new(None);

fn remember_loaded(daemon: &Process, threads: Option<&[Value]>) {
    let Ok(mut loaded) = LOADED.lock() else { return };
    match threads {
        Some(threads) => {
            *loaded = Some((daemon.clone(), threads.iter().filter_map(|t| t["id"].as_str()).map(str::to_owned).collect()));
        }
        // A failed listing knows nothing: liveness falls back to the daemon's pid. Only this
        // process's own listing is forgotten: the daemon's updater (same release, its own socket)
        // never answers one, and must not erase the daemon's.
        None if loaded.as_ref().is_some_and(|(known, _)| known == daemon) => *loaded = None,
        None => {}
    }
}

/// Whether the daemon `process` still has `conversation` loaded: None when its last listing is
/// unknown. A thread it dropped is stopped although the daemon runs on (`notLoaded`).
pub(super) fn loaded(process: &Process, conversation: &str) -> Option<bool> {
    let loaded = LOADED.lock().ok()?;
    let (daemon, threads) = loaded.as_ref()?;
    (daemon == process).then(|| threads.contains(conversation))
}

/// The approvals Lightbulb can hold for a Codex thread: a command, or a patch.
const APPROVAL_METHODS: [&str; 2] = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"];
/// How long a joining client waits for the daemon to replay a thread's open request.
const REPLAY: std::time::Duration = std::time::Duration::from_millis(1500);

fn waiting_on_approval(thread: &Value) -> bool {
    thread["status"]["activeFlags"].as_array().is_some_and(|flags| flags.iter().any(|f| f == "waitingOnApproval"))
}

/// The open approval of each waiting daemon thread, as its last discovery read it.
static APPROVALS: std::sync::Mutex<std::collections::BTreeMap<String, Value>> = std::sync::Mutex::new(std::collections::BTreeMap::new());

pub(super) fn remember_approval(thread: &str, request: Option<Value>) {
    let Ok(mut open) = APPROVALS.lock() else { return };
    match request {
        Some(request) => open.insert(thread.to_owned(), request),
        None => open.remove(thread),
    };
}

/// One discovery's word on a thread's approval: None when it no longer waits on one (the request
/// is gone), else the replay. A replay that failed (`Err`, a daemon timeout) knows nothing: the
/// thread still waits, so the request held before is kept rather than dropping its card.
fn update_approval(thread: &str, polled: Option<Result<Option<Value>>>) {
    match polled {
        None => remember_approval(thread, None),
        Some(Ok(request)) => remember_approval(thread, request),
        Some(Err(_)) => {}
    }
}

/// The approval request the daemon holds open for `thread`, as discovery last read it.
pub(super) fn open_request(thread: &str) -> Option<Value> {
    APPROVALS.lock().ok()?.get(thread).cloned()
}

/// A Codex approval as Lightbulb holds it: what it shows, and the input whose digest an answer
/// must name. A command is its whole command line, so it can be `full`. A patch request carries
/// no patch (only its item id), so nobody here saw the change: deny-only.
#[derive(Debug, PartialEq)]
pub(super) struct Approval {
    pub item: String,
    pub tool: &'static str,
    pub summary: String,
    pub input: Value,
    pub full: bool,
    /// "Allow for this conversation" (`acceptForSession`) is offered.
    pub session: bool,
}

pub(super) fn approval(request: &Value) -> Option<Approval> {
    let params = &request["params"];
    let item = params["itemId"].as_str().filter(|i| !i.is_empty() && i.len() <= 128)?.to_owned();
    match request["method"].as_str()? {
        "item/commandExecution/requestApproval" => {
            let command = params["command"].as_str().filter(|c| !c.trim().is_empty())?;
            let input = json!({"command": command});
            Some(Approval {
                item,
                tool: "Bash",
                summary: command.to_owned(),
                full: super::approval::shown_input("Bash", &input).1,
                input,
                session: decision(request, "allow", Some("session")).is_some(),
            })
        }
        "item/fileChange/requestApproval" => Some(Approval {
            input: json!({"itemId": item}),
            item,
            tool: "apply_patch",
            summary: params["reason"].as_str().unwrap_or("Edit files").to_owned(),
            full: false,
            session: false,
        }),
        _ => None,
    }
}

/// The approval's id in Lightbulb (`approval::valid_id`): stable for one request of one thread,
/// so rediscovering it never reads as a new prompt.
pub(super) fn approval_id(thread: &str, item: &str) -> String {
    format!("appr-{}", &hex::encode(Sha256::digest(format!("{thread}\n{item}")))[..32])
}

/// Codex's answer for a decision Lightbulb admitted, only if this request offers it. "Similar"
/// (`acceptWithExecpolicyAmendment`) and network rules persist past the thread: never from here.
fn decision(request: &Value, decision: &str, mode: Option<&str>) -> Option<&'static str> {
    let offered = |choice: &str| {
        request["params"]["availableDecisions"].as_array().is_none_or(|all| all.iter().any(|d| d == choice))
    };
    let wanted: &[&'static str] = match (decision, mode) {
        ("allow", None) => &["accept"],
        ("allow", Some("session")) => &["acceptForSession"],
        // Declined, the turn goes on; where Codex offers only cancel, the turn stops.
        ("deny", None) => &["decline", "cancel"],
        _ => return None,
    };
    wanted.iter().copied().find(|choice| offered(choice))
}

/// Join `thread` and read the approval the daemon replays to a new subscriber. Observed on
/// 0.163.0: a client that resumes a loaded thread is sent its open server requests right after
/// the resume, may answer one like the client that started the turn, and every subscriber then
/// gets `serverRequest/resolved`. A join without overrides changes nothing in the thread.
async fn replayed_request(conn: &mut Conn, thread: &str) -> Result<Option<Value>> {
    let resume = json!({"id":2,"method":"thread/resume","params":{"threadId":thread,"excludeTurns":true}});
    conn.send(Message::Text(resume.to_string().into())).await.map_err(|_| "control request failed")?;
    let (mut joined, mut found) = (false, None);
    for _ in 0..512 {
        let wait = if joined { REPLAY } else { HANDSHAKE };
        let Ok(frame) = tokio::time::timeout(wait, conn.next()).await else { break };
        let Some(frame) = frame else { break };
        let Message::Text(text) = frame.map_err(|_| "control response unavailable")? else { continue };
        if text.len() > 1024 * 1024 { continue; }
        let row: Value = serde_json::from_str(&text).map_err(|_| "control response invalid")?;
        if row.get("method").is_none() && row["id"].as_u64() == Some(2) {
            if row.get("error").is_some() { return Err("thread/resume refused".into()); }
            joined = true;
        } else if row["params"]["threadId"] == thread
            && row.get("id").is_some()
            && row["method"].as_str().is_some_and(|m| APPROVAL_METHODS.contains(&m))
        {
            found.get_or_insert(row);
        }
        if joined && found.is_some() { break; }
    }
    if !joined {
        return Err("thread/resume unanswered".into());
    }
    Ok(found)
}

/// Ok(None): the daemon joined us to the thread and replayed no approval. Err: unknown.
async fn open_approval(socket: &Path, thread: &str) -> Result<Option<Value>> {
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        let mut conn = connected(socket).await?;
        replayed_request(&mut conn, thread).await
    })
    .await
    .map_err(|_| "approval replay timed out".to_owned())?
}

/// Answer the thread's open approval, only if it is still the one held (`item`, `digest`) and
/// offers this decision. Ok(true) once the daemon says the request is resolved; Ok(false) when
/// it is not ours to answer (gone, changed, or another runtime); Err when that is unknown.
pub(super) async fn answer(home: &Path, native: &NativeSession, item: &str, digest: &str, choice: &str, mode: Option<&str>) -> Result<bool> {
    let (Some(socket), Some(inode)) = (&native.socket, native.socket_inode) else { return Ok(false) };
    if native.provider != "codex"
        || process(native.process.pid)? != native.process
        || control_socket(home, &native.process).await.as_ref() != Some(&(socket.clone(), inode))
    {
        return Ok(false);
    }
    answer_on(socket, &native.conversation, item, digest, choice, mode).await
}

async fn answer_on(socket: &Path, thread: &str, item: &str, digest: &str, choice: &str, mode: Option<&str>) -> Result<bool> {
    tokio::time::timeout(std::time::Duration::from_secs(10), async {
        let mut conn = connected(socket).await?;
        let Some(request) = replayed_request(&mut conn, thread).await? else { return Ok(false) };
        let held = approval(&request).filter(|a| a.item == item && super::approval::digest(&a.input) == digest);
        let Some(answer) = held.and(decision(&request, choice, mode)) else { return Ok(false) };
        let reply = json!({"id": request["id"], "result": {"decision": answer}});
        conn.send(Message::Text(reply.to_string().into())).await.map_err(|_| "approval answer failed")?;
        for _ in 0..512 {
            let Some(frame) = conn.next().await else { break };
            let Message::Text(text) = frame.map_err(|_| "approval answer unconfirmed")? else { continue };
            let Ok(row) = serde_json::from_str::<Value>(&text) else { continue };
            if row["method"] == "serverRequest/resolved"
                && row["params"]["threadId"] == thread
                && row["params"]["requestId"] == request["id"]
            {
                return Ok(true);
            }
        }
        Err("approval answer unconfirmed".into())
    })
    .await
    .map_err(|_| "approval answer unconfirmed".to_owned())?
}

/// The shared daemon's control socket while it runs: Codex's own link, resolved to a socket this
/// user owns in `/tmp/codex-daemon-<uid>`. None: no daemon, and a start falls back to `--no-daemon`.
pub(crate) fn daemon_socket(home: &Path) -> Option<PathBuf> {
    let uid = unsafe { libc::geteuid() };
    let dir = fs::canonicalize("/tmp").ok()?.join(format!("codex-daemon-{uid}"));
    let path = fs::canonicalize(home.join(".codex/app-server-control/app-server-control.sock")).ok()?;
    let meta = fs::symlink_metadata(&path).ok()?;
    (path.parent() == Some(dir.as_path()) && meta.file_type().is_socket() && meta.uid() == uid).then_some(path)
}

/// The daemon process, from its own pid file, only when it is a verified managed release.
fn daemon_process(home: &Path) -> Option<Process> {
    let text = fs::read_to_string(home.join(".codex/app-server-daemon/daemon.pid")).ok()?;
    let row: Value = serde_json::from_str(&text).ok()?;
    let daemon = process(u32::try_from(row["pid"].as_u64()?).ok()?).ok()?;
    daemon_release(home, &daemon).is_some().then_some(daemon)
}

/// Wakes discovery whenever the daemon says a thread started, closed or changed status, so a row's
/// status follows the app-server, not the 60 s backstop. Runs until aborted.
pub(super) async fn watch(home: PathBuf, wake: std::sync::Arc<tokio::sync::Notify>) {
    loop {
        if let Some(socket) = daemon_socket(&home) {
            if let Ok(mut conn) = connected(&socket).await {
                while let Some(Ok(frame)) = conn.next().await {
                    let Message::Text(text) = frame else { continue };
                    if text.len() <= 1024 * 1024 && wakes(&text) {
                        wake.notify_one();
                    }
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

fn wakes(frame: &str) -> bool {
    serde_json::from_str::<Value>(frame)
        .is_ok_and(|row| matches!(row["method"].as_str(), Some("thread/status/changed" | "thread/started" | "thread/closed")))
}

/// The daemon form of a held Codex argv (`session_start::argv`, `resume::argv`): a TUI client of
/// the shared daemon, not a private writer.
pub(crate) fn on_daemon(argv: &[String]) -> bool {
    argv.get(1..3).is_some_and(|flags| flags[0] == "--remote" && flags[1] == "unix://")
}

/// The private form of a daemon start argv (`session_start::argv`): what it falls back to when the
/// daemon does not answer.
pub(crate) fn private_argv(argv: &[String]) -> Vec<String> {
    if !on_daemon(argv) || argv.len() < 7 {
        return argv.to_vec();
    }
    // [program, --remote, unix://, -C, folder, -c, check_for_update_on_startup=false, rest...]
    let mut private = vec![argv[0].clone(), "--no-daemon".into()];
    private.extend_from_slice(&argv[7..]);
    private
}

/// A held Codex start on the shared daemon. Its start hook runs in the daemon, which has neither
/// the holder's environment nor its process, and nothing in the protocol names the client that
/// started a thread. So the thread is learned here and bound only when it is provably this start's
/// (`our_thread`): that binds the holder (`holder::manager::bind_thread`) and the start link. Not
/// proven: the thread is still listed, as a background one, and the start is not linked.
/// Err(UNAVAILABLE) before anything launched: the caller falls back to `--no-daemon`.
pub(crate) async fn start_on_daemon(
    home: &Path,
    folder: PathBuf,
    argv: Vec<String>,
    link: Option<(String, String)>,
) -> std::result::Result<HolderInfo, String> {
    let socket = daemon_socket(home).ok_or(UNAVAILABLE)?;
    // Listening before the TUI asks for its thread, and knowing what already runs.
    let mut conn = connected(&socket).await?;
    let before: std::collections::BTreeSet<String> = tokio::time::timeout(HANDSHAKE, call(&mut conn, 3, "thread/loaded/list", json!({})))
        .await
        .ok()
        .and_then(|listed| listed.ok())
        .ok_or(UNAVAILABLE)?["data"]
        .as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect();
    let want = fs::canonicalize(&folder).map_err(|e| e.to_string())?;
    let prompt = argv.iter().position(|a| a == "--").and_then(|at| argv.get(at + 1)).cloned();
    let info = crate::session_start::start_held(folder, argv).await?;
    let (home, held) = (home.to_path_buf(), info.clone());
    tokio::spawn(async move {
        let thread = match our_thread(&mut conn, &want, &before, prompt.as_deref(), GRACE).await {
            Ok(thread) => thread,
            Err(why) => {
                crate::log(&format!("[session-start] Codex: this start's thread is not bound ({why}); it is listed unlinked"));
                return;
            }
        };
        crate::holder::manager::bind_thread(&held, &thread);
        if let (Some((start_id, scope)), Some(daemon)) = (link, daemon_process(&home)) {
            if let Err(e) = super::start_links::record(&home, &start_id, &scope, "codex", &thread, daemon) {
                crate::log(&format!("[session-start] Codex start link not recorded: {e}"));
            }
        }
    });
    Ok(info)
}

/// How long after the first candidate another start in the same folder still makes it ambiguous.
const GRACE: std::time::Duration = std::time::Duration::from_secs(5);

/// This start's thread, or why it cannot be proven. A candidate is a person's thread that STARTED
/// (`thread/started`, not one already loaded: a resume elsewhere) in `folder` after the launch.
/// Exactly one may appear by GRACE after the first; two (another client, a second Lightbulb start
/// in the same folder) bind neither. With a prompt, the thread's first user message (`preview`,
/// observed verbatim on 0.164.0) must be it: a different first message is another client's.
async fn our_thread(
    conn: &mut Conn,
    folder: &Path,
    before: &std::collections::BTreeSet<String>,
    prompt: Option<&str>,
    grace: std::time::Duration,
) -> std::result::Result<String, &'static str> {
    use tokio::time::{timeout_at, Instant};
    let mut found: Vec<String> = vec![];
    let mut until = Instant::now() + std::time::Duration::from_secs(60);
    loop {
        match timeout_at(until, conn.next()).await {
            Ok(Some(Ok(Message::Text(text)))) => {
                if let Some(id) = started_thread(&text, folder).filter(|id| !before.contains(id) && !found.contains(id)) {
                    if found.is_empty() {
                        until = Instant::now() + grace;
                    }
                    found.push(id);
                }
            }
            Ok(Some(Ok(_))) => {}
            Ok(_) => return Err("the daemon closed the connection"),
            Err(_) => break,
        }
    }
    let [thread] = found.as_slice() else {
        return Err(if found.is_empty() { "no thread started" } else { "another thread started in the same folder" });
    };
    let Some(prompt) = prompt.filter(|p| !p.is_empty()) else { return Ok(thread.clone()) };
    for n in 0..60 {
        let read = call(conn, 200 + n, "thread/read", json!({"threadId": thread})).await.map_err(|_| "thread unreadable")?;
        match first_turn(read["thread"]["preview"].as_str().unwrap_or(""), prompt) {
            Some(true) => return Ok(thread.clone()),
            Some(false) => return Err("its first message is not this start's prompt"),
            None => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
        }
    }
    Err("its first message never came")
}

/// Whether a thread's first user message is this start's prompt; None while it has none.
fn first_turn(preview: &str, prompt: &str) -> Option<bool> {
    (!preview.is_empty()).then(|| preview == prompt)
}

fn started_thread(frame: &str, folder: &Path) -> Option<String> {
    let row: Value = serde_json::from_str(frame).ok()?;
    let thread = &row["params"]["thread"];
    if row["method"] != "thread/started" || subagent(thread) || thread["ephemeral"] == true {
        return None;
    }
    let cwd = fs::canonicalize(thread["cwd"].as_str()?).ok()?;
    let id = thread["id"].as_str().filter(|id| uuid::Uuid::parse_str(id).is_ok())?;
    (cwd == folder).then(|| id.to_owned())
}

/// Loads `thread` on the shared daemon for a resume, in `folder` with the mode the backend decided
/// (`plan`: read-only). A `--remote` TUI may not set permissions on a resume ("Permission overrides
/// are not supported when resuming a remote task", 0.163.0), and one that loads the thread itself
/// takes the person's global config, which may be `danger-full-access`. So the thread is loaded
/// here first, and the TUI rejoins it as it runs. A thread already running under looser terms is
/// refused. The connection stays open a while so the thread is loaded when the TUI arrives.
pub(super) async fn resume_on_daemon(socket: &Path, thread: &str, folder: &Path, plan: bool) -> Result<()> {
    let mut conn = connected(socket).await?;
    let params = resume_params(thread, folder, plan);
    let resumed = tokio::time::timeout(std::time::Duration::from_secs(20), call(&mut conn, 2, "thread/resume", params))
        .await
        .map_err(|_| "thread/resume timed out")??;
    resume_permissions(&resumed, plan)?;
    tokio::spawn(async move {
        // ponytail: a fixed hold, not a handshake (the daemon has no "client joined" event); 0.164.0
        // kept an idle thread loaded with no client at all.
        let _ = tokio::time::timeout(std::time::Duration::from_secs(60), async { while conn.next().await.is_some() {} }).await;
    });
    Ok(())
}

/// Internet is available in either mode; a teammate's filesystem remains read-only.
/// Named permission profiles and legacy sandbox modes are separate config systems, so the plan
/// request intentionally omits `sandbox`. A fresh name cannot inherit a user's broader profile.
fn resume_params(thread: &str, folder: &Path, plan: bool) -> Value {
    let mut params = json!({"threadId": thread, "cwd": folder, "excludeTurns": true,
        "approvalPolicy": "on-request"});
    if plan {
        let name = format!("lightbulb-plan-{}", uuid::Uuid::new_v4());
        let mut config = serde_json::Map::new();
        config.insert("default_permissions".into(), json!(name));
        config.insert(format!("permissions.{name}"),
            json!({"extends": ":read-only", "network": {"enabled": true}}));
        params["config"] = Value::Object(config);
    } else {
        params["sandbox"] = json!("workspace-write");
        params["config"] = json!({"sandbox_workspace_write.network_access": true});
    }
    params
}

/// `resume_on_daemon`'s refusal of a thread that already runs under looser permissions.
pub(super) const LOOSER: &str = "thread runs under looser permissions";

/// The daemon must keep file and approval restrictions while actually allowing Internet access.
/// A managed policy may refuse network access; report that refusal rather than silently opening
/// another unusable terminal or retrying with weaker permissions.
fn resume_permissions(resumed: &Value, plan: bool) -> Result<()> {
    if !within(resumed, plan) {
        return Err(LOOSER.into());
    }
    if resumed["sandbox"]["networkAccess"] != true {
        return Err("Codex did not allow Internet access for this session".into());
    }
    Ok(())
}

/// The thread's permissions after `thread/resume` are no looser than asked.
fn within(resumed: &Value, plan: bool) -> bool {
    let sandbox = resumed["sandbox"]["type"].as_str();
    let sandbox_ok = if plan { sandbox == Some("readOnly") } else { matches!(sandbox, Some("readOnly" | "workspaceWrite")) };
    sandbox_ok && matches!(resumed["approvalPolicy"].as_str(), Some("on-request" | "untrusted"))
}

const TURN_ROW_MAX: usize = 1024 * 1024;

#[derive(Default)]
struct TurnScan {
    inode: u64,
    offset: u64,
    boundary: [u8; 32],
    status: Option<&'static str>,
    turn: Option<String>,
    started_at: Option<String>,
    turn_cwd: Option<Option<PathBuf>>,
    partial: Vec<u8>,
    partial_applied: bool,
    skip_row: bool,
}

fn boundary(file: &mut fs::File, offset: u64) -> std::io::Result<[u8; 32]> {
    file.seek(SeekFrom::Start(offset.saturating_sub(64)))?;
    let mut bytes = vec![0; offset.min(64) as usize];
    file.read_exact(&mut bytes)?;
    Ok(Sha256::digest(bytes).into())
}

fn update_turn(scan: &mut TurnScan) -> bool {
    let row = &scan.partial;
    if ![b"task_started".as_slice(), b"task_complete", b"turn_aborted", b"turn_context"]
        .iter()
        .any(|needle| row.windows(needle.len()).any(|window| window == *needle))
    {
        return false;
    }
    let Ok(row) = serde_json::from_slice::<Value>(row) else { return false };
    if row["type"] == "turn_context" {
        scan.turn_cwd = Some(row["payload"]["cwd"].as_str().map(PathBuf::from));
        return true;
    }
    if row["type"] != "event_msg" { return false; }
    match row["payload"]["type"].as_str() {
        Some("task_started") => {
            scan.status = Some("busy");
            scan.turn = row["payload"]["turn_id"].as_str().map(str::to_owned);
            scan.started_at = row["timestamp"].as_str().map(str::to_owned);
            true
        }
        Some("task_complete" | "turn_aborted") => {
            scan.status = Some("idle");
            scan.turn = None;
            true
        }
        _ => false,
    }
}

/// Read each rollout byte once. A live turn may be farther from EOF than any
/// fixed tail window; subsequent discovery passes only inspect appended bytes.
fn latest_turn(path: &Path) -> Result<(&'static str, Option<String>, Option<String>, Option<Option<PathBuf>>)> {
    let mut file = secure_file(path)?;
    let meta = file.metadata().map_err(|_| "rollout unavailable")?;
    static CACHE: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<PathBuf, TurnScan>>> = std::sync::OnceLock::new();
    let mut cache = CACHE.get_or_init(Default::default).lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    let scan = cache.entry(path.to_path_buf()).or_default();
    let unchanged = scan.inode == meta.ino() && scan.offset <= meta.len()
        && boundary(&mut file, scan.offset).is_ok_and(|hash| hash == scan.boundary);
    if !unchanged { *scan = TurnScan::default(); }
    scan.inode = meta.ino();
    file.seek(SeekFrom::Start(scan.offset)).map_err(|_| "rollout unavailable")?;
    let mut reader = BufReader::new(file.take(meta.len() - scan.offset));
    loop {
        let bytes = reader.fill_buf().map_err(|_| "rollout unavailable")?;
        if bytes.is_empty() { break; }
        let end = bytes.iter().position(|byte| *byte == b'\n').map_or(bytes.len(), |n| n + 1);
        let complete = bytes[end - 1] == b'\n';
        if !scan.skip_row {
            if scan.partial.len() + end > TURN_ROW_MAX {
                // A turn context too large to parse must not leave an older
                // checkout looking current. Other oversized rows are ignored.
                if scan.partial.windows(b"\"type\":\"turn_context\"".len())
                    .any(|window| window == b"\"type\":\"turn_context\"")
                {
                    scan.turn_cwd = Some(None);
                }
                scan.skip_row = true;
                scan.partial.clear();
            } else {
                scan.partial.extend_from_slice(&bytes[..end]);
            }
        }
        scan.offset += end as u64;
        reader.consume(end);
        if complete {
            if !scan.skip_row && !scan.partial_applied { update_turn(scan); }
            scan.partial.clear();
            scan.partial_applied = false;
            scan.skip_row = false;
        }
    }
    if !scan.skip_row && !scan.partial_applied && !scan.partial.is_empty() {
        scan.partial_applied = update_turn(scan);
    }
    let mut file = reader.into_inner().into_inner();
    let hash = boundary(&mut file, scan.offset).map_err(|_| "rollout unavailable")?;
    scan.boundary = hash;
    if cache.len() > 512 { cache.retain(|key, _| key == path); }
    let scan = &cache[path];
    Ok((scan.status.unwrap_or("unknown"), scan.turn.clone(), scan.started_at.clone(), scan.turn_cwd.clone()))
}

/// `busy`, `idle` or `unknown` from the rollout's turn events alone.
pub(super) fn turn_status(path: &Path) -> &'static str {
    latest_turn(path).map_or("unknown", |(status, ..)| status)
}

pub(super) fn turn_finished(path: &Path, turn: &str) -> bool {
    latest_turn(path).is_ok_and(|(status, active, _, _)|
        status == "idle" || (status == "busy" && active.as_deref().is_some_and(|id| id != turn)))
}

fn title_in_window(file: &mut fs::File, offset: u64) -> Option<String> {
    file.seek(SeekFrom::Start(offset)).ok()?;
    let mut reader = BufReader::new(file.take(2 * 1024 * 1024));
    if offset > 0 {
        let mut partial = String::new();
        let _ = reader.read_line(&mut partial);
    }
    for line in reader.lines().map_while(|line| line.ok()) {
        let Ok(row) = serde_json::from_str::<Value>(&line) else { continue };
        let payload = &row["payload"];
        if row["type"] == "event_msg" && payload["type"] == "item_completed"
            && payload["item"]["type"] == "UserMessage"
        {
            let text = payload["item"]["content"].as_array().into_iter().flatten()
                .filter(|block| block["type"] == "text")
                .filter_map(|block| block["text"].as_str()).collect::<Vec<_>>().join(" ");
            let first = text.lines().next().unwrap_or("").trim();
            if !first.is_empty() && !first.starts_with(HEADER_PREFIX) && first != OWNER_LABEL.trim_end() {
                return Some(redact(first).chars().take(60).collect());
            }
        }
    }
    None
}

/// The name Codex gave the thread (or the person, with `/rename`): Codex appends one
/// `session_index.jsonl` row per naming, so the newest row for the thread wins. Only the
/// file's last MiB is read; a thread named before that falls back to its first prompt.
fn thread_name(index: &Path, thread: &str) -> Option<String> {
    let mut file = secure_file(index).ok()?;
    let len = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(1024 * 1024))).ok()?;
    let mut text = String::new();
    file.read_to_string(&mut text).ok()?;
    text.lines().rev().find_map(|line| {
        let row: Value = serde_json::from_str(line).ok()?;
        (row["id"] == thread).then(|| row["thread_name"].as_str().map(|n| n.trim().to_owned())).flatten()
    })
    .filter(|name| !name.is_empty())
    .map(|name| redact(&name).chars().take(60).collect())
}

/// Prefer the first prompt so the title survives a long turn and stays stable;
/// bound both scans to 2 MiB even when the rollout grows much larger.
pub(super) fn tail_state(path: &Path) -> (&'static str, Option<String>, Option<String>) {
    let (status, turn, _, _) = latest_turn(path).unwrap_or(("unknown", None, None, None));
    let Ok(mut file) = secure_file(path) else { return (status, turn, None) };
    let Ok(len) = file.metadata().map(|m| m.len()) else { return (status, turn, None) };
    let title = title_in_window(&mut file, 0).or_else(|| {
        (len > 2 * 1024 * 1024)
            .then(|| title_in_window(&mut file, len - 2 * 1024 * 1024))
            .flatten()
    });
    (status, turn, title)
}

pub(super) fn utc_timestamp(seconds: u64) -> Option<String> {
    let seconds = libc::time_t::try_from(seconds).ok()?;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::gmtime_r(&seconds, &mut tm) }.is_null() {
        return None;
    }
    Some(format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    ))
}

pub(super) fn active_turn_started_after(path: &Path, ended_at: u64, ended_at_ms: Option<u64>) -> bool {
    let Ok((_, _, timestamp, _)) = latest_turn(path) else { return false };
    let Some(timestamp) = timestamp else { return false };
    let Some(ended_stamp) = utc_timestamp(ended_at) else { return false };
    let Some(event_second) = timestamp.get(..19) else { return false };
    if event_second != ended_stamp { return event_second > ended_stamp.as_str(); }
    let Some(ended_at_ms) = ended_at_ms else { return false };
    let Some(event_fraction) = timestamp.get(20..).and_then(|rest| rest.split(|c| c == 'Z' || c == '+' || c == '-').next()) else { return false };
    let event_fraction = event_fraction.chars().take(9).collect::<String>();
    let Ok(mut event_nanos) = event_fraction.parse::<u64>() else { return false };
    for _ in event_fraction.len()..9 { event_nanos *= 10; }
    event_nanos / 1_000_000 > ended_at_ms % 1000
}

fn item(item: &Value, ts: Option<&str>) -> Option<Value> {
    let id = item["id"].as_str();
    let texts = |content: &Value, block: &str| {
        content
            .as_array()
            .into_iter()
            .flatten()
            .filter(|b| b["type"] == block)
            .filter_map(|b| b["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n")
    };
    let basename = |p: &str| {
        Path::new(p)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    Some(match item["type"].as_str()? {
        "UserMessage" => {
            let text = texts(&item["content"], "text");
            if text.is_empty() {
                return None;
            }
            if let Some(body) = owner_prompt(&text) {
                return Some(event("text", "user", body, ts, id));
            }
            match teammate_contribution(&text) {
                Some((provenance, body)) => {
                    let mut e = event("text", "user", &body, ts, id);
                    if let Some(actor) = actor_of(&provenance) {
                        e["actor"] = actor;
                    }
                    e
                }
                None => event("text", "user", &text, ts, id),
            }
        }
        "AgentMessage" => {
            // The TUI renders every AgentMessage, and the phase is often absent; withhold only a
            // phase this build does not know, which may not be transcript prose at all.
            if item["phase"]
                .as_str()
                .is_some_and(|p| p != "commentary" && p != "final_answer")
            {
                return None;
            }
            let text = texts(&item["content"], "Text");
            if text.is_empty() {
                return None;
            }
            event("text", "assistant", &text, ts, id)
        }
        "Reasoning" => {
            let text = item["summary_text"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("\n");
            event("thinking", "assistant", &text, ts, id)
        }
        "CommandExecution" => {
            let command = item["command"]
                .as_array()
                .and_then(|c| c.last())
                .and_then(Value::as_str)
                .unwrap_or("");
            let output = item["aggregated_output"].as_str().unwrap_or("");
            let (text, lines) = preview(output, PREVIEW_LINES);
            let failed =
                item["status"] == "failed" || item["exit_code"].as_i64().is_some_and(|c| c != 0);
            let mut e = event("tool", "assistant", &text, ts, id);
            let mut input = json!({"command": command});
            // Codex's own read/list/search verdict, so renderers can group "Explored" lines
            // as the terminal does. Only these keys; an absolute path leaves as its basename.
            let parsed: Vec<Value> = item["parsed_cmd"]
                .as_array()
                .into_iter()
                .flatten()
                .take(8)
                .map(|p| {
                    let mut out = serde_json::Map::new();
                    for key in ["type", "name", "path", "query"] {
                        if let Some(v) = p[key].as_str() {
                            let v = if key == "path" && v.starts_with('/') { basename(v) } else { v.to_owned() };
                            out.insert(key.into(), json!(v));
                        }
                    }
                    Value::Object(out)
                })
                .collect();
            if !parsed.is_empty() {
                input["parsed"] = json!(parsed);
            }
            let (input, input_cut) = bound_json(input, MAX_TOOL_INPUT);
            let (result, result_cut) = bound_json(
                json!({"exitCode": item["exit_code"], "output": output}),
                MAX_TOOL_RESULT,
            );
            e["tool"] = json!({"name": "CommandExecution",
                "status": if item["status"] == "in_progress" { "running" } else if failed { "error" } else { "done" },
                "input": input, "result": result, "lines": lines});
            if input_cut || result_cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "FileChange" => {
            let changes: serde_json::Map<String, Value> = item["changes"].as_object().into_iter().flatten()
                .map(|(path, change)| (basename(path), json!({"type": change["type"], "diff": if change["diff"].is_null() { &change["unified_diff"] } else { &change["diff"] }, "content": change["content"]})))
                .collect();
            let names = changes.keys().cloned().collect::<Vec<_>>().join(", ");
            let mut e = event("tool", "assistant", &names, ts, id);
            let (result, cut) = bound_json_by_path(Value::Object(changes), MAX_TOOL_RESULT);
            e["tool"] = json!({"name": "FileChange", "status": if item["status"] == "failed" { "error" } else { "done" },
                "result": result});
            if cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "McpToolCall" => {
            let private_send = matches!(item["server"].as_str(), Some("lightbulb_native_sessions" | "lightbulb"))
                && item["tool"].as_str().is_some_and(super::native_mcp::private_tool);
            let label = format!(
                "{}.{}",
                item["server"].as_str().unwrap_or(""),
                item["tool"].as_str().unwrap_or("")
            );
            let mut e = event("tool", "assistant", &label, ts, id);
            let (input, input_cut) = bound_json(
                if private_send { json!({"server": item["server"], "tool": item["tool"]}) }
                else { json!({"server": item["server"], "tool": item["tool"], "arguments": item["arguments"]}) },
                MAX_TOOL_INPUT,
            );
            let (result, result_cut) = bound_json(
                if private_send { json!({}) } else { json!({"content": item["result"]["content"]}) },
                MAX_TOOL_RESULT,
            );
            e["tool"] = json!({"name": "McpToolCall",
                "status": if item["result"]["isError"] == true || item["status"] == "failed" { "error" } else { "done" },
                "input": input, "result": result});
            if input_cut || result_cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "Extension" => {
            let query = item["query"].as_str().unwrap_or("");
            let mut e = event("tool", "assistant", query, ts, id);
            let (input, cut) = bound_json(json!({"query": query}), MAX_TOOL_INPUT);
            e["tool"] = json!({"name": item["kind"].as_str().unwrap_or("Extension"), "status": "done", "input": input});
            if cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "SubAgentActivity" | "CollabAgentToolCall" => {
            let label = item["agent_path"]
                .as_str()
                .or(item["tool"].as_str())
                .unwrap_or("");
            let mut e = event("tool", "assistant", label, ts, id);
            e["tool"] = json!({"name": item["type"], "status": "done"});
            e
        }
        "ContextCompaction" => lifecycle("compaction", "", ts, id),
        _ => return None,
    })
}

/// A spawned agent's own thread, seen from its parent: `started` opens it in the subagent
/// list and `completed` / `interrupted` closes it. Codex has no background/foreground split
/// (every spawned agent runs beside its parent), so none of these notify.
fn subagent_activity(item: &Value, ts: Option<&str>) -> Option<Value> {
    if item["type"] != "SubAgentActivity" {
        return None;
    }
    let child = item["agent_thread_id"].as_str()?;
    let label = item["agent_path"].as_str().unwrap_or("");
    let mut e = match item["kind"].as_str()? {
        "started" => lifecycle("subagent_start", label, ts, None),
        kind @ ("completed" | "interrupted") => {
            let mut e = lifecycle("subagent_end", label, ts, None);
            e["tool"] = json!({"name": "Agent", "status": if kind == "completed" { "done" } else { "error" },
                "result": {"status": kind, "background": false}});
            e
        }
        _ => return None,
    };
    e["subagent"] = json!(child);
    Some(e)
}

/// A Codex subagent's rollout, found by the thread id its parent's `SubAgentActivity`
/// names and admitted only when its own `session_meta` names `parent` as the thread that
/// spawned it. Codex files a rollout under the local date it started, so a subagent spawned
/// just now sits in one of the two newest day directories.
/// ponytail: an id never found (an ephemeral thread) is looked for on every read; cap it if
/// that ever shows up in a profile.
pub(super) fn child_rollout(transcript: &Path, parent: &str, child: &str) -> Option<(PathBuf, Value)> {
    let root = transcript.ancestors().nth(4)?;
    let newest = |dir: &Path| -> Vec<PathBuf> {
        let mut names: Vec<PathBuf> = fs::read_dir(dir)
            .into_iter()
            .flatten()
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.is_dir())
            .collect();
        names.sort();
        names.reverse();
        names
    };
    let days = newest(root)
        .iter()
        .flat_map(|year| newest(year))
        .flat_map(|month| newest(&month))
        .take(2)
        .collect::<Vec<_>>();
    let suffix = format!("-{child}.jsonl");
    let path = days.iter().find_map(|day| {
        fs::read_dir(day).ok()?.filter_map(|e| e.ok()).find_map(|e| {
            let name = e.file_name().into_string().ok()?;
            (name.starts_with("rollout-") && name.ends_with(&suffix)).then(|| e.path())
        })
    })?;
    let path = confined_path(root, &path).ok()?;
    let mut line = String::new();
    BufReader::new(secure_file(&path).ok()?.take(65536))
        .read_line(&mut line)
        .ok()?;
    let meta: Value = serde_json::from_str(&line).ok()?;
    let payload = &meta["payload"];
    let spawn = &payload["source"]["subagent"]["thread_spawn"];
    if meta["type"] != "session_meta" || payload["id"] != child || spawn["parent_thread_id"] != parent {
        return None;
    }
    let mut start = lifecycle(
        "subagent_start",
        spawn["agent_nickname"].as_str().unwrap_or(""),
        payload["timestamp"].as_str(),
        None,
    );
    start["subagent"] = json!(child);
    if let Some(role) = spawn["agent_role"].as_str() {
        start["tool"] = json!({"name": "Agent", "status": "running", "input": {"subagentType": role}});
    }
    Some((path, start))
}

/// An `update_plan` call is the whole checklist each time; the viewer draws the latest one.
/// No other response item is mirrored: the event_msg stream already carries them.
fn plan_update(payload: &Value, ts: Option<&str>) -> Option<Value> {
    if payload["type"] != "function_call" || payload["name"] != "update_plan" {
        return None;
    }
    let args: Value = serde_json::from_str(payload["arguments"].as_str()?).ok()?;
    let plan: Vec<Value> = args["plan"]
        .as_array()?
        .iter()
        .filter_map(|s| Some(json!({"step": s["step"].as_str()?, "status": s["status"].as_str()?})))
        .collect();
    let text = args["explanation"].as_str().unwrap_or("Updated plan");
    let mut e = event("tool", "assistant", text, ts, payload["call_id"].as_str());
    let (input, cut) = bound_json(json!({ "plan": plan }), MAX_TOOL_INPUT);
    e["tool"] = json!({"name": "update_plan", "status": "done", "input": input});
    if cut {
        e["truncated"] = json!(true);
    }
    Some(e)
}

/// `capture` already verified this rollout's writer; an app header may name the
/// bundle's prerelease (see `desktop_prerelease`).
fn accepted_header_version(source: &str, payload: &Value) -> bool {
    let version = payload["cli_version"].as_str().unwrap_or("");
    accepted_version(version) || (source == "vscode" && app_originator(payload) && desktop_prerelease(version))
}

pub(super) fn normalize(session: &str, row: &Value) -> Result<Vec<Value>> {
    if row.get("channel").is_some() || row.get("phase").is_some() {
        return Ok(vec![]);
    }
    let meta_source = row["payload"]["source"].as_str().unwrap_or("");
    if row["type"] == "session_meta"
        && (row["payload"]["id"] != session
            || !accepted_header_version(meta_source, &row["payload"])
            || !accepted_app_source(meta_source, &row["payload"])
            || !accepted_history_mode(meta_source, &row["payload"]))
    {
        return Err("runtime-unverified".into());
    }
    if row["type"] == "response_item" {
        let ts = row["timestamp"].as_str();
        return Ok(plan_update(&row["payload"], ts).into_iter().collect());
    }
    if row["type"] != "event_msg" {
        return Ok(vec![]);
    }
    let payload = &row["payload"];
    let ts = row["timestamp"].as_str();
    let turn = |suffix: &str| payload["turn_id"].as_str().map(|t| format!("{t}:{suffix}"));
    Ok(match payload["type"].as_str() {
        Some("task_started") => vec![lifecycle("turn_start", "", ts, turn("start").as_deref())],
        Some("task_complete") => vec![lifecycle("turn_end", "", ts, turn("end").as_deref())],
        Some("turn_aborted") => vec![lifecycle("turn_aborted", "", ts, turn("abort").as_deref())],
        Some("item_completed") => item(&payload["item"], ts)
            .into_iter()
            .chain(subagent_activity(&payload["item"], ts))
            .collect(),
        _ => vec![],
    })
}

/// Replace one of the user's own Codex files atomically, keeping its mode.
/// Not `private_write`: these are the user's configuration, not our 0600 state,
/// and a 0600 rewrite of a file Codex shares would be a surprise.
pub(super) fn replace(path: &Path, contents: &[u8]) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let mode = fs::symlink_metadata(path)
        .ok()
        .filter(|meta| meta.is_file())
        .map_or(0o600, |meta| meta.mode() & 0o777);
    let temp = path.with_file_name(format!(".{}.tmp", uuid::Uuid::new_v4()));
    let written = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(&temp)
        .map_err(|e| e.to_string())
        .and_then(|mut file| {
            file.write_all(contents)
                .and_then(|()| file.sync_all())
                .map_err(|e| e.to_string())
        })
        .and_then(|()| fs::rename(&temp, path).map_err(|e| e.to_string()));
    if written.is_err() {
        let _ = fs::remove_file(&temp);
    }
    written
}

/// Add or update one `[hooks.state."<key>"]` trust block, leaving every other
/// line of `config.toml` byte-for-byte alone.
fn upsert_trust(config: &str, key: &str, hash: &str) -> String {
    let header = format!("[hooks.state.\"{key}\"]");
    let trusted = format!("trusted_hash = \"{hash}\"");
    let mut lines: Vec<String> = config.lines().map(str::to_owned).collect();
    match lines.iter().position(|line| line.trim() == header) {
        Some(start) => {
            let block = &lines[start + 1..];
            // Bounded by the next table header: a block of ours that somehow
            // lost its hash must never rewrite the next table's.
            let end = block
                .iter()
                .position(|line| line.trim_start().starts_with('['))
                .unwrap_or(block.len());
            match block[..end]
                .iter()
                .position(|line| line.trim_start().starts_with("trusted_hash"))
            {
                Some(at) => lines[start + 1 + at] = trusted,
                None => lines.insert(start + 1, trusted),
            }
        }
        None => lines.extend([String::new(), header, trusted]),
    }
    let mut out = lines.join("\n");
    out.push('\n');
    out
}

/// Drop one `[hooks.state."<key>"]` block: its header, the lines up to the next
/// table, and the blank line `upsert_trust` put in front of it.
fn remove_trust(config: &str, key: &str) -> String {
    let header = format!("[hooks.state.\"{key}\"]");
    let mut kept: Vec<&str> = Vec::new();
    let mut dropping = false;
    for line in config.lines() {
        if line.trim() == header {
            if kept.last().is_some_and(|line| line.trim().is_empty()) {
                kept.pop();
            }
            dropping = true;
            continue;
        }
        if dropping {
            if !line.trim_start().starts_with('[') {
                continue;
            }
            dropping = false;
        }
        kept.push(line);
    }
    let mut out = kept.join("\n");
    if !out.is_empty() {
        out.push('\n');
    }
    out
}

/// Codex forces SessionEnd synchronous and normalizes its missing timeout to 1;
/// every other event normalizes to the written value.
fn hook_timing(event: &str) -> (u64, bool) {
    match event {
        "SessionEnd" => (1, false),
        _ => (5, true),
    }
}

/// The lifecycle events Codex is given.
fn codex_events() -> impl Iterator<Item = &'static str> {
    super::claude::HOOK_EVENTS.iter().map(|(event, _)| *event)
}

/// Events earlier releases installed for Codex and this one removes: PostToolUse carried the
/// retired held-request notice.
const RETIRED_EVENTS: &[&str] = &["PostToolUse"];

/// Read-only: "configured" when every lifecycle group runs `executable` and is
/// trusted in `config.toml` (an untrusted group is skipped silently), else "missing".
pub(super) fn hooks_state(home: &Path, executable: &Path) -> &'static str {
    let dir = home.join(".codex");
    let read = |path: &Path| -> Option<String> {
        let mut text = String::new();
        secure_file(path).ok()?.take(1024 * 1024).read_to_string(&mut text).ok()?;
        Some(text)
    };
    let path = dir.join("hooks.json");
    let configured = (|| {
        let doc: Value = serde_json::from_str(&read(&path)?).ok()?;
        let config = read(&dir.join("config.toml"))?;
        let command = super::claude::hook_command(executable, "codex").ok()?;
        codex_events().all(|event| {
            let Some(label) = lifecycle::codex_event_label(event) else { return true };
            let (timeout, is_async) = hook_timing(event);
            let entry = json!({"hooks":[{"type":"command","command":command,"timeout":timeout,"async":is_async}]});
            doc["hooks"][event].as_array().and_then(|entries| entries.iter().position(|e| e == &entry))
                .is_some_and(|index| {
                    let hash = lifecycle::codex_trust_hash(label, None, &command, timeout, is_async, None);
                    upsert_trust(&config, &lifecycle::codex_trust_key(&path, label, index), &hash) == config
                })
        }).then_some(())
    })();
    if configured.is_some() { "configured" } else { "missing" }
}

/// Install (or remove) the lifecycle hooks in `~/.codex/hooks.json` together
/// with the `config.toml` trust entries Codex requires before it will run them
/// -- an untrusted group is skipped silently, so the two must move as one.
pub(super) fn configure_hooks(home: &Path, executable: &Path, enabled: bool) -> Result<()> {
    let dir = home.join(".codex");
    if !dir.is_dir() {
        return Ok(()); // Codex is not installed on this Mac.
    }
    let path = dir.join("hooks.json");
    // The trust key is this path, written into a TOML bare-string table header.
    // A quote or backslash in it would corrupt the user's config.toml, so refuse
    // before either file is touched rather than escape our way through.
    if path.to_string_lossy().contains(['"', '\\']) {
        return Err("Codex config path cannot be trusted safely".into());
    }
    let mut doc: Value = if path.try_exists().map_err(|e| e.to_string())? {
        read_json(secure_file(&path)?, 1024 * 1024)
            .map_err(|_| "invalid Codex hooks")?
    } else if enabled {
        json!({})
    } else {
        return Ok(());
    };
    if !doc.is_object() {
        return Err("invalid Codex hooks".into());
    }
    if doc.get("hooks").is_none() {
        doc["hooks"] = json!({});
    }
    // Every launch re-asserts these, and a no-op must not rewrite (and
    // reformat) files the user owns.
    let original = doc.clone();
    let command = super::claude::hook_command(executable, "codex")?;
    let hooks = doc["hooks"].as_object_mut().ok_or("invalid Codex hooks")?;
    // key -> hash to trust, or None to untrust.
    let mut trust: Vec<(String, Option<String>)> = Vec::new();
    for (event, enabled) in codex_events().map(|e| (e, enabled)).chain(RETIRED_EVENTS.iter().map(|e| (*e, false))) {
        let Some(label) = lifecycle::codex_event_label(event) else {
            continue;
        };
        let (timeout, is_async) = hook_timing(event);
        let entry =
            json!({"hooks":[{"type":"command","command":command,"timeout":timeout,"async":is_async}]});
        let slot = if enabled {
            Some(hooks.entry(event).or_insert_with(|| json!([])))
        } else {
            hooks.get_mut(event)
        };
        let Some(slot) = slot else { continue };
        let entries = slot.as_array_mut().ok_or("invalid Codex hooks")?;
        // Our group is the one running our command. Re-appending it instead
        // would shift every group the user keeps after it -- and their trust
        // keys are that index.
        let found = entries
            .iter()
            .position(|existing| existing["hooks"][0]["command"].as_str() == Some(&*command));
        // Never guess an index when disabling: `<hooks.json>:<event>:0:0` is a
        // real key that another tool's first group may own.
        let index = if enabled {
            Some(match found {
                Some(at) => {
                    entries[at] = entry;
                    at
                }
                None => {
                    entries.push(entry);
                    entries.len() - 1
                }
            })
        } else {
            if let Some(at) = found {
                entries.remove(at);
            }
            found
        };
        if entries.is_empty() {
            hooks.remove(event);
        }
        if let Some(index) = index {
            trust.push((
                lifecycle::codex_trust_key(&path, label, index),
                enabled.then(|| {
                    lifecycle::codex_trust_hash(label, None, &command, timeout, is_async, None)
                }),
            ));
        }
    }
    if doc != original {
        if enabled {
            super::backup_once(&path)?;
        }
        replace(
            &path,
            &serde_json::to_vec_pretty(&doc).map_err(|e| e.to_string())?,
        )?;
    }
    if trust.is_empty() {
        return Ok(());
    }
    let config = dir.join("config.toml");
    let mut text = if config.try_exists().map_err(|e| e.to_string())? {
        let mut text = String::new();
        secure_file(&config)?
            .take(1024 * 1024)
            .read_to_string(&mut text)
            .map_err(|_| "invalid Codex config")?;
        text
    } else if enabled {
        String::new()
    } else {
        return Ok(());
    };
    let original = text.clone();
    for (key, hash) in &trust {
        text = match hash {
            Some(hash) => upsert_trust(&text, key, hash),
            None => remove_trust(&text, key),
        };
    }
    if text == original {
        return Ok(());
    }
    if enabled {
        super::backup_once(&config)?;
    }
    replace(&config, text.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;


    /// A `thread/read` result as Codex 0.164.0's app-server returns it (trimmed to what is read).
    fn thread(id: &str, status: Value, path: &Path) -> Value {
        json!({"id": id, "sessionId": id, "forkedFromId": null, "parentThreadId": null, "ephemeral": false,
            "status": status, "path": path, "cwd": "/tmp/cx-e2e", "source": "vscode", "threadSource": "user",
            "preview": "", "name": null, "turns": []})
    }

    #[test]
    fn local_sessions_codex_thread_status_follows_the_app_server() {
        // The state table (spec 2026-10-26 (e)): running, waiting on you, done, stopped.
        assert_eq!(thread_status(&json!({"type":"active","activeFlags":[]})), Some("busy"));
        assert_eq!(thread_status(&json!({"type":"active","activeFlags":["waitingOnApproval"]})), Some("waiting-approval"));
        assert_eq!(thread_status(&json!({"type":"active","activeFlags":["waitingOnUserInput"]})), Some("waiting-approval"));
        assert_eq!(thread_status(&json!({"type":"idle"})), Some("idle"));
        assert_eq!(thread_status(&json!({"type":"systemError"})), Some("idle"));
        assert_eq!(thread_status(&json!({"type":"notLoaded"})), None);
        assert_eq!(thread_status(&Value::Null), None);
    }

    /// The daemon lets an idle thread's rollout go but keeps the thread loaded: it is still a row.
    /// Subagents (nested under their parent), ephemeral helpers (the title namer) and threads it no
    /// longer runs are not.
    #[test]
    fn local_sessions_codex_an_idle_loaded_thread_is_a_row_whatever_its_rollout() {
        let home = tempfile::tempdir().unwrap();
        let root = home.path().join(".codex/sessions");
        let day = root.join("2026/10/26");
        fs::create_dir_all(&day).unwrap();
        let rollout = |id: &str| { let p = day.join(format!("rollout-2026-10-26T17-47-06-{id}.jsonl")); fs::write(&p, "{}\n").unwrap(); p };
        let idle = rollout("01a17a2e-71be-72a3-9ea3-db3681294921");
        assert_eq!(thread_rollout(&root, &thread("01a17a2e-71be-72a3-9ea3-db3681294921", json!({"type":"idle"}), &idle)), Some(idle.clone()));
        let mut child = thread("01a17a2f-0000-7000-8000-000000000001", json!({"type":"active","activeFlags":[]}), &rollout("01a17a2f-0000-7000-8000-000000000001"));
        child["parentThreadId"] = json!("01a17a2e-71be-72a3-9ea3-db3681294921");
        assert!(subagent(&child) && thread_rollout(&root, &child).is_none());
        let mut spawned = thread("01a17a2f-0000-7000-8000-000000000002", json!({"type":"idle"}), &rollout("01a17a2f-0000-7000-8000-000000000002"));
        spawned["source"] = json!({"subAgent":{"thread_spawn":{"parent_thread_id":"x","depth":1}}});
        assert!(subagent(&spawned) && thread_rollout(&root, &spawned).is_none());
        let mut namer = thread("01a17a2f-0000-7000-8000-000000000003", json!({"type":"idle"}), &rollout("01a17a2f-0000-7000-8000-000000000003"));
        namer["ephemeral"] = json!(true);
        assert!(thread_rollout(&root, &namer).is_none());
        assert!(thread_rollout(&root, &thread("a", json!({"type":"notLoaded"}), &idle)).is_none());
        assert!(thread_rollout(&root, &thread("a", json!({"type":"idle"}), Path::new("/etc/passwd"))).is_none(), "only under ~/.codex/sessions");
        assert!(thread_rollout(&root, &thread("a", json!({"type":"idle"}), &day.join("missing.jsonl"))).is_none(), "not written yet");
        assert!(!subagent(&thread("a", json!({"type":"idle"}), &idle)));
    }

    #[test]
    fn local_sessions_codex_counts_working_child_threads_by_parent_thread_id() {
        let parent = "01a17a2e-71be-72a3-9ea3-db3681294921";
        let child = |status: Value, of: &str| json!({"id":"c","parentThreadId":of,"status":status});
        let threads = [
            child(json!({"type":"active","activeFlags":[]}), parent),
            child(json!({"type":"active","activeFlags":["waitingOnApproval"]}), parent),
            child(json!({"type":"idle"}), parent),
            child(json!({"type":"active","activeFlags":[]}), "someone-else"),
            json!({"id":parent,"parentThreadId":null,"status":{"type":"active","activeFlags":[]}}),
        ];
        assert_eq!(working_children(threads.iter(), parent), 2);
    }

    #[test]
    fn local_sessions_codex_fork_lineage_and_status_come_from_the_thread() {
        let mut session = super::super::transport::tests::test_mirror("01a17a35-0000-7000-8000-000000000001", "g").native;
        session.status = "busy".into(); // what the rollout's tail guessed
        let mut fork = thread("01a17a35-0000-7000-8000-000000000001", json!({"type":"active","activeFlags":["waitingOnApproval"]}), Path::new("/x"));
        fork["forkedFromId"] = json!("01a17a2e-71be-72a3-9ea3-db3681294921");
        apply_thread(&mut session, &fork);
        assert_eq!(session.forked_from.as_deref(), Some("01a17a2e-71be-72a3-9ea3-db3681294921"));
        assert_eq!(session.status, "waiting-approval");
        let mut plain = super::super::transport::tests::test_mirror("01a17a35-0000-7000-8000-000000000002", "g").native;
        apply_thread(&mut plain, &thread("01a17a35-0000-7000-8000-000000000002", json!({"type":"idle"}), Path::new("/x")));
        assert_eq!((plain.forked_from, plain.status.as_str()), (None, "idle"));
    }

    #[test]
    fn local_sessions_codex_a_thread_the_daemon_dropped_is_stopped() {
        let daemon = process(std::process::id()).unwrap();
        let other = Process { pid: daemon.pid + 1, ..daemon.clone() };
        let listed = [json!({"id":"01a17a2e-71be-72a3-9ea3-db3681294921"})];
        remember_loaded(&daemon, Some(&listed));
        assert_eq!(loaded(&daemon, "01a17a2e-71be-72a3-9ea3-db3681294921"), Some(true));
        assert_eq!(loaded(&daemon, "01a17a29-13c2-71e1-8501-912c2cf234c4"), Some(false));
        assert_eq!(loaded(&other, "01a17a2e-71be-72a3-9ea3-db3681294921"), None, "another daemon: unknown");
        remember_loaded(&other, None);
        assert_eq!(loaded(&daemon, "01a17a29-13c2-71e1-8501-912c2cf234c4"), Some(false), "the updater's failed listing is not the daemon's");
        remember_loaded(&daemon, None);
        assert_eq!(loaded(&daemon, "01a17a29-13c2-71e1-8501-912c2cf234c4"), None, "a failed listing proves nothing");
    }

    /// One connection lists the loaded threads page by page and reads each one, past the
    /// notifications the daemon sends every client; it introduces itself as the TUI (CLIENT_NAME).
    async fn read(conn: &mut WebSocketStream<tokio::net::UnixStream>) -> Value {
        serde_json::from_str(&conn.next().await.unwrap().unwrap().into_text().unwrap()).unwrap()
    }

    /// `item/commandExecution/requestApproval` as Codex 0.163.0 replayed it to a second client
    /// (a read-only thread asked to `mkdir` outside its sandbox).
    fn mkdir_request() -> Value {
        json!({"method":"item/commandExecution/requestApproval","id":0,"params":{"kind":"command",
            "threadId":"01a17b15-9831-7651-8cd8-b55882740c23","turnId":"01a17b15-9bc9-77a1-85ec-7e8a48445038",
            "itemId":"exec-84fa3836-f8cd-4230-9403-f62dd39973fb","startedAtMs":1793066384867u64,"environmentId":"local",
            "reason":"Allow the requested command to create /tmp/cxappr/outside_made outside the read-only sandbox?",
            "command":"/bin/zsh -lc 'mkdir -p /tmp/cxappr/outside_made && echo done'","cwd":"/tmp/cxappr/w",
            "commandActions":[{"type":"unknown","command":"mkdir -p /tmp/cxappr/outside_made && echo done"}],
            "proposedExecpolicyAmendment":["mkdir","-p","/tmp/cxappr/outside_made"],
            "availableDecisions":["accept",{"acceptWithExecpolicyAmendment":{"execpolicy_amendment":["mkdir","-p","/tmp/cxappr/outside_made"]}},"cancel"]}})
    }

    #[test]
    fn local_sessions_codex_an_approval_is_held_as_its_whole_command() {
        let request = mkdir_request();
        let open = approval(&request).unwrap();
        assert_eq!(open.tool, "Bash");
        assert_eq!(open.input, json!({"command":"/bin/zsh -lc 'mkdir -p /tmp/cxappr/outside_made && echo done'"}));
        assert_eq!(open.summary, open.input["command"]);
        assert!(open.full && !open.session, "the whole command line; this one offers no conversation-wide allow");
        assert!(super::super::approval::valid_id(&approval_id("t", &open.item)));
        assert_eq!(approval_id("t", &open.item), approval_id("t", &open.item), "rediscovered, it is the same prompt");
        assert_ne!(approval_id("t", &open.item), approval_id("u", &open.item));
        // Only what Codex offered, and never a rule that outlives the thread.
        assert_eq!(decision(&request, "allow", None), Some("accept"));
        assert_eq!(decision(&request, "allow", Some("session")), None);
        assert_eq!(decision(&request, "allow", Some("similar")), None);
        assert_eq!(decision(&request, "deny", None), Some("cancel"), "decline is not offered: the turn stops");
        let mut open_ended = request.clone();
        open_ended["params"]["availableDecisions"] = json!(["accept", "acceptForSession", "decline", "cancel"]);
        assert!(approval(&open_ended).unwrap().session);
        assert_eq!(decision(&open_ended, "allow", Some("session")), Some("acceptForSession"));
        assert_eq!(decision(&open_ended, "deny", None), Some("decline"));
        assert_eq!(decision(&open_ended, "deny", Some("session")), None);
        // A patch request carries no patch: nobody here saw the change, so it is never `full`.
        let patch = json!({"method":"item/fileChange/requestApproval","id":1,"params":{"threadId":"t","turnId":"u","itemId":"call_1","startedAtMs":1,"reason":"write outside the workspace"}});
        let open = approval(&patch).unwrap();
        assert!(!open.full && open.tool == "apply_patch" && open.summary == "write outside the workspace");
        assert!(!super::super::approval::exact(open.tool, &open.input, None, None));
        assert!(approval(&json!({"method":"item/permissions/requestApproval","id":2,"params":{"itemId":"x"}})).is_none());
    }

    /// A daemon thread holding `request` open: replays it to a client that joins the thread, and
    /// resolves it when answered (as 0.163.0 does). Returns what the client answered.
    async fn daemon_holding(request: Value) -> (std::path::PathBuf, tempfile::TempDir, tokio::task::JoinHandle<Option<Value>>) {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("control.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut conn = tokio_tungstenite::accept_async(stream).await.unwrap();
            read(&mut conn).await;
            conn.send(Message::Text(json!({"id":1,"result":{}}).to_string().into())).await.unwrap();
            read(&mut conn).await;
            let resume = read(&mut conn).await;
            assert_eq!(resume["method"], "thread/resume");
            assert_eq!(resume["params"], json!({"threadId":request["params"]["threadId"],"excludeTurns":true}), "a join, no overrides");
            // The server request's id is the daemon's own counter: it can equal ours.
            let mut early = request.clone();
            early["id"] = json!(2);
            early["params"]["threadId"] = json!("another-thread");
            conn.send(Message::Text(early.to_string().into())).await.unwrap();
            conn.send(Message::Text(json!({"id":2,"result":{"thread":{"id":request["params"]["threadId"]}}}).to_string().into())).await.unwrap();
            conn.send(Message::Text(json!({"method":"thread/goal/cleared","params":{"threadId":request["params"]["threadId"]}}).to_string().into())).await.unwrap();
            conn.send(Message::Text(request.to_string().into())).await.unwrap();
            let answered = match tokio::time::timeout(std::time::Duration::from_secs(3), conn.next()).await {
                Ok(Some(Ok(Message::Text(text)))) => serde_json::from_str::<Value>(&text).ok(),
                _ => None,
            };
            if answered.as_ref().is_some_and(|a| a["id"] == request["id"]) {
                let resolved = json!({"method":"serverRequest/resolved","params":{"threadId":request["params"]["threadId"],"requestId":request["id"]}});
                conn.send(Message::Text(resolved.to_string().into())).await.unwrap();
            }
            answered
        });
        (socket, dir, server)
    }

    #[test]
    fn local_sessions_codex_a_failed_replay_keeps_the_held_approval() {
        let thread = "01a17b15-0000-7000-8000-00000000fa11";
        update_approval(thread, Some(Ok(Some(mkdir_request()))));
        update_approval(thread, Some(Err("approval replay timed out".into())));
        assert_eq!(open_request(thread), Some(mkdir_request()), "still waiting, the replay failed: kept");
        update_approval(thread, Some(Ok(None)));
        assert_eq!(open_request(thread), None, "the replay says it is gone");
        update_approval(thread, Some(Ok(Some(mkdir_request()))));
        update_approval(thread, None);
        assert_eq!(open_request(thread), None, "no longer waiting on approval");
    }

    #[tokio::test]
    async fn local_sessions_codex_an_approval_is_answered_over_the_daemon_only_as_held() {
        let request = mkdir_request();
        let thread = request["params"]["threadId"].as_str().unwrap().to_owned();
        let open = approval(&request).unwrap();
        let digest = super::super::approval::digest(&open.input);
        // The replayed request, what discovery holds.
        let (socket, _dir, server) = daemon_holding(request.clone()).await;
        let mut conn = connected(&socket).await.unwrap();
        assert_eq!(replayed_request(&mut conn, &thread).await.unwrap(), Some(request.clone()));
        drop(conn);
        assert_eq!(server.await.unwrap(), None, "reading it answers nothing");
        // Allowed: Codex's own `accept`, confirmed by `serverRequest/resolved`.
        let (socket, _dir, server) = daemon_holding(request.clone()).await;
        assert!(answer_on(&socket, &thread, &open.item, &digest, "allow", None).await.unwrap());
        assert_eq!(server.await.unwrap(), Some(json!({"id":0,"result":{"decision":"accept"}})));
        // Denied where only cancel is offered.
        let (socket, _dir, server) = daemon_holding(request.clone()).await;
        assert!(answer_on(&socket, &thread, &open.item, &digest, "deny", None).await.unwrap());
        assert_eq!(server.await.unwrap(), Some(json!({"id":0,"result":{"decision":"cancel"}})));
        // A changed command, another item, or a choice it does not offer: nothing is sent.
        for (item, digest, mode) in [(open.item.as_str(), "e".repeat(64), None), ("exec-other", digest.clone(), None), (open.item.as_str(), digest.clone(), Some("session"))] {
            let (socket, _dir, server) = daemon_holding(request.clone()).await;
            assert!(!answer_on(&socket, &thread, item, &digest, "allow", mode).await.unwrap());
            assert_eq!(server.await.unwrap(), None);
        }
    }

    #[tokio::test]
    async fn local_sessions_codex_lists_loaded_threads_over_the_control_socket() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("control.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut conn = tokio_tungstenite::accept_async(stream).await.unwrap();
            let init = read(&mut conn).await;
            assert_eq!(init["params"]["clientInfo"]["name"], "codex-tui", "the daemon stamps its first client's name on threads");
            conn.send(Message::Text(json!({"id":1,"result":{}}).to_string().into())).await.unwrap();
            assert_eq!(read(&mut conn).await["method"], "initialized");
            let page = read(&mut conn).await;
            assert_eq!(page["method"], "thread/loaded/list");
            conn.send(Message::Text(json!({"method":"thread/status/changed","params":{"threadId":"t1","status":{"type":"idle"}}}).to_string().into())).await.unwrap();
            conn.send(Message::Text(json!({"id":page["id"],"result":{"data":["t1"],"nextCursor":"c2"}}).to_string().into())).await.unwrap();
            let page = read(&mut conn).await;
            assert_eq!(page["params"]["cursor"], "c2");
            conn.send(Message::Text(json!({"id":page["id"],"result":{"data":["t2"],"nextCursor":null}}).to_string().into())).await.unwrap();
            for (id, status) in [("t1", json!({"type":"idle"})), ("t2", json!({"type":"active","activeFlags":[]}))] {
                let request = read(&mut conn).await;
                assert_eq!((request["method"].as_str(), request["params"]["threadId"].as_str()), (Some("thread/read"), Some(id)));
                conn.send(Message::Text(json!({"id":request["id"],"result":{"thread":{"id":id,"status":status}}}).to_string().into())).await.unwrap();
            }
        });
        let threads = loaded_threads(&socket).await.unwrap();
        assert_eq!(threads.iter().map(|t| t["id"].as_str().unwrap()).collect::<Vec<_>>(), ["t1", "t2"]);
        assert_eq!(thread_status(&threads[1]["status"]), Some("busy"));
        server.await.unwrap();
    }

    #[test]
    fn local_sessions_codex_a_held_start_is_the_thread_started_in_its_folder() {
        let dir = tempfile::tempdir().unwrap();
        let folder = fs::canonicalize(dir.path()).unwrap();
        let started = |thread: Value| json!({"method":"thread/started","params":{"thread":thread}}).to_string();
        let mut mine = json!({"id":"01a17a2e-71be-72a3-9ea3-db3681294921","cwd":dir.path(),"ephemeral":false,"parentThreadId":null,"source":"vscode"});
        assert_eq!(started_thread(&started(mine.clone()), &folder).as_deref(), Some("01a17a2e-71be-72a3-9ea3-db3681294921"));
        mine["ephemeral"] = json!(true);
        assert!(started_thread(&started(mine.clone()), &folder).is_none(), "the title namer");
        mine["ephemeral"] = json!(false);
        mine["parentThreadId"] = json!("01a17a2e-0000-7000-8000-000000000000");
        assert!(started_thread(&started(mine.clone()), &folder).is_none(), "a subagent");
        mine["parentThreadId"] = Value::Null;
        mine["cwd"] = json!("/");
        assert!(started_thread(&started(mine), &folder).is_none(), "another folder's start");
        assert!(started_thread(r#"{"method":"thread/status/changed","params":{}}"#, &folder).is_none());
        assert!(wakes(r#"{"method":"thread/status/changed","params":{"threadId":"t","status":{"type":"idle"}}}"#));
        assert!(wakes(r#"{"method":"thread/closed","params":{"threadId":"t"}}"#));
        assert!(!wakes(r#"{"method":"account/updated","params":{}}"#));
    }

    /// The fake daemon side of `our_thread`: sends `frames`, then answers each `thread/read` with
    /// `preview`.
    async fn daemon_says(frames: Vec<Value>, preview: &'static str) -> (std::path::PathBuf, tempfile::TempDir, tokio::task::JoinHandle<()>) {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("control.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut conn = tokio_tungstenite::accept_async(stream).await.unwrap();
            read(&mut conn).await;
            conn.send(Message::Text(json!({"id":1,"result":{}}).to_string().into())).await.unwrap();
            read(&mut conn).await;
            for frame in frames {
                conn.send(Message::Text(frame.to_string().into())).await.unwrap();
            }
            while let Some(Ok(Message::Text(text))) = conn.next().await {
                let request: Value = serde_json::from_str(&text).unwrap();
                let reply = json!({"id":request["id"],"result":{"thread":{"id":request["params"]["threadId"],"preview":preview}}});
                if conn.send(Message::Text(reply.to_string().into())).await.is_err() { break; }
            }
        });
        (socket, dir, server)
    }

    /// Nothing in the protocol names the client that started a thread, so a held start binds only
    /// a thread provably its own: the only one started in its folder, whose first message is its
    /// prompt. Another client's start in the same folder during the wait binds nothing.
    #[tokio::test]
    async fn local_sessions_codex_a_held_start_binds_only_its_own_thread() {
        let work = tempfile::tempdir().unwrap();
        let folder = fs::canonicalize(work.path()).unwrap();
        let started = |id: &str| json!({"method":"thread/started","params":{"thread":{"id":id,"cwd":work.path(),"ephemeral":false,"parentThreadId":null,"source":"vscode"}}});
        let (ours, theirs, old) = ("01a17a65-6aa7-7222-8b6a-03942b282538", "01a17a65-0000-7000-8000-000000000002", "01a17a64-91ae-72c2-9a36-febc23604305");
        let before: std::collections::BTreeSet<String> = [old.to_owned()].into();
        let grace = std::time::Duration::from_millis(300);
        let run = |frames: Vec<Value>, preview: &'static str, prompt: Option<&'static str>| {
            let (folder, before) = (folder.clone(), before.clone());
            async move {
                let (socket, _dir, server) = daemon_says(frames, preview).await;
                let mut conn = connected(&socket).await.unwrap();
                let picked = our_thread(&mut conn, &folder, &before, prompt, grace).await;
                drop(conn);
                server.abort();
                picked
            }
        };
        assert_eq!(run(vec![started(old), started(ours)], "fix it", Some("fix it")).await.as_deref(), Ok(ours), "an already loaded thread is no candidate");
        assert_eq!(run(vec![started(ours)], "", None).await.as_deref(), Ok(ours), "no prompt: the only start in the folder");
        assert_eq!(run(vec![started(ours), started(theirs)], "fix it", Some("fix it")).await,
            Err("another thread started in the same folder"));
        assert_eq!(run(vec![started(theirs)], "something else", Some("fix it")).await,
            Err("its first message is not this start's prompt"));
        assert!(first_turn("", "fix it").is_none() && first_turn("fix it", "fix it") == Some(true));
    }

    /// An app-server that accepts the socket but never answers `initialize` fails the door in
    /// HANDSHAKE as UNAVAILABLE, which a start or resume turns into its `--no-daemon` fallback.
    #[tokio::test]
    async fn local_sessions_codex_a_stalled_daemon_handshake_is_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("control.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut conn = tokio_tungstenite::accept_async(stream).await.unwrap();
            read(&mut conn).await; // initialize, never answered
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        });
        let at = std::time::Instant::now();
        assert_eq!(connected(&socket).await.err().as_deref(), Some(UNAVAILABLE));
        assert!(at.elapsed() < HANDSHAKE + std::time::Duration::from_secs(2));
        assert_eq!(connected(&dir.path().join("missing.sock")).await.err().as_deref(), Some(UNAVAILABLE));
        server.abort();
        let daemon: Vec<String> = ["/bin/codex", "--remote", "unix://", "-C", "/w", "-c", "check_for_update_on_startup=false",
            "-m", "gpt", "--sandbox", "workspace-write", "--ask-for-approval", "on-request", "--", "fix it"].map(String::from).into();
        assert_eq!(private_argv(&daemon), ["/bin/codex", "--no-daemon", "-m", "gpt", "--sandbox", "workspace-write",
            "--ask-for-approval", "on-request", "--", "fix it"]);
        assert_eq!(private_argv(&daemon[..1]), ["/bin/codex"]);
    }

    #[test]
    fn local_sessions_codex_daemon_resume_requests_internet_without_more_file_access() {
        let workspace = resume_params("thread", Path::new("/work"), false);
        assert_eq!(workspace["sandbox"], "workspace-write");
        assert_eq!(workspace["config"]["sandbox_workspace_write.network_access"], true);
        assert_eq!(workspace["approvalPolicy"], "on-request");
        let plan = resume_params("thread", Path::new("/work"), true);
        assert!(plan.get("sandbox").is_none(), "do not mix named and legacy permissions");
        let name = plan["config"]["default_permissions"].as_str().unwrap();
        assert!(name.starts_with("lightbulb-plan-"));
        assert!(uuid::Uuid::parse_str(name.strip_prefix("lightbulb-plan-").unwrap()).is_ok());
        assert_eq!(plan["config"][format!("permissions.{name}")],
            json!({"extends": ":read-only", "network": {"enabled": true}}));
        assert_eq!(plan["approvalPolicy"], "on-request");
        assert_eq!(plan["cwd"], "/work");
        assert_eq!(plan["excludeTurns"], true);
        assert_ne!(plan["config"]["default_permissions"],
            resume_params("thread", Path::new("/work"), true)["config"]["default_permissions"]);
    }

    /// A teammate's resume is read-only: a thread the daemon already runs looser is refused.
    #[test]
    fn local_sessions_codex_a_daemon_resume_is_never_looser_than_asked() {
        let resumed = |sandbox: &str, approval: &str| json!({"sandbox":{"type":sandbox,"networkAccess":true},"approvalPolicy":approval});
        assert!(within(&resumed("readOnly", "on-request"), true));
        assert!(!within(&resumed("workspaceWrite", "on-request"), true));
        assert!(!within(&resumed("dangerFullAccess", "on-request"), false));
        assert!(!within(&resumed("readOnly", "never"), false));
        assert!(within(&resumed("workspaceWrite", "on-request"), false));
        for plan in [true, false] {
            let sandbox = if plan { "readOnly" } else { "workspaceWrite" };
            assert!(resume_permissions(&json!({"sandbox":{"type":sandbox,"networkAccess":false},"approvalPolicy":"on-request"}), plan).is_err());
            assert!(resume_permissions(&json!({"sandbox":{"type":sandbox},"approvalPolicy":"on-request"}), plan).is_err());
            assert!(resume_permissions(&resumed(sandbox, "on-request"), plan).is_ok());
        }
    }

    #[test]
    fn setup_backs_up_codex_files_once() {
        let home = tempfile::tempdir().unwrap();
        let codex = home.path().join(".codex");
        fs::create_dir(&codex).unwrap();
        fs::write(codex.join("hooks.json"), r#"{"hooks":{}}"#).unwrap();
        fs::write(codex.join("config.toml"), "model = \"o3\"\n").unwrap();
        configure_hooks(home.path(), Path::new("/Applications/A.app/x"), true).unwrap();
        // A moved app edits both files again; the first backups stay.
        configure_hooks(home.path(), Path::new("/Applications/B.app/x"), true).unwrap();
        assert_eq!(fs::read_to_string(codex.join("hooks.json.lightbulb-backup")).unwrap(), r#"{"hooks":{}}"#);
        assert_eq!(fs::read_to_string(codex.join("config.toml.lightbulb-backup")).unwrap(), "model = \"o3\"\n");
    }

    #[test]
    fn serves_many_is_loose_about_apps_and_the_daemon_directory() {
        let mut session = super::super::transport::tests::test_mirror("33333333-3333-4333-8333-333333333333", "g").native;
        session.app = false;
        let home = Path::new("/Users/u");
        let mut at = |exe: &str| {
            session.process.executable = PathBuf::from(exe);
            serves_many_in(&session, Some(home))
        };
        assert!(at("/Applications/Codex.app/Contents/Resources/codex"));
        // Group-writable or oddly named release: the strict trust checks would reject it.
        assert!(at("/Users/u/.codex/packages/app-server-daemon/releases/odd-dir/bin/codex"));
        assert!(!at("/opt/homebrew/bin/codex"));
    }

    #[tokio::test]
    async fn local_sessions_codex_control_uses_websocket_on_unix_socket() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("control.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut conn = tokio_tungstenite::accept_async(stream).await.unwrap();
            let init: Value = serde_json::from_str(&conn.next().await.unwrap().unwrap().into_text().unwrap()).unwrap();
            assert_eq!(init["method"], "initialize");
            conn.send(Message::Text(json!({"id":1,"result":{}}).to_string().into())).await.unwrap();
            let ready: Value = serde_json::from_str(&conn.next().await.unwrap().unwrap().into_text().unwrap()).unwrap();
            assert_eq!(ready["method"], "initialized");
            let request: Value = serde_json::from_str(&conn.next().await.unwrap().unwrap().into_text().unwrap()).unwrap();
            assert_eq!(request["method"], "turn/interrupt");
            conn.send(Message::Text(json!({"id":2,"result":{}}).to_string().into())).await.unwrap();
        });
        let response = control_rpc(&socket, &json!({"id":2,"method":"turn/interrupt","params":{"threadId":"thread","turnId":"turn"}})).await.unwrap();
        assert_eq!(response["result"], json!({}));
        server.await.unwrap();
    }

    #[test]
    fn local_sessions_codex_hooks_install_trust_and_remove() {
        let home = tempfile::tempdir().unwrap();
        let codex = home.path().join(".codex");
        fs::create_dir_all(&codex).unwrap();
        fs::write(codex.join("config.toml"), "model = \"x\"\n\n[hooks.state]\n\n[hooks.state.\"other:stop:0:0\"]\ntrusted_hash = \"sha256:aa\"\n").unwrap();
        fs::write(
            codex.join("hooks.json"),
            r#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"theirs"}]}]}}"#,
        )
        .unwrap();
        configure_hooks(home.path(), Path::new("/app"), true).unwrap();
        let hooks: Value =
            serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
        assert_eq!(
            hooks["hooks"]["Stop"].as_array().unwrap().len(),
            2,
            "theirs kept, ours appended"
        );
        assert_eq!(hooks["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 1);
        let config = fs::read_to_string(codex.join("config.toml")).unwrap();
        let key = format!("{}:stop:1:0", codex.join("hooks.json").display());
        assert!(config.contains(&format!("[hooks.state.\"{key}\"]")));
        let expected = lifecycle::codex_trust_hash(
            "stop",
            None,
            "'/app' local-session-hook codex",
            5,
            true,
            None,
        );
        assert!(config.contains(&format!("trusted_hash = \"{expected}\"")));
        assert!(config.contains("other:stop:0:0"));

        // A group the user adds AFTER ours keeps its index across a resume --
        // their own trust key is that index, so moving ours invalidates it.
        let mut doc: Value =
            serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
        doc["hooks"]["Stop"]
            .as_array_mut()
            .unwrap()
            .push(json!({"hooks":[{"type":"command","command":"later"}]}));
        fs::write(
            codex.join("hooks.json"),
            serde_json::to_vec_pretty(&doc).unwrap(),
        )
        .unwrap();
        configure_hooks(home.path(), Path::new("/app"), true).unwrap(); // idempotent
        let doc: Value =
            serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
        let stop = doc["hooks"]["Stop"].as_array().unwrap();
        assert_eq!(stop.len(), 3);
        assert_eq!(stop[0]["hooks"][0]["command"], "theirs");
        assert_eq!(
            stop[2]["hooks"][0]["command"], "later",
            "ours stayed at index 1 instead of being re-appended"
        );
        assert_eq!(
            fs::read_to_string(codex.join("config.toml"))
                .unwrap()
                .matches(&key)
                .count(),
            1
        );
        configure_hooks(home.path(), Path::new("/app"), false).unwrap();
        let hooks: Value =
            serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
        let stop = hooks["hooks"]["Stop"].as_array().unwrap();
        assert_eq!(
            (
                stop.len(),
                stop[0]["hooks"][0]["command"].as_str(),
                stop[1]["hooks"][0]["command"].as_str(),
            ),
            (2, Some("theirs"), Some("later")),
            "only our group is removed, and in place"
        );
        assert!(hooks["hooks"].get("SessionEnd").is_none());
        let config = fs::read_to_string(codex.join("config.toml")).unwrap();
        assert!(
            !config.contains(&key)
                && config.contains("other:stop:0:0")
                && config.starts_with("model = \"x\"")
        );

        // Codex installed, but no hooks.json or config.toml written yet.
        let fresh = tempfile::tempdir().unwrap();
        fs::create_dir_all(fresh.path().join(".codex")).unwrap();
        configure_hooks(fresh.path(), Path::new("/app"), true).unwrap();
        let hooks: Value = serde_json::from_str(
            &fs::read_to_string(fresh.path().join(".codex/hooks.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"],
            "'/app' local-session-hook codex"
        );
        assert!(
            hooks["hooks"].get("Notification").is_none(),
            "Codex has no Notification event"
        );
        let config = fs::read_to_string(fresh.path().join(".codex/config.toml")).unwrap();
        assert_eq!(config.matches("[hooks.state.").count(), 4);

        // No Codex on this Mac: nothing to install, and not an error.
        let bare = tempfile::tempdir().unwrap();
        configure_hooks(bare.path(), Path::new("/app"), true).unwrap();
        assert!(!bare.path().join(".codex").exists());
    }

    /// Claude has two hooks Codex must never be given (`PreToolUse`,
    /// `PermissionRequest`). Codex trusts a hook group by a hash of its identity
    /// at an index, so one extra installed entry silently invalidates keys and
    /// Codex then skips the group. This pins the whole set -- events, commands
    /// and trust hashes -- to exactly what shipped.
    #[test]
    fn local_sessions_codex_hook_set_and_trust_hashes_are_frozen() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".codex")).unwrap();
        configure_hooks(home.path(), Path::new("/app"), true).unwrap();
        let doc: Value =
            serde_json::from_str(&fs::read_to_string(home.path().join(".codex/hooks.json")).unwrap())
                .unwrap();
        let hooks = doc["hooks"].as_object().unwrap();
        assert_eq!(
            hooks.keys().map(String::as_str).collect::<Vec<_>>(),
            ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"],
            "Codex installs the five shared events minus Notification, and nothing else"
        );
        for (event, timeout, is_async) in [
            ("SessionStart", json!(5), json!(true)),
            ("UserPromptSubmit", json!(5), json!(true)),
            ("Stop", json!(5), json!(true)),
            ("SessionEnd", json!(1), json!(false)),
        ] {
            assert_eq!(
                hooks[event],
                json!([{"hooks":[{"type":"command","command":"'/app' local-session-hook codex",
                    "timeout":timeout,"async":is_async}]}]),
                "{event}"
            );
        }
        let config = fs::read_to_string(home.path().join(".codex/config.toml")).unwrap();
        let path = home.path().join(".codex/hooks.json");
        for (index, label, hash) in [
            (0, "session_start", "sha256:e92f980584c0d2e69241c742b5098cc77ee3c72dde0acf0000958fef54d406c2"),
            (0, "user_prompt_submit", "sha256:1b9663f4792c847238b9ce60fa8356836634675cf6edcd96b9660a8bbac50d5a"),
            (0, "stop", "sha256:560328742055693cdfd246bce82c94a8b396c388029444fb0adb39b8899b547e"),
            (0, "session_end", "sha256:73824c3fd7c65d780e2b8aa1740084f4ce2090a802322de8aac307784a3698d8"),
        ] {
            let key = lifecycle::codex_trust_key(&path, label, index);
            assert!(
                config.contains(&format!("[hooks.state.\"{key}\"]\ntrusted_hash = \"{hash}\"")),
                "{label} trust entry moved"
            );
        }
        assert_eq!(config.matches("[hooks.state.").count(), 4);
    }

    /// The PostToolUse group an earlier release installed (for the retired held-request notice)
    /// goes, with its trust entry; another tool's PostToolUse stays.
    #[test]
    fn local_sessions_codex_removes_our_old_post_tool_use_hook() {
        let home = tempfile::tempdir().unwrap();
        let dir = home.path().join(".codex");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hooks.json");
        let ours = json!({"hooks":[{"type":"command","command":"'/app' local-session-hook codex","timeout":5,"async":false}]});
        let theirs = json!({"hooks":[{"type":"command","command":"/bin/other","timeout":5,"async":false}]});
        fs::write(&path, json!({"hooks":{"PostToolUse":[ours]}}).to_string()).unwrap();
        let key = lifecycle::codex_trust_key(&path, "post_tool_use", 0);
        fs::write(dir.join("config.toml"), format!("[hooks.state.\"{key}\"]\ntrusted_hash = \"sha256:0be577df5e9da83b39a4848f75b425d09b47d0100006fc1501d19a67d0c4fc6a\"\n")).unwrap();
        configure_hooks(home.path(), Path::new("/app"), true).unwrap();
        let doc: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert!(doc["hooks"].get("PostToolUse").is_none(), "{doc}");
        assert!(!fs::read_to_string(dir.join("config.toml")).unwrap().contains(&key));
        assert_eq!(hooks_state(home.path(), Path::new("/app")), "configured");
        fs::write(&path, json!({"hooks":{"PostToolUse":[theirs]}}).to_string()).unwrap();
        configure_hooks(home.path(), Path::new("/app"), true).unwrap();
        let doc: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(doc["hooks"]["PostToolUse"], json!([theirs]));
    }

    #[test]
    fn local_sessions_codex_refuses_a_path_the_trust_table_cannot_quote() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("we\"ird");
        fs::create_dir_all(home.join(".codex")).unwrap();
        let config = home.join(".codex/config.toml");
        fs::write(&config, "model = \"x\"\n").unwrap();
        assert_eq!(
            configure_hooks(&home, Path::new("/app"), true).unwrap_err(),
            "Codex config path cannot be trusted safely"
        );
        assert_eq!(fs::read_to_string(&config).unwrap(), "model = \"x\"\n");
        assert!(!home.join(".codex/hooks.json").exists());
    }

    #[test]
    fn local_sessions_codex_items_become_terminal_items() {
        let meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.149.0","source":"cli"}});
        assert!(normalize("s", &meta).unwrap().is_empty());
        assert!(normalize("other", &meta).is_err());
        let app_meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.153.4","source":"vscode","originator":"Codex Desktop","history_mode":"paginated"}});
        assert!(normalize("s", &app_meta).unwrap().is_empty());
        let old_meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.140.0","source":"cli"}});
        assert!(normalize("s", &old_meta).is_err());
        let legacy_meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.153.4","source":"vscode","originator":"Codex Desktop","history_mode":"legacy"}});
        assert!(normalize("s", &legacy_meta).is_err());
        let unknown_history_meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.153.4","source":"vscode","originator":"Codex Desktop"}});
        assert!(normalize("s", &unknown_history_meta).is_err());
        let other_originator_meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.153.4","source":"vscode","originator":"vscode-extension","history_mode":"paginated"}});
        assert!(normalize("s", &other_originator_meta).is_err());
        let ts = "2026-09-12T10:00:00.000Z";
        let ev = |item: Value| json!({"type":"event_msg","timestamp":ts,"payload":{"type":"item_completed","thread_id":"s","turn_id":"t1","item":item}});
        let user = normalize("s", &ev(json!({"type":"UserMessage","id":"u1","content":[{"type":"text","text":"[Lightbulb teammate contribution from @aden (Aden Kim) {\"roomId\":\"r\",\"requestId\":\"q\",\"actorUserId\":\"user_a\"}]\nrun it"}]}))).unwrap();
        assert_eq!((user[0]["text"].as_str(), user[0]["actor"].clone()), (Some("run it"), json!({"userId":"user_a"})));
        let owner = normalize("s", &ev(json!({"type":"UserMessage","id":"u2","content":[{"type":"text",
            "text":format!("{OWNER_LABEL}[Lightbulb workspace advisory: Re-read files.]\ncontinue") }]}))).unwrap();
        assert_eq!(owner[0]["text"], "continue");
        assert!(owner[0].get("actor").is_none());
        let bare_owner = normalize("s", &ev(json!({"type":"UserMessage","id":"u3","content":[{"type":"text",
            "text":format!("{OWNER_LABEL}continue") }]}))).unwrap();
        assert_eq!(bare_owner[0]["text"], "continue");
        let reasoning = normalize(
            "s",
            &ev(json!({"type":"Reasoning","id":"r1","summary_text":["**Planning the rerun**"]})),
        )
        .unwrap();
        assert_eq!(
            (reasoning[0]["kind"].as_str(), reasoning[0]["text"].as_str()),
            (Some("thinking"), Some("**Planning the rerun**"))
        );
        let output = (1..=25)
            .map(|i| format!("l{i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let cmd = normalize("s", &ev(json!({"type":"CommandExecution","id":"e1","command":["/bin/zsh","-lc","git status"],"cwd":"file:///x","aggregated_output":output,"exit_code":1,"status":"completed"}))).unwrap();
        assert_eq!(cmd[0]["tool"]["name"], "CommandExecution");
        assert_eq!(cmd[0]["tool"]["input"]["command"], "git status");
        assert!(cmd[0]["tool"]["input"].get("parsed").is_none(), "no parsed_cmd, no key");
        let explored = normalize("s", &ev(json!({"type":"CommandExecution","id":"e2","command":["/bin/zsh","-lc","sed -n 1,200p app.py"],
            "aggregated_output":"","exit_code":0,"status":"completed","parsed_cmd":[
                {"type":"read","cmd":"sed -n '1,200p' app.py","name":"app.py","path":"app.py","extra":1},
                {"type":"list_files","cmd":"ls -la","path":null},
                {"type":"search","cmd":"rg foo src","query":"foo","path":"/Users/me/repo/src"}]}))).unwrap();
        assert_eq!(explored[0]["tool"]["input"]["parsed"], json!([
            {"type":"read","name":"app.py","path":"app.py"},
            {"type":"list_files"},
            {"type":"search","path":"src","query":"foo"}]));
        let empty = normalize("s", &ev(json!({"type":"CommandExecution","id":"e3","command":["x"],"aggregated_output":"","exit_code":0,"status":"completed","parsed_cmd":[]}))).unwrap();
        assert!(empty[0]["tool"]["input"].get("parsed").is_none());
        assert_eq!(cmd[0]["tool"]["status"], "error");
        assert_eq!(cmd[0]["tool"]["lines"], 25);
        assert_eq!(
            cmd[0]["text"].as_str().unwrap().lines().count(),
            PREVIEW_LINES
        );
        let change = normalize("s", &ev(json!({"type":"FileChange","id":"f1","changes":{"/Users/me/repo/src/a.ts":{"type":"update","diff":"-a\n+b"}}}))).unwrap();
        assert_eq!(change[0]["text"], "a.ts");
        assert_eq!(change[0]["tool"]["result"]["a.ts"]["diff"], "-a\n+b");
        // Current builds (0.153) write the hunk as `unified_diff`.
        let change = normalize("s", &ev(json!({"type":"FileChange","id":"f2","changes":{"/r/b.md":{"type":"update","unified_diff":"@@ -1 +1 @@\n-a\n+b","move_path":null}}}))).unwrap();
        assert_eq!(change[0]["tool"]["result"]["b.md"]["diff"], "@@ -1 +1 @@\n-a\n+b");
        // A secret-named FILE is no secret: it stays in the changes list, and a secret in it is still cut.
        let change = normalize("s", &ev(json!({"type":"FileChange","id":"f3","changes":{
            "/r/src/auth.ts":{"type":"update","diff":"+const key = \"AKIAIOSFODNN7EXAMPLE\""},
            "/r/secrets.yaml":{"type":"add","content":"aws: AKIAIOSFODNN7EXAMPLE"}}}))).unwrap();
        let result = &change[0]["tool"]["result"];
        let diff = result["auth.ts"]["diff"].as_str().unwrap();
        let content = result["secrets.yaml"]["content"].as_str().unwrap();
        assert_eq!(result["auth.ts"]["type"], "update");
        assert_eq!(result["secrets.yaml"]["type"], "add");
        assert!(diff.contains("[redacted]") && !diff.contains("AKIAIOSFODNN7EXAMPLE"), "{diff}");
        assert!(content.contains("[redacted]") && !content.contains("AKIAIOSFODNN7EXAMPLE"), "{content}");
        let mcp = normalize("s", &ev(json!({"type":"McpToolCall","id":"m1","server":"codex_app","tool":"open","arguments":{"a":1},"status":"completed","result":{"content":[{"type":"text","text":"ok"}],"isError":false}}))).unwrap();
        assert_eq!(
            (mcp[0]["text"].as_str(), mcp[0]["tool"]["status"].as_str()),
            (Some("codex_app.open"), Some("done"))
        );
        let send = normalize("s", &ev(json!({"type":"McpToolCall","id":"m2","server":"lightbulb_native_sessions","tool":"send",
            "arguments":{"destination":"person:private","text":"secret text","requestId":"private-request"},
            "status":"completed","result":{"content":[{"type":"text","text":"person:private"}],"isError":false}}))).unwrap();
        assert_eq!(send[0]["tool"]["input"], json!({"server":"lightbulb_native_sessions","tool":"send"}));
        assert!(!send[0].to_string().contains("person:private"));
        assert!(!send[0].to_string().contains("secret text"));
        let receipt = normalize("s", &ev(json!({"type":"McpToolCall","id":"m3","server":"lightbulb_native_sessions","tool":"receipt",
            "arguments":{"requestId":"private-request"},"status":"completed",
            "result":{"content":[{"type":"text","text":"person:private"}],"isError":false}}))).unwrap();
        assert!(!receipt[0].to_string().contains("person:private"));
        assert!(!receipt[0].to_string().contains("private-request"));
        for tool in ["find_tasks", "read_task", "update_task"] {
            let task = normalize("s", &ev(json!({"type":"McpToolCall","id":"m4","server":"lightbulb","tool":tool,
                "arguments":{"task":"ISOA-9","append_note":"secret note"},"status":"completed",
                "result":{"content":[{"type":"text","text":"secret description"}],"isError":false}}))).unwrap();
            assert!(!task[0].to_string().contains("secret"), "{tool} call and result stay out of the transcript");
        }
        let code_mode = json!({"type":"response_item","payload":{"type":"custom_tool_call",
            "name":"exec","input":"tools.mcp__lightbulb_native_sessions__send({text:'secret text'})"}});
        assert!(normalize("s", &code_mode).unwrap().is_empty(), "Code Mode source stays out of the shared transcript");
        let search = normalize(
            "s",
            &ev(json!({"type":"Extension","kind":"web.search","id":"w1","query":"k6 timeout"})),
        )
        .unwrap();
        assert_eq!(
            (
                search[0]["tool"]["name"].as_str(),
                search[0]["text"].as_str()
            ),
            (Some("web.search"), Some("k6 timeout"))
        );
        let secret_search = normalize(
            "s",
            &ev(json!({"type":"Extension","kind":"web.search","id":"w2","query":"find AKIAIOSFODNN7EXAMPLE"})),
        )
        .unwrap();
        let query = secret_search[0]["tool"]["input"]["query"].as_str().unwrap();
        assert!(query.contains("[redacted]"));
        assert!(!query.contains("AKIAIOSFODNN7EXAMPLE"));
        let final_answer = normalize("s", &ev(json!({"type":"AgentMessage","id":"a1","phase":"final_answer","content":[{"type":"Text","text":"Done."}]}))).unwrap();
        assert_eq!(
            (
                final_answer[0]["kind"].as_str(),
                final_answer[0]["text"].as_str()
            ),
            (Some("text"), Some("Done."))
        );
        // Absent phase is ordinary; only an unrecognized phase is withheld.
        let commentary = normalize("s", &ev(json!({"type":"AgentMessage","id":"a2","phase":"commentary","content":[{"type":"Text","text":"Thinking aloud."}]}))).unwrap();
        assert_eq!(commentary[0]["text"].as_str(), Some("Thinking aloud."));
        let bare = normalize("s", &ev(json!({"type":"AgentMessage","id":"a3","content":[{"type":"Text","text":"Still done."}]}))).unwrap();
        assert_eq!(bare[0]["text"].as_str(), Some("Still done."));
        assert!(normalize("s", &ev(json!({"type":"AgentMessage","id":"a4","phase":"analysis","content":[{"type":"Text","text":"scratch"}]}))).unwrap().is_empty());
        assert_eq!(
            normalize("s", &ev(json!({"type":"ContextCompaction","id":"c1"}))).unwrap()[0]
                ["lifecycle"],
            "compaction"
        );
        let started = json!({"type":"event_msg","timestamp":ts,"payload":{"type":"task_started","turn_id":"t1"}});
        assert_eq!(
            normalize("s", &started).unwrap()[0]["lifecycle"],
            "turn_start"
        );
        let aborted = json!({"type":"event_msg","timestamp":ts,"payload":{"type":"turn_aborted","turn_id":"t1","reason":"interrupted"}});
        assert_eq!(
            normalize("s", &aborted).unwrap()[0]["lifecycle"],
            "turn_aborted"
        );
        assert!(normalize("s", &json!({"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"legacy"}]}})).unwrap().is_empty());
        let args = r#"{"explanation":"Start with the map","plan":[{"step":"Map it","status":"in_progress"},{"step":"Fix it","status":"pending"},{"bad":1}]}"#;
        let plan = json!({"type":"response_item","timestamp":"2026-09-25T00:00:00Z",
            "payload":{"type":"function_call","name":"update_plan","call_id":"call_1","arguments":args}});
        let events = normalize("s", &plan).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["text"], "Start with the map");
        assert_eq!(events[0]["tool"]["name"], "update_plan");
        let steps = json!([{"step":"Map it","status":"in_progress"},{"step":"Fix it","status":"pending"}]);
        assert_eq!(events[0]["tool"]["input"]["plan"], steps);
        let other = json!({"type":"response_item","payload":{"type":"function_call","name":"exec","arguments":"{}"}});
        assert!(normalize("s", &other).unwrap().is_empty());
        assert!(normalize(
            "s",
            &json!({"type":"event_msg","payload":{"type":"token_count"}})
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn local_sessions_codex_tail_state_reports_busy_idle_and_title() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let started = json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"t9"}});
        let complete =
            json!({"type":"event_msg","payload":{"type":"task_complete","turn_id":"t9"}});
        let user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"Check whether the timeout also happens at concurrency 50, please, and report back with numbers"}]}}});
        fs::write(&path, format!("{user}\n{started}\n")).unwrap();
        let (status, turn, title) = tail_state(&path);
        assert_eq!((status, turn.as_deref()), ("busy", Some("t9")));
        assert_eq!(title.as_deref().map(str::len), Some(60));
        fs::write(&path, format!("{user}\n{started}\n{complete}\n")).unwrap();
        assert_eq!(tail_state(&path).0, "idle");
        fs::write(&path, "{}\n").unwrap();
        assert_eq!(tail_state(&path), ("unknown", None, None));
        let secret_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"use sk-abcdefghijklmnopqrstuvwxyz12 please"}]}}});
        fs::write(&path, format!("{secret_user}\n")).unwrap();
        let title = tail_state(&path).2.unwrap();
        assert!(title.contains("[redacted]"));
        assert!(!title.contains("sk-abcdefghijklmnopqrstuvwxyz12"));
        let header_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"[Lightbulb teammate contribution from @aden (Aden Kim) {\"roomId\":\"r\"}]\nrun it"}]}}});
        let later_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"plain later prompt"}]}}});
        fs::write(&path, format!("{header_user}\n{later_user}\n")).unwrap();
        assert_eq!(tail_state(&path).2.as_deref(), Some("plain later prompt"));
    }

    #[test]
    fn local_sessions_codex_settles_only_after_the_exact_turn_ends() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let started = json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"old"}});
        let completed = json!({"type":"event_msg","payload":{"type":"task_complete","turn_id":"old"}});
        fs::write(&path, format!("{started}\n")).unwrap();
        assert!(!turn_finished(&path, "old"));
        fs::write(&path, format!("{started}\n{completed}\n")).unwrap();
        assert!(turn_finished(&path, "old"));
    }

    #[test]
    fn local_sessions_codex_title_survives_a_long_turn() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"Original session title"}]}}});
        let padding = format!("{{\"type\":\"opaque\",\"padding\":\"{}\"}}\n", "x".repeat(3 * 1024 * 1024));
        fs::write(&path, format!("{user}\n{padding}")).unwrap();
        assert_eq!(tail_state(&path).2.as_deref(), Some("Original session title"));
    }

    #[test]
    fn local_sessions_codex_finds_active_turn_beyond_the_recent_two_megabytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let ended_at = 1_800_000_000;
        let started_at = utc_timestamp(ended_at + 1).unwrap();
        let started = json!({"type":"event_msg","timestamp":format!("{started_at}.000Z"),
            "payload":{"type":"task_started","turn_id":"long-turn"}});
        let huge_non_lifecycle_row = format!(
            "{{\"type\":\"opaque\",\"padding\":\"{}\"}}\n",
            "x".repeat(6 * 1024 * 1024)
        );
        fs::write(&path, format!("{}\n{huge_non_lifecycle_row}", started)).unwrap();
        assert_eq!(tail_state(&path).0, "busy");
        assert_eq!(tail_state(&path).1.as_deref(), Some("long-turn"));
        assert!(active_turn_started_after(&path, ended_at, Some(ended_at * 1000)));
    }

    #[test]
    fn local_sessions_codex_tracks_a_turn_beyond_thirty_two_megabytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let start = "{\"type\":\"event_msg\",\"timestamp\":\"2026-09-30T00:00:00.000Z\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"long\"}}\n";
        fs::write(&path, start).unwrap();
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        let padding = format!("{{\"type\":\"opaque\",\"padding\":\"{}\"}}\n", "x".repeat(1024 * 1024));
        for _ in 0..33 { file.write_all(padding.as_bytes()).unwrap(); }
        assert_eq!(tail_state(&path).0, "busy");
        assert_eq!(tail_state(&path).1.as_deref(), Some("long"));
        file.write_all(b"{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"}}").unwrap();
        assert_eq!(tail_state(&path).0, "idle");
        file.write_all(b"\n").unwrap();
        assert_eq!(tail_state(&path).0, "idle");
        file.write_all(b"{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"next\"}}").unwrap();
        assert_eq!(tail_state(&path).1.as_deref(), Some("next"));
        file.write_all(b"\n").unwrap();
        assert_eq!(tail_state(&path).1.as_deref(), Some("next"));
        fs::write(&path, "{}\n").unwrap();
        assert_eq!(tail_state(&path).0, "unknown");
    }

    #[test]
    fn local_sessions_codex_accepts_exact_verified_versions_and_sources() {
        assert!(accepted_version("0.155.0-alpha.2.6"));
        assert!(accepted_version("0.157.1"));
        assert!(accepted_version("0.159.2"));
        assert!(accepted_version("0.159.3"));
        assert!(!accepted_version("0.155.0-alpha.2.7"));
        let meta = json!({"type":"session_meta","payload":{"id":"s","source":"vscode",
            "originator":"Codex Desktop","cli_version":"0.155.0-alpha.2.6","history_mode":"paginated"}});
        assert!(normalize("s", &meta).unwrap().is_empty());
        let tui = json!({"type":"session_meta","payload":{"id":"s","source":"vscode",
            "originator":"codex-tui","cli_version":"0.157.1","history_mode":"paginated"}});
        assert!(normalize("s", &tui).unwrap().is_empty());
        let delegated_tui = json!({"type":"session_meta","payload":{"id":"s","source":"vscode",
            "originator":"codex-tui","cli_version":"0.159.2","history_mode":"paginated"}});
        assert!(normalize("s", &delegated_tui).unwrap().is_empty());
        assert!(normalize("s", &json!({"type":"session_meta","payload":{"id":"s","source":"vscode",
            "originator":"codex-tui","cli_version":"0.155.0-alpha.2.6","history_mode":"paginated"}})).is_err());
    }

    #[test]
    fn control_socket_must_be_unique_owned_socket_under_daemon_dir() {
        let dir = tempfile::Builder::new().tempdir_in("/tmp").unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("a".repeat(64));
        let _listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        let uid = unsafe { libc::geteuid() };
        let listing = format!("p123\nf32\nn{}\n", path.display());
        assert_eq!(socket_from_lsof(&listing, &root, uid).unwrap().0, path);
        assert_eq!(socket_from_lsof(&(listing.clone() + &listing), &root, uid).unwrap().0, path);
        let other = root.join("b".repeat(64));
        let _other_listener = std::os::unix::net::UnixListener::bind(&other).unwrap();
        assert!(socket_from_lsof(&(listing.clone() + &format!("f33\nn{}\n", other.display())), &root, uid).is_none());
        assert!(socket_from_lsof(&listing, &root, uid + 1).is_none());
        assert!(socket_from_lsof(&listing, Path::new("/unrelated"), uid).is_none());
    }
    #[test]
    fn local_sessions_codex_accepts_a_newer_self_updated_daemon() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(home.path()).unwrap();
        let triple = if cfg!(target_arch = "aarch64") { "aarch64" } else { "x86_64" };
        let releases = home.join(".codex/packages/app-server-daemon/releases");
        let binary = releases.join(format!("0.160.0-{triple}-apple-darwin/bin/codex"));
        fs::create_dir_all(binary.parent().unwrap()).unwrap();
        fs::write(&binary, "").unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
        let process = Process {pid: 1, birth_micros: 1, executable: binary.clone()};
        assert_eq!(daemon_release(&home, &process), Some((binary.clone(), "0.160.0".into())));
        assert!(accepted_version("0.160.0") && accepted_version("0.161.12") && accepted_version("1.0.0"));
        assert!(!accepted_version("0.159.1") && !accepted_version("0.160.0-alpha.1") && !accepted_version("0.160"));
        assert!(accepted_app_source("vscode", &json!({"originator":"codex-tui","cli_version":"0.160.0"})));
        // Writable by others, an older release, a prerelease or a symlink escape: not the daemon.
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o775)).unwrap();
        assert!(daemon_release(&home, &process).is_none());
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
        for version in ["0.158.0", "0.160.0-alpha.1"] {
            let other = releases.join(format!("{version}-{triple}-apple-darwin/bin/codex"));
            fs::create_dir_all(other.parent().unwrap()).unwrap();
            fs::write(&other, "").unwrap();
            assert!(daemon_release(&home, &Process {executable: other, ..process.clone()}).is_none());
        }
        let away = tempfile::tempdir().unwrap();
        let outside = fs::canonicalize(away.path()).unwrap();
        fs::create_dir_all(outside.join("bin")).unwrap();
        fs::write(outside.join("bin/codex"), "").unwrap();
        let linked = releases.join(format!("0.161.0-{triple}-apple-darwin"));
        std::os::unix::fs::symlink(&outside, &linked).unwrap();
        assert!(daemon_release(&home, &Process {executable: linked.join("bin/codex"), ..process.clone()}).is_none());
        // Its rollout, reported and verified as 0.160.0, is mirrored unrestricted.
        let root = home.join(".codex/sessions");
        fs::create_dir_all(&root).unwrap();
        let rollout = root.join("rollout-new.jsonl");
        fs::write(&rollout, format!("{}\n", json!({"type":"session_meta","payload":{"id":"new","source":"vscode",
            "originator":"codex-tui","history_mode":"paginated","cli_version":"0.160.0","cwd":"/tmp"}}))).unwrap();
        let session = from_rollout(&root, process, &rollout, "0.160.0").unwrap();
        assert_eq!((session.version.as_str(), session.restriction), ("0.160.0", None));
    }

    #[test]
    fn local_sessions_codex_accepts_managed_daemon_rollout() {
        let home = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(home.path()).unwrap();
        let triple = if cfg!(target_arch = "aarch64") { "aarch64" } else { "x86_64" };
        let binary = home.join(format!(".codex/packages/app-server-daemon/releases/0.159.2-{triple}-apple-darwin/bin/codex"));
        fs::create_dir_all(binary.parent().unwrap()).unwrap();
        fs::write(&binary, "").unwrap();
        let process = Process {pid: 1, birth_micros: 1, executable: binary.clone()};
        assert_eq!(daemon_program(&home, &process), Some(binary));
        assert!(daemon_program(&home, &Process {executable: home.join("other/codex"), ..process.clone()}).is_none());
        // Only a thread on the shared daemon gets a live terminal.
        const THREAD: &str = "01a10845-c8f1-7fa2-844b-13790e2db151";
        assert_eq!(attach_thread(&home, &process, THREAD).as_deref(), Some(THREAD));
        assert_eq!(attach_thread(&home, &Process {executable: home.join("other/codex"), ..process.clone()}, THREAD), None, "a --no-daemon TUI or the desktop app");
        assert_eq!(attach_thread(&home, &process, "not-a-thread-id"), None);
        let root = home.join(".codex/sessions");
        fs::create_dir_all(&root).unwrap();
        let rollout = root.join("rollout-phone.jsonl");
        let second_rollout = root.join("rollout-app.jsonl");
        let first_cwd = home.join("first-checkout");
        let second_cwd = home.join("second-checkout");
        for (path, id, cwd) in [(&rollout, "phone", &first_cwd), (&second_rollout, "app", &second_cwd)] {
            fs::write(path, format!("{}\n", json!({"type":"session_meta","payload":{"id":id,"source":"vscode","originator":"codex-tui","history_mode":"paginated","cli_version":"0.159.2","cwd":cwd}}))).unwrap();
        }
        let first = from_rollout(&root, process.clone(), &rollout, "0.159.2").unwrap();
        let second = from_rollout(&root, process, &second_rollout, "0.159.2").unwrap();
        assert_eq!(first.restriction, None);
        assert_eq!(first.process, second.process);
        assert_eq!(rollout_cwd(&home, &first).unwrap(), first_cwd);
        assert_eq!(rollout_cwd(&home, &second).unwrap(), second_cwd);
        let resumed_cwd = home.join("resumed-checkout");
        fs::write(&rollout, format!("{}\n{}\n{}\n", json!({"type":"session_meta","payload":{"id":"phone","cwd":first_cwd}}),
            json!({"type":"turn_context","payload":{"cwd":second_cwd}}),
            json!({"type":"turn_context","payload":{"cwd":resumed_cwd}}))).unwrap();
        assert_eq!(rollout_cwd(&home, &first).unwrap(), resumed_cwd);
        fs::write(&rollout, format!("{}\n{}\n", json!({"type":"session_meta","payload":{"id":"phone","cwd":first_cwd}}),
            json!({"type":"turn_context","payload":{}}))).unwrap();
        assert!(rollout_cwd(&home, &first).is_err());
        fs::write(&rollout, format!("{}\n{}\n", json!({"type":"session_meta","payload":{"id":"phone","cwd":first_cwd}}),
            json!({"type":"turn_context","payload":{"cwd":resumed_cwd,"padding":"x".repeat(TURN_ROW_MAX)}}))).unwrap();
        assert!(rollout_cwd(&home, &first).is_err());
    }

    #[test]
    fn local_sessions_codex_finds_only_a_new_unfinished_turn_after_end() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let ended_at = 1_800_000_000;
        let before = utc_timestamp(ended_at - 1).unwrap();
        let ended = utc_timestamp(ended_at).unwrap();
        let after = utc_timestamp(ended_at + 1).unwrap();
        let started = |ts: &str| {
            format!(
                "{{\"type\":\"event_msg\",\"timestamp\":\"{ts}.000Z\",\"payload\":{{\"type\":\"task_started\"}}}}\n"
            )
        };
        let complete = format!(
            "{{\"type\":\"event_msg\",\"timestamp\":\"{ended}.000Z\",\"payload\":{{\"type\":\"task_complete\"}}}}\n"
        );
        fs::write(&path, format!("{}{complete}", started(&before))).unwrap();
        assert!(!active_turn_started_after(
            &path,
            ended_at,
            Some(ended_at * 1000)
        ));
        fs::write(
            &path,
            format!("{}{complete}{}", started(&before), started(&after)),
        )
        .unwrap();
        assert!(active_turn_started_after(
            &path,
            ended_at,
            Some(ended_at * 1000)
        ));
        let same_second = format!(
            "{{\"type\":\"event_msg\",\"timestamp\":\"{ended}.001Z\",\"payload\":{{\"type\":\"task_started\"}}}}\n"
        );
        fs::write(&path, same_second).unwrap();
        assert!(active_turn_started_after(
            &path,
            ended_at,
            Some(ended_at * 1000)
        ));
        assert!(!active_turn_started_after(
            &path,
            ended_at,
            Some(ended_at * 1000 + 2)
        ));
        assert!(!active_turn_started_after(&path, ended_at, None));
        let completed_at = utc_timestamp(ended_at + 2).unwrap();
        let completed = format!(
            "{{\"type\":\"event_msg\",\"timestamp\":\"{after}.000Z\",\"payload\":{{\"type\":\"task_started\"}}}}\n{{\"type\":\"event_msg\",\"timestamp\":\"{completed_at}.000Z\",\"payload\":{{\"type\":\"task_complete\"}}}}\n"
        );
        fs::write(&path, completed).unwrap();
        assert_eq!(tail_state(&path).0, "idle");
        assert!(active_turn_started_after(&path, ended_at, Some(ended_at * 1000)));
    }
    #[test]
    fn local_sessions_codex_finds_the_running_desktop_bundle_layout() {
        let dir = tempfile::tempdir().unwrap();
        let resources = dir.path();
        let old = resources.join("codex");
        let new = resources.join("codex-cli/CodexCLI.app/Contents/MacOS/codex");
        fs::create_dir_all(new.parent().unwrap()).unwrap();
        fs::write(&new, "new bundle").unwrap();
        assert_eq!(desktop_program_in(resources, Some(&new)), Some(new.clone()));
        fs::write(&old, "old bundle").unwrap();
        assert_eq!(desktop_program_in(resources, Some(&new)), Some(new.clone()));
        assert_eq!(desktop_program_in(resources, Some(&old)), Some(old.clone()));
        assert_eq!(desktop_program_in(resources, Some(&resources.join("other/codex"))), None);
        fs::remove_file(&new).unwrap();
        std::os::unix::fs::symlink(&old, &new).unwrap();
        assert_eq!(desktop_program_in(resources, Some(&new)), None, "never execute an unexpected symlink");
    }

    #[test]
    fn local_sessions_codex_app_work_threads_and_bundled_prerelease_are_mirrored() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-work.jsonl");
        fs::write(&path, format!("{}\n", json!({"type":"session_meta","payload":{
            "id":"work", "source":"vscode", "originator":"codex_work_desktop",
            "cli_version":"0.160.1", "history_mode":"paginated"}}))).unwrap();
        let process = Process { pid: 42, birth_micros: 123, executable: "/app/codex".into() };
        let work = from_writer(&root, process.clone(), &path, "0.162.0-alpha.17.2", true).unwrap();
        assert!(work.app);
        assert_eq!((work.restriction, work.version.as_str()), (None, "0.162.0-alpha.17.2"));
        assert_eq!(from_rollout(&root, process.clone(), &path, "0.160.1").unwrap().restriction, None);
        // A prerelease is the app's only when the bundle wrote it; no other suffix is.
        assert_eq!(from_rollout(&root, process.clone(), &path, "0.162.0-alpha.17.2").unwrap().restriction.as_deref(), Some("runtime-unverified"));
        for refused in ["0.160.1-custom", "0.158.0-alpha.1", "0.162.0-alpha.", "0.162.0-alpha.1.2.3", "0.162.0-beta.1"] {
            assert_eq!(from_writer(&root, process.clone(), &path, refused, true).unwrap().restriction.as_deref(), Some("runtime-unverified"), "{refused}");
        }
        assert!(normalize("work", &json!({"type":"session_meta","payload":{"id":"work","source":"vscode",
            "originator":"codex_work_desktop","cli_version":"0.160.1","history_mode":"paginated"}})).unwrap().is_empty());
    }

    #[test]
    fn local_sessions_codex_app_conversation_started_by_the_bundled_prerelease_is_verified() {
        // Live shape, 2026-10-11: a new app conversation's header names the bundle's prerelease.
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-new.jsonl");
        let meta = json!({"type":"session_meta","payload":{"id":"new", "source":"vscode",
            "originator":"codex_work_desktop", "cli_version":"0.162.0-alpha.17.2", "history_mode":"paginated"}});
        fs::write(&path, format!("{meta}\n")).unwrap();
        let process = Process { pid: 42, birth_micros: 123, executable: "/app/codex".into() };
        let new = from_writer(&root, process.clone(), &path, "0.162.0-alpha.17.2", true).unwrap();
        assert_eq!((new.restriction, new.version.as_str()), (None, "0.162.0-alpha.17.2"));
        assert!(normalize("new", &meta).unwrap().is_empty());
        // Not the bundle, not an app header, or not an alpha: still unverified.
        assert_eq!(from_writer(&root, process.clone(), &path, "0.162.0-alpha.17.2", false).unwrap().restriction.as_deref(), Some("runtime-unverified"));
        let tui = json!({"type":"session_meta","payload":{"id":"new", "source":"vscode",
            "originator":"codex-tui", "cli_version":"0.162.0-alpha.17.2", "history_mode":"paginated"}});
        assert!(normalize("new", &tui).is_err());
        let custom = json!({"type":"session_meta","payload":{"id":"new", "source":"vscode",
            "originator":"codex_work_desktop", "cli_version":"0.160.1-custom", "history_mode":"paginated"}});
        fs::write(&path, format!("{custom}\n")).unwrap();
        assert_eq!(from_writer(&root, process, &path, "0.162.0-alpha.17.2", true).unwrap().restriction.as_deref(), Some("runtime-unverified"));
        assert!(normalize("new", &custom).is_err());
    }

    #[test]
    fn local_sessions_codex_desktop_upgrade_keeps_supported_history_verified() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-app.jsonl");
        fs::write(&path, format!("{}\n", json!({"type":"session_meta","payload":{
            "id":"app", "source":"vscode", "originator":"Codex Desktop",
            "cli_version":"0.155.0-alpha.2.6", "history_mode":"paginated"}}))).unwrap();
        let process = Process { pid: 42, birth_micros: 123, executable: "/app/codex".into() };
        let resumed = from_rollout(&root, process.clone(), &path, "0.160.1").unwrap();
        assert_eq!(resumed.restriction, None);
        assert_eq!(resumed.version, "0.160.1", "runtime version must describe the verified writer");
        for unverified in ["", "0.150.0", "0.160.1-custom"] {
            assert_eq!(from_rollout(&root, process.clone(), &path, unverified).unwrap().restriction.as_deref(), Some("runtime-unverified"));
        }
        let meta = json!({"type":"session_meta","payload":{"id":"app","source":"vscode",
            "originator":"Codex Desktop","cli_version":"0.150.0","history_mode":"paginated"}});
        fs::write(&path, format!("{meta}\n")).unwrap();
        assert_eq!(from_rollout(&root, process, &path, "0.160.1").unwrap().restriction.as_deref(), Some("runtime-unverified"));
    }

    #[test]
    fn local_sessions_codex_cli_upgrade_keeps_supported_history_verified() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-cli.jsonl");
        fs::write(&path, format!("{}\n", json!({"type":"session_meta","payload":{
            "id":"cli", "source":"cli", "originator":"codex-tui",
            "cli_version":"0.163.0", "history_mode":"paginated"}}))).unwrap();
        let process = Process { pid: 42, birth_micros: 123, executable: "/bin/codex".into() };
        let resumed = from_rollout(&root, process.clone(), &path, "0.166.0").unwrap();
        assert_eq!(resumed.restriction, None);
        assert_eq!(resumed.version, "0.166.0");
        for unverified in ["", "0.150.0", "0.166.0-custom"] {
            assert_eq!(from_rollout(&root, process.clone(), &path, unverified).unwrap().restriction.as_deref(), Some("runtime-unverified"));
        }
    }

    /// A fork's own rollout header names its origin (seen on a `codex fork` through the daemon,
    /// 0.164.0), so a stopped fork keeps its lineage with no app-server to ask.
    #[test]
    fn local_sessions_codex_a_fork_rollout_names_its_origin() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let process = Process { pid: 42, birth_micros: 123, executable: "/bin/codex".into() };
        let path = root.join("rollout-fork.jsonl");
        fs::write(&path, concat!(r#"{"type":"session_meta","payload":{"id":"01a17a4c-cd88-7092-8922-62097afde080","#,
            r#""forked_from_id":"01a17a4b-d15a-7772-8898-d84c8a12fa4b","forked_from_ordinal_exclusive":31,"#,
            r#""source":"cli","cli_version":"0.159.3","history_mode":"paginated","cwd":"/tmp/cx-e2e"}}"#, "\n")).unwrap();
        let fork = from_rollout(&root, process.clone(), &path, "0.159.3").unwrap();
        assert_eq!(fork.forked_from.as_deref(), Some("01a17a4b-d15a-7772-8898-d84c8a12fa4b"));
        fs::write(&path, concat!(r#"{"type":"session_meta","payload":{"id":"01a17a4b-d15a-7772-8898-d84c8a12fa4b","#,
            r#""source":"cli","cli_version":"0.159.3","history_mode":"paginated"}}"#, "\n")).unwrap();
        assert_eq!(from_rollout(&root, process, &path, "0.159.3").unwrap().forked_from, None);
    }

    #[test]
    fn local_sessions_codex_discovers_only_process_owned_cli_profile_history() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-owned.jsonl");
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"t1\"}}\n").unwrap();
        let process = Process {
            pid: 42,
            birth_micros: 123,
            executable: "/bin/codex".into(),
        };
        let found = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(found.conversation, "owned");
        assert_eq!(found.process.birth_micros, 123);
        assert_eq!(found.status, "busy");
        assert!(!found.app, "a `cli` rollout is a terminal Codex, whatever binary wrote it");
        assert!(from_rollout(
            &root.join("other-profile"),
            process.clone(),
            &path,
            "0.149.0"
        )
        .is_err());
        // The backend caps cwd_name at 128 characters; a longer basename would
        // fail registration and loop the mirror.
        let long = "d".repeat(200);
        fs::write(&path, format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"owned\",\"source\":\"cli\",\"cli_version\":\"0.149.0\",\"cwd\":\"/tmp/{long}\"}}}}\n")).unwrap();
        let bounded_cwd = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(bounded_cwd.cwd_name.unwrap().chars().count(), 128);
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"history_mode\":\"paginated\",\"cli_version\":\"0.153.4\"}}\n").unwrap();
        let app = from_rollout(&root, process.clone(), &path, "0.153.4").unwrap();
        assert!(app.app, "the Codex app's own conversation: never a terminal");
        assert_eq!((app.conversation.as_str(), app.version.as_str(), app.restriction), ("app", "0.153.4", None));
        let unverified = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(unverified.restriction.as_deref(), Some("runtime-unverified"));
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"history_mode\":\"paginated\",\"cli_version\":\"0.150.0\"}}\n").unwrap();
        assert_eq!(from_rollout(&root, process.clone(), &path, "0.150.0").unwrap().restriction.as_deref(), Some("runtime-unverified"));
        // The ChatGPT app's older history layout has none of the item_completed
        // rows this parser reads (text/tools/title live elsewhere), so it is
        // refused rather than mirrored blank.
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"cli_version\":\"0.153.4\",\"history_mode\":\"legacy\"}}\n").unwrap();
        assert!(from_rollout(&root, process.clone(), &path, "0.153.4").is_err());
        // vscode fails closed: an absent/unknown history_mode is refused, not
        // treated as the verified paginated grammar.
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"cli_version\":\"0.153.4\"}}\n").unwrap();
        assert!(from_rollout(&root, process.clone(), &path, "0.153.4").is_err());
        // The VS Code extension shares source "vscode" with the desktop app;
        // only the app's originator is accepted.
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"vscode-extension\",\"history_mode\":\"paginated\",\"cli_version\":\"0.153.4\"}}\n").unwrap();
        assert!(from_rollout(&root, process.clone(), &path, "0.153.4").is_err());
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"app\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"history_mode\":\"paginated\",\"cli_version\":\"0.153.4\"}}\n").unwrap();
        assert_eq!(from_rollout(&root, process.clone(), &path, "0.153.4").unwrap().restriction, None);
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":{\"subagent\":{}},\"cli_version\":\"0.149.0\"}}\n").unwrap();
        assert!(from_rollout(&root, process.clone(), &path, "0.149.0").is_err());
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":\"exec\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        assert!(from_rollout(&root, process, &path, "0.149.0").is_err());
    }
    #[tokio::test]
    async fn local_sessions_codex_capture_mirrors_every_writer_rollout_and_excludes_readers() {
        let dir = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(dir.path()).unwrap();
        let root = home.join(".codex/sessions");
        fs::create_dir_all(&root).unwrap();
        let first = root.join("rollout-first.jsonl");
        let second = root.join("rollout-second.jsonl");
        let read_only = root.join("rollout-read-only.jsonl");
        let refused = root.join("rollout-refused.jsonl");
        fs::write(&first, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"one\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        fs::write(&second, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"two\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        fs::write(&read_only, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"three\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        fs::write(&refused, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"four\",\"source\":\"exec\",\"cli_version\":\"0.149.0\"}}\n").unwrap();

        // One real process (this test binary itself) holding four rollouts open
        // at once: two writers (accepted), one reader only (accepted content,
        // but must be excluded -- a process merely reading another thread's
        // history, the way the app-server loads history or the CLI resume
        // picker browses it, must never be mistaken for that thread's owner),
        // and one writer `from_rollout` refuses on content grounds. Holding the
        // handles here (rather than in a spawned child) makes every fd open
        // before `capture` ever runs, so there is nothing to race.
        let _first_writer = fs::OpenOptions::new().append(true).open(&first).unwrap();
        let _second_writer = fs::OpenOptions::new().append(true).open(&second).unwrap();
        let _reader = fs::File::open(&read_only).unwrap();
        let _refused_writer = fs::OpenOptions::new().append(true).open(&refused).unwrap();

        let sessions = capture(
            &home,
            Path::new("/nonexistent-codex-binary"),
            std::process::id(),
        )
        .await
        .unwrap();

        assert_eq!(
            sessions.len(),
            2,
            "the read-only handle and the refused exec-sourced rollout are both excluded"
        );
        let mut conversations: Vec<_> = sessions.iter().map(|s| s.conversation.clone()).collect();
        conversations.sort();
        assert_eq!(conversations, vec!["one".to_string(), "two".to_string()]);
        assert_eq!(sessions[0].process, sessions[1].process);
    }

    #[tokio::test]
    #[ignore = "requires an explicitly owned disposable native Codex PID and conversation ID"]
    async fn local_sessions_owned_codex_capture_binds_the_running_binary() {
        let home = PathBuf::from(std::env::var("HOME").unwrap());
        let pid = std::env::var("LIGHTBULB_TEST_CODEX_PID")
            .unwrap()
            .parse()
            .unwrap();
        let conversation = std::env::var("LIGHTBULB_TEST_CODEX_THREAD").unwrap();
        let program = provider_program(&home, "codex").unwrap();
        let session = capture(&home, &program, pid)
            .await
            .unwrap()
            .into_iter()
            .find(|s| s.conversation == conversation)
            .unwrap();
        assert_eq!(session.conversation, conversation);
        assert_eq!(session.restriction, None);
        assert_eq!(session.version, "0.149.0");
    }

    #[test]
    fn local_sessions_codex_receipt_is_exact_and_thread_bound() {
        let observed = "Queued message 01a08ec5-b961-77c0-962d-25097fe0f5f4 for thread 01a08ec5-554f-7dc1-baf4-5755bb2de4d1.\n";
        assert_eq!(
            queued_id(observed, "01a08ec5-554f-7dc1-baf4-5755bb2de4d1").as_deref(),
            Some("01a08ec5-b961-77c0-962d-25097fe0f5f4")
        );
        assert!(queued_id(observed, "other").is_none());
        assert!(queued_id(
            &format!("unknown {observed}"),
            "01a08ec5-554f-7dc1-baf4-5755bb2de4d1"
        )
        .is_none());
    }

    #[test]
    fn local_sessions_codex_subagents_attach_by_the_parent_their_rollout_names() {
        let activity = |kind: &str| json!({"type":"event_msg","timestamp":"2026-09-25T10:00:00.000Z","payload":{"type":"item_completed",
            "item":{"type":"SubAgentActivity","id":"i","kind":kind,"agent_path":"/root/worker","agent_thread_id":"child-1"}}});
        let started = normalize("parent", &activity("started")).unwrap();
        assert_eq!(started[1]["lifecycle"], "subagent_start");
        assert_eq!(started[1]["subagent"], "child-1");
        let ended = normalize("parent", &activity("completed")).unwrap();
        assert_eq!(ended[1]["lifecycle"], "subagent_end");
        assert_eq!(ended[1]["tool"]["result"]["background"], false, "a Codex child never notifies");
        assert_eq!(normalize("parent", &activity("interacted")).unwrap().len(), 1);

        let root = tempfile::tempdir().unwrap();
        let day = |d: &str| {
            let p = root.path().join("2026/09").join(d);
            fs::create_dir_all(&p).unwrap();
            p
        };
        let parent_path = day("24").join("rollout-2026-09-24T09-00-00-parent.jsonl");
        fs::write(&parent_path, "").unwrap();
        let meta = |parent: &str| json!({"type":"session_meta","payload":{"id":"child-1","timestamp":"2026-09-25T10:00:00.000Z",
            "source":{"subagent":{"thread_spawn":{"parent_thread_id":parent,"agent_nickname":"Poincare","agent_role":"explorer","depth":1}}}}});
        let child = day("25").join("rollout-2026-09-25T10-00-00-child-1.jsonl");
        fs::write(&child, format!("{}\n", meta("someone-else"))).unwrap();
        assert!(child_rollout(&parent_path, "parent", "child-1").is_none(), "another thread's child is not ours");
        fs::write(&child, format!("{}\n", meta("parent"))).unwrap();
        let (path, start) = child_rollout(&parent_path, "parent", "child-1").unwrap();
        assert_eq!(path, child);
        assert_eq!((start["text"].as_str(), start["subagent"].as_str()), (Some("Poincare"), Some("child-1")));
        assert_eq!(start["tool"]["input"]["subagentType"], "explorer");
    }
    #[test]
    fn local_sessions_codex_thread_name_is_the_newest_index_row() {
        let dir = tempfile::tempdir().unwrap();
        let index = dir.path().join("session_index.jsonl");
        assert_eq!(thread_name(&index, "t1"), None, "no index: the first prompt stays the title");
        fs::write(&index, concat!(
            "{\"id\":\"t1\",\"thread_name\":\"First name\"}\n",
            "{\"id\":\"t2\",\"thread_name\":\"Other thread\"}\n",
            "not json\n",
            "{\"id\":\"t1\",\"thread_name\":\"Renamed\"}\n",
        )).unwrap();
        assert_eq!(thread_name(&index, "t1").as_deref(), Some("Renamed"));
        assert_eq!(thread_name(&index, "t2").as_deref(), Some("Other thread"));
        assert_eq!(thread_name(&index, "t3"), None);
    }

}

