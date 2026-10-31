//! Signs the engine in and joins the workspace's network, natively.
//!
//! Security rule: the window loads a hosted page, and script on it must never be able to join or
//! leave a network. So no command takes a ticket or a team; `network_connect` and `network_leave`
//! change nothing until a person answers a native alert. The page has `dialog:default`, so it can
//! draw a look-alike of its own, but it cannot click the real alert or read its answer, and a
//! look-alike's yes reaches nothing here. `Status` holds no ticket, team id or token; every error
//! and log line here is a fixed string, never `reqwest`'s text (it can hold a URL) or a server or
//! engine body.
//!
//! Why the device key rather than Clerk: the page holds the Clerk session, so a route Clerk could
//! open is a route page script could open. `/network/join-line` refuses Clerk and takes a token
//! this process signs with the Keychain's CLI key, which the page never sees. No redirects are
//! followed, so a redirect cannot carry that token elsewhere.
//!
//! Which network comes from the Mac's pair, not the page. A page with another person's Clerk token
//! could re-pair the Mac, so a pair whose owner differs from the recorded one gets a native alert
//! before any sign-in or join, and every join posts a native notification naming the workspace.

use crate::{device_token, engine_client, keychain, pair_state};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::future::Future;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;
use tokio::io::{AsyncBufReadExt, BufReader};

const EVERY: Duration = Duration::from_secs(600);
const AFTER_FAILURE: Duration = Duration::from_secs(60);
const LOGIN_LIMIT: Duration = Duration::from_secs(60);
const READ_LIMIT: Duration = Duration::from_secs(5);
const LEAVE_LIMIT: Duration = Duration::from_secs(30);
/// As the CLI's: dialing, and ringing a parked member's doorbell.
const JOIN_LIMIT: Duration = Duration::from_secs(660);
const HTTP_LIMIT: Duration = Duration::from_secs(10);
/// API Gateway cuts a request at 30 s, so a cold member answers 503 while it wakes.
const LINE_LIMIT: Duration = Duration::from_secs(40);
const LINE_WINDOW: Duration = Duration::from_secs(180);
const LINE_RETRY: u64 = 15;

const NOT_AVAILABLE: &str = "Not available on this Mac.";
const SERVICE_OFF: &str = "The background service is off.";
const COULD_NOT_LEAVE: &str = "Could not leave the network.";

/// The alerts, for the once-per-launch limit.
const CONNECT: &str = "connect";
const LEAVE: &str = "leave";
const ASK: &str = "ask";
const OWNER_CHANGE: &str = "owner change";

/// `leave_team`'s refusal when an agent process is above this app (the engine's `not_a_person`).
const AGENT_SHELL: &str = "the app was opened from an agent's shell";

// --- The decision ---

#[derive(Debug, PartialEq, Eq)]
enum Action {
    Done,
    Join,
    Ask,
    OptOut,
    Stay,
}

fn decide(
    engine_team: &str,
    alone: bool,
    removed: bool,
    ws_team: Option<&str>,
    opted_out: bool,
    joined_before: Option<&str>,
) -> Action {
    if ws_team == Some(engine_team) {
        Action::Done
    } else if opted_out {
        Action::Stay
    } else if removed || (alone && joined_before.is_some_and(|team| team != engine_team)) {
        // On a network of one that is not the one it joined: the person left, or was removed.
        Action::OptOut
    } else if alone {
        Action::Join
    } else {
        Action::Ask
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Proof {
    Good,
    Missing,
    Expired,
    AnotherAccount,
}

/// What `login.json` holds. The signature is the server's to check; this only says whether
/// signing in again is worth trying and allowed.
fn read_proof(login: Option<&[u8]>, user_id: &str, now: u64) -> Proof {
    let claims = || -> Option<Value> {
        let login: Value = serde_json::from_slice(login?).ok()?;
        let middle = login.get("proof")?.as_str()?.split('.').nth(1)?;
        serde_json::from_slice(&base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(middle).ok()?).ok()
    };
    let Some(claims) = claims() else {
        return Proof::Missing;
    };
    match (claims["sub"].as_str(), claims["exp"].as_f64()) {
        (Some(sub), _) if sub != user_id => Proof::AnotherAccount,
        (Some(_), Some(exp)) if exp > now as f64 => Proof::Good,
        (Some(_), Some(_)) => Proof::Expired,
        _ => Proof::Missing,
    }
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |since| since.as_secs())
}

// --- `<app data>/network_connect.json` ---

#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct Saved {
    /// The pair's owner this computer has consented to.
    owner: Option<String>,
    opt_out: HashMap<String, bool>,
    joined: HashMap<String, String>,
    notice: Option<String>,
    /// This app signed the engine in: a missing proof then means the person logged out.
    signed_in: HashMap<String, bool>,
    /// Connect's yes after a removal: `removed` opts out no more, until the next join.
    removal_overridden: HashMap<String, bool>,
    /// A `team.join` for this owner may still land; the Done run that sees it tells the person.
    attempted: Option<String>,
}

/// Serializes read-modify-write: a dismissed notice must not lose an opt-out written beside it.
static FILE: Mutex<()> = Mutex::new(());

/// Only a file that is not there is empty. `None`: there but unreadable, so every owner reads
/// opted out and only the native Connect rewrites it. An opt-out must not vanish with a bad write.
fn load(dir: &Path) -> Option<Saved> {
    match std::fs::read(dir.join("network_connect.json")) {
        Ok(bytes) => serde_json::from_slice(&bytes).ok(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Some(Saved::default()),
        Err(_) => None,
    }
}

fn save(dir: &Path, saved: &Saved) -> std::io::Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join("network_connect.json.tmp");
    // A leftover would keep its own (maybe wider) mode: `mode` applies only on create.
    let _ = std::fs::remove_file(&tmp);
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
    file.write_all(&serde_json::to_vec(saved)?)?;
    file.sync_all()?;
    std::fs::rename(&tmp, dir.join("network_connect.json"))
}

fn write_saved(dir: &Path, saved: &Saved) {
    if save(dir, saved).is_err() {
        crate::log("[network] could not save network_connect.json");
    }
}

fn update(dir: &Path, change: impl FnOnce(&mut Saved)) {
    let _one_at_a_time = FILE.lock().unwrap_or_else(|e| e.into_inner());
    let Some(mut saved) = load(dir) else {
        crate::log("[network] network_connect.json is unreadable; left as it is");
        return;
    };
    change(&mut saved);
    write_saved(dir, &saved);
}

/// The native Connect's way past an unreadable file.
fn rewrite(dir: &Path, change: impl FnOnce(&mut Saved)) {
    let _one_at_a_time = FILE.lock().unwrap_or_else(|e| e.into_inner());
    let mut saved = load(dir).unwrap_or_default();
    change(&mut saved);
    write_saved(dir, &saved);
}

/// A person's yes to Connect, for the current pair. It also forgets the network last joined:
/// otherwise, alone on a new network of one, `decide` would read the old id as "the person left".
fn consent_to_connect(saved: &mut Saved, owner: &str) {
    saved.owner = Some(owner.to_owned());
    saved.opt_out.remove(owner);
    saved.joined.remove(owner);
    saved.signed_in.remove(owner);
    saved.removal_overridden.insert(owner.to_owned(), true);
}

fn record_owner(dir: &Path, owner: &str) {
    update(dir, |saved| {
        saved.owner.get_or_insert_with(|| owner.to_owned());
    });
}

fn opted_out_now(dir: &Path, owner: &str) -> bool {
    load(dir).is_none_or(|saved| saved.opt_out.get(owner) == Some(&true))
}

// --- What this launch remembers, never written anywhere ---

struct Memory {
    /// (owner, alert) that did not end in yes.
    spent: Vec<(String, &'static str)>,
    /// (owner, team) a person said Join to on the Ask.
    ask_yes: Option<(String, String)>,
    /// A once link not yet used, with its owner. A Link entry is permanent and replay is O(n²),
    /// so a retry spends this one rather than growing the roster.
    held: Option<(String, Line)>,
}

static MEMORY: Mutex<Memory> = Mutex::new(Memory::new());

impl Memory {
    const fn new() -> Self {
        Memory { spent: Vec::new(), ask_yes: None, held: None }
    }
}

fn lock(memory: &Mutex<Memory>) -> MutexGuard<'_, Memory> {
    memory.lock().unwrap_or_else(|e| e.into_inner())
}

fn spent(memory: &Mutex<Memory>, owner: &str, kind: &str) -> bool {
    lock(memory).spent.iter().any(|(who, what)| who == owner && *what == kind)
}

/// Check and take in one step, before the alert: a second call during an alert raises nothing.
fn claim(memory: &Mutex<Memory>, owner: &str, kind: &'static str) -> bool {
    let mut memory = lock(memory);
    if memory.spent.iter().any(|(who, what)| who == owner && *what == kind) {
        return false;
    }
    memory.spent.push((owner.to_owned(), kind));
    true
}

fn unclaim(memory: &Mutex<Memory>, owner: &str, kind: &str) {
    lock(memory).spent.retain(|(who, what)| !(who == owner && *what == kind));
}

/// Connect's yes is a person acting: the Ask it may need can come back.
fn release(memory: &Mutex<Memory>, owner: &str) {
    let mut memory = lock(memory);
    memory.spent.retain(|(who, _)| who != owner);
    memory.ask_yes = None;
}

// --- What is true now, and when to look again ---

/// All the page learns. No ticket, team id or token has a field here.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    state: &'static str,
    workspace: Option<String>,
    notice: Option<String>,
    /// That alert is used up for this owner: the footer says to reopen rather than show a dead button.
    connect_limited: bool,
    leave_limited: bool,
}

/// With no workspace known there is nothing for the page to show.
fn status(state: &'static str, workspace: Option<String>, notice: Option<String>, limited: (bool, bool)) -> Status {
    if state == "hidden" || workspace.is_none() {
        return Status { state: "hidden", workspace: None, notice: None, connect_limited: false, leave_limited: false };
    }
    Status { state, workspace, notice, connect_limited: limited.0, leave_limited: limited.1 }
}

fn limited(memory: &Mutex<Memory>, owner: Option<&str>) -> (bool, bool) {
    owner.map_or((false, false), |owner| (spent(memory, owner, CONNECT), spent(memory, owner, LEAVE)))
}

struct Live {
    state: &'static str,
    /// The last state that was not `connecting`, for a run that cannot say anything newer.
    settled: &'static str,
    workspace: Option<String>,
    /// Whose workspace that is: a Connect after a re-pair must not name the old one.
    workspace_owner: Option<String>,
    running: bool,
    again: bool,
    last: Option<Instant>,
    /// `None`: no retry until Connect.
    wait: Option<Duration>,
    failures: u32,
    owner: Option<String>,
}

const STARTING: Live = Live {
    state: "hidden",
    settled: "hidden",
    workspace: None,
    workspace_owner: None,
    running: false,
    again: false,
    last: None,
    wait: Some(EVERY),
    failures: 0,
    owner: None,
};
static LIVE: Mutex<Live> = Mutex::new(STARTING);

fn live() -> MutexGuard<'static, Live> {
    LIVE.lock().unwrap_or_else(|e| e.into_inner())
}

fn publish(state: &'static str, workspace: &str, owner: &str) {
    let mut live = live();
    live.state = state;
    live.workspace = Some(workspace.to_owned());
    live.workspace_owner = Some(owner.to_owned());
}

/// Each failed run can mint a once link, and a Link entry is permanent.
fn backoff(failures: u32) -> Duration {
    Duration::from_secs(60 * [1, 5, 15, 60][failures.saturating_sub(1).min(3) as usize])
}

/// Single flight; at most once per `wait`, at once when the pair's owner changed. An owner change
/// during a run asks for another run after it.
fn admit(live: &mut Live, owner: Option<&str>, now: Instant) -> bool {
    let changed = live.owner.as_deref() != owner;
    if live.running {
        live.again |= changed;
        live.owner = owner.map(str::to_owned);
        return false;
    }
    let due = match (live.last, live.wait) {
        (None, _) => true,
        (Some(_), None) => false,
        (Some(last), Some(wait)) => now.duration_since(last) >= wait,
    };
    if !changed && !due {
        return false;
    }
    if changed {
        live.failures = 0;
    }
    live.owner = owner.map(str::to_owned);
    live.last = Some(now);
    live.running = true;
    true
}

/// Connect's yes: go now, with a clean slate.
fn reopen(live: &mut Live) {
    live.last = None;
    live.wait = Some(EVERY);
    live.failures = 0;
    live.again |= live.running;
}

struct Ended {
    state: &'static str,
    workspace: Option<String>,
    soon: bool,
    /// No retry until Connect: another try would say the same.
    stop: bool,
    /// A /team blip: says nothing new, and is no failed run.
    blip: bool,
    owner: Option<String>,
}

fn ended(state: &'static str, workspace: Option<&str>) -> Ended {
    Ended {
        state,
        workspace: workspace.map(str::to_owned),
        soon: matches!(state, "failed" | "off" | "connecting"),
        stop: false,
        blip: false,
        owner: None,
    }
}

/// Nothing newer is known: what was shown, with `connecting` back to what it settled on, and the
/// workspace only if it is this pair's.
fn kept(live: &Live, owner: &str) -> Ended {
    if live.workspace_owner.as_deref() != Some(owner) {
        return Ended { soon: true, blip: true, ..ended("hidden", None) };
    }
    let state = if live.state == "connecting" { live.settled } else { live.state };
    Ended { soon: true, blip: true, ..ended(state, live.workspace.as_deref()) }
}

/// `true`: run again now, someone asked during that run.
fn finish(live: &mut Live, end: Ended, now: Instant) -> bool {
    match end.state {
        "failed" if !end.blip => live.failures += 1,
        "connected" if !end.blip => live.failures = 0,
        _ => {}
    }
    live.wait = if end.stop {
        None
    } else if end.state == "failed" && !end.blip {
        Some(backoff(live.failures))
    } else if end.soon {
        Some(AFTER_FAILURE)
    } else {
        Some(EVERY)
    };
    live.state = end.state;
    if end.state != "connecting" {
        live.settled = end.state;
    }
    live.workspace = end.workspace;
    live.workspace_owner = end.owner;
    live.running = live.again;
    if live.again {
        live.last = Some(now);
    }
    std::mem::take(&mut live.again)
}

// --- The backend ---

struct Api {
    base: String,
    key_pem: String,
    device_id: String,
    owner: String,
}

/// No detail beyond the status (0: no usable answer) and `Retry-After`, so no text can leak.
struct Refused {
    status: u16,
    retry_after: Option<u64>,
}

const NO_ANSWER: Refused = Refused { status: 0, retry_after: None };

impl Api {
    /// `POST` when `post`. A fresh token per call: the line's retries outlive a 90 s token.
    async fn call(&self, post: bool, path: &str, body: Option<Value>, limit: Duration) -> Result<Value, Refused> {
        let token = device_token::mint_device_token(&self.key_pem, &self.device_id, &self.owner, 90)
            .map_err(|_| NO_ANSWER)?;
        let client = reqwest::Client::builder()
            .timeout(limit)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| NO_ANSWER)?;
        let url = format!("{}/api/v1/network{path}", self.base.trim_end_matches('/'));
        let mut request = if post { client.post(url) } else { client.get(url) }.bearer_auth(token);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().await.map_err(|_| NO_ANSWER)?;
        if !response.status().is_success() {
            let retry_after = response
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse().ok());
            return Err(Refused { status: response.status().as_u16(), retry_after });
        }
        response.json().await.map_err(|_| NO_ANSWER)
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Team {
    workspace: Option<String>,
    team_id: Option<String>,
    user_id: String,
}

/// A bearer credential. No `Debug`, no `Serialize`: it cannot reach a format string or a file.
#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Line {
    ticket: String,
    team_id: String,
    name: Option<String>,
    expires_at: Option<i64>,
}

/// As the engine's `join_line`: at least a second, at most a minute.
fn pause(retry_after: Option<u64>) -> Duration {
    Duration::from_secs(retry_after.unwrap_or(LINE_RETRY).clamp(1, 60))
}

/// Retried on a 5xx or no answer, waiting `Retry-After` when given, for up to `LINE_WINDOW`.
async fn fetch_line(api: &Api) -> Result<Line, Refused> {
    let start = Instant::now();
    loop {
        match api.call(true, "/join-line", None, LINE_LIMIT).await {
            Ok(answer) => return serde_json::from_value(answer).map_err(|_| NO_ANSWER),
            Err(refused) if refused.status == 0 || refused.status >= 500 => {
                let wait = pause(refused.retry_after);
                if start.elapsed() + wait >= LINE_WINDOW {
                    return Err(refused);
                }
                tokio::time::sleep(wait).await;
            }
            Err(refused) => return Err(refused),
        }
    }
}

/// `lightbulb login --app`, confirmed here with the device key. An older engine has no `--app`:
/// it exits, the line never comes, and that is a logged failure.
async fn sign_in(api: &Api, state: &Path) -> Result<(), &'static str> {
    let mut child = tokio::process::Command::new(state.join("bin/lightbulb"))
        .args(["login", "--app"])
        .env("LIGHTBULB_API", format!("{}/api/v1", api.base.trim_end_matches('/')))
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| "the engine did not start")?;
    let stdout = child.stdout.take().ok_or("the engine has no output")?;
    tokio::time::timeout(LOGIN_LIMIT, async {
        let mut stdout = BufReader::new(stdout);
        let mut line = String::new();
        stdout.read_line(&mut line).await.map_err(|_| "the engine's output was unreadable")?;
        let line: Value = serde_json::from_str(&line).map_err(|_| "the engine gave no sign-in line")?;
        let code = line["user_code"].as_str().ok_or("the engine gave no sign-in line")?;
        api.call(true, "/device/approve-desktop", Some(json!({"userCode": code})), HTTP_LIMIT)
            .await
            .map_err(|_| "the backend did not approve")?;
        match child.wait().await {
            Ok(exit) if exit.success() => Ok(()),
            _ => Err("the engine did not finish signing in"),
        }
    })
    .await
    .map_err(|_| "signing in timed out")?
}

/// A failure here is logged and does not stop the join. A person's logout and another account's
/// sign-in are theirs: neither is undone.
async fn ensure_signed_in(ctx: &Ctx, user_id: &str) {
    let login = std::fs::read(ctx.state.join("login.json")).ok();
    match read_proof(login.as_deref(), user_id, now_secs()) {
        Proof::Good => return,
        Proof::AnotherAccount => {
            crate::log("[network] the engine is signed in to another account; left as it is");
            return;
        }
        Proof::Missing if load(&ctx.data).is_some_and(|saved| saved.signed_in.contains_key(&ctx.owner)) => return,
        Proof::Missing | Proof::Expired => {}
    }
    match sign_in(&ctx.api, &ctx.state).await {
        Ok(()) => update(&ctx.data, |saved| {
            saved.signed_in.insert(ctx.owner.clone(), true);
        }),
        Err(why) => crate::log(&format!("[network] engine sign-in: {why}")),
    }
}

// --- The engine ---

struct EngineTeam {
    id: String,
    name: Option<String>,
    alone: bool,
    removed: bool,
    computer: String,
}

async fn read_engine(sock: &Path) -> Option<EngineTeam> {
    let answer = engine_client::ask_at(sock, "team.get", None, READ_LIMIT).await.ok()?;
    if answer["ok"] != true {
        return None;
    }
    let members = answer["members"].as_array().map_or(&[][..], Vec::as_slice);
    Some(EngineTeam {
        id: answer["team_id"].as_str().filter(|id| !id.is_empty())?.to_owned(),
        name: answer["name"].as_str().filter(|name| !name.is_empty()).map(str::to_owned),
        alone: members.len() <= 1,
        removed: !matches!(answer.get("removed"), None | Some(Value::Null) | Some(Value::Bool(false))),
        computer: members.first().and_then(|me| me["computer"].as_str()).unwrap_or("").to_owned(),
    })
}

async fn leave_team(sock: &Path) -> Result<(), &'static str> {
    let answer = engine_client::ask_at(sock, "team.leave", None, LEAVE_LIMIT).await.map_err(|_| SERVICE_OFF)?;
    if answer["reason"] == "not_a_person" {
        return Err(AGENT_SHELL);
    }
    if answer["ok"] == true { Ok(()) } else { Err(COULD_NOT_LEAVE) }
}

#[derive(Debug, PartialEq, Eq)]
enum Joined {
    Admitted,
    /// Still dialing or being asked: keep the line, keep `connecting`, burn nothing.
    InProgress,
    /// The link is dead: the next try mints another.
    Dead(&'static str),
    /// Another try would say the same: no retry until Connect.
    Stop(&'static str),
    /// Try this same line again later.
    Retry(&'static str),
    /// An agent process is above this app: no retry until Connect, the line kept.
    AgentShell,
}

async fn send_join(sock: &Path, ticket: &str, name: Option<&str>) -> Result<Value, Joined> {
    let mut body = json!({"ticket": ticket});
    if let Some(name) = name {
        body["name"] = name.into();
    }
    engine_client::ask_at(sock, "team.join", Some(body), JOIN_LIMIT).await.map_err(|e| no_answer(&e))
}

/// `ask_at`'s own fixed text. A timeout is the engine still dialing, not a refusal.
fn no_answer(error: &str) -> Joined {
    if error.contains("in time") {
        Joined::InProgress
    } else {
        Joined::Retry("the engine did not answer")
    }
}

fn read_join(answer: &Value) -> Joined {
    let reason = answer["reason"].as_str();
    match answer["state"].as_str() {
        Some("admitted") => return Joined::Admitted,
        Some("waiting") => return Joined::InProgress,
        _ => {}
    }
    if reason == Some("not_a_person") {
        return Joined::AgentShell;
    }
    // The engine's `d.joining` is held: a join of its own is running.
    if answer["ok"] == false && answer["error"] == "bad_request" && reason.is_none() {
        return Joined::InProgress;
    }
    match reason {
        Some("name_taken") => Joined::Stop("the name is taken"),
        Some("bad_name") => Joined::Stop("the name was refused"),
        Some("expired_ticket") | Some("bad_ticket") => Joined::Dead("the link is dead"),
        _ => Joined::Retry("the engine refused"),
    }
}

/// The person's first name; on `name_taken` the same ticket as `<Name> (<computer>)`; on `bad_name`
/// no name, the engine's own default. A blank name from the backend passes no name at all.
async fn join(sock: &Path, line: &Line, computer: &str) -> Joined {
    let name = line.name.as_deref().map(str::trim).filter(|name| !name.is_empty());
    let last = async {
        let mut answer = send_join(sock, &line.ticket, name).await?;
        if let (Some(name), false, true) = (name, computer.is_empty(), answer["reason"] == "name_taken") {
            answer = send_join(sock, &line.ticket, Some(&format!("{name} ({computer})"))).await?;
        }
        if name.is_some() && answer["reason"] == "bad_name" {
            answer = send_join(sock, &line.ticket, None).await?;
        }
        Ok::<Value, Joined>(answer)
    };
    match last.await {
        Ok(answer) => read_join(&answer),
        Err(end) => end,
    }
}

// --- One run ---

struct Ctx {
    api: Api,
    /// The engine's state directory: `sock`, `login.json`, `bin/lightbulb`.
    state: PathBuf,
    /// The app's data directory.
    data: PathBuf,
    owner: String,
    memory: &'static Mutex<Memory>,
}

/// The held line, else a new one. Not one of another owner's, an expired one, or one for a
/// network the workspace has moved off.
async fn line_for(ctx: &Ctx, ws_team: Option<&str>) -> Result<Line, Refused> {
    {
        let mut memory = lock(ctx.memory);
        if let Some((owner, line)) = &memory.held {
            let live = line.expires_at.is_none_or(|at| at > now_secs() as i64);
            if *owner == ctx.owner && live && ws_team.is_none_or(|team| team == line.team_id) {
                return Ok(line.clone());
            }
        }
        memory.held = None;
    }
    let line = fetch_line(&ctx.api).await?;
    lock(ctx.memory).held = Some((ctx.owner.clone(), line.clone()));
    Ok(line)
}

fn drop_line(ctx: &Ctx) {
    lock(ctx.memory).held = None;
}

/// On the network: the line is used, and a yes about another network says nothing about a later one.
fn on_the_network(ctx: &Ctx) {
    let mut memory = lock(ctx.memory);
    memory.held = None;
    memory.ask_yes = None;
}

fn agent_shell(workspace: &str) -> Ended {
    Ended { stop: true, ..ended("agentShell", Some(workspace)) }
}

fn opt_out(ctx: &Ctx, workspace: &str) -> Ended {
    update(&ctx.data, |saved| {
        saved.opt_out.insert(ctx.owner.clone(), true);
    });
    ended("optedOut", Some(workspace))
}

/// An alert that gave no answer, or was already used up: nothing written, nothing done.
fn unanswered(ctx: &Ctx, workspace: &str) -> Ended {
    ended(if opted_out_now(&ctx.data, &ctx.owner) { "optedOut" } else { "failed" }, Some(workspace))
}

fn line_failed(refused: Refused, workspace: &str) -> Ended {
    match refused.status {
        // Not this person's workspace after all.
        403 => ended("hidden", None),
        // No workspace agents yet: nothing to connect to, and no button would help.
        409 => Ended { soon: false, ..ended("off", Some(workspace)) },
        _ => {
            crate::log(&format!("[network] join line: status {}", refused.status));
            ended("failed", Some(workspace))
        }
    }
}

enum Reply {
    Yes,
    No,
    /// The dialog gave no answer, or this alert was already used up this launch.
    Nothing,
}

/// A native alert at most once per launch per owner per kind; only a yes leaves it available.
async fn ask_once<A, F>(ctx: &Ctx, ask: &A, kind: &'static str, text: String) -> Reply
where
    A: Fn(&'static str, String) -> F,
    F: Future<Output = Option<bool>>,
{
    if !claim(ctx.memory, &ctx.owner, kind) {
        return Reply::Nothing;
    }
    match ask(kind, text).await {
        Some(true) => {
            unclaim(ctx.memory, &ctx.owner, kind);
            Reply::Yes
        }
        Some(false) => Reply::No,
        None => Reply::Nothing,
    }
}

/// Before any sign-in or join: a pair whose owner is not the recorded one needs a person's yes.
/// `Some`: stop here.
async fn owner_gate<A, F>(ctx: &Ctx, ask: &A, workspace: &str) -> Option<Ended>
where
    A: Fn(&'static str, String) -> F,
    F: Future<Output = Option<bool>>,
{
    let Some(saved) = load(&ctx.data) else {
        return Some(ended("optedOut", Some(workspace)));
    };
    match saved.owner.as_deref() {
        None => {
            record_owner(&ctx.data, &ctx.owner);
            None
        }
        Some(owner) if owner == ctx.owner => None,
        Some(_) => {
            let text = format!("This computer is now paired with {workspace}. Connect it to {workspace}'s network?");
            match ask_once(ctx, ask, OWNER_CHANGE, text).await {
                Reply::Yes => {
                    update(&ctx.data, |saved| saved.owner = Some(ctx.owner.clone()));
                    None
                }
                Reply::No => Some(opt_out(ctx, workspace)),
                Reply::Nothing => Some(unanswered(ctx, workspace)),
            }
        }
    }
}

/// Steps 3 and 4 after the decision or the person's yes. `leave` comes after the line, so a line
/// that does not come costs the person nothing. The line stays held unless it was used or is dead.
async fn join_workspace(
    ctx: &Ctx,
    team: &Team,
    workspace: &str,
    ws_team: Option<&str>,
    leave: bool,
    computer: String,
    tell: &impl Fn(String),
) -> Ended {
    let sock = ctx.state.join("sock");
    publish("connecting", workspace, &ctx.owner);
    ensure_signed_in(ctx, &team.user_id).await;
    let line = match line_for(ctx, ws_team).await {
        Ok(line) => line,
        Err(refused) => return line_failed(refused, workspace),
    };
    // A Leave or a Not now can land during the minutes the line takes.
    if opted_out_now(&ctx.data, &ctx.owner) {
        return ended("optedOut", Some(workspace));
    }
    let mut computer = computer;
    if leave {
        // From here the next run must read "alone" as a join, not as the person having left.
        update(&ctx.data, |saved| {
            saved.joined.remove(&ctx.owner);
        });
        match leave_team(&sock).await {
            Ok(()) => {}
            Err(AGENT_SHELL) => return agent_shell(workspace),
            Err(_) => {
                crate::log("[network] leaving the other network failed");
                return ended("failed", Some(workspace));
            }
        }
        computer = read_engine(&sock).await.map_or(String::new(), |now| now.computer);
    }
    update(&ctx.data, |saved| saved.attempted = Some(ctx.owner.clone()));
    let end = join(&sock, &line, &computer).await;
    if end == Joined::InProgress {
        return ended("connecting", Some(workspace));
    }
    update(&ctx.data, |saved| saved.attempted = None);
    match end {
        Joined::Admitted => {
            on_the_network(ctx);
            update(&ctx.data, |saved| {
                saved.joined.insert(ctx.owner.clone(), line.team_id.clone());
                saved.notice = Some(workspace.to_owned());
                saved.removal_overridden.remove(&ctx.owner);
            });
            tell(format!("Your computer joined {workspace}'s network."));
            ended("connected", Some(workspace))
        }
        Joined::Dead(why) => {
            drop_line(ctx);
            crate::log(&format!("[network] join: {why}"));
            ended("failed", Some(workspace))
        }
        Joined::Stop(why) => {
            crate::log(&format!("[network] join: {why}"));
            Ended { stop: true, ..ended("failed", Some(workspace)) }
        }
        Joined::Retry(why) => {
            crate::log(&format!("[network] join: {why}"));
            ended("failed", Some(workspace))
        }
        Joined::AgentShell => agent_shell(workspace),
        Joined::InProgress => ended("connecting", Some(workspace)),
    }
}

/// `ask` raises a native alert and answers yes, no, or nothing; `tell` posts the notification.
/// Both are native, and passed in so this runs without Tauri in tests.
async fn connect<A, F>(ctx: &Ctx, ask: A, tell: impl Fn(String)) -> Ended
where
    A: Fn(&'static str, String) -> F,
    F: Future<Output = Option<bool>>,
{
    let sock = ctx.state.join("sock");
    let Some(engine) = read_engine(&sock).await else {
        return ended("off", None);
    };
    let team: Team = match ctx.api.call(false, "/team", None, HTTP_LIMIT).await.map(serde_json::from_value) {
        Ok(Ok(team)) => team,
        // Maybe just invited, with the webhook behind: look again soon.
        Err(Refused { status: 403, .. }) => return Ended { soon: true, ..ended("hidden", None) },
        // Offline or a blip says nothing new: keep what is shown, look again soon.
        _ => return kept(&live(), &ctx.owner),
    };
    let Some(workspace) = team.workspace.clone().filter(|workspace| !workspace.is_empty()) else {
        return ended("hidden", None);
    };
    let Some(saved) = load(&ctx.data) else {
        crate::log("[network] network_connect.json is unreadable; reads as opted out");
        return ended("optedOut", Some(&workspace));
    };
    // Logged out by the person, after this app signed in: no join decision at all.
    let login = std::fs::read(ctx.state.join("login.json")).ok();
    if saved.signed_in.contains_key(&ctx.owner) && read_proof(login.as_deref(), &team.user_id, now_secs()) == Proof::Missing {
        return ended("optedOut", Some(&workspace));
    }
    let opted_out = saved.opt_out.get(&ctx.owner) == Some(&true);
    let mut ws_team = team.team_id.clone().filter(|id| !id.is_empty());
    if ws_team.is_none() && !opted_out {
        if let Some(end) = owner_gate(ctx, &ask, &workspace).await {
            return end;
        }
        publish("connecting", &workspace, &ctx.owner);
        match line_for(ctx, None).await {
            Ok(line) => ws_team = Some(line.team_id),
            Err(refused) => return line_failed(refused, &workspace),
        }
    }
    let ws_team = ws_team.as_deref();
    let joined_before = saved.joined.get(&ctx.owner).map(String::as_str);
    let removed = engine.removed && !saved.removal_overridden.contains_key(&ctx.owner);
    let action = decide(&engine.id, engine.alone, removed, ws_team, opted_out, joined_before);
    if matches!(action, Action::Done | Action::Join | Action::Ask) {
        if let Some(end) = owner_gate(ctx, &ask, &workspace).await {
            return end;
        }
    }
    match action {
        Action::Done => {
            ensure_signed_in(ctx, &team.user_id).await;
            on_the_network(ctx);
            let landed = saved.attempted.as_deref() == Some(ctx.owner.as_str());
            // Remembered even when the CLI did the joining, so a later `lightbulb leave` reads as one.
            update(&ctx.data, |saved| {
                saved.joined.insert(ctx.owner.clone(), engine.id.clone());
                if landed {
                    saved.attempted = None;
                    saved.notice = Some(workspace.clone());
                }
            });
            if landed {
                tell(format!("Your computer joined {workspace}'s network."));
            }
            ended("connected", Some(&workspace))
        }
        Action::Stay => ended("optedOut", Some(&workspace)),
        Action::OptOut => opt_out(ctx, &workspace),
        Action::Join => join_workspace(ctx, &team, &workspace, ws_team, false, engine.computer, &tell).await,
        Action::Ask => {
            let said_yes = lock(ctx.memory).ask_yes.as_ref().is_some_and(|(owner, on)| *owner == ctx.owner && *on == engine.id);
            if !said_yes {
                let on = engine.name.as_deref().unwrap_or("another network");
                let text = format!("Your computer is on {on}. Join {workspace}'s network instead?");
                match ask_once(ctx, &ask, ASK, text).await {
                    Reply::Yes => lock(ctx.memory).ask_yes = Some((ctx.owner.clone(), engine.id.clone())),
                    Reply::No => return opt_out(ctx, &workspace),
                    Reply::Nothing => return unanswered(ctx, &workspace),
                }
            }
            // The yes was about the network the alert named.
            match read_engine(&sock).await {
                Some(now) if now.id == engine.id => {}
                _ => {
                    crate::log("[network] the network changed during the alert; stopped");
                    return ended("failed", Some(&workspace));
                }
            }
            join_workspace(ctx, &team, &workspace, ws_team, true, engine.computer, &tell).await
        }
    }
}

fn api_for(pair: &pair_state::PairState) -> Option<Api> {
    let key = keychain::load(keychain::KeychainSlot::Cli, crate::KEYCHAIN_SERVICE).ok()?;
    Some(Api {
        base: crate::LIGHTBULB_API_BASE.to_owned(),
        key_pem: key.private_key_pem,
        device_id: pair.device_id_cli.clone(),
        owner: pair.owner_id.clone(),
    })
}

async fn run(app: &tauri::AppHandle) -> Ended {
    let Some((data, pair)) = paired(app) else {
        return ended("hidden", None);
    };
    let Ok(state) = engine_client::state_dir() else {
        return ended("off", None);
    };
    let Some(api) = api_for(&pair) else {
        crate::log("[network] no device key");
        return ended("hidden", None);
    };
    let ctx = Ctx { api, state, data, owner: pair.owner_id.clone(), memory: &MEMORY };
    let ask = |kind: &'static str, text: String| {
        let app = app.clone();
        let yes = if kind == OWNER_CHANGE { "Connect" } else { "Join" };
        async move { crate::confirm_native_labelled(&app, "Lightbulb", text, yes, "Not now").await }
    };
    let tell = |text: String| {
        tauri::async_runtime::spawn(crate::notify::show("Lightbulb".into(), Some(text), None, None, None));
    };
    Ended { owner: Some(pair.owner_id.clone()), ..connect(&ctx, ask, tell).await }
}

/// At launch, before the page runs: the pair as it stands is the one consented to, if none was yet.
pub fn record_owner_at_launch(app: &tauri::AppHandle) {
    if let Some((data, pair)) = paired(app) {
        record_owner(&data, &pair.owner_id);
    }
}

/// Cheap and returns at once: `send_auth_token` calls it every 45 s.
pub fn kick(app: &tauri::AppHandle) {
    let owner = paired(app).map(|(_, pair)| pair.owner_id);
    if !admit(&mut live(), owner.as_deref(), Instant::now()) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            let end = run(&app).await;
            if !finish(&mut live(), end, Instant::now()) {
                break;
            }
        }
    });
}

// --- Commands: none takes an argument from the page ---

fn paired(app: &tauri::AppHandle) -> Option<(PathBuf, pair_state::PairState)> {
    let data = app.path().app_data_dir().ok()?;
    let pair = pair_state::load(&data)?;
    Some((data, pair))
}

/// (workspace, its owner) as the backend last named it, while there is one.
fn known_workspace() -> Option<(String, String)> {
    let live = live();
    if live.state == "hidden" {
        return None;
    }
    Some((live.workspace.clone()?, live.workspace_owner.clone()?))
}

/// The workspace of the CURRENT pair: what is known if it is that pair's, else the backend's word.
/// `None` for a person with no workspace, who gets no alert at all.
async fn workspace_name(api: &Api, owner: &str, known: Option<(String, String)>) -> Option<String> {
    if let Some((workspace, of)) = known {
        if of == owner {
            return Some(workspace);
        }
    }
    let answer = api.call(false, "/team", None, HTTP_LIMIT).await.ok()?;
    let team: Team = serde_json::from_value(answer).ok()?;
    team.workspace.filter(|workspace| !workspace.is_empty())
}

#[tauri::command]
pub fn network_status(app: tauri::AppHandle) -> Status {
    let (state, workspace) = {
        let live = live();
        (live.state, live.workspace.clone())
    };
    let notice = app.path().app_data_dir().ok().and_then(|data| load(&data)?.notice);
    let owner = paired(&app).map(|(_, pair)| pair.owner_id);
    status(state, workspace, notice, limited(&MEMORY, owner.as_deref()))
}

#[tauri::command]
pub async fn network_connect(app: tauri::AppHandle) -> Result<(), String> {
    let (data, pair) = paired(&app).ok_or(NOT_AVAILABLE)?;
    let owner = pair.owner_id.clone();
    let api = api_for(&pair).ok_or(NOT_AVAILABLE)?;
    let workspace = workspace_name(&api, &owner, known_workspace()).await.ok_or(crate::DECLINED_ON_MAC)?;
    let text = format!("Connect this computer to {workspace}'s network?");
    if !claim(&MEMORY, &owner, CONNECT) || !crate::confirm_native(&app, "Lightbulb", text).await {
        return Err(crate::DECLINED_ON_MAC.to_owned());
    }
    unclaim(&MEMORY, &owner, CONNECT);
    rewrite(&data, |saved| consent_to_connect(saved, &owner));
    release(&MEMORY, &owner);
    reopen(&mut live());
    kick(&app);
    Ok(())
}

// ponytail: a Leave during a run's join can lose to it; the next run shows "connected" and Leave works again.
#[tauri::command]
pub async fn network_leave(app: tauri::AppHandle) -> Result<(), String> {
    let (data, pair) = paired(&app).ok_or(NOT_AVAILABLE)?;
    let owner = pair.owner_id.clone();
    let api = api_for(&pair).ok_or(NOT_AVAILABLE)?;
    let workspace = workspace_name(&api, &owner, known_workspace()).await.ok_or(crate::DECLINED_ON_MAC)?;
    let sock = engine_client::state_dir().map_err(|_| SERVICE_OFF)?.join("sock");
    let team = read_engine(&sock).await.ok_or(SERVICE_OFF)?.name.unwrap_or_else(|| "this network".to_owned());
    if !claim(&MEMORY, &owner, LEAVE) || !crate::confirm_native(&app, "Lightbulb", format!("Leave {team}?")).await {
        return Err(crate::DECLINED_ON_MAC.to_owned());
    }
    unclaim(&MEMORY, &owner, LEAVE);
    match leave_team(&sock).await {
        Ok(()) => {}
        Err(AGENT_SHELL) => {
            publish("agentShell", &workspace, &owner);
            live().wait = None;
            return Err(crate::DECLINED_ON_MAC.to_owned());
        }
        Err(why) => return Err(why.to_owned()),
    }
    update(&data, |saved| {
        saved.opt_out.insert(owner.clone(), true);
        saved.notice = None;
        saved.attempted = None;
    });
    publish("optedOut", &workspace, &owner);
    Ok(())
}

#[tauri::command]
pub fn network_notice_seen(app: tauri::AppHandle) {
    if let Ok(data) = app.path().app_data_dir() {
        update(&data, |saved| saved.notice = None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::io::AsyncWriteExt;
    use tokio::net::UnixListener;
    use wiremock::matchers::{body_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const TICKET: &str = "lb1-SECRET-TICKET";

    #[test]
    fn same_team_is_done() {
        assert_eq!(decide("t", true, false, Some("t"), false, None), Action::Done);
    }

    #[test]
    fn same_team_wins_over_opted_out() {
        assert_eq!(decide("t", false, true, Some("t"), true, Some("x")), Action::Done);
    }

    #[test]
    fn alone_joins() {
        assert_eq!(decide("solo", true, false, Some("t"), false, None), Action::Join);
        assert_eq!(decide("solo", true, false, Some("t"), false, Some("solo")), Action::Join);
    }

    #[test]
    fn opted_out_stays() {
        assert_eq!(decide("solo", true, false, Some("t"), true, None), Action::Stay);
        assert_eq!(decide("x", false, true, None, true, None), Action::Stay);
    }

    #[test]
    fn removed_opts_out() {
        assert_eq!(decide("solo", true, true, Some("t"), false, None), Action::OptOut);
    }

    #[test]
    fn person_left_is_an_opt_out() {
        assert_eq!(decide("fresh", true, false, Some("t"), false, Some("t")), Action::OptOut);
    }

    #[test]
    fn other_network_asks() {
        assert_eq!(decide("x", false, false, Some("t"), false, None), Action::Ask);
    }

    #[test]
    fn connect_after_a_leave_joins_rather_than_opting_out_again() {
        let mut saved = Saved::default();
        saved.opt_out.insert("o".into(), true);
        saved.joined.insert("o".into(), "t".into());
        consent_to_connect(&mut saved, "o");
        let before = saved.joined.get("o").map(String::as_str);
        assert_eq!(decide("fresh", true, false, Some("t"), saved.opt_out.contains_key("o"), before), Action::Join);
    }

    fn proof(claims: Value) -> Vec<u8> {
        let middle = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(claims.to_string());
        json!({"proof": format!("h.{middle}.s")}).to_string().into_bytes()
    }

    #[test]
    fn proof_needs_login() {
        assert_eq!(read_proof(Some(&proof(json!({"sub": "u1", "exp": 200}))), "u1", 100), Proof::Good);
        assert_eq!(read_proof(None, "u1", 100), Proof::Missing);
        assert_eq!(read_proof(Some(&proof(json!({"sub": "u1", "exp": 99}))), "u1", 100), Proof::Expired);
        assert_eq!(read_proof(Some(&proof(json!({"sub": "u2", "exp": 200}))), "u1", 100), Proof::AnotherAccount);
        assert_eq!(read_proof(Some(b"{\"proof\":\"nodots\"}"), "u1", 100), Proof::Missing);
        assert_eq!(read_proof(Some(b"not json"), "u1", 100), Proof::Missing);
    }

    #[test]
    fn opt_out_survives_reload() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        // A wider leftover must not lend the file its mode.
        let tmp = dir.path().join("network_connect.json.tmp");
        std::fs::write(&tmp, b"x").unwrap();
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o644)).unwrap();
        update(dir.path(), |saved| {
            saved.owner = Some("o".into());
            saved.opt_out.insert("o".into(), true);
            saved.joined.insert("o".into(), "t".into());
            saved.notice = Some("Acme".into());
            saved.signed_in.insert("o".into(), true);
            saved.attempted = Some("o".into());
        });
        let file = dir.path().join("network_connect.json");
        assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
        let text = std::fs::read_to_string(&file).unwrap();
        for key in ["\"owner\"", "\"optOut\"", "\"joined\"", "\"notice\"", "\"signedIn\"", "\"attempted\""] {
            assert!(text.contains(key), "{key}: {text}");
        }
        let again = load(dir.path()).unwrap();
        assert_eq!(again.opt_out.get("o"), Some(&true));
        assert_eq!(again.joined.get("o").map(String::as_str), Some("t"));
        assert_eq!(again.notice.as_deref(), Some("Acme"));
    }

    #[test]
    fn the_gate_runs_once_and_waits() {
        let mut live = STARTING;
        let t0 = Instant::now();
        assert!(admit(&mut live, Some("o"), t0));
        assert!(!admit(&mut live, Some("o"), t0 + EVERY), "single flight");
        assert!(!finish(&mut live, ended("connected", Some("A")), t0));
        assert!(!admit(&mut live, Some("o"), t0 + EVERY - Duration::from_secs(1)));
        assert!(admit(&mut live, Some("o"), t0 + EVERY));
        assert!(!finish(&mut live, ended("off", None), t0 + EVERY));
        assert!(!admit(&mut live, Some("o"), t0 + EVERY + AFTER_FAILURE - Duration::from_secs(1)));
        assert!(admit(&mut live, Some("o"), t0 + EVERY + AFTER_FAILURE));
        assert!(!finish(&mut live, ended("connected", Some("A")), t0));
        assert!(admit(&mut live, Some("other"), t0), "a changed owner goes at once");
        assert!(!admit(&mut live, Some("third"), t0));
        assert!(finish(&mut live, ended("connected", Some("A")), t0), "and another run follows");
        assert!(live.running);
        assert!(!finish(&mut live, ended("connected", Some("A")), t0));
        assert!(!live.running);
    }

    #[test]
    fn backoff_grows() {
        let minutes: Vec<u64> = (1..=6).map(|failures| backoff(failures).as_secs() / 60).collect();
        assert_eq!(minutes, [1, 5, 15, 60, 60, 60]);
        let mut live = STARTING;
        let t0 = Instant::now();
        assert!(admit(&mut live, Some("o"), t0));
        finish(&mut live, ended("failed", Some("A")), t0);
        assert_eq!(live.wait, Some(Duration::from_secs(60)));
        assert!(admit(&mut live, Some("o"), t0 + Duration::from_secs(60)));
        finish(&mut live, ended("failed", Some("A")), t0);
        assert_eq!(live.wait, Some(Duration::from_secs(300)));
        assert!(!admit(&mut live, Some("o"), t0 + Duration::from_secs(60 + 299)));
        assert!(admit(&mut live, Some("o"), t0 + Duration::from_secs(60 + 300)));
        finish(&mut live, ended("connected", Some("A")), t0);
        assert_eq!((live.failures, live.wait), (0, Some(EVERY)));
        finish(&mut live, ended("failed", Some("A")), t0);
        reopen(&mut live);
        assert_eq!(live.failures, 0, "Connect's yes resets it");
    }

    #[test]
    fn retry_after_is_clamped() {
        assert_eq!(pause(Some(0)), Duration::from_secs(1));
        assert_eq!(pause(Some(30)), Duration::from_secs(30));
        assert_eq!(pause(Some(3600)), Duration::from_secs(60));
        assert_eq!(pause(None), Duration::from_secs(15));
    }

    #[test]
    fn status_serializes_only_its_three_fields() {
        let shown = status("failed", Some("Acme".into()), None, (true, false));
        assert_eq!(
            serde_json::to_value(shown).unwrap(),
            json!({"state": "failed", "workspace": "Acme", "notice": null, "connectLimited": true, "leaveLimited": false})
        );
    }

    #[test]
    fn no_workspace_known_is_hidden() {
        let shown = status("off", None, Some("Acme".into()), (true, true));
        assert_eq!(
            serde_json::to_value(shown).unwrap(),
            json!({"state": "hidden", "workspace": null, "notice": null, "connectLimited": false, "leaveLimited": false})
        );
    }

    #[test]
    fn no_log_line_names_a_secret() {
        let source = include_str!("network_connect.rs");
        let code = &source[..source.find("#[cfg(test)]").unwrap()];
        for line in code.lines().filter(|line| line.contains("log(")) {
            for word in ["ticket", "token", "proof", "code", "body", "answer", "{e", "key_pem", "url"] {
                assert!(!line.to_lowercase().contains(word), "{word}: {line}");
            }
        }
    }

    #[test]
    fn a_timeout_is_in_progress_and_a_dead_socket_is_not() {
        assert_eq!(no_answer("engine did not answer in time"), Joined::InProgress);
        assert!(matches!(no_answer("engine unreachable: x"), Joined::Retry(_)));
    }

    #[test]
    fn the_engines_answers_read_as_they_mean() {
        assert_eq!(read_join(&json!({"ok": false, "error": "bad_request"})), Joined::InProgress);
        assert_eq!(read_join(&json!({"ok": false, "error": "bad_request", "reason": "not_a_person"})), Joined::AgentShell);
        assert_eq!(read_join(&json!({"ok": true, "state": "waiting"})), Joined::InProgress);
        assert!(matches!(read_join(&json!({"ok": true, "state": "refused", "reason": "bad_ticket"})), Joined::Dead(_)));
        assert!(matches!(read_join(&json!({"ok": true, "state": "refused", "reason": "expired_ticket"})), Joined::Dead(_)));
        assert!(matches!(read_join(&json!({"ok": true, "state": "refused", "reason": "unreachable"})), Joined::Retry(_)));
    }

    #[test]
    fn an_alert_is_limited_per_owner_and_per_kind_and_connect_releases_it() {
        let memory = Mutex::new(Memory::new());
        assert!(claim(&memory, "o", ASK));
        assert!(!claim(&memory, "o", ASK));
        assert!(spent(&memory, "o", ASK));
        assert!(!spent(&memory, "o", LEAVE));
        assert!(!spent(&memory, "p", ASK));
        assert_eq!(limited(&memory, Some("o")), (false, false));
        assert!(claim(&memory, "o", CONNECT));
        assert_eq!(limited(&memory, Some("o")), (true, false));
        assert_eq!(limited(&memory, Some("p")), (false, false));
        assert_eq!(limited(&memory, None), (false, false));
        unclaim(&memory, "o", CONNECT);
        assert!(!spent(&memory, "o", CONNECT), "a yes leaves it available");
        release(&memory, "o");
        assert!(!spent(&memory, "o", ASK));
    }

    /// A stub engine as `engine_client_tests::stub`, but each op answers its scripted lines in turn.
    fn stub(dir: &Path, script: Vec<(&'static str, Value)>) -> Arc<Mutex<Vec<Value>>> {
        let listener = UnixListener::bind(dir.join("sock")).unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        let mut lines: HashMap<&str, VecDeque<Value>> = HashMap::new();
        for (op, line) in script {
            lines.entry(op).or_default().push_back(line);
        }
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let mut stream = BufReader::new(stream);
                let mut line = String::new();
                stream.read_line(&mut line).await.unwrap();
                let frame: Value = serde_json::from_str(&line).unwrap();
                log.lock().unwrap().push(frame.clone());
                let mut answer = json!({"id": 1, "ok": true});
                let scripted = lines.get_mut(frame["op"].as_str().unwrap()).and_then(VecDeque::pop_front);
                answer.as_object_mut().unwrap().extend(scripted.unwrap_or(json!({})).as_object().unwrap().clone());
                stream.get_mut().write_all(format!("{answer}\n").as_bytes()).await.unwrap();
            }
        });
        seen
    }

    fn ops(seen: &Arc<Mutex<Vec<Value>>>) -> Vec<String> {
        seen.lock().unwrap().iter().map(|frame| frame["op"].as_str().unwrap().to_owned()).collect()
    }

    fn line(name: Option<&str>) -> Line {
        Line { ticket: TICKET.into(), team_id: "t".into(), name: name.map(str::to_owned), expires_at: None }
    }

    #[tokio::test]
    async fn join_retries_name_taken_with_the_computer() {
        let dir = tempfile::tempdir().unwrap();
        let seen = stub(dir.path(), vec![
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "admitted"})),
        ]);
        assert_eq!(join(&dir.path().join("sock"), &line(Some("Ada")), "mac").await, Joined::Admitted);
        let seen = seen.lock().unwrap();
        assert_eq!(seen[0], json!({"id": 1, "op": "team.join", "ticket": TICKET, "name": "Ada"}));
        assert_eq!(seen[1], json!({"id": 1, "op": "team.join", "ticket": TICKET, "name": "Ada (mac)"}));
    }

    #[tokio::test]
    async fn a_bad_fallback_name_retries_with_no_name_and_stops() {
        let dir = tempfile::tempdir().unwrap();
        let seen = stub(dir.path(), vec![
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "refused", "reason": "bad_name"})),
            ("team.join", json!({"state": "refused", "reason": "bad_name"})),
        ]);
        let end = join(&dir.path().join("sock"), &line(Some("Ada")), "mac!").await;
        assert_eq!(end, Joined::Stop("the name was refused"));
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 3);
        assert_eq!(seen[2], json!({"id": 1, "op": "team.join", "ticket": TICKET}));
    }

    #[tokio::test]
    async fn a_blank_name_passes_no_name() {
        let dir = tempfile::tempdir().unwrap();
        let seen = stub(dir.path(), vec![("team.join", json!({"state": "admitted"}))]);
        join(&dir.path().join("sock"), &line(Some("  ")), "mac").await;
        assert_eq!(seen.lock().unwrap()[0], json!({"id": 1, "op": "team.join", "ticket": TICKET}));
    }

    async fn fixture(team: Value) -> (tempfile::TempDir, MockServer, Ctx) {
        let dir = tempfile::tempdir().unwrap();
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/network/team"))
            .respond_with(ResponseTemplate::new(200).set_body_json(team))
            .mount(&server)
            .await;
        std::fs::write(dir.path().join("login.json"), proof(json!({"sub": "u1", "exp": 99_999_999_999u64}))).unwrap();
        let ctx = Ctx {
            api: Api {
                base: server.uri(),
                key_pem: keychain::generate_keypair().private_key_pem,
                device_id: "d".into(),
                owner: "o".into(),
            },
            state: dir.path().to_owned(),
            data: dir.path().to_owned(),
            owner: "o".into(),
            memory: Box::leak(Box::new(Mutex::new(Memory::new()))),
        };
        (dir, server, ctx)
    }

    fn acme(team_id: Option<&str>) -> Value {
        json!({"workspace": "Acme", "teamId": team_id, "userId": "u1", "name": "Ada"})
    }

    fn alone_on(team: &str) -> Value {
        json!({"team_id": team, "name": "Solo", "members": [{"name": "Ada", "computer": "mac", "key": "k"}], "removed": null})
    }

    fn with_others(team: &str) -> Value {
        json!({"team_id": team, "name": "Friends", "members": [{"computer": "mac"}, {"computer": "b"}], "removed": null})
    }

    async fn line_answers(server: &MockServer) {
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(200).set_body_json(
                json!({"ticket": TICKET, "teamId": "t", "workspace": "Acme", "name": "Ada", "expiresAt": 99_999_999_999u64}),
            ))
            .mount(server)
            .await;
    }

    async fn never_asked(_: &'static str, _: String) -> Option<bool> {
        panic!("nothing should ask the person here")
    }

    fn never_told(_: String) {
        panic!("nothing should notify here")
    }

    fn set(dir: &Path, change: impl FnOnce(&mut Saved)) {
        update(dir, change);
    }

    fn fake_engine(state: &Path, script: &str) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(state.join("bin")).unwrap();
        let bin = state.join("bin/lightbulb");
        std::fs::write(&bin, format!("#!/bin/sh\n{script}\n")).unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// An engine whose start leaves a mark, and has no proof to find: a sign-in would run it.
    fn engine_that_marks(dir: &Path) -> PathBuf {
        std::fs::remove_file(dir.join("login.json")).unwrap();
        let mark = dir.join("signed-in-mark");
        fake_engine(dir, &format!("touch '{}'; exit 2", mark.display()));
        mark
    }

    #[tokio::test]
    async fn owner_change_asks_before_sign_in_and_join() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let mark = engine_that_marks(dir.path());
        set(dir.path(), |saved| saved.owner = Some("old".into()));
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        let asked = Mutex::new(Vec::new());
        let end = connect(
            &ctx,
            |kind, text| {
                assert!(!mark.exists(), "asked before the sign-in");
                assert_eq!(ops(&seen), ["team.get"], "asked before the join");
                asked.lock().unwrap().push((kind, text));
                async { Some(true) }
            },
            |_| {},
        )
        .await;
        assert_eq!(end.state, "connected");
        assert!(mark.exists());
        assert_eq!(asked.into_inner().unwrap(), [(OWNER_CHANGE, "This computer is now paired with Acme. Connect it to Acme's network?".to_owned())]);
        assert_eq!(load(dir.path()).unwrap().owner.as_deref(), Some("o"));
    }

    #[tokio::test]
    async fn owner_change_on_a_computer_already_on_the_team_asks_before_the_sign_in() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let mark = engine_that_marks(dir.path());
        set(dir.path(), |saved| saved.owner = Some("old".into()));
        let _seen = stub(dir.path(), vec![("team.get", alone_on("t"))]);
        let end = connect(&ctx, |_, _| async { Some(false) }, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert!(!mark.exists());
    }

    #[tokio::test]
    async fn same_owner_is_silent() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        set(dir.path(), |saved| saved.owner = Some("o".into()));
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected");
    }

    #[tokio::test]
    async fn no_recorded_owner_is_silent_and_recorded() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected");
        assert_eq!(load(dir.path()).unwrap().owner.as_deref(), Some("o"));
        record_owner(dir.path(), "someone else");
        assert_eq!(load(dir.path()).unwrap().owner.as_deref(), Some("o"), "the launch records only when none is");
    }

    #[tokio::test]
    async fn declined_owner_change_opts_out_and_does_not_sign_in() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let mark = engine_that_marks(dir.path());
        set(dir.path(), |saved| saved.owner = Some("old".into()));
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        Mock::given(method("POST")).respond_with(ResponseTemplate::new(500)).expect(0).mount(&server).await;
        let end = connect(&ctx, |_, _| async { Some(false) }, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert!(!mark.exists());
        assert_eq!(ops(&seen), ["team.get"]);
        let saved = load(dir.path()).unwrap();
        assert_eq!(saved.opt_out.get("o"), Some(&true));
        assert_eq!(saved.owner.as_deref(), Some("old"), "left as it was");
    }

    #[tokio::test]
    async fn a_dialog_with_no_answer_writes_nothing_and_is_not_raised_again() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![("team.get", with_others("x")), ("team.get", with_others("x"))]);
        let asked = AtomicUsize::new(0);
        let ask = |_, _| {
            asked.fetch_add(1, Ordering::SeqCst);
            async { None }
        };
        assert_eq!(connect(&ctx, ask, never_told).await.state, "failed");
        assert_eq!(load(dir.path()).unwrap().opt_out.get("o"), None, "no opt-out");
        assert_eq!(connect(&ctx, ask, never_told).await.state, "failed");
        assert_eq!(asked.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn connects_yes_brings_a_used_up_ask_back() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![("team.get", with_others("x")), ("team.get", with_others("x"))]);
        let asked = AtomicUsize::new(0);
        let ask = |_, _| {
            asked.fetch_add(1, Ordering::SeqCst);
            async { Some(false) }
        };
        assert_eq!(connect(&ctx, ask, never_told).await.state, "optedOut");
        rewrite(dir.path(), |saved| consent_to_connect(saved, "o"));
        release(ctx.memory, "o");
        assert_eq!(connect(&ctx, ask, never_told).await.state, "optedOut");
        assert_eq!(asked.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_yes_then_a_failed_line_does_not_ask_again() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", with_others("x")),
            ("team.get", with_others("x")),
            ("team.get", with_others("x")),
            ("team.get", with_others("x")),
            ("team.get", alone_on("fresh")),
            ("team.join", json!({"state": "admitted"})),
        ]);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(400))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        line_answers(&server).await;
        let asked = AtomicUsize::new(0);
        let ask = |_, _| {
            asked.fetch_add(1, Ordering::SeqCst);
            async { Some(true) }
        };
        assert_eq!(connect(&ctx, ask, |_| {}).await.state, "failed");
        assert_eq!(ops(&seen), ["team.get", "team.get"], "nothing left on a failed line");
        assert_eq!(connect(&ctx, ask, |_| {}).await.state, "connected");
        assert_eq!(asked.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn corrupt_state_reads_as_opted_out_and_is_not_overwritten() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let file = dir.path().join("network_connect.json");
        std::fs::write(&file, b"not json{{").unwrap();
        let seen = stub(dir.path(), vec![("team.get", alone_on("t"))]);
        Mock::given(method("POST")).respond_with(ResponseTemplate::new(500)).expect(0).mount(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(ops(&seen), ["team.get"]);
        assert!(opted_out_now(dir.path(), "anyone"));
        record_owner(dir.path(), "o");
        set(dir.path(), |saved| saved.notice = None);
        assert_eq!(std::fs::read(&file).unwrap(), b"not json{{");
        rewrite(dir.path(), |saved| consent_to_connect(saved, "o"));
        assert!(!opted_out_now(dir.path(), "o"), "only the native Connect rewrites it");
    }

    #[tokio::test]
    async fn connects_alert_names_the_workspace_of_the_current_pair() {
        let (_dir, server, ctx) = fixture(acme(Some("t"))).await;
        let stale = Some(("Old Co".to_owned(), "old".to_owned()));
        assert_eq!(workspace_name(&ctx.api, "o", stale).await.as_deref(), Some("Acme"));
        let before = server.received_requests().await.unwrap().len();
        let current = Some(("Acme known".to_owned(), "o".to_owned()));
        assert_eq!(workspace_name(&ctx.api, "o", current).await.as_deref(), Some("Acme known"));
        assert_eq!(server.received_requests().await.unwrap().len(), before, "no fetch when it is this pair's");
    }

    #[tokio::test]
    async fn a_person_with_no_workspace_gets_no_name_to_alert_with() {
        let (_dir, _server, ctx) = fixture(json!({"workspace": null, "teamId": null, "userId": "u1", "name": null})).await;
        assert_eq!(workspace_name(&ctx.api, "o", None).await, None);
    }

    #[tokio::test]
    async fn a_logged_out_person_does_not_join() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let mark = engine_that_marks(dir.path());
        set(dir.path(), |saved| {
            saved.signed_in.insert("o".into(), true);
        });
        let seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "admitted"})),
        ]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "optedOut");
        assert!(!mark.exists(), "no sign-in");
        assert_eq!(ops(&seen), ["team.get"], "no join");
        assert!(server.received_requests().await.unwrap().iter().all(|request| request.url.path().ends_with("/team")), "no line");

        rewrite(dir.path(), |saved| consent_to_connect(saved, "o"));
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected", "Connect's yes signs in and joins");
        assert!(mark.exists());
        assert_eq!(ops(&seen), ["team.get", "team.get", "team.join"]);
    }

    #[tokio::test]
    async fn another_accounts_proof_is_left_alone() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let mark = engine_that_marks(dir.path());
        std::fs::write(dir.path().join("login.json"), proof(json!({"sub": "someone", "exp": 99_999_999_999u64}))).unwrap();
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected");
        assert!(!mark.exists(), "no sign-in");
    }

    #[tokio::test]
    async fn an_expired_proof_signs_in_again_and_remembers_it() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        std::fs::write(dir.path().join("login.json"), proof(json!({"sub": "u1", "exp": 1}))).unwrap();
        set(dir.path(), |saved| {
            saved.signed_in.insert("o".into(), true);
        });
        fake_engine(dir.path(), r#"echo '{"user_code":"AB-CD"}'"#);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/device/approve-desktop"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status": "approved"})))
            .expect(1)
            .mount(&server)
            .await;
        ensure_signed_in(&ctx, "u1").await;
        assert!(load(dir.path()).unwrap().signed_in.contains_key("o"));
    }

    #[tokio::test]
    async fn connect_after_a_removal_joins() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let removed = json!({"team_id": "solo", "name": "Solo", "members": [{"computer": "mac"}], "removed": {"by": "x"}});
        let seen = stub(dir.path(), vec![
            ("team.get", removed.clone()),
            ("team.get", removed.clone()),
            ("team.join", json!({"state": "admitted"})),
            ("team.get", removed),
        ]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "optedOut");
        rewrite(dir.path(), |saved| consent_to_connect(saved, "o"));
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected");
        assert_eq!(ops(&seen), ["team.get", "team.get", "team.join"]);
        assert!(!load(dir.path()).unwrap().removal_overridden.contains_key("o"), "cleared on the join");
    }

    #[tokio::test]
    async fn an_unreachable_failure_reuses_the_line() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "refused", "reason": "unreachable"})),
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "admitted"})),
        ]);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ticket": TICKET, "teamId": "t", "name": "Ada"})))
            .expect(1)
            .mount(&server)
            .await;
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "failed");
        assert_eq!(connect(&ctx, never_asked, |_| {}).await.state, "connected");
        let seen = seen.lock().unwrap();
        assert_eq!(seen[1]["ticket"], TICKET);
        assert_eq!(seen[3]["ticket"], TICKET);
        assert!(lock(ctx.memory).held.is_none(), "used, so gone");
    }

    #[tokio::test]
    async fn a_dead_or_expired_or_other_networks_line_is_minted_again() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "refused", "reason": "bad_ticket"})),
        ]);
        line_answers(&server).await;
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "failed");
        assert!(lock(ctx.memory).held.is_none(), "dead, so gone");
        let hold = |team: &str, expires: Option<i64>| {
            lock(ctx.memory).held = Some(("o".into(), Line { ticket: "HELD".into(), team_id: team.into(), name: None, expires_at: expires }));
        };
        hold("t", Some(1));
        assert_eq!(line_for(&ctx, Some("t")).await.ok().unwrap().ticket, TICKET, "expired");
        hold("moved", None);
        assert_eq!(line_for(&ctx, Some("t")).await.ok().unwrap().ticket, TICKET, "the workspace moved networks");
        hold("t", None);
        assert_eq!(line_for(&ctx, Some("t")).await.ok().unwrap().ticket, "HELD");
    }

    #[tokio::test]
    async fn name_taken_twice_stops_retrying() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!((end.state, end.stop), ("failed", true));
        let mut gate = STARTING;
        let t0 = Instant::now();
        assert!(admit(&mut gate, Some("o"), t0));
        finish(&mut gate, end, t0);
        assert!(!admit(&mut gate, Some("o"), t0 + Duration::from_secs(86_400)), "no retry");
        reopen(&mut gate);
        assert!(admit(&mut gate, Some("o"), t0), "until Connect");
    }

    #[tokio::test]
    async fn a_join_that_lands_late_still_notifies() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "waiting"})),
            ("team.get", alone_on("t")),
            ("team.get", alone_on("t")),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "connecting");
        assert!(lock(ctx.memory).held.is_some(), "the line is kept");
        assert_eq!(load(dir.path()).unwrap().attempted.as_deref(), Some("o"));
        let mut gate = STARTING;
        finish(&mut gate, end, Instant::now());
        assert_eq!((gate.failures, gate.wait), (0, Some(AFTER_FAILURE)), "burns no backoff");

        let told = Mutex::new(Vec::new());
        assert_eq!(connect(&ctx, never_asked, |text| told.lock().unwrap().push(text)).await.state, "connected");
        assert_eq!(told.into_inner().unwrap(), ["Your computer joined Acme's network."]);
        let saved = load(dir.path()).unwrap();
        assert!(saved.attempted.is_none());
        assert_eq!(saved.notice.as_deref(), Some("Acme"));
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "connected", "once");
        assert_eq!(ops(&seen), ["team.get", "team.join", "team.get", "team.get"]);
    }

    #[tokio::test]
    async fn join_failure_keeps_no_ticket_in_status() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"state": "refused", "reason": format!("bad {TICKET}")})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "failed");
        assert!(end.soon);
        let notice = load(dir.path()).unwrap().notice;
        let shown = serde_json::to_string(&status(end.state, end.workspace, notice, (false, false))).unwrap();
        assert!(!shown.contains("SECRET"), "{shown}");
        let refused = join(&dir.path().join("sock"), &line(None), "mac").await;
        assert!(!format!("{refused:?}").contains("SECRET"), "{refused:?}");
        assert!(!std::fs::read_to_string(dir.path().join("network_connect.json")).unwrap().contains("SECRET"));
    }

    #[tokio::test]
    async fn alone_on_its_own_network_joins_saves_and_tells() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        let told = Mutex::new(Vec::new());
        let end = connect(&ctx, never_asked, |text| told.lock().unwrap().push(text)).await;
        assert_eq!((end.state, end.workspace.as_deref()), ("connected", Some("Acme")));
        assert_eq!(ops(&seen), ["team.get", "team.join"]);
        assert_eq!(told.into_inner().unwrap(), ["Your computer joined Acme's network."]);
        let saved = load(dir.path()).unwrap();
        assert_eq!(saved.joined.get("o").map(String::as_str), Some("t"));
        assert_eq!(saved.notice.as_deref(), Some("Acme"));
        assert!(saved.attempted.is_none());
    }

    #[tokio::test]
    async fn no_stored_team_fetches_the_line_first_and_may_find_it_is_done() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let seen = stub(dir.path(), vec![("team.get", alone_on("t"))]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "connected");
        assert_eq!(ops(&seen), ["team.get"], "the spare line is dropped, never sent");
        assert!(lock(ctx.memory).held.is_none());
        assert_eq!(load(dir.path()).unwrap().joined.get("o").map(String::as_str), Some("t"));
    }

    #[tokio::test]
    async fn a_person_with_no_workspace_gets_nothing_and_no_line() {
        let (dir, server, ctx) = fixture(json!({"workspace": null, "teamId": null, "userId": "u1", "name": null})).await;
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        Mock::given(method("POST")).respond_with(ResponseTemplate::new(500)).expect(0).mount(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "hidden");
        assert_eq!(ops(&seen), ["team.get"]);
    }

    #[tokio::test]
    async fn opted_out_touches_nothing() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        set(dir.path(), |saved| {
            saved.opt_out.insert("o".into(), true);
        });
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        Mock::given(method("POST")).respond_with(ResponseTemplate::new(500)).expect(0).mount(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(ops(&seen), ["team.get"]);
    }

    #[tokio::test]
    async fn a_person_who_left_while_the_app_was_closed_is_not_rejoined() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        set(dir.path(), |saved| {
            saved.joined.insert("o".into(), "t".into());
        });
        let seen = stub(dir.path(), vec![("team.get", alone_on("fresh"))]);
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(ops(&seen), ["team.get"]);
        assert_eq!(load(dir.path()).unwrap().opt_out.get("o"), Some(&true));
    }

    #[tokio::test]
    async fn not_now_opts_out_and_stays_on_the_other_network() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", with_others("x"))]);
        let asked = Mutex::new(String::new());
        let end = connect(
            &ctx,
            |_, text| {
                *asked.lock().unwrap() = text;
                async { Some(false) }
            },
            never_told,
        )
        .await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(asked.into_inner().unwrap(), "Your computer is on Friends. Join Acme's network instead?");
        assert_eq!(ops(&seen), ["team.get"]);
        assert_eq!(load(dir.path()).unwrap().opt_out.get("o"), Some(&true));
    }

    #[tokio::test]
    async fn join_on_the_ask_leaves_then_joins_with_the_computer_after_the_leave() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", with_others("x")),
            ("team.get", with_others("x")),
            ("team.get", alone_on("fresh")),
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "admitted"})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, |_, _| async { Some(true) }, |_| {}).await;
        assert_eq!(end.state, "connected");
        assert_eq!(ops(&seen), ["team.get", "team.get", "team.leave", "team.get", "team.join", "team.join"]);
        assert_eq!(seen.lock().unwrap()[5]["name"], "Ada (mac)");
        assert!(lock(ctx.memory).ask_yes.is_none(), "cleared on the join");
    }

    #[tokio::test]
    async fn a_network_that_changed_during_the_ask_stops_the_run() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", with_others("x")), ("team.get", with_others("y"))]);
        let end = connect(&ctx, |_, _| async { Some(true) }, never_told).await;
        assert_eq!(end.state, "failed");
        assert_eq!(ops(&seen), ["team.get", "team.get"], "no leave, no join");
    }

    #[tokio::test]
    async fn an_opt_out_landing_before_the_join_is_honoured() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", with_others("x")), ("team.get", with_others("x"))]);
        line_answers(&server).await;
        let end = connect(
            &ctx,
            |_, _| {
                set(dir.path(), |saved| {
                    saved.opt_out.insert("o".into(), true);
                });
                async { Some(true) }
            },
            never_told,
        )
        .await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(ops(&seen), ["team.get", "team.get"], "no leave, no join");
    }

    #[tokio::test]
    async fn yes_on_the_ask_with_no_line_leaves_nothing() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", with_others("x"))]);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(400))
            .mount(&server)
            .await;
        let end = connect(&ctx, |_, _| async { Some(true) }, never_told).await;
        assert_eq!(end.state, "failed");
        assert!(!ops(&seen).contains(&"team.leave".to_owned()));
    }

    #[tokio::test]
    async fn no_workspace_agents_yet_shows_nothing_and_does_not_hurry() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(409))
            .mount(&server)
            .await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!((end.state, end.soon), ("off", false));
    }

    #[tokio::test]
    async fn engine_off_is_off_before_any_request() {
        let (_dir, server, ctx) = fixture(acme(Some("t"))).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!((end.state, end.soon), ("off", true));
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn the_line_retries_a_503_and_not_a_409() {
        let (_dir, server, ctx) = fixture(acme(Some("t"))).await;
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(503).insert_header("Retry-After", "1"))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        line_answers(&server).await;
        assert_eq!(fetch_line(&ctx.api).await.ok().unwrap().ticket, TICKET);

        server.reset().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(409))
            .expect(1)
            .mount(&server)
            .await;
        assert_eq!(fetch_line(&ctx.api).await.err().unwrap().status, 409);
    }

    #[tokio::test]
    async fn a_redirect_does_not_carry_the_token() {
        let (_dir, server, ctx) = fixture(acme(Some("t"))).await;
        Mock::given(method("POST"))
            .and(path("/api/v1/network/join-line"))
            .respond_with(ResponseTemplate::new(302).insert_header("Location", format!("{}/elsewhere", server.uri())))
            .mount(&server)
            .await;
        Mock::given(path("/elsewhere")).respond_with(ResponseTemplate::new(200)).expect(0).mount(&server).await;
        assert_eq!(fetch_line(&ctx.api).await.err().unwrap().status, 302);
    }

    #[tokio::test]
    async fn every_request_carries_a_device_token_and_never_a_ticket_in_the_url() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        connect(&ctx, never_asked, |_| {}).await;
        for request in server.received_requests().await.unwrap() {
            let bearer = request.headers.get("authorization").unwrap().to_str().unwrap().to_owned();
            let claims = bearer.strip_prefix("Bearer ").unwrap().split('.').nth(1).unwrap().to_owned();
            let claims: Value = serde_json::from_slice(&base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(claims).unwrap()).unwrap();
            assert_eq!((claims["kind"].as_str(), claims["owner"].as_str()), (Some("device"), Some("o")));
            assert!(!request.url.as_str().contains("SECRET"));
        }
    }

    #[tokio::test]
    async fn sign_in_approves_the_engines_code_with_the_device_key() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let script = format!(r#"[ "$1 $2" = "login --app" ] && [ "$LIGHTBULB_API" = "{}/api/v1" ] || exit 9; echo '{{"user_code":"AB-CD"}}'"#, server.uri());
        fake_engine(dir.path(), &script);
        Mock::given(method("POST"))
            .and(path("/api/v1/network/device/approve-desktop"))
            .and(body_json(json!({"userCode": "AB-CD"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status": "approved"})))
            .expect(1)
            .mount(&server)
            .await;
        sign_in(&ctx.api, dir.path()).await.unwrap();
    }

    #[tokio::test]
    async fn an_older_engine_without_login_app_fails_and_the_join_goes_on() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        std::fs::remove_file(dir.path().join("login.json")).unwrap();
        fake_engine(dir.path(), "echo 'unexpected argument' >&2; exit 2");
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo")), ("team.join", json!({"state": "admitted"}))]);
        line_answers(&server).await;
        assert!(sign_in(&ctx.api, dir.path()).await.is_err());
        let end = connect(&ctx, never_asked, |_| {}).await;
        assert_eq!(end.state, "connected");
        assert_eq!(ops(&seen), ["team.get", "team.join"]);
        assert!(!load(dir.path()).unwrap().signed_in.contains_key("o"));
    }

    #[tokio::test]
    async fn not_a_person_on_the_join_is_held_with_no_retry_and_the_line_kept() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", alone_on("solo")),
            ("team.join", json!({"ok": false, "error": "bad_request", "reason": "not_a_person"})),
            ("team.get", alone_on("solo")),
            ("team.join", json!({"ok": false, "error": "bad_request", "reason": "not_a_person"})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!((end.state, end.stop, end.workspace.as_deref()), ("agentShell", true, Some("Acme")));
        assert!(lock(ctx.memory).held.is_some(), "the line is kept");
        let mut gate = STARTING;
        let t0 = Instant::now();
        assert!(admit(&mut gate, Some("o"), t0));
        finish(&mut gate, end, t0);
        assert_eq!((gate.failures, gate.wait), (0, None), "no backoff, no retry");

        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "agentShell", "Connect's yes lands on the same state");
        assert_eq!(ops(&seen), ["team.get", "team.join", "team.get", "team.join"]);
    }

    #[tokio::test]
    async fn not_a_person_on_the_leave_is_held_too() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", with_others("x")),
            ("team.get", with_others("x")),
            ("team.leave", json!({"ok": false, "error": "bad_request", "reason": "not_a_person"})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, |_, _| async { Some(true) }, never_told).await;
        assert_eq!(end.state, "agentShell");
        assert_eq!(ops(&seen), ["team.get", "team.get", "team.leave"]);
    }

    #[test]
    fn a_blip_keeps_this_pairs_workspace_only() {
        let mut live = STARTING;
        live.state = "failed";
        live.settled = "failed";
        live.workspace = Some("Old Co".into());
        live.workspace_owner = Some("old".into());
        let end = kept(&live, "new");
        assert_eq!((end.state, end.workspace), ("hidden", None));
        live.workspace_owner = Some("new".into());
        assert_eq!(kept(&live, "new").workspace.as_deref(), Some("Old Co"));
    }

    #[test]
    fn a_blip_is_no_failed_run_and_connecting_goes_back_to_what_it_settled_on() {
        let t0 = Instant::now();
        let mut live = STARTING;
        finish(&mut live, Ended { owner: Some("o".into()), ..ended("failed", Some("A")) }, t0);
        assert_eq!(live.failures, 1);
        let blip = kept(&live, "o");
        finish(&mut live, Ended { owner: Some("o".into()), ..blip }, t0);
        assert_eq!((live.failures, live.state, live.wait), (1, "failed", Some(AFTER_FAILURE)));
        finish(&mut live, Ended { owner: Some("o".into()), ..ended("connecting", Some("A")) }, t0);
        assert_eq!((live.state, live.settled), ("connecting", "failed"));
        let end = kept(&live, "o");
        assert_eq!(end.state, "failed");
    }

    #[tokio::test]
    async fn a_failed_team_call_runs_nothing_and_counts_as_a_blip() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        server.reset().await;
        Mock::given(path("/api/v1/network/team")).respond_with(ResponseTemplate::new(500)).mount(&server).await;
        let seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        let end = connect(&ctx, never_asked, never_told).await;
        assert!(end.blip);
        assert_eq!(ops(&seen), ["team.get"]);
    }

    #[tokio::test]
    async fn a_403_on_the_team_call_is_hidden_and_looks_again_soon() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        server.reset().await;
        Mock::given(path("/api/v1/network/team")).respond_with(ResponseTemplate::new(403)).mount(&server).await;
        let _seen = stub(dir.path(), vec![("team.get", alone_on("solo"))]);
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!((end.state, end.soon, end.blip), ("hidden", true, false));
    }

    #[tokio::test]
    async fn the_current_pairs_name_failing_gives_no_name_to_alert_with() {
        let (_dir, server, ctx) = fixture(acme(Some("t"))).await;
        server.reset().await;
        Mock::given(path("/api/v1/network/team")).respond_with(ResponseTemplate::new(500)).mount(&server).await;
        let stale = Some(("Old Co".to_owned(), "old".to_owned()));
        assert_eq!(workspace_name(&ctx.api, "o", stale).await, None);
    }

    #[tokio::test]
    async fn a_done_run_clears_a_yes_about_another_network() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let _seen = stub(dir.path(), vec![("team.get", alone_on("t"))]);
        lock(ctx.memory).ask_yes = Some(("o".into(), "x".into()));
        assert_eq!(connect(&ctx, never_asked, never_told).await.state, "connected");
        assert!(lock(ctx.memory).ask_yes.is_none());
    }
}
