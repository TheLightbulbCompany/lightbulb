//! Resume, the owner's Mac side: start an ended session again because its owner, or a teammate
//! who could send to it when it ended, asked from web, desktop or iPhone (spec §5.7).
//!
//! The relay loop hands this task the `resume` list from a heartbeat acknowledgement. The backend
//! decided who may resume and in which mode; this side checks what only this Mac knows: sharing is
//! on, the CLI is installed, the session's folder still exists, and nothing already runs that
//! conversation (two `claude --resume` processes would fork it). A conversation the provider's own
//! daemon still runs (a Claude background job, a Codex app-server thread) is shown again instead
//! (`reattach`). Anyone but this Mac's own human
//! gets plan mode whatever the request says. It starts the provider in a holder the way New
//! session does. A refusal is reported to the backend once; it never touches a mirror.
use super::transport::relay_log;
use super::*;
use std::collections::{BTreeMap, HashSet};
use std::sync::OnceLock;
use std::time::Duration;
use tokio::sync::mpsc;

#[derive(Clone, Debug, PartialEq)]
pub(super) struct Wanted {
    resume_id: String,
    session_id: String,
    provider: String,
    conversation_id: String,
    by_user_id: String,
    by_name: String,
    title: String,
    /// "plan" or "default", as the backend decided for this person.
    mode: String,
    /// A live mirror on this Mac already holds this conversation.
    running: bool,
    /// That mirror, when the provider's own daemon hosts the conversation (a Claude background
    /// job, a Codex app-server thread): Lightbulb shows it again instead of starting it twice.
    hosted: Option<Hosted>,
    /// The requested conversation continues as `conversation_id` (`latest`): another session's
    /// row, which no run started here can settle this request on.
    continued: bool,
}

/// A live mirror whose conversation runs in the provider's own daemon. Closing a held Claude
/// terminal can move its conversation into a background job (`claude bg-spare`, prod 2026-10-26):
/// it runs on, so Resume shares that mirror again and its attach holder shows it.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct Hosted {
    key: String,
    session_id: Option<String>,
    /// The generation the relay last acknowledged: the one its row is at.
    generation: String,
    shared: bool,
    /// Its run is not the one the relay has: its registration is a new process's, which is what
    /// answers a waiting resume (`session_resume.on_register`). A same-generation one answers none.
    fresh: bool,
    /// The provider's id for it: what `claude attach` takes.
    job: String,
    /// Its mirror ended because the daemon retired the job's process (`bg retire …: idle-prompt`,
    /// prod 2026-10-26). The job and its conversation remain: attaching starts a new process, whose
    /// registration answers the resume. Never `claude --resume` it (a second branch).
    retired: bool,
}

// One sender per running relay host, as for pick-up snapshots: the relay loop only offers.
static WANTED: std::sync::Mutex<Option<mpsc::Sender<Vec<Wanted>>>> = std::sync::Mutex::new(None);

pub(super) fn channel() -> mpsc::Receiver<Vec<Wanted>> {
    let (tx, rx) = mpsc::channel(1);
    if let Ok(mut slot) = WANTED.lock() {
        *slot = Some(tx);
    }
    rx
}

/// Called from the relay loop with an acknowledgement's `resume` key (absent on an older backend).
pub(super) fn offer(resume: &Value, mirrors: &BTreeMap<String, Mirror>) {
    let config = claude_config(&home_dir());
    let wanted = tag(resume, |provider, conversation| {
        let mut newest = latest(&config, provider, conversation);
        // Only while the transcript cannot say (it is gone): a readable one that did not move on
        // means its person kept using it, and the continuation is a branch beside it. Then a live
        // session that remembers `conversation` as one of its earlier ids is where it went.
        if newest == conversation && provider == "claude" && super::snapshot::find_transcript(&config, conversation).is_none() {
            if let Some(m) = mirrors
                .values()
                .find(|m| m.ended_at.is_none() && m.native.provider == provider && m.native.aliases.iter().any(|a| a == conversation))
            {
                newest.clone_from(&m.native.conversation);
            }
        }
        let running = live(mirrors, provider, &newest);
        (newest, running)
    });
    if wanted.is_empty() {
        return;
    }
    if let Some(tx) = WANTED.lock().ok().and_then(|s| s.clone()) {
        let _ = tx.try_send(wanted);
    }
}

/// Whether a live mirror holds `conversation` (Some), and whether its provider's daemon hosts it.
fn live(mirrors: &BTreeMap<String, Mirror>, provider: &str, conversation: &str) -> Option<Option<Hosted>> {
    let held = |m: &&Mirror| m.native.provider == provider && m.native.conversation == conversation;
    let retired = |m: &&Mirror| m.native.job.as_deref().and_then(crate::holder::manager::attach_provider) == Some("claude");
    let (key, m) = mirrors
        .iter()
        .find(|(_, m)| m.ended_at.is_none() && held(m))
        .or_else(|| mirrors.iter().find(|(_, m)| held(m) && retired(m)))?;
    Some(m.native.job.clone().map(|job| Hosted {
        key: key.clone(),
        session_id: m.session_id.clone(),
        generation: m.registration.acknowledged.clone().unwrap_or_else(|| m.generation.clone()),
        shared: !m.withheld(),
        fresh: m.registration.acknowledged.as_ref().is_some_and(|known| *known != m.generation),
        job,
        retired: m.ended_at.is_some(),
    }))
}

/// The newest conversation `conversation` continues as. ←, `/background` and a peer reply move a
/// Claude conversation into a background job under a NEW id and write a `continued-in` row to the
/// origin's transcript; `claude --resume` of that origin is allowed and starts a second live branch
/// of it (how "ship-agent-network-phase-1 (2)" appeared). So Resume follows the chain as `/resume`
/// does: to a continuation whose transcript exists, while nothing was typed in the origin after it.
pub(crate) fn latest(config: &Path, provider: &str, conversation: &str) -> String {
    let mut id = conversation.to_owned();
    if provider != "claude" {
        return id;
    }
    for _ in 0..8 {
        let next = super::snapshot::find_transcript(config, &id)
            .and_then(|path| super::claude::continued_in(&path))
            .filter(|next| *next != id && super::snapshot::find_transcript(config, next).is_some());
        match next {
            Some(next) => id = next,
            None => break,
        }
    }
    id
}

/// `running` names the conversation a resume of `conversation` acts on (`latest`), and whether a
/// live mirror holds it.
fn tag(resume: &Value, running: impl Fn(&str, &str) -> (String, Option<Option<Hosted>>)) -> Vec<Wanted> {
    let uuid = |v: &Value| v.as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok()).map(str::to_owned);
    let text = |v: &Value, max: usize, or: &str| v.as_str().unwrap_or(or).chars().take(max).collect::<String>();
    resume
        .as_array()
        .into_iter()
        .flatten()
        .take(8)
        .filter_map(|e| {
            let provider = e["provider"].as_str().filter(|p| matches!(*p, "claude" | "codex"))?.to_owned();
            // A UUID can never be read as a flag by the CLI it is handed to.
            let requested = uuid(&e["conversationId"])?;
            let (conversation_id, running) = running(&provider, &requested);
            let continued = conversation_id != requested;
            Some(Wanted {
                resume_id: uuid(&e["resumeId"])?,
                session_id: uuid(&e["sessionId"])?,
                by_user_id: e["byUserId"].as_str().filter(|s| !s.is_empty())?.chars().take(128).collect(),
                by_name: text(&e["byName"], 80, "A teammate"),
                title: text(&e["title"], 120, "a session"),
                mode: if e["mode"] == "default" { "default".into() } else { "plan".into() },
                provider,
                conversation_id,
                // A retired job runs nothing until it is attached.
                running: running.as_ref().is_some_and(|h| h.as_ref().is_none_or(|h| !h.retired)),
                hosted: running.flatten(),
                continued,
            })
        })
        .collect()
}

/// The exact argv: the fixed installed binary, the conversation, and a mode no looser than the
/// owner's default. Anyone but this Mac's own human gets plan mode, whatever the request says.
/// `daemon`: Codex's shared daemon runs. Then the holder is one more client that rejoins the thread
/// the daemon runs (`codex::resume_on_daemon` loads it first, with this mode), so every client sees
/// ONE thread. Only without a daemon is it a private `--no-daemon` writer.
/// `kept`: the Claude permission mode the session last ran in (`last_claude_mode`). This Mac's own
/// human resumes in it, as `claude --resume` in their terminal would; anyone else in plan.
fn argv(program: &Path, w: &Wanted, own: bool, daemon: bool, kept: Option<&str>) -> Vec<String> {
    let plan = !own || w.mode != "default";
    let program = program.to_string_lossy().into_owned();
    if w.provider == "codex" && daemon {
        // As `holder::manager::CODEX_ATTACH_SCRIPT` rejoins: no permission flags (a remote resume
        // refuses them), and no update prompt over the conversation.
        return vec![
            program,
            "--remote".into(),
            "unix://".into(),
            "-c".into(),
            "check_for_update_on_startup=false".into(),
            "resume".into(),
            w.conversation_id.clone(),
        ];
    }
    if w.provider == "codex" {
        // A plan keeps read-only files while networking remains available. Codex's
        // legacy read-only flag has no network setting; use its documented profile.
        let mut args = vec![program, "--no-daemon".into()];
        if plan {
            let profile = format!("lightbulb-plan-{}", uuid::Uuid::new_v4().simple());
            args.extend(["-c".into(), format!("default_permissions=\"{profile}\""), "-c".into(),
                format!("permissions.{profile}={{extends=\":read-only\",network={{enabled=true}}}}")]);
        } else {
            args.extend(["--sandbox".into(), "workspace-write".into(), "-c".into(), crate::session_start::CODEX_INTERNET.into()]);
        }
        args.extend(["--ask-for-approval".into(), "on-request".into(), "-c".into(),
            "check_for_update_on_startup=false".into(), "resume".into(), w.conversation_id.clone()]);
        args
    } else {
        vec![
            program,
            "--resume".into(),
            w.conversation_id.clone(),
            "--permission-mode".into(),
            (if plan { "plan" } else { claude_mode(kept) }).into(),
        ]
    }
}

/// The `--permission-mode` that keeps a session's last mode. Never one that skips every prompt
/// (`bypassPermissions`, `dontAsk`): a click in a browser does not grant that; those, and a session
/// with no mode on record, resume in `manual`.
fn claude_mode(kept: Option<&str>) -> &'static str {
    match kept {
        Some("acceptEdits") => "acceptEdits",
        Some("auto") => "auto",
        Some("plan") => "plan",
        _ => "manual",
    }
}

/// The permission mode a Claude session last ran in: its transcript's last `permission-mode` row
/// (Claude writes one whenever the mode is set or changed).
fn last_claude_mode(claude_config: &Path, conversation: &str) -> Option<String> {
    let transcript = super::snapshot::find_transcript(claude_config, conversation)?;
    BufReader::new(fs::File::open(transcript).ok()?)
        .lines()
        .map_while(std::result::Result::ok)
        .filter(|line| line.contains("\"permission-mode\""))
        .filter_map(|line| serde_json::from_str::<Value>(&line).ok())
        .filter(|row| row["type"] == "permission-mode")
        .filter_map(|row| row["permissionMode"].as_str().map(str::to_owned))
        .last()
}

/// The folder the session last ran in, from its own transcript (Claude) or rollout (Codex), if it
/// still exists. The same folder, never one the request names.
pub(crate) fn folder(home: &Path, claude_config: &Path, provider: &str, conversation: &str) -> Option<PathBuf> {
    let cwd = if provider == "codex" {
        codex_cwd(home, conversation)
    } else {
        super::snapshot::find_transcript(claude_config, conversation)
            .and_then(|t| super::snapshot::cwd_and_version(&t).ok()?.0)
    }?;
    fs::canonicalize(cwd).ok().filter(|p| p.is_dir())
}

/// `~/.codex/sessions/YYYY/MM/DD/rollout-…-<thread>.jsonl`: its first row is the thread's header.
fn codex_cwd(home: &Path, thread: &str) -> Option<PathBuf> {
    let suffix = format!("-{thread}.jsonl");
    let level = |dir: PathBuf| fs::read_dir(dir).into_iter().flatten().flatten().map(|e| e.path());
    let rollout = level(home.join(".codex/sessions"))
        .flat_map(level)
        .flat_map(level)
        .flat_map(level)
        .find(|p| p.file_name().is_some_and(|n| n.to_string_lossy().ends_with(&suffix)))?;
    let mut line = String::new();
    BufReader::new(fs::File::open(rollout).ok()?.take(65536)).read_line(&mut line).ok()?;
    let row: Value = serde_json::from_str(&line).ok()?;
    if row["type"] != "session_meta" || row["payload"]["id"] != thread {
        return None;
    }
    row["payload"]["cwd"].as_str().map(PathBuf::from)
}

/// The local-sessions dir a resume checks sharing in.
/// None inside: the dir was unavailable, so every resume is refused as SHARING_OFF.
static STATE_DIR: OnceLock<Option<PathBuf>> = OnceLock::new();

/// From the app's setup.
pub(crate) fn install(app: &tauri::AppHandle) {
    let _ = STATE_DIR.set(super::enrollment::state_dir(app).ok());
}

/// From `serve`, which has no app.
pub(crate) fn install_dir(dir: PathBuf) {
    let _ = STATE_DIR.set(Some(dir));
}

pub(super) struct Resumer {
    pub api: String,
    pub key_pem: String,
    pub device: String,
    pub owner: String,
    /// This Mac's own human: the only person who gets anything but plan mode.
    pub human: String,
}

/// Runs on its own task (see `enrollment.rs`), never inside the relay's `select!`.
pub(super) async fn run(mut rx: mpsc::Receiver<Vec<Wanted>>, me: Resumer) {
    // The same request rides every acknowledgement until its run registers: act on it once.
    let mut handled = HashSet::<String>::new();
    while let Some(list) = rx.recv().await {
        for wanted in list {
            if !handled.insert(wanted.resume_id.clone()) {
                continue;
            }
            let Some(dir) = STATE_DIR.get() else {
                handled.remove(&wanted.resume_id);
                continue;
            };
            let short = &wanted.session_id[..8];
            match start(dir.as_deref(), &wanted, &me).await {
                Ok(Some(true)) => relay_log(&format!("{short} resumed")),
                Ok(Some(false)) => relay_log(&format!("{short} already runs; its registration answers the resume")),
                // Nothing will answer it yet: the next acknowledgement asks again.
                Ok(None) => {
                    handled.remove(&wanted.resume_id);
                }
                Err(code) => {
                    relay_log(&format!("{short} resume refused: {code}"));
                    if let Err(e) = report(&me, &wanted, code).await {
                        relay_log(&format!("{short} resume refusal not reported: {e}"));
                        handled.remove(&wanted.resume_id); // the next acknowledgement asks again
                    }
                }
            }
        }
    }
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default()
}

/// Some(true): started or shown again. Some(false): a registration answers it. None: ask again.
async fn start(dir: Option<&Path>, w: &Wanted, me: &Resumer) -> std::result::Result<Option<bool>, &'static str> {
    if !dir.is_some_and(super::enrollment::sharing_on) {
        return Err("SHARING_OFF");
    }
    let own = w.by_user_id == me.human;
    // Never `--resume` a continued origin (a second live branch of it). Its continuation is its own
    // session: shown again when it runs here, and this request is refused so it settles.
    if w.continued {
        // Settled only once the continuation is shown: a failed reattach is the refusal, and one
        // that waits (not bound yet) is asked again.
        if let Some(hosted) = &w.hosted {
            if reattach(me, hosted, own).await?.is_none() && !hosted.shared {
                return Ok(None);
            }
        }
        return Err("CONTINUED");
    }
    // A retired job removed since (`claude rm`) has nothing to attach: it resumes as any other.
    let jobs = claude_config(&home_dir()).join("jobs");
    if let Some(hosted) = w.hosted.as_ref().filter(|h| !h.retired || jobs.join(&h.job).is_dir()) {
        return reattach(me, hosted, own).await;
    }
    // A background job Claude still lists (its mirror is gone or never knew the job) is attached,
    // never refused as running elsewhere: `free` below would find it in the same listing.
    if w.provider == "claude" {
        if let Some(job) = claude::background_job(&home_dir(), &w.conversation_id).await {
            reopen(job).await?;
            return Ok(Some(true));
        }
    }
    let want = w.clone();
    let Some((folder, argv)) = tauri::async_runtime::spawn_blocking(move || prepare(&want, own, &home_dir()))
        .await
        .map_err(|_| "LAUNCH_FAILED")??
    else {
        return Ok(Some(false));
    };
    // `running` only knows held sessions: a private `claude --resume` / `codex resume` of the same
    // conversation is invisible to the mirrors. Two writers on one transcript fork it, so a resume
    // that cannot prove the conversation is free does not start.
    free(running_conversations(home_dir(), w.provider.clone()).await, &w.conversation_id)?;
    let mut argv = argv;
    let daemon = codex::on_daemon(&argv);
    if daemon {
        let loaded = match codex::daemon_socket(&home_dir()) {
            Some(socket) => codex::resume_on_daemon(&socket, &w.conversation_id, &folder, !own || w.mode != "default").await,
            None => Err(codex::UNAVAILABLE.to_owned()),
        };
        match loaded {
            Ok(()) => {}
            // A daemon that is gone or stalled: the private writer it always was.
            Err(e) if e == codex::UNAVAILABLE => {
                relay_log(&format!("{} resume: {e}, so it starts as a private --no-daemon writer", &w.session_id[..8]));
                argv = self::argv(Path::new(&argv[0]), w, own, false, None);
            }
            Err(e) => {
                relay_log(&format!("{} resume on the Codex daemon refused: {e}", &w.session_id[..8]));
                return Err(if e == codex::LOOSER { "ALREADY_RUNNING" } else { "LAUNCH_FAILED" });
            }
        }
    }
    if w.provider == "claude" {
        // Nobody is at this terminal to answer Claude's trust dialog, and until it is answered
        // Claude writes no session file, so Lightbulb never sees the run and the resume waits
        // forever. The folder passed the owner's approved-folder check above: that is the consent.
        if let Err(e) = trust_claude_folder(&claude_global_config(&home_dir()), &folder) {
            relay_log(&format!("{} resume: Claude trust not recorded: {e}", &w.session_id[..8]));
        }
    }
    let held = crate::session_start::start_held(folder, argv).await.map_err(|e| {
        relay_log(&format!("{} resume launch failed: {e}", &w.session_id[..w.session_id.len().min(8)]));
        "LAUNCH_FAILED"
    })?;
    bind_resumed_holder(&held, w);
    let who = if own { "You".to_owned() } else { w.by_name.clone() };
    let how = if own && w.mode == "default" {
        "End it from its panel in Lightbulb."
    } else {
        "In plan mode. End it from its panel in Lightbulb."
    };
    let _ = crate::notify::show(
        format!("{who} resumed {}", w.title),
        Some(how.into()),
        Some(format!("resume-{}", w.resume_id)),
        None,
        None,
    )
    .await;
    Ok(Some(true))
}

fn bind_resumed_holder(held: &crate::holder::manager::HolderInfo, w: &Wanted) {
    if w.provider == "codex" && codex::on_daemon(&held.argv) {
        // The RPC above proved this daemon thread loaded. A private TUI's
        // requested target is not ownership: discovery binds its actual writer.
        crate::holder::manager::bind_thread(held, &w.conversation_id);
    }
}

/// The conversation runs on in its provider's daemon: share its mirror again, and the relay loop
/// registers its run (that registration answers the resume) and starts an attach holder for
/// whoever watches. A run its person paused is unpaused first, as the share skill does: its
/// register carries `unshared` over. Only this Mac's own human may undo their own pause.
async fn reattach(me: &Resumer, hosted: &Hosted, own: bool) -> std::result::Result<Option<bool>, &'static str> {
    if !hosted.shared {
        if !own {
            return Err("SHARING_OFF");
        }
        // A mirror the relay has not bound yet cannot be unpaused, and sharing it would let its first
        // registration settle the resume with the row still paused: wait until it is bound.
        let Some(sid) = &hosted.session_id else {
            return Ok(None);
        };
        let unpause = json!({"sessionId": sid, "generation": hosted.generation, "decision": "unpause"});
        post(me, "/api/v1/messaging/native-session-decisions", &unpause).await.map_err(|e| {
            relay_log(&format!("{} unpause for resume failed: {e}", &sid[..sid.len().min(8)]));
            "LAUNCH_FAILED"
        })?;
        super::transport::ask_share(&hosted.key, true);
    }
    if hosted.retired {
        reopen(hosted.job.clone()).await?;
        return Ok(Some(true));
    }
    Ok(hosted.fresh.then_some(!hosted.shared))
}

/// `claude attach <job>` in an attach holder: it starts a retired job's process again.
async fn reopen(job: String) -> std::result::Result<(), &'static str> {
    let shown = job.clone();
    tauri::async_runtime::spawn_blocking(move || super::screen::reopen(&job))
        .await
        .map_err(|_| "LAUNCH_FAILED")?
        .map_err(|e| {
            relay_log(&format!("attach {shown} for resume failed: {e}"));
            "LAUNCH_FAILED"
        })
}

/// Every conversation a local `provider` process runs now, held or private: the provider's own
/// unfiltered listing, plus any process whose arguments name a conversation (a `--resume` that
/// has not reached the listing yet). Err when either cannot be read. Nothing here reaches the
/// backend: only the refusal code does.
async fn running_conversations(home: PathBuf, provider: String) -> std::result::Result<Vec<String>, String> {
    let mut ids = if provider == "codex" {
        codex::discover(&home).await?.into_iter().map(|s| s.conversation).collect::<Vec<_>>()
    } else {
        claude::running_conversations(&home).await?
    };
    let args = native_output(Path::new("/bin/ps"), &["-axo", "pid=,args="]).await?;
    let holders: HashSet<u32> = tauri::async_runtime::spawn_blocking(|| crate::holder::manager::list().into_iter().map(|h| h.pid).collect())
        .await
        .map_err(|_| "holder list unavailable")?;
    ids.extend(named_in(&args, &provider, &holders));
    Ok(ids)
}

/// Every word of each `ps -axo pid=,args=` line that mentions `provider`, except a holder's own
/// line. A holder's arguments name the provider and `--resume <conversation>` it started, and a
/// holder whose provider ended stays up to show its last picture until the app quits: read as a
/// running conversation, it refused Resume with ALREADY_RUNNING on a session nobody could type
/// into (prod 2026-10-22). A live holder is `prepare`'s to answer, and its provider child has a
/// line of its own here.
fn named_in(ps: &str, provider: &str, holders: &HashSet<u32>) -> Vec<String> {
    ps.lines()
        .filter_map(|line| line.trim_start().split_once(' '))
        .filter(|(pid, args)| args.contains(provider) && !pid.parse().is_ok_and(|pid: u32| holders.contains(&pid)))
        .flat_map(|(_, args)| args.split_whitespace().map(str::to_owned).collect::<Vec<_>>())
        .collect()
}

/// Ok when nothing runs `conversation`; ALREADY_RUNNING when something does; LAUNCH_FAILED when
/// the listing could not be read (unproven is not free).
fn free(listing: std::result::Result<Vec<String>, String>, conversation: &str) -> std::result::Result<(), &'static str> {
    match listing {
        Ok(ids) if ids.iter().any(|id| id == conversation) => Err("ALREADY_RUNNING"),
        Ok(_) => Ok(()),
        Err(e) => {
            relay_log(&format!("resume refused: local sessions unreadable ({e})"));
            Err("LAUNCH_FAILED")
        }
    }
}

/// Everything this Mac must agree to before anything starts. `Ok(None)`: a holder already runs
/// this conversation -- most likely the one this resume started before the app restarted -- and
/// its registration answers the request.
fn prepare(w: &Wanted, own: bool, home: &Path) -> std::result::Result<Option<(PathBuf, Vec<String>)>, &'static str> {
    if crate::holder::manager::list().iter().any(|h| h.argv.iter().any(|a| a == &w.conversation_id) && !crate::holder::manager::is_ended(h)) {
        return Ok(None);
    }
    if w.running {
        return Err("ALREADY_RUNNING");
    }
    // As the Claude app gates its own Resume (`liveOwnershipRefusal`): any live process Claude's
    // records name for this id -- a terminal parked in the agent view included -- is a writer, and
    // `--resume` beside it is a second branch. Its row stays live meanwhile, and shows its tail.
    if w.provider == "claude" && claude::Holders::read(&claude_config(home)).holds(&w.conversation_id, true, None) {
        return Err("ALREADY_RUNNING");
    }
    if !crate::session_start::tools(home).iter().any(|t| t["id"] == w.provider.as_str()) {
        return Err("TOOL_UNAVAILABLE");
    }
    let program = provider_program(home, &w.provider).ok_or("TOOL_UNAVAILABLE")?;
    let folder = folder(home, &claude_config(home), &w.provider, &w.conversation_id).ok_or("FOLDER_MISSING")?;
    // The folder it last ran in must still be approved now, as for New session and the CLI: the
    // owner may have removed it from File access after the session ended.
    let folder = approved_folder(&folder, &crate::session_start::approved_now())?;
    let daemon = w.provider == "codex" && codex::daemon_socket(home).is_some();
    if w.provider == "codex" && !daemon {
        relay_log(&format!("{} resume: Codex's shared daemon is not running, so it starts as a private --no-daemon writer", &w.session_id[..8]));
    }
    let kept = (w.provider == "claude").then(|| last_claude_mode(&claude_config(home), &w.conversation_id)).flatten();
    Ok(Some((folder, argv(&program, w, own, daemon, kept.as_deref()))))
}

pub(crate) fn claude_config(home: &Path) -> PathBuf {
    std::env::var_os("CLAUDE_CONFIG_DIR").map(PathBuf::from).unwrap_or_else(|| home.join(".claude"))
}

/// Claude's global config, where it keeps the folders a person trusted.
fn claude_global_config(home: &Path) -> PathBuf {
    std::env::var_os("CLAUDE_CONFIG_DIR").map_or_else(|| home.join(".claude.json"), |d| PathBuf::from(d).join(".claude.json"))
}

/// Record `folder` as trusted in Claude's `config`, as accepting Claude's trust dialog there does
/// (`projects[<folder>].hasTrustDialogAccepted`, the key Claude's own error text names). Claude's
/// trust of a parent stops at a git root, so a new worktree under a trusted folder is untrusted.
/// Claude saves this file by merging into what is on disk, so the entry survives its next save.
/// A missing config is left missing; one that is not a JSON object is left as it is.
fn trust_claude_folder(config: &Path, folder: &Path) -> std::result::Result<(), String> {
    use std::os::unix::fs::OpenOptionsExt;
    let text = match fs::read_to_string(config) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(format!("read: {e}")),
    };
    let mut doc: Value = serde_json::from_str(&text).map_err(|e| format!("not JSON: {e}"))?;
    let key = folder.to_str().ok_or("folder is not UTF-8")?;
    if doc["projects"][key]["hasTrustDialogAccepted"] == true {
        return Ok(());
    }
    let project = doc
        .as_object_mut()
        .ok_or("not an object")?
        .entry("projects")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("projects is not an object")?
        .entry(key)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("project is not an object")?;
    project.insert("hasTrustDialogAccepted".into(), Value::Bool(true));
    super::backup_once(config)?;
    // Renamed over it: Claude never reads a half-written file.
    let tmp = config.with_file_name(format!(".claude.json.lightbulb-{}", uuid::Uuid::new_v4().simple()));
    let written = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&tmp)
        .and_then(|mut f| f.write_all(serde_json::to_string_pretty(&doc).map_err(std::io::Error::other)?.as_bytes()))
        .and_then(|()| fs::rename(&tmp, config));
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    written.map_err(|e| format!("write: {e}"))
}

pub(crate) fn approved_folder(folder: &Path, approved: &[PathBuf]) -> std::result::Result<PathBuf, &'static str> {
    folder
        .to_str()
        .and_then(|f| crate::file_access::canonicalize(f).ok())
        .filter(|f| crate::session_start::inside_approved(f, approved))
        .ok_or("FOLDER_NOT_APPROVED")
}

async fn report(me: &Resumer, w: &Wanted, code: &str) -> Result<()> {
    let path = format!("/api/v1/desktop/session-resume/{}/failed", w.session_id);
    post(me, &path, &json!({"resumeId": w.resume_id, "code": code})).await
}

async fn post(me: &Resumer, path: &str, body: &Value) -> Result<()> {
    let token = crate::device_token::mint_device_token(
        &me.key_pem,
        &me.device,
        &me.owner,
        crate::device_token::DEVICE_TOKEN_LIFETIME_SECS,
    )?;
    let url = format!("{}{path}", me.api.trim_end_matches('/'));
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let status = client
        .post(url)
        .bearer_auth(token)
        .json(body)
        .send()
        .await
        .map_err(|_| "request failed")?
        .status()
        .as_u16();
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!("{path} returned {status}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ACK: &str =
        include_str!("../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat-ack-resume.json");
    const CONV: &str = "11111111-2222-4333-8444-555555555555";

    pub(super) fn wanted(provider: &str, mode: &str) -> Wanted {
        Wanted {
            resume_id: "aaaaaaaa-1111-4111-8111-111111111111".into(),
            session_id: "bbbbbbbb-2222-4222-8222-222222222222".into(),
            provider: provider.into(),
            conversation_id: CONV.into(),
            by_user_id: "user_bob".into(),
            by_name: "Bob".into(),
            title: "Fix login".into(),
            mode: mode.into(),
            running: false,
            hosted: None,
            continued: false,
        }
    }

    #[test]
    fn every_codex_resume_opens_the_conversation_without_an_update_menu() {
        for daemon in [false, true] {
            let args = argv(Path::new("/bin/codex"), &wanted("codex", "default"), true, daemon, None);
            assert!(args.windows(2).any(|a| a == ["-c", "check_for_update_on_startup=false"]), "{args:?}");
        }
    }

    #[test]
    fn a_private_codex_resume_waits_for_writer_proof_and_a_daemon_is_bound() {
        let dir = tempfile::tempdir().unwrap();
        let mut held = crate::holder::manager::HolderInfo {
            id: "ab12cd34".into(), socket: dir.path().join("ab12cd34.sock"),
            pid: 1, provider_pid: 2, cwd: dir.path().into(), watch: true, type_input: true,
            argv: argv(Path::new("/bin/codex"), &wanted("codex", "default"), true, false, None),
        };
        bind_resumed_holder(&held, &wanted("codex", "default"));
        assert!(crate::holder::manager::thread(&held).is_none());
        held.argv = argv(Path::new("/bin/codex"), &wanted("codex", "default"), true, true, None);
        bind_resumed_holder(&held, &wanted("codex", "default"));
        assert_eq!(crate::holder::manager::thread(&held).as_deref(), Some(CONV));
        std::fs::remove_file(held.socket.with_extension("thread")).unwrap();
        held.argv = argv(Path::new("/bin/claude"), &wanted("claude", "default"), true, false, None);
        bind_resumed_holder(&held, &wanted("claude", "default"));
        assert!(crate::holder::manager::thread(&held).is_none());
    }

    #[test]
    fn resume_offer_reads_the_relay_entry_and_sees_a_running_conversation() {
        let ack: Value = serde_json::from_str(ACK).unwrap();
        assert_eq!(tag(&ack["resume"], |_, c| (c.to_owned(), None)), vec![wanted("claude", "plan")]);
        let held = tag(&ack["resume"], |provider, conversation| (conversation.to_owned(), (provider == "claude" && conversation == CONV).then_some(None)));
        assert!(held[0].running);
        let mut odd = ack["resume"].clone();
        odd[0]["conversationId"] = json!("--dangerously-skip-permissions");
        assert!(tag(&odd, |_, c| (c.to_owned(), None)).is_empty(), "a conversation id that is not a UUID is refused");
        let mut shell = ack["resume"].clone();
        shell[0]["provider"] = json!("bash");
        assert!(tag(&shell, |_, c| (c.to_owned(), None)).is_empty());
        let mut loose = ack["resume"].clone();
        loose[0]["mode"] = json!("bypassPermissions");
        assert_eq!(tag(&loose, |_, c| (c.to_owned(), None))[0].mode, "plan", "anything but default reads as plan");
    }

    /// On Codex's shared daemon a resume is one more client that rejoins the thread the daemon
    /// runs (`codex::resume_on_daemon` loaded it with the mode), never a `--no-daemon` writer that
    /// forks it; a remote resume takes no permission flags (Codex refuses them).
    #[test]
    fn resume_argv_on_the_codex_daemon_rejoins_the_thread() {
        let held = argv(Path::new("/bin/codex"), &wanted("codex", "default"), false, true, None);
        assert_eq!(held, ["/bin/codex", "--remote", "unix://", "-c", "check_for_update_on_startup=false", "resume", CONV]);
        assert!(codex::on_daemon(&held));
        assert!(!held.iter().any(|a| a == "--no-daemon" || a == "--sandbox" || a == "--ask-for-approval"));
        // Without a daemon it is the private writer it always was.
        assert!(!codex::on_daemon(&argv(Path::new("/bin/codex"), &wanted("codex", "default"), false, false, None)));
        assert_eq!(argv(Path::new("/bin/claude"), &wanted("claude", "default"), true, true, None)[1], "--resume");
    }

    #[test]
    fn resume_argv_gives_anyone_but_this_macs_human_plan_mode() {
        let claude = Path::new("/bin/claude");
        assert_eq!(argv(claude, &wanted("claude", "default"), false, false, None), ["/bin/claude", "--resume", CONV, "--permission-mode", "plan"]);
        assert_eq!(argv(claude, &wanted("claude", "default"), true, false, None), ["/bin/claude", "--resume", CONV, "--permission-mode", "manual"]);
        assert_eq!(argv(claude, &wanted("claude", "plan"), true, false, None)[4], "plan");
        // The owner's own resume keeps the mode the session last ran in; never one that skips every prompt.
        for (kept, flag) in [("auto", "auto"), ("acceptEdits", "acceptEdits"), ("plan", "plan"), ("default", "manual"), ("bypassPermissions", "manual"), ("dontAsk", "manual")] {
            assert_eq!(argv(claude, &wanted("claude", "default"), true, false, Some(kept))[4], flag, "{kept}");
        }
        assert_eq!(argv(claude, &wanted("claude", "default"), false, false, Some("auto"))[4], "plan", "a teammate's resume is plan");
        assert_eq!(argv(claude, &wanted("claude", "plan"), true, false, Some("auto"))[4], "plan", "a plan request stays plan");
        let codex = Path::new("/bin/codex");
        let plan = argv(codex, &wanted("codex", "default"), false, false, None);
        assert_eq!(&plan[..2], ["/bin/codex", "--no-daemon"]);
        assert!(plan.iter().any(|arg| arg.starts_with("default_permissions=\"lightbulb-plan-")));
        assert!(plan.iter().any(|arg| arg.contains("extends=\":read-only\",network={enabled=true}")));
        assert!(!plan.iter().any(|arg| arg == "--sandbox" || arg == "workspace-write" || arg == "danger-full-access"));
        assert!(plan.windows(2).any(|pair| pair == ["--ask-for-approval", "on-request"]));
        assert_eq!(&plan[plan.len()-2..], ["resume", CONV]);
        let owner = argv(codex, &wanted("codex", "default"), true, false, None);
        assert!(owner.iter().any(|arg| arg == crate::session_start::CODEX_INTERNET));
        assert_eq!(argv(codex, &wanted("codex", "default"), true, false, None)[3], "workspace-write");
        // A resumed Claude Code session runs in the classic renderer, as a new one does (owner ruling 2026-10-07).
        let classic = |argv: &[String]| crate::holder::run::provider_environment(vec![], argv).iter().any(|(k, _)| k == crate::holder::run::CLASSIC_RENDERER);
        assert!(classic(&argv(claude, &wanted("claude", "default"), true, false, None)) && !classic(&argv(codex, &wanted("codex", "default"), true, false, None)));
    }

    #[test]
    fn resume_reads_the_mode_the_session_last_ran_in() {
        let home = tempfile::tempdir().unwrap();
        let claude = home.path().join(".claude");
        let project = claude.join("projects/-work");
        fs::create_dir_all(&project).unwrap();
        assert_eq!(last_claude_mode(&claude, CONV), None, "no transcript");
        fs::write(project.join(format!("{CONV}.jsonl")), concat!(
            "{\"type\":\"permission-mode\",\"permissionMode\":\"default\"}\n",
            "{\"type\":\"user\",\"permissionMode\":\"plan\",\"message\":\"\\\"permission-mode\\\"\"}\n",
            "{\"type\":\"permission-mode\",\"permissionMode\":\"auto\"}\n",
            "not json \"permission-mode\"\n",
        )).unwrap();
        assert_eq!(last_claude_mode(&claude, CONV).as_deref(), Some("auto"));
    }

    #[test]
    fn resume_runs_in_the_folder_the_session_last_ran_in() {
        let home = tempfile::tempdir().unwrap();
        let work = home.path().join("work");
        fs::create_dir_all(&work).unwrap();
        let claude = home.path().join(".claude");
        let project = claude.join("projects/-work");
        fs::create_dir_all(&project).unwrap();
        fs::write(project.join(format!("{CONV}.jsonl")), format!("{{\"cwd\":\"{}\",\"version\":\"2.1.288\"}}\n", work.display())).unwrap();
        let canonical = fs::canonicalize(&work).unwrap();
        assert_eq!(folder(home.path(), &claude, "claude", CONV), Some(canonical.clone()));
        let day = home.path().join(".codex/sessions/2026/10/03");
        fs::create_dir_all(&day).unwrap();
        fs::write(
            day.join(format!("rollout-2026-10-03T10-00-00-{CONV}.jsonl")),
            format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"{CONV}\",\"cwd\":\"{}\"}}}}\n", work.display()),
        )
        .unwrap();
        assert_eq!(folder(home.path(), &claude, "codex", CONV), Some(canonical));
        fs::remove_dir_all(&work).unwrap();
        assert_eq!(folder(home.path(), &claude, "claude", CONV), None, "a folder that is gone refuses it");
    }

    #[test]
    fn resume_runs_only_in_a_folder_that_is_approved_now() {
        let approved = tempfile::tempdir().unwrap();
        let inside = approved.path().join("repo");
        std::fs::create_dir(&inside).unwrap();
        let elsewhere = tempfile::tempdir().unwrap();
        let list = vec![crate::file_access::canonicalize(approved.path().to_str().unwrap()).unwrap()];
        assert_eq!(approved_folder(&inside, &list), Ok(crate::file_access::canonicalize(inside.to_str().unwrap()).unwrap()));
        assert_eq!(approved_folder(elsewhere.path(), &list), Err("FOLDER_NOT_APPROVED"));
        assert_eq!(approved_folder(&inside, &[]), Err("FOLDER_NOT_APPROVED"), "approval removed after the end");
    }

    #[test]
    fn resume_refuses_a_conversation_a_private_process_runs() {
        // `claude agents --json` (or Codex discovery) lists a private `claude --resume` of it.
        let listed = Ok(vec!["99999999-0000-4000-8000-000000000000".into(), CONV.into()]);
        assert_eq!(free(listed, CONV), Err("ALREADY_RUNNING"));
        assert_eq!(free(Ok(vec![]), CONV), Ok(()), "nothing runs it: the resume starts");
        assert_eq!(free(Ok(vec!["other".into()]), CONV), Ok(()));
        assert_eq!(free(Err("native Claude listing unavailable".into()), CONV), Err("LAUNCH_FAILED"),
            "an unreadable listing proves nothing: refuse");
    }

    /// An ended holder stays up with `claude --resume <conv>` in its arguments to show the last
    /// picture: that is not the conversation running (prod 2026-10-22, "Not resumed: it is already
    /// running on your Mac" until the app was quit). A private `claude --resume` still is.
    #[test]
    fn resume_ps_scan_ignores_holder_processes_but_not_a_private_resume() {
        let ps = format!(
            "  7571 /Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop holder /tmp/h/52.sock -- /Users/a/.local/share/claude/versions/2.1.308 --resume {CONV} --permission-mode manual\n\
             812 /usr/bin/vim notes.txt\n"
        );
        let holders = HashSet::from([7571]);
        assert!(free(Ok(named_in(&ps, "claude", &holders)), CONV).is_ok(), "an ended holder runs nothing");
        let private = format!("{ps} 9001 /Users/a/.local/share/claude/versions/2.1.308 --resume {CONV}\n");
        assert_eq!(free(Ok(named_in(&private, "claude", &holders)), CONV), Err("ALREADY_RUNNING"));
        assert_eq!(free(Ok(named_in(&ps, "claude", &HashSet::new())), CONV), Err("ALREADY_RUNNING"), "the old scan's refusal");
    }

    #[test]
    fn resume_prepare_refuses_a_conversation_another_run_already_holds() {
        let mut w = wanted("claude", "plan");
        w.running = true;
        assert_eq!(prepare(&w, true, Path::new("/nonexistent")), Err("ALREADY_RUNNING"));
    }

    #[test]
    fn resume_prepare_refuses_an_id_a_live_claude_process_holds_even_parked() {
        // A terminal that parked the conversation in the agent view still names it, and `claude
        // agents` does not list it: `free` cannot see it. Claude's records can.
        let home = tempfile::tempdir().unwrap();
        claude::tests::live_record(&home.path().join(".claude"), CONV, Some("251719c2"));
        assert_eq!(prepare(&wanted("claude", "plan"), true, home.path()), Err("ALREADY_RUNNING"));
        claude::tests::live_record(&home.path().join(".claude"), "99999999-2222-4333-8444-555555555555", None);
        assert_ne!(prepare(&wanted("claude", "plan"), true, home.path()), Err("ALREADY_RUNNING"), "nothing holds it now");
    }

    fn hosted_mirrors(job: Option<&str>, shared: bool) -> BTreeMap<String, Mirror> {
        let mut m = super::super::transport::tests::test_mirror(CONV, "gen-2");
        m.native.provider = "claude".into();
        m.native.job = job.map(str::to_owned);
        m.session_id = Some("bbbbbbbb-2222-4222-8222-222222222222".into());
        m.registration.acknowledged = Some("gen-1".into());
        m.shared = shared;
        BTreeMap::from([("k".repeat(64), m)])
    }

    /// Prod 2026-10-26: the held terminal closed, Claude moved the conversation into background
    /// job 70f755ac, and Resume answered "already running on your Mac" with no way back.
    #[test]
    fn resume_sees_a_conversation_its_providers_daemon_hosts() {
        let job = hosted_mirrors(Some("70f755ac"), false);
        assert_eq!(
            live(&job, "claude", CONV),
            Some(Some(Hosted { key: "k".repeat(64), session_id: Some("bbbbbbbb-2222-4222-8222-222222222222".into()), generation: "gen-1".into(), shared: false, fresh: true, job: "70f755ac".into(), retired: false })),
            "the run the relay knows, at the generation its row is at"
        );
        // A Codex app-server thread is hosted the same way (its job is the thread id).
        let mut codex = hosted_mirrors(Some(CONV), true);
        codex.values_mut().for_each(|m| m.native.provider = "codex".into());
        assert!(live(&codex, "codex", CONV).flatten().is_some_and(|h| h.shared));
        // A private terminal (`claude --resume`, `codex --no-daemon`): running, nothing to attach.
        assert_eq!(live(&hosted_mirrors(None, true), "claude", CONV), Some(None));
        let mut ended = hosted_mirrors(None, true);
        ended.values_mut().for_each(|m| m.ended_at = Some(1));
        assert_eq!(live(&ended, "claude", CONV), None, "an ended terminal session runs nothing");
    }

    /// Prod 2026-10-26, "Product Cleanup" (job 251719c2): Claude's daemon retired the job's idle
    /// process (`bg retire 251719c2: idle-prompt, idle 61m`), so its mirror ended and the row
    /// offered Resume. `claude agents --json` still listed the blocked job, `free` found it, and
    /// Resume said "still running on your Mac … End it there". The job is attached instead: that
    /// starts its process again (a new pid, so a new generation) and shows it.
    #[test]
    fn resume_attaches_a_background_job_whose_process_the_daemon_retired() {
        let mut ended = hosted_mirrors(Some("251719c2"), true);
        ended.values_mut().for_each(|m| m.ended_at = Some(1));
        let hosted = live(&ended, "claude", CONV).flatten().expect("a retired job is still attachable");
        assert!(hosted.retired && hosted.job == "251719c2" && hosted.shared);
        let ack: Value = serde_json::from_str(ACK).unwrap();
        let w = &tag(&ack["resume"], |p, c| (c.to_owned(), live(&ended, p, c)))[0];
        assert!(w.hosted.is_some() && !w.running, "attached, never refused as running");
        // A live mirror of the conversation wins over its ended one.
        let mut both = ended.clone();
        both.extend(hosted_mirrors(Some("251719c2"), true).into_iter().map(|(_, m)| ("l".repeat(64), m)));
        assert!(live(&both, "claude", CONV).flatten().is_some_and(|h| !h.retired));
        // The listing names the job even with no pid: that is the job to attach, not a refusal.
        let rows: Vec<Value> = serde_json::from_value(json!([
            {"id": "ae0e0fa4", "kind": "background", "sessionId": "ae0e0fa4-30b4-47c2-ab61-9c71f2002199", "state": "working"},
            {"id": "251719c2", "kind": "background", "sessionId": CONV, "state": "working"},
            {"kind": "interactive", "sessionId": "33333333-3333-4333-8333-333333333333", "pid": 1}
        ])).unwrap();
        assert_eq!(claude::job_of(&rows, CONV).as_deref(), Some("251719c2"));
        assert_eq!(claude::job_of(&rows, "33333333-3333-4333-8333-333333333333"), None, "a terminal session is not a job");
    }

    /// 70f755ac continued in ae0e0fa4 (←): resuming 70f755ac with `claude --resume` would start
    /// a second live branch of it, so a resume acts on ae0e0fa4.
    #[test]
    fn resume_follows_a_continued_conversation_to_its_newest_continuation() {
        let config = tempfile::tempdir().unwrap();
        let project = config.path().join("projects/-work");
        fs::create_dir_all(&project).unwrap();
        let (a, b, c) = (CONV, "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333");
        let row = |from: &str, to: &str| format!("{{\"type\":\"continued-in\",\"sessionId\":\"{from}\",\"continuedInSessionId\":\"{to}\"}}\n");
        fs::write(project.join(format!("{a}.jsonl")), format!("{{\"type\":\"user\"}}\n{}", row(a, b))).unwrap();
        fs::write(project.join(format!("{b}.jsonl")), format!("{{\"type\":\"assistant\"}}\n{}{{\"type\":\"ai-title\"}}\n", row(b, c))).unwrap();
        assert_eq!(latest(config.path(), "claude", a), b, "a continuation whose transcript is missing is not followed");
        fs::write(project.join(format!("{c}.jsonl")), "{\"type\":\"user\"}\n").unwrap();
        assert_eq!(latest(config.path(), "claude", a), c, "the whole chain");
        assert_eq!(latest(config.path(), "codex", a), a, "a Codex thread keeps one id for life");
        // Typed in after the continuation: the origin is a real branch of its own.
        fs::write(project.join(format!("{a}.jsonl")), format!("{}{{\"type\":\"user\"}}\n", row(a, b))).unwrap();
        assert_eq!(latest(config.path(), "claude", a), a);
    }

    #[tokio::test]
    async fn resume_reattaches_a_daemon_hosted_conversation_instead_of_refusing_it() {
        let me = Resumer { api: "http://127.0.0.1:9".into(), key_pem: String::new(), device: "d".into(), owner: "o".into(), human: "user_bob".into() };
        let hosted = |shared: bool, sid: Option<&str>| Hosted { key: format!("{}{}{}", "e".repeat(62), u8::from(shared), u8::from(sid.is_some())), session_id: sid.map(str::to_owned), generation: "gen-1".into(), shared, fresh: true, job: "70f755ac".into(), retired: false };
        // Shared already: its registration answers the resume; nothing starts.
        assert_eq!(reattach(&me, &hosted(true, Some("s")), true).await, Ok(Some(false)));
        // Its run is the one the relay has: no registration answers the resume, so it is asked again.
        assert_eq!(reattach(&me, &Hosted { fresh: false, ..hosted(true, Some("s")) }, true).await, Ok(None));
        // Paused by its person: only that person's own Resume shares it again.
        assert_eq!(reattach(&me, &hosted(false, Some("s")), false).await, Err("SHARING_OFF"));
        let unbound = hosted(false, None);
        assert_eq!(reattach(&me, &unbound, true).await, Ok(None), "not bound yet: asked again, nothing shared");
        assert_eq!(super::super::transport::asked(&unbound.key), None);
        // A run the relay has, paused: the unpause must land first, or nothing is shared.
        assert_eq!(reattach(&me, &hosted(false, Some("s")), true).await, Err("LAUNCH_FAILED"));
        assert_eq!(super::super::transport::asked(&hosted(false, Some("s")).key), None);
        // A continued origin is never started again: its request settles as CONTINUED.
        let continued = Wanted { continued: true, ..wanted("claude", "plan") };
        assert_eq!(start(Some(Path::new("/nonexistent")), &continued, &me).await, Err("SHARING_OFF"), "sharing is checked first");
        let ack: Value = serde_json::from_str(ACK).unwrap();
        let next = "22222222-2222-4222-8222-222222222222";
        let tagged = tag(&ack["resume"], |_, _| (next.to_owned(), None));
        assert!(tagged[0].continued && tagged[0].conversation_id == next, "it acts on the continuation, and knows it");
        assert!(!tag(&ack["resume"], |_, c| (c.to_owned(), None))[0].continued);
        // With no daemon to attach to, a running conversation is still refused.
        let mut w = wanted("claude", "plan");
        w.running = true;
        assert_eq!(prepare(&w, true, Path::new("/nonexistent")), Err("ALREADY_RUNNING"));
    }

    #[test]
    fn resume_prepare_refuses_a_missing_cli_before_anything_starts() {
        let home = tempfile::tempdir().unwrap();
        let mut w = wanted("codex", "plan");
        w.provider = "codex".into();
        // An empty home has no Codex models cache, and CI's PATH has no `codex` with `--no-daemon`.
        if crate::session_start::tools(home.path()).iter().all(|t| t["id"] != "codex") {
            assert_eq!(prepare(&w, false, home.path()), Err("TOOL_UNAVAILABLE"));
        }
    }

    /// A resume starts `claude --resume` in the folder the conversation last ran in, with nobody at
    /// the terminal. A folder Claude has not trusted yet (a new git worktree: Claude's trust stops
    /// at a git root, so a trusted parent does not cover it) shows Claude's trust dialog there, and
    /// Claude writes no `~/.claude/sessions/<pid>.json` until it is answered: Lightbulb never sees
    /// the run, and the resume waits forever (prod 2026-11-05, "Opening this session on your Mac…").
    #[test]
    fn resume_trusts_its_approved_folder_for_claude_before_it_starts() {
        let home = tempfile::tempdir().unwrap();
        let config = home.path().join(".claude.json");
        let folder = Path::new("/Users/x/repo/worktree");
        std::fs::write(&config, r#"{"oauthAccount":{"a":1},"projects":{"/Users/x":{"hasTrustDialogAccepted":true,"allowedTools":["Bash"]}}}"#).unwrap();
        trust_claude_folder(&config, folder).unwrap();
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&config).unwrap()).unwrap();
        assert_eq!(doc["projects"]["/Users/x/repo/worktree"]["hasTrustDialogAccepted"], true);
        assert_eq!(doc["projects"]["/Users/x"]["allowedTools"], json!(["Bash"]), "other projects are kept");
        assert_eq!(doc["oauthAccount"], json!({"a": 1}), "every other key is kept");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&config).unwrap().permissions().mode() & 0o777, 0o600);
        // Already trusted: the file is not rewritten.
        let before = std::fs::read(&config).unwrap();
        std::fs::write(&config, &before).unwrap();
        let mtime = std::fs::metadata(&config).unwrap().modified().unwrap();
        std::thread::sleep(Duration::from_millis(20));
        trust_claude_folder(&config, folder).unwrap();
        assert_eq!(std::fs::metadata(&config).unwrap().modified().unwrap(), mtime);
        // A project entry Claude already has keeps its other fields.
        std::fs::write(&config, r#"{"projects":{"/Users/x/repo/worktree":{"hasTrustDialogAccepted":false,"allowedTools":["Edit"]}}}"#).unwrap();
        trust_claude_folder(&config, folder).unwrap();
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&config).unwrap()).unwrap();
        assert_eq!(doc["projects"]["/Users/x/repo/worktree"], json!({"hasTrustDialogAccepted": true, "allowedTools": ["Edit"]}));
        // No config (Claude never ran here) or one that is not JSON: nothing is written.
        let none = home.path().join("missing.json");
        assert!(trust_claude_folder(&none, folder).is_ok());
        assert!(!none.exists());
        std::fs::write(&config, "{not json").unwrap();
        assert!(trust_claude_folder(&config, folder).is_err());
        assert_eq!(std::fs::read_to_string(&config).unwrap(), "{not json");
    }
}
