//! Signs the engine in and joins the workspace's network, natively.
//!
//! Security rule: the window loads a hosted page, and script on it must never be able to join or
//! leave a network. So no command takes a ticket or a team; `network_connect` and `network_leave`
//! change nothing until a person answers a native alert the page cannot draw or click; `Status`
//! holds no ticket, team id or token; every error and log line here is a fixed string, never
//! `reqwest`'s text (it can hold a URL) or a server or engine body.
//!
//! Why the device key rather than Clerk: the page holds the Clerk session, so a route Clerk could
//! open is a route page script could open. `/network/join-line` refuses Clerk and takes a token
//! this process signs with the Keychain's CLI key, which the page never sees. No redirects are
//! followed, so a redirect cannot carry that token elsewhere.
//!
//! Which network comes from the Mac's pair, not the page. A page with another person's Clerk token
//! could re-pair the Mac, so every join posts a native notification naming the workspace.

use crate::{device_token, engine_client, keychain, pair_state};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::future::Future;
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
const JOIN_LIMIT: Duration = Duration::from_secs(200);
const HTTP_LIMIT: Duration = Duration::from_secs(10);
/// API Gateway cuts a request at 30 s, so a cold member answers 503 while it wakes.
const LINE_LIMIT: Duration = Duration::from_secs(40);
const LINE_WINDOW: Duration = Duration::from_secs(180);
const LINE_RETRY: u64 = 15;

const NOT_AVAILABLE: &str = "Not available on this Mac.";
const SERVICE_OFF: &str = "The background service is off.";
const COULD_NOT_LEAVE: &str = "Could not leave the network.";

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

/// Whether `login.json` holds a live proof for this person. The signature is the server's to check;
/// this only says whether signing in again is worth trying.
fn proof_is_good(login: Option<&[u8]>, user_id: &str, now: u64) -> bool {
    let claims = || -> Option<Value> {
        let login: Value = serde_json::from_slice(login?).ok()?;
        let middle = login.get("proof")?.as_str()?.split('.').nth(1)?;
        serde_json::from_slice(&base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(middle).ok()?).ok()
    };
    claims().is_some_and(|claims| {
        claims["sub"].as_str() == Some(user_id) && claims["exp"].as_f64().is_some_and(|exp| exp > now as f64)
    })
}

// --- `<app data>/network_connect.json` ---

#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct Saved {
    opt_out: HashMap<String, bool>,
    joined: HashMap<String, String>,
    notice: Option<String>,
}

/// Serializes read-modify-write: a dismissed notice must not lose an opt-out written beside it.
static FILE: Mutex<()> = Mutex::new(());

fn load(dir: &Path) -> Saved {
    std::fs::read(dir.join("network_connect.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

fn save(dir: &Path, saved: &Saved) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join("network_connect.json.tmp");
    std::fs::write(&tmp, serde_json::to_vec(saved)?)?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    std::fs::rename(&tmp, dir.join("network_connect.json"))
}

fn update(dir: &Path, change: impl FnOnce(&mut Saved)) {
    let _one_at_a_time = FILE.lock().unwrap_or_else(|e| e.into_inner());
    let mut saved = load(dir);
    change(&mut saved);
    if save(dir, &saved).is_err() {
        crate::log("[network] could not save network_connect.json");
    }
}

/// Connect also forgets the network it last joined: otherwise, alone on a new network of one,
/// `decide` would read the old id as "the person left" and opt straight out again.
fn clear_opt_out(saved: &mut Saved, owner: &str) {
    saved.opt_out.remove(owner);
    saved.joined.remove(owner);
}

// --- What is true now, and when to look again ---

/// All the page learns. No ticket, team id or token has a field here.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    state: &'static str,
    workspace: Option<String>,
    notice: Option<String>,
}

struct Live {
    state: &'static str,
    workspace: Option<String>,
    running: bool,
    again: bool,
    last: Option<Instant>,
    soon: bool,
    owner: Option<String>,
}

const STARTING: Live =
    Live { state: "hidden", workspace: None, running: false, again: false, last: None, soon: false, owner: None };
static LIVE: Mutex<Live> = Mutex::new(STARTING);

fn live() -> MutexGuard<'static, Live> {
    LIVE.lock().unwrap_or_else(|e| e.into_inner())
}

fn publish(state: &'static str, workspace: Option<&str>) {
    let mut live = live();
    live.state = state;
    live.workspace = workspace.map(str::to_owned);
}

/// Single flight; at most once per `EVERY`, `AFTER_FAILURE` after a failure, at once when the
/// pair's owner changed. An owner change during a run asks for another run after it.
fn admit(live: &mut Live, owner: Option<&str>, now: Instant) -> bool {
    let changed = live.owner.as_deref() != owner;
    if live.running {
        live.again |= changed;
        live.owner = owner.map(str::to_owned);
        return false;
    }
    let wait = if live.soon { AFTER_FAILURE } else { EVERY };
    if !changed && live.last.is_some_and(|last| now.duration_since(last) < wait) {
        return false;
    }
    live.owner = owner.map(str::to_owned);
    live.last = Some(now);
    live.running = true;
    true
}

struct Ended {
    state: &'static str,
    workspace: Option<String>,
    soon: bool,
}

fn ended(state: &'static str, workspace: Option<&str>) -> Ended {
    Ended { state, workspace: workspace.map(str::to_owned), soon: matches!(state, "failed" | "off") }
}

/// `true`: run again now, someone asked during that run.
fn finish(live: &mut Live, end: Ended, now: Instant) -> bool {
    live.state = end.state;
    live.workspace = end.workspace;
    live.soon = end.soon;
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

/// A bearer credential. No `Debug`: it cannot reach a format string by accident.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Line {
    ticket: String,
    team_id: String,
    name: Option<String>,
}

/// Retried on a 5xx or no answer, waiting `Retry-After` when given, for up to `LINE_WINDOW`.
async fn fetch_line(api: &Api) -> Result<Line, Refused> {
    let start = Instant::now();
    loop {
        match api.call(true, "/join-line", None, LINE_LIMIT).await {
            Ok(answer) => return serde_json::from_value(answer).map_err(|_| NO_ANSWER),
            Err(refused) if refused.status == 0 || refused.status >= 500 => {
                let wait = Duration::from_secs(refused.retry_after.unwrap_or(LINE_RETRY).max(1));
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

/// A failure here is logged and does not stop the join.
async fn ensure_signed_in(api: &Api, state: &Path, user_id: &str) {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |since| since.as_secs());
    let login = std::fs::read(state.join("login.json")).ok();
    if proof_is_good(login.as_deref(), user_id, now) {
        return;
    }
    if let Err(why) = sign_in(api, state).await {
        crate::log(&format!("[network] engine sign-in: {why}"));
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
    if answer["ok"] == true { Ok(()) } else { Err(COULD_NOT_LEAVE) }
}

async fn send_join(sock: &Path, ticket: &str, name: Option<&str>) -> Result<Value, &'static str> {
    let mut body = json!({"ticket": ticket});
    if let Some(name) = name {
        body["name"] = name.into();
    }
    engine_client::ask_at(sock, "team.join", Some(body), JOIN_LIMIT).await.map_err(|_| "the engine did not answer")
}

/// The person's first name; on `name_taken` the same ticket as `<Name> (<computer>)`; on `bad_name`
/// no name, the engine's own default. A blank name from the backend passes no name at all.
async fn join(sock: &Path, line: &Line, computer: &str) -> Result<(), &'static str> {
    let reason = |answer: &Value| answer["reason"].as_str().unwrap_or("").to_owned();
    let name = line.name.as_deref().map(str::trim).filter(|name| !name.is_empty());
    let mut answer = send_join(sock, &line.ticket, name).await?;
    if let (Some(name), "name_taken", false) = (name, reason(&answer).as_str(), computer.is_empty()) {
        answer = send_join(sock, &line.ticket, Some(&format!("{name} ({computer})"))).await?;
    }
    if name.is_some() && reason(&answer) == "bad_name" {
        answer = send_join(sock, &line.ticket, None).await?;
    }
    if answer["state"] == "admitted" {
        return Ok(());
    }
    Err(match (answer["state"].as_str(), reason(&answer).as_str()) {
        (_, "already_in_team") => "already in a team",
        (_, "name_taken") => "the name is taken",
        (_, "bad_name") => "the name was refused",
        (Some("waiting"), _) => "waiting to be admitted",
        _ => "the engine refused",
    })
}

// --- One run ---

struct Ctx {
    api: Api,
    /// The engine's state directory: `sock`, `login.json`, `bin/lightbulb`.
    state: PathBuf,
    /// The app's data directory.
    data: PathBuf,
    owner: String,
}

fn opt_out(ctx: &Ctx, workspace: &str) -> Ended {
    update(&ctx.data, |saved| {
        saved.opt_out.insert(ctx.owner.clone(), true);
    });
    ended("optedOut", Some(workspace))
}

fn line_failed(refused: Refused, workspace: &str) -> Ended {
    match refused.status {
        // Not this person's workspace after all.
        403 => ended("hidden", None),
        // No workspace agents yet: nothing to connect to, and no button would help.
        409 => Ended { state: "off", workspace: Some(workspace.to_owned()), soon: false },
        _ => {
            crate::log(&format!("[network] join line: status {}", refused.status));
            ended("failed", Some(workspace))
        }
    }
}

/// Steps 3 and 4 after the decision or the person's yes. `leave` comes after the line, so a line
/// that does not come costs the person nothing. The ticket is dropped on return, whatever happens.
async fn join_workspace(
    ctx: &Ctx,
    team: &Team,
    workspace: &str,
    line: Option<Line>,
    leave: bool,
    computer: String,
    tell: impl Fn(String),
) -> Ended {
    let sock = ctx.state.join("sock");
    publish("connecting", Some(workspace));
    ensure_signed_in(&ctx.api, &ctx.state, &team.user_id).await;
    let line = match line {
        Some(line) => line,
        None => match fetch_line(&ctx.api).await {
            Ok(line) => line,
            Err(refused) => return line_failed(refused, workspace),
        },
    };
    let mut computer = computer;
    if leave {
        // From here the next run must read "alone" as a join, not as the person having left.
        update(&ctx.data, |saved| {
            saved.joined.remove(&ctx.owner);
        });
        if leave_team(&sock).await.is_err() {
            crate::log("[network] leaving the other network failed");
            return ended("failed", Some(workspace));
        }
        computer = read_engine(&sock).await.map_or(String::new(), |now| now.computer);
    }
    match join(&sock, &line, &computer).await {
        Ok(()) => {
            update(&ctx.data, |saved| {
                saved.joined.insert(ctx.owner.clone(), line.team_id.clone());
                saved.notice = Some(workspace.to_owned());
            });
            tell(format!("Your computer joined {workspace}'s network."));
            ended("connected", Some(workspace))
        }
        Err(why) => {
            crate::log(&format!("[network] join: {why}"));
            ended("failed", Some(workspace))
        }
    }
}

/// `ask_person` shows the Ask text and answers yes or no; `tell` posts the notification. Both are
/// native, and passed in so this runs without Tauri in tests.
async fn connect<A, F>(ctx: &Ctx, ask_person: A, tell: impl Fn(String)) -> Ended
where
    A: FnOnce(String) -> F,
    F: Future<Output = bool>,
{
    let sock = ctx.state.join("sock");
    let Some(engine) = read_engine(&sock).await else {
        return ended("off", None);
    };
    let team: Team = match ctx.api.call(false, "/team", None, HTTP_LIMIT).await.map(serde_json::from_value) {
        Ok(Ok(team)) => team,
        Err(Refused { status: 403, .. }) => return ended("hidden", None),
        // Offline or a blip says nothing new: keep what is shown, look again soon.
        _ => {
            let live = live();
            return Ended { state: live.state, workspace: live.workspace.clone(), soon: true };
        }
    };
    let Some(workspace) = team.workspace.clone().filter(|workspace| !workspace.is_empty()) else {
        return ended("hidden", None);
    };
    let saved = load(&ctx.data);
    let opted_out = saved.opt_out.get(&ctx.owner) == Some(&true);
    let mut ws_team = team.team_id.clone().filter(|id| !id.is_empty());
    let mut line = None;
    if ws_team.is_none() && !opted_out {
        publish("connecting", Some(&workspace));
        match fetch_line(&ctx.api).await {
            Ok(fetched) => {
                ws_team = Some(fetched.team_id.clone());
                line = Some(fetched);
            }
            Err(refused) => return line_failed(refused, &workspace),
        }
    }
    let joined_before = saved.joined.get(&ctx.owner).map(String::as_str);
    match decide(&engine.id, engine.alone, engine.removed, ws_team.as_deref(), opted_out, joined_before) {
        Action::Done => {
            ensure_signed_in(&ctx.api, &ctx.state, &team.user_id).await;
            // Remembered even when the CLI did the joining, so a later `lightbulb leave` reads as one.
            update(&ctx.data, |saved| {
                saved.joined.insert(ctx.owner.clone(), engine.id.clone());
            });
            ended("connected", Some(&workspace))
        }
        Action::Stay => ended("optedOut", Some(&workspace)),
        Action::OptOut => opt_out(ctx, &workspace),
        Action::Join => join_workspace(ctx, &team, &workspace, line, false, engine.computer, tell).await,
        Action::Ask => {
            let on = engine.name.as_deref().unwrap_or("another network");
            if !ask_person(format!("Your computer is on {on}. Join {workspace}'s network instead?")).await {
                return opt_out(ctx, &workspace);
            }
            join_workspace(ctx, &team, &workspace, line, true, engine.computer, tell).await
        }
    }
}

async fn run(app: &tauri::AppHandle) -> Ended {
    let Ok(data) = app.path().app_data_dir() else {
        return ended("hidden", None);
    };
    let Some(pair) = pair_state::load(&data) else {
        return ended("hidden", None);
    };
    let Ok(state) = engine_client::state_dir() else {
        return ended("off", None);
    };
    let Ok(key) = keychain::load(keychain::KeychainSlot::Cli, crate::KEYCHAIN_SERVICE) else {
        crate::log("[network] no device key");
        return ended("hidden", None);
    };
    let ctx = Ctx {
        api: Api {
            base: crate::LIGHTBULB_API_BASE.to_owned(),
            key_pem: key.private_key_pem,
            device_id: pair.device_id_cli,
            owner: pair.owner_id.clone(),
        },
        state,
        data,
        owner: pair.owner_id,
    };
    let asking = app.clone();
    let ask_person =
        |text: String| async move { crate::confirm_native_labelled(&asking, "Lightbulb", text, "Join", "Not now").await };
    let tell = |text: String| {
        tauri::async_runtime::spawn(crate::notify::show("Lightbulb".into(), Some(text), None, None, None));
    };
    connect(&ctx, ask_person, tell).await
}

/// Cheap and returns at once: `send_auth_token` calls it about every minute.
pub fn kick(app: &tauri::AppHandle) {
    let owner = app.path().app_data_dir().ok().and_then(|data| pair_state::load(&data)).map(|pair| pair.owner_id);
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

fn kick_now(app: &tauri::AppHandle) {
    {
        let mut live = live();
        live.last = None;
        live.again |= live.running;
    }
    kick(app);
}

// --- Commands: none takes an argument from the page ---

/// The workspace as the backend last named it, and only while there is one: a person with no
/// workspace gets no alert at all.
fn known_workspace() -> Option<String> {
    let live = live();
    (live.state != "hidden").then(|| live.workspace.clone()).flatten()
}

fn paired(app: &tauri::AppHandle) -> Option<(PathBuf, String)> {
    let data = app.path().app_data_dir().ok()?;
    let owner = pair_state::load(&data)?.owner_id;
    Some((data, owner))
}

#[tauri::command]
pub fn network_status(app: tauri::AppHandle) -> Status {
    let (state, workspace) = {
        let live = live();
        (live.state, live.workspace.clone())
    };
    let notice = app.path().app_data_dir().ok().filter(|_| state != "hidden").and_then(|data| load(&data).notice);
    Status { state, workspace, notice }
}

#[tauri::command]
pub async fn network_connect(app: tauri::AppHandle) -> Result<(), String> {
    let workspace = known_workspace().ok_or(NOT_AVAILABLE)?;
    let (data, owner) = paired(&app).ok_or(NOT_AVAILABLE)?;
    let text = format!("Connect this computer to {workspace}'s network?");
    if !crate::confirm_native(&app, "Lightbulb", text).await {
        return Err(crate::DECLINED_ON_MAC.to_owned());
    }
    update(&data, |saved| clear_opt_out(saved, &owner));
    kick_now(&app);
    Ok(())
}

// ponytail: a Leave during a run's join can lose to it; the next run shows "connected" and Leave works again.
#[tauri::command]
pub async fn network_leave(app: tauri::AppHandle) -> Result<(), String> {
    let workspace = known_workspace().ok_or(NOT_AVAILABLE)?;
    let (data, owner) = paired(&app).ok_or(NOT_AVAILABLE)?;
    let sock = engine_client::state_dir().map_err(|_| SERVICE_OFF)?.join("sock");
    let team = read_engine(&sock).await.ok_or(SERVICE_OFF)?.name.unwrap_or_else(|| "this network".to_owned());
    if !crate::confirm_native(&app, "Lightbulb", format!("Leave {team}?")).await {
        return Err(crate::DECLINED_ON_MAC.to_owned());
    }
    leave_team(&sock).await?;
    update(&data, |saved| {
        saved.opt_out.insert(owner.clone(), true);
        saved.notice = None;
    });
    publish("optedOut", Some(&workspace));
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
        clear_opt_out(&mut saved, "o");
        let before = saved.joined.get("o").map(String::as_str);
        assert_eq!(decide("fresh", true, false, Some("t"), saved.opt_out.contains_key("o"), before), Action::Join);
    }

    fn proof(claims: Value) -> Vec<u8> {
        let middle = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(claims.to_string());
        json!({"proof": format!("h.{middle}.s")}).to_string().into_bytes()
    }

    #[test]
    fn proof_needs_login() {
        assert!(proof_is_good(Some(&proof(json!({"sub": "u1", "exp": 200}))), "u1", 100));
        assert!(!proof_is_good(None, "u1", 100), "missing");
        assert!(!proof_is_good(Some(&proof(json!({"sub": "u1", "exp": 99}))), "u1", 100), "expired");
        assert!(!proof_is_good(Some(&proof(json!({"sub": "u2", "exp": 200}))), "u1", 100), "other sub");
        assert!(!proof_is_good(Some(b"{\"proof\":\"nodots\"}"), "u1", 100), "not a JWS");
        assert!(!proof_is_good(Some(b"not json"), "u1", 100));
    }

    #[test]
    fn opt_out_survives_reload() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        update(dir.path(), |saved| {
            saved.opt_out.insert("o".into(), true);
            saved.joined.insert("o".into(), "t".into());
            saved.notice = Some("Acme".into());
        });
        let file = dir.path().join("network_connect.json");
        assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
        let text = std::fs::read_to_string(&file).unwrap();
        assert!(text.contains("\"optOut\"") && text.contains("\"joined\"") && text.contains("\"notice\""));
        let again = load(dir.path());
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
        assert!(!finish(&mut live, ended("failed", Some("A")), t0 + EVERY));
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
    fn status_serializes_only_its_three_fields() {
        let status = Status { state: "failed", workspace: Some("Acme".into()), notice: None };
        assert_eq!(serde_json::to_value(status).unwrap(), json!({"state": "failed", "workspace": "Acme", "notice": null}));
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
        Line { ticket: TICKET.into(), team_id: "t".into(), name: name.map(str::to_owned) }
    }

    #[tokio::test]
    async fn join_retries_name_taken_with_the_computer() {
        let dir = tempfile::tempdir().unwrap();
        let seen = stub(dir.path(), vec![
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "admitted"})),
        ]);
        join(&dir.path().join("sock"), &line(Some("Ada")), "mac").await.unwrap();
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
        let refused = join(&dir.path().join("sock"), &line(Some("Ada")), "mac!").await.unwrap_err();
        assert_eq!(refused, "the name was refused");
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 3);
        assert_eq!(seen[2], json!({"id": 1, "op": "team.join", "ticket": TICKET}));
    }

    #[tokio::test]
    async fn a_blank_name_passes_no_name() {
        let dir = tempfile::tempdir().unwrap();
        let seen = stub(dir.path(), vec![("team.join", json!({"state": "admitted"}))]);
        join(&dir.path().join("sock"), &line(Some("  ")), "mac").await.unwrap();
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
                json!({"ticket": TICKET, "teamId": "t", "workspace": "Acme", "name": "Ada", "expiresAt": 1}),
            ))
            .mount(server)
            .await;
    }

    async fn never_asked(_: String) -> bool {
        panic!("nothing should ask the person here")
    }

    fn never_told(_: String) {
        panic!("nothing should notify here")
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
        let status = serde_json::to_string(&Status { state: end.state, workspace: end.workspace, notice: load(dir.path()).notice }).unwrap();
        assert!(!status.contains("SECRET"), "{status}");
        let refused = join(&dir.path().join("sock"), &line(None), "mac").await.unwrap_err();
        assert!(!refused.contains("SECRET"), "{refused}");
        assert!(!std::fs::read_to_string(dir.path().join("network_connect.json")).unwrap_or_default().contains("SECRET"));
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
        let saved = load(dir.path());
        assert_eq!(saved.joined.get("o").map(String::as_str), Some("t"));
        assert_eq!(saved.notice.as_deref(), Some("Acme"));
    }

    #[tokio::test]
    async fn no_stored_team_fetches_the_line_first_and_may_find_it_is_done() {
        let (dir, server, ctx) = fixture(acme(None)).await;
        let seen = stub(dir.path(), vec![("team.get", alone_on("t"))]);
        line_answers(&server).await;
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "connected");
        assert_eq!(ops(&seen), ["team.get"], "the spare line is dropped, never sent");
        assert_eq!(load(dir.path()).joined.get("o").map(String::as_str), Some("t"));
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
        update(dir.path(), |saved| {
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
        update(dir.path(), |saved| {
            saved.joined.insert("o".into(), "t".into());
        });
        let seen = stub(dir.path(), vec![("team.get", alone_on("fresh"))]);
        let end = connect(&ctx, never_asked, never_told).await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(ops(&seen), ["team.get"]);
        assert_eq!(load(dir.path()).opt_out.get("o"), Some(&true));
    }

    #[tokio::test]
    async fn not_now_opts_out_and_stays_on_the_other_network() {
        let (dir, _server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![("team.get", with_others("x"))]);
        let asked = Mutex::new(String::new());
        let end = connect(
            &ctx,
            |text| {
                *asked.lock().unwrap() = text;
                async { false }
            },
            never_told,
        )
        .await;
        assert_eq!(end.state, "optedOut");
        assert_eq!(asked.into_inner().unwrap(), "Your computer is on Friends. Join Acme's network instead?");
        assert_eq!(ops(&seen), ["team.get"]);
        assert_eq!(load(dir.path()).opt_out.get("o"), Some(&true));
    }

    #[tokio::test]
    async fn join_on_the_ask_leaves_then_joins_with_the_computer_after_the_leave() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        let seen = stub(dir.path(), vec![
            ("team.get", with_others("x")),
            ("team.get", alone_on("fresh")),
            ("team.join", json!({"state": "refused", "reason": "name_taken"})),
            ("team.join", json!({"state": "admitted"})),
        ]);
        line_answers(&server).await;
        let end = connect(&ctx, |_| async { true }, |_| {}).await;
        assert_eq!(end.state, "connected");
        assert_eq!(ops(&seen), ["team.get", "team.leave", "team.get", "team.join", "team.join"]);
        assert_eq!(seen.lock().unwrap()[4]["name"], "Ada (mac)");
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
        let end = connect(&ctx, |_| async { true }, never_told).await;
        assert_eq!(end.state, "failed");
        assert_eq!(ops(&seen), ["team.get"]);
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

    fn fake_engine(state: &Path, script: &str) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(state.join("bin")).unwrap();
        let bin = state.join("bin/lightbulb");
        std::fs::write(&bin, format!("#!/bin/sh\n{script}\n")).unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    #[tokio::test]
    async fn sign_in_approves_the_engines_code_with_the_device_key() {
        let (dir, server, ctx) = fixture(acme(Some("t"))).await;
        fake_engine(dir.path(), r#"[ "$1 $2" = "login --app" ] || exit 9; echo '{"user_code":"AB-CD"}'"#);
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
    }
}
