//! Resume, the owner's Mac side: start an ended session again because its owner, or a teammate
//! who could send to it when it ended, asked from web, desktop or iPhone (spec §5.7).
//!
//! The relay loop hands this task the `resume` list from a heartbeat acknowledgement. The backend
//! decided who may resume and in which mode; this side checks what only this Mac knows: sharing is
//! on, the CLI is installed, the session's folder still exists, and nothing already runs that
//! conversation (two `claude --resume` processes would fork it). Anyone but this Mac's own human
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
    let wanted = tag(resume, |provider, conversation| {
        mirrors
            .values()
            .any(|m| m.ended_at.is_none() && m.native.provider == provider && m.native.conversation == conversation)
    });
    if wanted.is_empty() {
        return;
    }
    if let Some(tx) = WANTED.lock().ok().and_then(|s| s.clone()) {
        let _ = tx.try_send(wanted);
    }
}

fn tag(resume: &Value, running: impl Fn(&str, &str) -> bool) -> Vec<Wanted> {
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
            let conversation_id = uuid(&e["conversationId"])?;
            let running = running(&provider, &conversation_id);
            Some(Wanted {
                resume_id: uuid(&e["resumeId"])?,
                session_id: uuid(&e["sessionId"])?,
                by_user_id: e["byUserId"].as_str().filter(|s| !s.is_empty())?.chars().take(128).collect(),
                by_name: text(&e["byName"], 80, "A teammate"),
                title: text(&e["title"], 120, "a session"),
                mode: if e["mode"] == "default" { "default".into() } else { "plan".into() },
                provider,
                conversation_id,
                running,
            })
        })
        .collect()
}

/// The exact argv: the fixed installed binary, the conversation, and a mode no looser than the
/// owner's default. Anyone but this Mac's own human gets plan mode, whatever the request says.
fn argv(program: &Path, w: &Wanted, own: bool) -> Vec<String> {
    let plan = !own || w.mode != "default";
    let program = program.to_string_lossy().into_owned();
    if w.provider == "codex" {
        // Root options before the subcommand; `--no-daemon` as New session starts Codex.
        vec![
            program,
            "--no-daemon".into(),
            "--sandbox".into(),
            (if plan { "read-only" } else { "workspace-write" }).into(),
            "--ask-for-approval".into(),
            "on-request".into(),
            "resume".into(),
            w.conversation_id.clone(),
        ]
    } else {
        vec![
            program,
            "--resume".into(),
            w.conversation_id.clone(),
            "--permission-mode".into(),
            (if plan { "plan" } else { "manual" }).into(),
        ]
    }
}

/// The folder the session last ran in, from its own transcript (Claude) or rollout (Codex), if it
/// still exists. The same folder, never one the request names.
fn folder(home: &Path, claude_config: &Path, w: &Wanted) -> Option<PathBuf> {
    let cwd = if w.provider == "codex" {
        codex_cwd(home, &w.conversation_id)
    } else {
        super::snapshot::find_transcript(claude_config, &w.conversation_id)
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

static APP: OnceLock<tauri::AppHandle> = OnceLock::new();

/// From the app's setup: a resume is shown in the app, so starting one needs it.
pub(crate) fn install(app: &tauri::AppHandle) {
    let _ = APP.set(app.clone());
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
            let Some(app) = APP.get().cloned() else {
                handled.remove(&wanted.resume_id);
                continue;
            };
            let short = &wanted.session_id[..8];
            match start(&app, &wanted, &me).await {
                Ok(true) => relay_log(&format!("{short} resumed")),
                Ok(false) => relay_log(&format!("{short} already runs in a holder; its registration answers the resume")),
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

async fn start(app: &tauri::AppHandle, w: &Wanted, me: &Resumer) -> std::result::Result<bool, &'static str> {
    if !super::enrollment::sharing_on(app) {
        return Err("SHARING_OFF");
    }
    let own = w.by_user_id == me.human;
    let want = w.clone();
    let Some((folder, argv)) = tauri::async_runtime::spawn_blocking(move || prepare(&want, own, &home_dir()))
        .await
        .map_err(|_| "LAUNCH_FAILED")??
    else {
        return Ok(false);
    };
    // `running` only knows held sessions: a private `claude --resume` / `codex resume` of the same
    // conversation is invisible to the mirrors. Two writers on one transcript fork it, so a resume
    // that cannot prove the conversation is free does not start.
    free(running_conversations(home_dir(), w.provider.clone()).await, &w.conversation_id)?;
    crate::session_start::start_held(folder, argv).await.map_err(|e| {
        relay_log(&format!("{} resume launch failed: {e}", &w.session_id[..w.session_id.len().min(8)]));
        "LAUNCH_FAILED"
    })?;
    let who = if own { "You".to_owned() } else { w.by_name.clone() };
    let how = if own && w.mode == "default" {
        "End it from its panel in Lightbulb."
    } else {
        "In plan mode. End it from its panel in Lightbulb."
    };
    let _ = crate::notify::native_notify(
        format!("{who} resumed {}", w.title),
        Some(how.into()),
        Some(format!("resume-{}", w.resume_id)),
        None,
        None,
    )
    .await;
    Ok(true)
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
    let args = native_output(Path::new("/bin/ps"), &["-axo", "args="]).await?;
    ids.extend(
        args.lines()
            .filter(|line| line.contains(provider.as_str()))
            .flat_map(|line| line.split_whitespace().map(str::to_owned).collect::<Vec<_>>()),
    );
    Ok(ids)
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
    if crate::holder::manager::list().iter().any(|h| h.argv.iter().any(|a| a == &w.conversation_id)) {
        return Ok(None);
    }
    if w.running {
        return Err("ALREADY_RUNNING");
    }
    if !crate::session_start::tools(home).iter().any(|t| t["id"] == w.provider.as_str()) {
        return Err("TOOL_UNAVAILABLE");
    }
    let program = provider_program(home, &w.provider).ok_or("TOOL_UNAVAILABLE")?;
    let claude = std::env::var_os("CLAUDE_CONFIG_DIR").map(PathBuf::from).unwrap_or_else(|| home.join(".claude"));
    let folder = folder(home, &claude, w).ok_or("FOLDER_MISSING")?;
    // The folder it last ran in must still be approved now, as for New session and the CLI: the
    // owner may have removed it from File access after the session ended.
    let folder = approved_folder(&folder, &crate::session_start::approved_now())?;
    Ok(Some((folder, argv(&program, w, own))))
}

fn approved_folder(folder: &Path, approved: &[PathBuf]) -> std::result::Result<PathBuf, &'static str> {
    folder
        .to_str()
        .and_then(|f| crate::file_access::canonicalize(f).ok())
        .filter(|f| crate::session_start::inside_approved(f, approved))
        .ok_or("FOLDER_NOT_APPROVED")
}

async fn report(me: &Resumer, w: &Wanted, code: &str) -> Result<()> {
    let token = crate::device_token::mint_device_token(
        &me.key_pem,
        &me.device,
        &me.owner,
        crate::device_token::DEVICE_TOKEN_LIFETIME_SECS,
    )?;
    let url = format!("{}/api/v1/desktop/session-resume/{}/failed", me.api.trim_end_matches('/'), w.session_id);
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let status = client
        .post(url)
        .bearer_auth(token)
        .json(&json!({"resumeId": w.resume_id, "code": code}))
        .send()
        .await
        .map_err(|_| "resume report failed")?
        .status()
        .as_u16();
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!("resume report returned {status}"))
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
        }
    }

    #[test]
    fn resume_offer_reads_the_relay_entry_and_sees_a_running_conversation() {
        let ack: Value = serde_json::from_str(ACK).unwrap();
        assert_eq!(tag(&ack["resume"], |_, _| false), vec![wanted("claude", "plan")]);
        let held = tag(&ack["resume"], |provider, conversation| provider == "claude" && conversation == CONV);
        assert!(held[0].running);
        let mut odd = ack["resume"].clone();
        odd[0]["conversationId"] = json!("--dangerously-skip-permissions");
        assert!(tag(&odd, |_, _| false).is_empty(), "a conversation id that is not a UUID is refused");
        let mut shell = ack["resume"].clone();
        shell[0]["provider"] = json!("bash");
        assert!(tag(&shell, |_, _| false).is_empty());
        let mut loose = ack["resume"].clone();
        loose[0]["mode"] = json!("bypassPermissions");
        assert_eq!(tag(&loose, |_, _| false)[0].mode, "plan", "anything but default reads as plan");
    }

    #[test]
    fn resume_argv_gives_anyone_but_this_macs_human_plan_mode() {
        let claude = Path::new("/bin/claude");
        assert_eq!(argv(claude, &wanted("claude", "default"), false), ["/bin/claude", "--resume", CONV, "--permission-mode", "plan"]);
        assert_eq!(argv(claude, &wanted("claude", "default"), true), ["/bin/claude", "--resume", CONV, "--permission-mode", "manual"]);
        assert_eq!(argv(claude, &wanted("claude", "plan"), true)[4], "plan");
        let codex = Path::new("/bin/codex");
        assert_eq!(
            argv(codex, &wanted("codex", "default"), false),
            ["/bin/codex", "--no-daemon", "--sandbox", "read-only", "--ask-for-approval", "on-request", "resume", CONV]
        );
        assert_eq!(argv(codex, &wanted("codex", "default"), true)[3], "workspace-write");
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
        assert_eq!(folder(home.path(), &claude, &wanted("claude", "plan")), Some(canonical.clone()));
        let day = home.path().join(".codex/sessions/2026/10/03");
        fs::create_dir_all(&day).unwrap();
        fs::write(
            day.join(format!("rollout-2026-10-03T10-00-00-{CONV}.jsonl")),
            format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"{CONV}\",\"cwd\":\"{}\"}}}}\n", work.display()),
        )
        .unwrap();
        assert_eq!(folder(home.path(), &claude, &wanted("codex", "plan")), Some(canonical));
        fs::remove_dir_all(&work).unwrap();
        assert_eq!(folder(home.path(), &claude, &wanted("claude", "plan")), None, "a folder that is gone refuses it");
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

    #[test]
    fn resume_prepare_refuses_a_conversation_another_run_already_holds() {
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
}
