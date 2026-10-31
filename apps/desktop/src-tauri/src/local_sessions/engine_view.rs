//! The engine's `sessions` answer as the desktop reads it, and the listing row `claude::capture`
//! takes from it. Pure but for `read`.

use serde_json::{json, Value};

pub(in crate::local_sessions) struct EngineRow {
    pub key: String,
    pub provider: String,
    pub conversation: String,
    pub pid: u32,
    pub birth_micros: u64,
    pub cwd: String,
    /// The desktop's word (`desktop_status`).
    pub status: String,
    pub waiting_for: Option<String>,
    pub kind: Option<String>,
    pub title: Option<String>,
    /// The background job: (id, state).
    pub job: Option<(String, String)>,
}

pub(in crate::local_sessions) struct EngineEnded {
    pub key: String,
    pub provider: String,
    pub conversation: String,
    pub reason: Option<String>,
}

pub(in crate::local_sessions) struct EngineView {
    pub live: Vec<EngineRow>,
    pub ended: Vec<EngineEnded>,
}

impl EngineView {
    /// A session's key on the network; its live row first.
    pub(in crate::local_sessions) fn key(&self, provider: &str, conversation: &str) -> Option<&str> {
        let live = self.live.iter().find(|r| r.provider == provider && r.conversation == conversation).map(|r| &r.key);
        live.or_else(|| self.ended.iter().find(|r| r.provider == provider && r.conversation == conversation).map(|r| &r.key)).map(String::as_str)
    }
}

pub(in crate::local_sessions) fn desktop_status(engine: &str) -> &'static str {
    match engine {
        "live" => "busy",
        "waiting" => "waiting-approval",
        "idle" => "idle",
        _ => "unknown",
    }
}

/// The backend's `EndedReason` has `process-exited` and `exited`.
pub(in crate::local_sessions) fn ended_reason(engine: Option<&str>) -> &'static str {
    match engine {
        None | Some("process-exited") => "process-exited",
        Some(_) => "exited",
    }
}

pub(in crate::local_sessions) fn parse_sessions(answer: &Value) -> Result<EngineView, String> {
    if answer["ok"] == false {
        return Err(answer["error"].as_str().unwrap_or("engine refused").to_owned());
    }
    // ponytail: `sessions` has no `ended` before 0.8.0; ask `directory` natively if that must work.
    let (Some(live), Some(ended)) = (answer["sessions"].as_array(), answer["ended"].as_array()) else {
        return Err("the engine is older than 0.8.0".to_owned());
    };
    let text = |v: &Value, k: &str| v[k].as_str().map(str::to_owned);
    let live = live
        .iter()
        .filter_map(|r| {
            Some(EngineRow {
                key: text(r, "key")?,
                provider: text(r, "provider")?,
                conversation: text(r, "conversation")?,
                pid: u32::try_from(r["pid"].as_u64()?).ok()?,
                birth_micros: r["birth_micros"].as_u64().unwrap_or(0),
                cwd: text(r, "cwd").unwrap_or_default(),
                status: desktop_status(r["status"].as_str().unwrap_or("")).to_owned(),
                waiting_for: text(r, "waiting_for"),
                kind: text(r, "kind"),
                title: text(r, "title").filter(|t| !t.trim().is_empty()),
                job: r["job"].is_object().then(|| Some((text(&r["job"], "id")?, text(&r["job"], "state")?))).flatten(),
            })
        })
        .collect();
    let ended = ended
        .iter()
        .filter_map(|r| {
            Some(EngineEnded { key: text(r, "key")?, provider: text(r, "provider")?, conversation: text(r, "conversation")?, reason: text(r, "reason") })
        })
        .collect();
    Ok(EngineView { live, ended })
}

/// The row `claude::capture` and `listing_status` read, in Claude's own words.
pub(in crate::local_sessions) fn listing_row(row: &EngineRow) -> Value {
    // The engine makes every blocked job `waiting` ("input needed"), but a job at its shell with a
    // hand-off note waits on no prompt (#4414). Only a real prompt says `permission prompt`.
    let at_shell = row.job.as_ref().is_some_and(|j| j.1 == "blocked") && row.waiting_for.as_deref() != Some("permission prompt");
    let status = match row.status.as_str() {
        _ if at_shell => "idle",
        "busy" => "busy",
        "waiting-approval" => "waiting",
        "idle" => "idle",
        _ => "unknown",
    };
    json!({
        "kind": row.kind.as_deref().unwrap_or("interactive"),
        "id": row.job.as_ref().map(|j| &j.0),
        "pid": row.pid,
        "sessionId": row.conversation,
        "cwd": row.cwd,
        "state": row.job.as_ref().map(|j| &j.1),
        "status": status,
        "waitingFor": row.waiting_for.as_deref().filter(|_| !at_shell),
    })
}

/// The conversation of this Mac's own live Claude row whose engine key is `session_id`; nothing
/// else (a teammate's id, an ended row, another provider, garbage) names one to share.
pub(in crate::local_sessions) fn own_claude_row<'a>(view: &'a EngineView, session_id: &str) -> Option<&'a EngineRow> {
    view.live.iter().find(|r| r.provider == "claude" && r.key == session_id)
}

/// The words of the native confirm for sharing `row`: what the engine says it is, never what the
/// page says. Title: first line, 80 characters; folder 40; both without control or format characters.
pub(in crate::local_sessions) fn share_prompt(row: &EngineRow) -> String {
    // Control and format characters (a bare \r, U+2028, bidi overrides) could redraw the text.
    let plain = |s: &str, cap: usize| Some(s.chars().filter(|c| !c.is_control() && !"\u{61c}\u{200b}\u{200c}\u{200d}\u{200e}\u{200f}\u{2028}\u{2029}\u{202a}\u{202b}\u{202c}\u{202d}\u{202e}\u{2060}\u{2066}\u{2067}\u{2068}\u{2069}\u{feff}".contains(*c)).take(cap).collect::<String>()).map(|s| s.trim().to_owned()).filter(|s| !s.is_empty());
    let title = row.title.as_deref().and_then(|t| t.lines().next()).and_then(|t| plain(t, 80));
    let place = std::path::Path::new(&row.cwd).file_name().and_then(|n| plain(&n.to_string_lossy(), 40));
    let what = match (title, place) {
        (Some(t), Some(p)) => format!("\"{t}\" in {p}. "),
        (Some(t), None) => format!("\"{t}\". "),
        (None, Some(p)) => format!("In {p}. "),
        (None, None) => String::new(),
    };
    format!("{what}Teammates will be able to watch it. You can stop sharing at any time.")
}

pub(in crate::local_sessions) async fn read() -> Result<EngineView, String> {
    parse_sessions(&crate::engine_client::ask("sessions", None).await?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::local_sessions::claude::listing_status;

    fn real() -> Value {
        json!({"key":"abc/claude:U","provider":"claude","conversation":"U","pid":7,"birth_micros":9,"cwd":"/x","status":"live","tier":"push","kind":"background","entrypoint":"cli","job":{"id":"deadbeef","state":"working"}})
    }

    #[test]
    fn words() {
        assert_eq!(["live", "waiting", "idle", "x"].map(desktop_status), ["busy", "waiting-approval", "idle", "unknown"]);
        for (engine, want) in [(None, "process-exited"), (Some("process-exited"), "process-exited"), (Some("moved-on"), "exited"), (Some("session-end"), "exited"), (Some("job-done"), "exited")] {
            assert_eq!(ended_reason(engine), want);
        }
    }

    #[test]
    fn parses_and_skips() {
        assert_eq!(parse_sessions(&json!({"ok":false,"error":"nope"})).err().as_deref(), Some("nope"));
        assert!(parse_sessions(&json!({"ok":true,"sessions":[]})).err().unwrap().contains("older than 0.8.0"));
        let mut nopid = real();
        nopid.as_object_mut().unwrap().remove("pid");
        let ended = json!([{"key":"k","provider":"claude","conversation":null},{"key":"k2","provider":"claude","conversation":"C","reason":"moved-on"}]);
        let mut no_job = real();
        no_job.as_object_mut().unwrap().remove("job");
        let mut half_job = real();
        half_job["job"] = json!({"id":"deadbeef"});
        let view = parse_sessions(&json!({"ok":true,"sessions":[real(), nopid, no_job, half_job],"ended":ended})).unwrap();
        assert_eq!(view.live.len(), 3);
        assert!(view.live[1].job.is_none() && view.live[2].job.is_none(), "a row needs no job");
        let row = &view.live[0];
        assert_eq!((row.key.as_str(), row.pid, row.status.as_str(), row.job.clone()), ("abc/claude:U", 7, "busy", Some(("deadbeef".into(), "working".into()))));
        assert_eq!(view.ended.len(), 1);
        assert_eq!(view.ended[0].reason.as_deref(), Some("moved-on"));
    }

    #[test]
    fn listing_row_reads_back_as_the_desktop_word() {
        let row = |status: &str, kind: Option<&str>, job: Option<(&str, &str)>| EngineRow {
            key: "k".into(), provider: "claude".into(), conversation: "U".into(), pid: 7, birth_micros: 0, cwd: "/x".into(),
            status: desktop_status(status).into(), waiting_for: Some("permission prompt".into()), kind: kind.map(Into::into), title: None,
            job: job.map(|(a, b)| (a.into(), b.into())),
        };
        let r = listing_row(&row("live", None, None));
        assert_eq!((r["kind"].as_str(), r["pid"].as_u64(), r["sessionId"].as_str(), r["state"].is_null(), r["id"].is_null()), (Some("interactive"), Some(7), Some("U"), true, true));
        for (engine, kind, job, want) in [
            ("live", None, None, "busy"),
            ("idle", None, None, "idle"),
            ("waiting", None, None, "waiting-approval"),
            ("x", None, None, "unknown"),
            ("live", Some("background"), Some(("deadbeef", "working")), "busy"),
            ("live", Some("background"), Some(("deadbeef", "done")), "idle"),
            ("live", Some("background"), Some(("deadbeef", "failed")), "idle"),
            ("live", Some("background"), Some(("deadbeef", "stopped")), "idle"),
            ("waiting", Some("background"), Some(("deadbeef", "blocked")), "waiting-approval"),
            ("idle", Some("background"), Some(("deadbeef", "blocked")), "idle"),
        ] {
            assert_eq!(listing_status(&listing_row(&row(engine, kind, job))), want, "{engine} {job:?}");
        }
        // The engine makes every blocked job `waiting`: only a real prompt says `permission prompt` (#4414).
        let word = |word: Option<&str>| EngineRow { waiting_for: word.map(Into::into), ..row("waiting", Some("background"), Some(("deadbeef", "blocked"))) };
        for said in [Some("input needed"), None] {
            let r = listing_row(&word(said));
            assert_eq!((listing_status(&r), r["status"].as_str(), r["waitingFor"].is_null()), ("idle", Some("idle"), true), "{said:?}");
        }
        let r = listing_row(&word(Some("permission prompt")));
        assert_eq!((listing_status(&r), r["waitingFor"].as_str()), ("waiting-approval", Some("permission prompt")));
        let job = listing_row(&row("live", Some("background"), Some(("deadbeef", "working"))));
        assert_eq!((job["id"].as_str(), job["state"].as_str(), job["waitingFor"].as_str()), (Some("deadbeef"), Some("working"), Some("permission prompt")));
    }

    #[test]
    fn a_background_row_feeds_job_id_listed_and_attention() {
        let mut v = real();
        v["job"]["state"] = "done".into();
        let view = parse_sessions(&json!({"ok":true,"sessions":[v],"ended":[]})).unwrap();
        let rows = [listing_row(&view.live[0])];
        let (conversations, jobs) = crate::local_sessions::claude::listed(&rows);
        assert_eq!((conversations.into_iter().collect::<Vec<_>>(), jobs.into_iter().collect::<Vec<_>>()), (vec!["U".to_owned()], vec!["deadbeef".to_owned()]));
        let home = tempfile::tempdir().unwrap();
        assert!(crate::local_sessions::claude::attention(home.path(), &rows[0], Some("deadbeef")).done);
    }

    #[test]
    fn the_confirm_names_the_session_from_the_engine_row() {
        let row = |title: Option<&str>, cwd: &str| EngineRow { title: title.map(Into::into), cwd: cwd.into(), ..parse_sessions(&json!({"ok":true,"sessions":[real()],"ended":[]})).unwrap().live.remove(0) };
        let tail = "Teammates will be able to watch it. You can stop sharing at any time.";
        assert_eq!(share_prompt(&row(Some("Fix login"), "/Users/a/repo")), format!("\"Fix login\" in repo. {tail}"));
        assert_eq!(share_prompt(&row(None, "/Users/a/repo")), format!("In repo. {tail}"));
        assert_eq!(share_prompt(&row(Some("T"), "")), format!("\"T\". {tail}"));
        assert_eq!(share_prompt(&row(None, "")), tail);
        let long = format!("first line\n{}", "x".repeat(200));
        assert_eq!(share_prompt(&row(Some(&long), "/r")), format!("\"first line\" in r. {tail}"));
        assert!(share_prompt(&row(Some(&"y".repeat(200)), "/r")).starts_with(&format!("\"{}\" in r.", "y".repeat(80))));
    }

    #[test]
    fn the_confirm_drops_what_a_title_or_folder_could_use_to_disguise_itself() {
        let row = |title: &str, cwd: &str| EngineRow { title: Some(title.into()), cwd: cwd.into(), ..parse_sessions(&json!({"ok":true,"sessions":[real()],"ended":[]})).unwrap().live.remove(0) };
        let tail = "Teammates will be able to watch it. You can stop sharing at any time.";
        assert_eq!(share_prompt(&row("ev\u{202e}il\r\u{2028}x\u{200b}", "/r/\u{202e}fo\u{200b}o\x07")), format!("\"evilx\" in foo. {tail}"));
        assert_eq!(share_prompt(&row("\u{202e}\r ", "")), tail, "empty after cleaning");
        let long = format!("/r/{}", "z".repeat(200));
        assert!(share_prompt(&row("t", &long)).contains(&format!(" in {}. ", "z".repeat(40))));
    }

    #[test]
    fn only_a_live_claude_row_of_this_mac_names_a_conversation() {
        let mut codex = real();
        codex["key"] = "abc/codex:C".into();
        codex["provider"] = "codex".into();
        let ended = json!([{"key":"gone/claude:E","provider":"claude","conversation":"E"}]);
        let view = parse_sessions(&json!({"ok":true,"sessions":[real(), codex],"ended":ended})).unwrap();
        assert_eq!(own_claude_row(&view, "abc/claude:U").map(|r| r.conversation.as_str()), Some("U"));
        for id in ["abc/codex:C", "gone/claude:E", "U", "teammate/claude:T", "", "../../etc"] {
            assert!(own_claude_row(&view, id).is_none(), "{id}");
        }
    }
}
