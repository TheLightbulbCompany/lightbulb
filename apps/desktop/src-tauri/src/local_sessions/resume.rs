//! Resume, the owner's Mac side: start an ended session again because its owner, or a teammate
//! who could send to it when it ended, asked from web, desktop or iPhone (spec §5.7).
//!
//! The relay loop hands this task the `resume` list from a heartbeat acknowledgement. The backend
//! decided who may resume and in which mode; this side checks what only this Mac knows: sharing is
//! on, the CLI is installed, the session's folder still exists, and nothing already runs that
//! conversation (two `claude --resume` processes would fork it). Anyone but this Mac's own human
//! gets plan mode whatever the request says. It starts the provider in a holder the way New
//! session does. A refusal is reported to the backend once; it never touches a mirror.
use super::*;
use std::collections::BTreeMap;
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
}
