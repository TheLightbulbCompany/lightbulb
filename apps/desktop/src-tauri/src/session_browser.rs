//! The Lightbulb browser for shared Claude Code and Codex sessions (spec 2026-10-11).
//!
//! A fixed loopback port speaks the slice of Chrome's DevTools surface chrome-devtools-mcp and
//! Playwright MCP use: `GET /json/version` (with or without a trailing slash), `/json/list`, and the
//! browser WebSocket. A connection is identified by its peer process, walked up to one shared
//! session (anything else is refused with `NOT_SHARED` or the ambiguity message), and offered to the
//! backend (`browser.cdp.offer`). The backend answers `browser.cdp.open` (an `agent_chrome` channel
//! whose Chrome side is this connection) or `browser.cdp.close` with the message the client is shown.
//! The socket stays open for the client's whole connection. 300 s after its last command this sends
//! `browser.cdp.idle` (the backend releases the lease, saving); the next command is held, with any
//! that follow, while `browser.cdp.active` re-acquires it, and runs once `browser.cdp.leased` says
//! so (or each gets a CDP error naming the code). A connection closes only when its client leaves,
//! its session ends or stops being shared, or Browser is switched off. The person's everyday Chrome
//! is never involved.

/// What a session's CDP client reads when the Lightbulb browser refuses it.
pub(crate) const NOT_SHARED: &str = "Share this session to use the Lightbulb browser";

use crate::agent_chrome::Channels;
use crate::holder::identity::{self, Identity};
use crate::node_client::EventSender;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;
use tauri::Manager;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

pub(crate) const PORT: u16 = 39222;
/// No command for this long: the backend releases the session's lease (and saves its sign-ins).
pub(crate) const IDLE_RELEASE: Duration = Duration::from_secs(300);
/// The backend's answer to an offer or an `active`: it may wake the runtime (~55 s cold) and run a
/// sign-in sync first.
const OFFER_WAIT: Duration = Duration::from_secs(150);
/// Commands held before an answer (the offer's, or a re-acquire's); more are answered with an error.
const MAX_EARLY: usize = 256;
const NO_ANSWER: &str = "The Lightbulb browser did not answer; try again";
const OVERFLOW: &str = "Too many commands while the Lightbulb browser loads; try again";
const LIVE_CHECK: Duration = Duration::from_secs(30);
const WS_PATH: &str = "/devtools/browser/lightbulb";
const NOT_CONNECTED: &str = "Lightbulb on this Mac is not connected to its workspace yet";

type Ws = tokio_tungstenite::WebSocketStream<TcpStream>;

pub(crate) enum Decision {
    Chrome(mpsc::Sender<String>),
    Refused(String),
}

struct Pending {
    to_client: mpsc::Sender<String>,
    decide: oneshot::Sender<Decision>,
}

/// Offers awaiting the backend's answer, by channel.
static PENDING: LazyLock<Mutex<HashMap<String, Pending>>> = LazyLock::new(Default::default);
/// Open connections, by channel: where the backend's lease answers go.
static OPEN: LazyLock<Mutex<HashMap<String, mpsc::Sender<Result<(), String>>>>> = LazyLock::new(Default::default);

/// A connection's lease as the Mac sees it: commands pass while it is held; after idle the first
/// command asks for it back (`Wake`) and it and those after it wait (`Hold`) for the answer.
#[derive(Default)]
struct Gate {
    idle: bool,
    waking: bool,
    held: Vec<String>,
}

enum Step {
    Forward(String),
    Wake,
    Hold,
    Overflow(String),
}

impl Gate {
    fn command(&mut self, text: String) -> Step {
        if self.waking {
            if self.held.len() >= MAX_EARLY {
                return Step::Overflow(text);
            }
            self.held.push(text);
            return Step::Hold;
        }
        if self.idle {
            (self.idle, self.waking) = (false, true);
            self.held.push(text);
            return Step::Wake;
        }
        Step::Forward(text)
    }

    fn active(&self) -> bool {
        !self.idle && !self.waking
    }

    fn waking(&self) -> bool {
        self.waking
    }

    fn went_idle(&mut self) {
        self.idle = true;
    }

    /// The backend's answer: the held commands, in order, to run (`ok`) or to refuse. A late `ok`
    /// after the Mac gave up still makes it active: the backend holds the lease then.
    fn leased(&mut self, ok: bool) -> Vec<String> {
        (self.waking, self.idle) = (false, !ok);
        std::mem::take(&mut self.held)
    }
}

/// A command's CDP error answer (an event has none).
fn cdp_error(text: &str, message: &str) -> Option<String> {
    serde_json::from_str::<Value>(text)
        .ok()
        .and_then(|v| v.get("id").cloned())
        .map(|id| json!({"id": id, "error": {"code": -32000, "message": message}}).to_string())
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn log(msg: &str) {
    crate::log(&format!("[session-browser] {msg}"));
}

pub(crate) fn http_response(path: &str) -> (u16, String) {
    match path.trim_end_matches('/') {
        "/json/version" => (
            200,
            json!({"Browser": "Lightbulb/1", "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": format!("ws://127.0.0.1:{PORT}{WS_PATH}")})
            .to_string(),
        ),
        "/json/list" | "/json" => (200, "[]".into()),
        _ => (404, "{}".into()),
    }
}

/// A web page can reach loopback too, and a browser always sends Origin on a WebSocket handshake;
/// a CDP client (Node `ws`, Playwright) never does. Same rule as `proxy.rs`.
fn no_browser(req: &Request, resp: Response) -> Result<Response, ErrorResponse> {
    if req.headers().contains_key("origin") {
        return Err(tokio_tungstenite::tungstenite::http::Response::builder().status(403).body(None).unwrap());
    }
    Ok(resp)
}

/// Bind the port, or say loudly why not: another program there would be the browser a session drives.
async fn bind_or_report(port: u16, report: impl FnOnce(String)) -> Option<TcpListener> {
    match TcpListener::bind(("127.0.0.1", port)).await {
        Ok(listener) => Some(listener),
        Err(e) => {
            log(&format!("port {port} unavailable: {e}"));
            report(format!(
                "Port {port} is in use by another program, so Claude Code and Codex sessions cannot use the \
                 Lightbulb browser. Quit that program, then reopen Lightbulb."
            ));
            None
        }
    }
}

pub async fn serve(app: tauri::AppHandle) {
    let Some(listener) = bind_or_report(PORT, |text| {
        tauri::async_runtime::spawn(async move {
            crate::notify::native_notify("Lightbulb".into(), Some(text), None, None, None).await;
        });
    })
    .await
    else {
        return;
    };
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                tokio::spawn(connection(app.clone(), stream));
            }
            Err(e) => log(&format!("accept failed: {e}")),
        }
    }
}

async fn connection(app: tauri::AppHandle, mut stream: TcpStream) {
    let (Ok(local), Ok(peer)) = (stream.local_addr(), stream.peer_addr()) else { return };
    let mut head = [0u8; 4096];
    let Ok(n) = stream.peek(&mut head).await else { return };
    let path = String::from_utf8_lossy(&head[..n]).split_whitespace().nth(1).unwrap_or("").to_string();
    if !path.starts_with("/devtools/browser/") {
        let (status, body) = http_response(&path);
        let _ = stream.read(&mut head).await; // read the request, so closing never resets the answer
        let reply = format!(
            "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            if status == 200 { "OK" } else { "Not Found" },
            body.len()
        );
        let _ = stream.write_all(reply.as_bytes()).await;
        let _ = stream.shutdown().await;
        return;
    }
    let source = {
        let app = app.clone();
        tokio::task::spawn_blocking(move || source_of(&app, local, peer))
            .await
            .unwrap_or_else(|_| Err(NOT_SHARED.into()))
    };
    let Ok(ws) = tokio_tungstenite::accept_hdr_async(stream, no_browser).await else { return };
    match source {
        Ok(session) => run(app, ws, session).await,
        Err(message) => refuse(ws, Vec::new(), &message).await,
    }
}

fn source_of(app: &tauri::AppHandle, local: SocketAddr, peer: SocketAddr) -> Result<String, String> {
    let chain = peer_pid(local, peer).map(ancestors).unwrap_or_default();
    if chain.is_empty() {
        return Err(NOT_SHARED.into());
    }
    let scope = crate::local_sessions::enrollment::session_scope(app).map_err(|_| NOT_SHARED.to_string())?;
    crate::local_sessions::shared_session_for(&scope, &chain)
}

async fn still_shared(app: &tauri::AppHandle, session: &str) -> bool {
    let (app, session) = (app.clone(), session.to_string());
    tokio::task::spawn_blocking(move || {
        crate::local_sessions::enrollment::session_scope(&app)
            .is_ok_and(|scope| crate::local_sessions::session_still_shared(&scope, &session))
    })
    .await
    .unwrap_or(false)
}

fn current(app: &tauri::AppHandle) -> Option<(Channels, EventSender)> {
    let mut found = None;
    crate::with_agent_chrome_channels(app, |c| found = Some((c.clone(), c.out())));
    found
}

/// Each command's CDP error answer; false when the client is gone.
async fn answer_all(sink: &mut SplitSink<Ws, Message>, texts: Vec<String>, message: &str) -> bool {
    for reply in texts.iter().filter_map(|text| cdp_error(text, message)) {
        if sink.send(Message::Text(reply.into())).await.is_err() {
            return false;
        }
    }
    true
}

async fn run(app: tauri::AppHandle, ws: Ws, session: String) {
    let Some((channels, out)) = current(&app) else {
        return refuse(ws, Vec::new(), NOT_CONNECTED).await;
    };
    let id = uuid::Uuid::new_v4().simple().to_string();
    let browser_on = app.path().app_data_dir().is_ok_and(|d| crate::desktop_permissions::load_from(&d).browser);
    let (to_client, mut from_upstream) = mpsc::channel::<String>(1024);
    let (decide, decided) = oneshot::channel();
    lock(&PENDING).insert(id.clone(), Pending { to_client, decide });
    let offer = json!({"channel": id, "sessionId": session, "macBrowser": browser_on});
    if out.send("browser.cdp.offer", offer).await.is_err() {
        lock(&PENDING).remove(&id);
        return refuse(ws, Vec::new(), NOT_CONNECTED).await;
    }
    let (mut sink, mut stream) = ws.split();
    let mut early = Vec::new();
    let decided = tokio::time::timeout(OFFER_WAIT, decided);
    tokio::pin!(decided);
    let upstream = loop {
        tokio::select! {
            answer = &mut decided => match answer {
                Ok(Ok(Decision::Chrome(tx))) => break tx,
                Ok(Ok(Decision::Refused(message))) => return refuse_split(sink, stream, early, &message).await,
                _ => {
                    lock(&PENDING).remove(&id);
                    let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "TIMEOUT"})).await;
                    return refuse_split(sink, stream, early, NO_ANSWER).await;
                }
            },
            msg = stream.next() => match msg {
                Some(Ok(Message::Text(text))) if early.len() < MAX_EARLY => early.push(text.to_string()),
                Some(Ok(Message::Text(text))) => {
                    if !answer_all(&mut sink, vec![text.to_string()], OVERFLOW).await {
                        lock(&PENDING).remove(&id);
                        if !channels.end_local(&id) {
                            let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "CLOSED"})).await;
                        }
                        return;
                    }
                }
                Some(Ok(_)) => {}
                _ => {
                    // The client left before the answer: whatever the backend took, it releases.
                    lock(&PENDING).remove(&id);
                    if !channels.end_local(&id) {
                        let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "CLOSED"})).await;
                    }
                    return;
                }
            },
        }
    };
    let (leased, mut answers) = mpsc::channel::<Result<(), String>>(4);
    lock(&OPEN).insert(id.clone(), leased);
    let mut open = true;
    for text in early {
        open = open && upstream.send(text).await.is_ok();
    }
    let mut gate = Gate::default();
    let far = || tokio::time::Instant::now() + Duration::from_secs(365 * 24 * 3600);
    let idle = tokio::time::sleep(IDLE_RELEASE);
    let wake = tokio::time::sleep_until(far());
    tokio::pin!(idle, wake);
    let mut check = tokio::time::interval(LIVE_CHECK);
    check.tick().await;
    while open {
        tokio::select! {
            msg = stream.next() => match msg {
                Some(Ok(Message::Text(text))) => {
                    idle.as_mut().reset(tokio::time::Instant::now() + IDLE_RELEASE);
                    match gate.command(text.to_string()) {
                        Step::Forward(text) => open = upstream.send(text).await.is_ok(),
                        Step::Wake => {
                            wake.as_mut().reset(tokio::time::Instant::now() + OFFER_WAIT);
                            open = out.send("browser.cdp.active", json!({"channel": id})).await.is_ok();
                        }
                        Step::Hold => {}
                        Step::Overflow(text) => open = answer_all(&mut sink, vec![text], OVERFLOW).await,
                    }
                }
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => open = false,
                Some(Ok(_)) => {}
            },
            text = from_upstream.recv() => match text {
                Some(text) => open = sink.send(Message::Text(text.into())).await.is_ok(),
                None => open = false, // the channel ended: Chrome quit, Browser off, or the backend closed it
            },
            Some(answer) = answers.recv() => {
                wake.as_mut().reset(far());
                match answer {
                    Ok(()) => {
                        for text in gate.leased(true) {
                            open = open && upstream.send(text).await.is_ok();
                        }
                    }
                    Err(message) => open = answer_all(&mut sink, gate.leased(false), &message).await,
                }
                idle.as_mut().reset(tokio::time::Instant::now() + IDLE_RELEASE);
            }
            _ = &mut wake, if gate.waking() => {
                wake.as_mut().reset(far());
                open = answer_all(&mut sink, gate.leased(false), NO_ANSWER).await;
            }
            _ = &mut idle, if gate.active() => {
                // The lease goes back (and the sign-ins are saved); the socket and Chrome stay.
                gate.went_idle();
                open = out.send("browser.cdp.idle", json!({"channel": id})).await.is_ok();
            }
            _ = check.tick() => open = still_shared(&app, &session).await,
        }
    }
    lock(&OPEN).remove(&id);
    channels.end_local(&id);
    let _ = sink.close().await;
}

async fn refuse(ws: Ws, early: Vec<String>, message: &str) {
    let (sink, stream) = ws.split();
    refuse_split(sink, stream, early, message).await
}

/// Tell a refused client why, in CDP's own error shape (puppeteer and Playwright surface the
/// message), for the commands it already sent and those of the next second; then close.
async fn refuse_split(mut sink: SplitSink<Ws, Message>, mut stream: SplitStream<Ws>, early: Vec<String>, message: &str) {
    let answer = |text: &str| cdp_error(text, message);
    for reply in early.iter().filter_map(|text| answer(text)) {
        let _ = sink.send(Message::Text(reply.into())).await;
    }
    let _ = tokio::time::timeout(Duration::from_secs(1), async {
        while let Some(Ok(Message::Text(text))) = stream.next().await {
            if let Some(reply) = answer(&text) {
                let _ = sink.send(Message::Text(reply.into())).await;
            }
        }
    })
    .await;
    // A close reason is at most 123 bytes.
    let reason: String = message.char_indices().take_while(|(i, c)| i + c.len_utf8() <= 120).map(|(_, c)| c).collect();
    let _ = sink.send(Message::Close(Some(CloseFrame { code: CloseCode::Policy, reason: reason.into() }))).await;
}

fn clean(text: &str) -> String {
    text.chars().filter(|c| !c.is_control()).take(300).collect()
}

fn refusal(code: &str) -> &'static str {
    match code {
        "CAPACITY" => "This Mac's browser is busy; try again shortly.",
        "BROWSER_OFF" => "Browser access on this Mac is off in Lightbulb's Settings.",
        _ => "The Lightbulb browser is not available right now",
    }
}

/// Backend events for this module's channels. node_invoke calls it before `Channels::on_event`:
/// true when handled here.
pub(crate) fn on_event(channels: &Channels, event: &str, payload: &Value) -> bool {
    let Some(id) = payload.get("channel").and_then(Value::as_str) else { return false };
    match event {
        "browser.cdp.open" => {
            let Some(pending) = lock(&PENDING).remove(id) else { return false };
            let profile = payload.get("profileId").and_then(Value::as_str).unwrap_or("");
            let decision = match channels.open_local(id, profile, pending.to_client) {
                Ok(tx) => Decision::Chrome(tx),
                Err(code) => {
                    channels.send_close(id, code);
                    Decision::Refused(refusal(code).into())
                }
            };
            let _ = pending.decide.send(decision);
            true
        }
        "browser.cdp.close" => {
            let Some(pending) = lock(&PENDING).remove(id) else { return false };
            let message = payload.get("message").and_then(Value::as_str).map(clean).filter(|m| !m.is_empty());
            let _ = pending.decide.send(Decision::Refused(message.unwrap_or_else(|| refusal("").into())));
            true
        }
        "browser.cdp.leased" => {
            let Some(leased) = lock(&OPEN).get(id).cloned() else { return false };
            let answer = if payload.get("ok").and_then(Value::as_bool) == Some(true) {
                Ok(())
            } else {
                let code = payload.get("code").and_then(Value::as_str).map(clean).unwrap_or_default();
                let message = payload.get("message").and_then(Value::as_str).map(clean).unwrap_or_default();
                Err(format!("{code}: {message}"))
            };
            let _ = leased.try_send(answer);
            true
        }
        _ => false,
    }
}

/// `struct socket_fdinfo` (<sys/proc_info.h>): libc has the call but not the struct. These offsets
/// are pinned by `peer_pid_finds_this_processs_own_client` (IPv4 and IPv6).
const SOCKET_FDINFO_SIZE: usize = 792;
const PROC_PIDFDSOCKETINFO: i32 = 3;
const SOCKINFO_TCP: i32 = 2;
const SOI_KIND: usize = 256;
const TCP_FPORT: usize = 264;
const TCP_LPORT: usize = 268;
const INSI_VFLAG: usize = 288;
const INSI_FADDR: usize = 296;
const INSI_LADDR: usize = 312;
const INI_IPV4: u8 = 0x1;

/// The process that owns the client end of a loopback TCP connection: its local end is `client`
/// (the accepted stream's peer) and its foreign end `server` (the accepted stream's local
/// address). Spike 2026-10-11: libproc socket matching, ~0.3 ms median, no root. Looks at this
/// user's processes only; None when no process or more than one does (deny).
#[cfg(target_os = "macos")]
pub(crate) fn peer_pid(server: SocketAddr, client: SocketAddr) -> Option<Identity> {
    let n = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    let mut pids = vec![0 as libc::pid_t; n.max(0) as usize + 64];
    let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as i32;
    let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
    pids.truncate(n.max(0) as usize);
    let (server, client) = (canonical(server), canonical(client));
    sole_owner(
        pids.into_iter()
            .filter(|pid| *pid > 1)
            .filter_map(|pid| identity::read(pid as u32).filter(|_| owns_client(pid, server, client))),
    )
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn peer_pid(_server: SocketAddr, _client: SocketAddr) -> Option<Identity> {
    None
}

/// One process, or nobody: a socket two processes hold (a fork-inherited fd) names no one.
fn sole_owner<T: PartialEq>(owners: impl IntoIterator<Item = T>) -> Option<T> {
    let mut owners = owners.into_iter();
    let first = owners.next()?;
    owners.all(|other| other == first).then_some(first)
}

fn canonical(addr: SocketAddr) -> SocketAddr {
    SocketAddr::new(addr.ip().to_canonical(), addr.port())
}

#[cfg(target_os = "macos")]
fn owns_client(pid: libc::pid_t, server: SocketAddr, client: SocketAddr) -> bool {
    let size = std::mem::size_of::<libc::proc_fdinfo>();
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, std::ptr::null_mut(), 0) };
    if bytes <= 0 {
        return false;
    }
    let mut fds = vec![unsafe { std::mem::zeroed::<libc::proc_fdinfo>() }; bytes as usize / size + 16];
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, fds.as_mut_ptr().cast(), (fds.len() * size) as i32) };
    fds.truncate(bytes.max(0) as usize / size);
    fds.iter().filter(|fd| fd.proc_fdtype == libc::PROX_FDTYPE_SOCKET as u32).any(|fd| {
        let mut info = [0u8; SOCKET_FDINFO_SIZE];
        let got = unsafe {
            libc::proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDSOCKETINFO, info.as_mut_ptr().cast(), SOCKET_FDINFO_SIZE as i32)
        };
        got == SOCKET_FDINFO_SIZE as i32 && tcp_ends(&info) == Some((client, server))
    })
}

/// A TCP socket's (local, foreign) ends, canonical (a v4-mapped IPv6 address reads as IPv4).
fn tcp_ends(info: &[u8; SOCKET_FDINFO_SIZE]) -> Option<(SocketAddr, SocketAddr)> {
    let int = |at: usize| i32::from_ne_bytes(info[at..at + 4].try_into().unwrap_or([0; 4]));
    if int(SOI_KIND) != SOCKINFO_TCP {
        return None;
    }
    let end = |addr_at: usize, port_at: usize| {
        let bytes: [u8; 16] = info[addr_at..addr_at + 16].try_into().unwrap_or([0; 16]);
        // An IPv4 end keeps its address in the last 4 bytes (in4in6_addr).
        let ip = if info[INSI_VFLAG] & INI_IPV4 != 0 {
            std::net::IpAddr::from([bytes[12], bytes[13], bytes[14], bytes[15]])
        } else {
            std::net::IpAddr::from(bytes)
        };
        // insi_lport/insi_fport hold the port in network byte order in their low 16 bits.
        canonical(SocketAddr::new(ip, u16::from_be(int(port_at) as u16)))
    };
    Some((end(INSI_LADDR, TCP_LPORT), end(INSI_FADDR, TCP_FPORT)))
}

/// The peer's pid and its parents, closest first, this user's processes only, at most 32: the
/// session's own process is somewhere up there (npx -> node -> claude). Empty (deny) when the
/// peer is no longer the process the socket lookup named, before or after the walk.
pub(crate) fn ancestors(peer: Identity) -> Vec<u32> {
    if identity::read(peer.pid) != Some(peer) {
        return Vec::new();
    }
    let mut chain = Vec::new();
    let mut next = Some(peer.pid);
    while let Some(pid) = next.filter(|p| *p > 1 && chain.len() < 32) {
        chain.push(pid);
        next = identity::read(pid).map(|identity| identity.parent);
    }
    if identity::read(peer.pid) != Some(peer) {
        return Vec::new();
    }
    chain
}

#[cfg(test)]
mod tests {
    use super::*;

    use futures_util::{SinkExt, StreamExt};
    use serde_json::{json, Value};
    use tokio_tungstenite::tungstenite::Message;

    #[test]
    fn the_http_surface_is_what_cdp_clients_read_first() {
        let (status, body) = http_response("/json/version");
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!((status, v["webSocketDebuggerUrl"].as_str()), (200, Some("ws://127.0.0.1:39222/devtools/browser/lightbulb")));
        assert_eq!(http_response("/json/version/"), (status, body), "with a trailing slash too");
        assert_eq!(http_response("/json/list"), (200, "[]".into()));
        assert_eq!(http_response("/json/new?about:blank").0, 404);
    }

    #[test]
    fn a_web_pages_handshake_is_refused() {
        use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
        let page = Request::builder().uri("/devtools/browser/lightbulb").header("origin", "https://evil.example").body(()).unwrap();
        assert_eq!(no_browser(&page, Response::new(())).unwrap_err().status(), 403);
        let tool = Request::builder().uri("/devtools/browser/lightbulb").body(()).unwrap();
        assert!(no_browser(&tool, Response::new(())).is_ok());
    }

    #[tokio::test]
    async fn a_refused_client_reads_why_in_cdp_error_form() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}{WS_PATH}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let ws = tokio_tungstenite::accept_async(stream).await.unwrap();
            refuse(ws, vec![r#"{"id":1,"method":"Target.getBrowserContexts"}"#.into()], NOT_SHARED).await;
        });
        let (mut client, _) = tokio_tungstenite::connect_async(url).await.unwrap();
        client.send(Message::Text(r#"{"id":2,"method":"Target.setDiscoverTargets","params":{"discover":true}}"#.into())).await.unwrap();
        let mut answers = Vec::new();
        while let Some(Ok(Message::Text(text))) = client.next().await {
            answers.push(serde_json::from_str::<Value>(&text).unwrap());
        }
        server.await.unwrap();
        assert_eq!(answers, [json!({"id": 1, "error": {"code": -32000, "message": NOT_SHARED}}),
            json!({"id": 2, "error": {"code": -32000, "message": NOT_SHARED}})]);
    }

    #[tokio::test]
    async fn the_backends_answer_opens_the_chrome_channel_or_refuses_with_its_message() {
        let (channels, _rx) = crate::agent_chrome::test_channels();
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o1".into(), Pending { to_client, decide });
        assert!(on_event(&channels, "browser.cdp.open", &json!({"channel": "o1", "profileId": "p"})));
        assert!(matches!(decided.await, Ok(Decision::Chrome(_))));
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o2".into(), Pending { to_client, decide });
        let message = "Share this session to use the Lightbulb browser.\u{7}";
        assert!(on_event(&channels, "browser.cdp.close", &json!({"channel": "o2", "code": "not_shared", "message": message})));
        let Ok(Decision::Refused(shown)) = decided.await else { panic!("refused") };
        assert_eq!(shown, "Share this session to use the Lightbulb browser.");
        assert!(!on_event(&channels, "browser.cdp.frame", &json!({"channel": "o1", "data": "{}"})), "Chrome frames are Channels' own");
        assert!(!on_event(&channels, "browser.cdp.open", &json!({"channel": "run-1"})), "a run's channel");
    }

    #[tokio::test]
    async fn a_full_mac_refuses_the_session_and_tells_the_backend() {
        let (channels, mut rx) = crate::agent_chrome::test_channels();
        for id in ["a", "b", "c", "d"] {
            channels.on_event("browser.cdp.open", &json!({"channel": id, "profileId": "p"}));
        }
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o3".into(), Pending { to_client, decide });
        assert!(on_event(&channels, "browser.cdp.open", &json!({"channel": "o3", "profileId": "p"})));
        let Ok(Decision::Refused(shown)) = decided.await else { panic!("refused") };
        assert!(shown.contains("busy"));
        let Some(Message::Text(text)) = tokio::time::timeout(std::time::Duration::from_secs(1), rx.recv()).await.unwrap() else { panic!() };
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!((v["payload"]["channel"].as_str(), v["payload"]["code"].as_str()), (Some("o3"), Some("CAPACITY")));
    }

    #[test]
    fn an_idle_connection_holds_the_next_commands_in_order_until_the_lease_is_back() {
        let mut gate = Gate::default();
        assert!(matches!(gate.command("a".into()), Step::Forward(t) if t == "a"));
        gate.went_idle();
        assert!(!gate.active());
        assert!(matches!(gate.command("b".into()), Step::Wake), "the first command after idle asks for the lease");
        assert!(matches!(gate.command("c".into()), Step::Hold), "later ones wait behind it");
        assert_eq!(gate.leased(true), ["b", "c"], "then all run, in the order sent");
        assert!(gate.active());
        assert!(matches!(gate.command("d".into()), Step::Forward(t) if t == "d"));
    }

    #[test]
    fn a_refused_reacquire_returns_the_held_commands_and_the_next_one_tries_again() {
        let mut gate = Gate::default();
        gate.went_idle();
        gate.command("b".into());
        assert_eq!(gate.leased(false), ["b"]);
        assert!(!gate.active(), "still idle");
        assert!(matches!(gate.command("c".into()), Step::Wake));
        assert_eq!(gate.leased(true), ["c"]);
        assert!(gate.leased(true).is_empty(), "a late second answer has nothing to run");
    }

    #[test]
    fn too_many_held_commands_overflow() {
        let mut gate = Gate::default();
        gate.went_idle();
        gate.command("0".into());
        for i in 1..MAX_EARLY {
            assert!(matches!(gate.command(i.to_string()), Step::Hold));
        }
        assert!(matches!(gate.command("x".into()), Step::Overflow(t) if t == "x"));
    }

    #[test]
    fn a_held_command_that_cannot_run_reads_the_code() {
        let reply = cdp_error(r#"{"id":7,"sessionId":"S","method":"Page.navigate"}"#, "mac_browser_busy: This Mac's browser is busy; try again shortly.").unwrap();
        assert_eq!(serde_json::from_str::<Value>(&reply).unwrap(),
            json!({"id": 7, "error": {"code": -32000, "message": "mac_browser_busy: This Mac's browser is busy; try again shortly."}}));
        assert_eq!(cdp_error("{\"method\":\"Target.attachedToTarget\"}", "x"), None, "an event has nothing to answer");
    }

    #[tokio::test]
    async fn the_backends_lease_answer_reaches_its_connection() {
        let (channels, _rx) = crate::agent_chrome::test_channels();
        let (leased, mut answers) = mpsc::channel(4);
        lock(&OPEN).insert("l1".into(), leased);
        assert!(on_event(&channels, "browser.cdp.leased", &json!({"channel": "l1", "ok": true})));
        assert_eq!(answers.recv().await, Some(Ok(())));
        assert!(on_event(&channels, "browser.cdp.leased", &json!({"channel": "l1", "ok": false,
            "code": "browser_profile_held_by_another_workspace", "message": "Held by Acme.\u{7}"})));
        assert_eq!(answers.recv().await, Some(Err("browser_profile_held_by_another_workspace: Held by Acme.".into())));
        lock(&OPEN).remove("l1");
        assert!(!on_event(&channels, "browser.cdp.leased", &json!({"channel": "l1", "ok": true})), "not ours any more");
    }

    #[tokio::test]
    async fn a_taken_port_is_reported_and_nothing_is_served() {
        let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = taken.local_addr().unwrap().port();
        let reported = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = reported.clone();
        assert!(bind_or_report(port, move |text| sink.lock().unwrap().push(text)).await.is_none());
        assert!(reported.lock().unwrap()[0].contains(&port.to_string()));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn peer_pid_finds_this_processs_own_client() {
        // Pins the socket_fdinfo offsets on real loopback sockets: this process is both ends.
        for loopback in ["127.0.0.1:0", "[::1]:0"] {
            let listener = std::net::TcpListener::bind(loopback).unwrap();
            let client = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
            let (accepted, _) = listener.accept().unwrap();
            let (server, peer) = (accepted.local_addr().unwrap(), accepted.peer_addr().unwrap());
            assert_eq!(peer, client.local_addr().unwrap());
            let found = peer_pid(server, peer).map(|identity| identity.pid);
            assert_eq!(found, Some(std::process::id()), "{loopback}");
            let nobody = SocketAddr::new(peer.ip(), 1);
            assert_eq!(peer_pid(server, nobody), None, "nobody's client port");
        }
    }

    #[test]
    fn a_socket_more_than_one_process_holds_names_no_one() {
        assert_eq!(sole_owner([7, 7]), Some(7), "one process, the fd twice");
        assert_eq!(sole_owner([7, 8]), None, "a fork-inherited fd: parent and child");
        assert_eq!(sole_owner(Vec::<u32>::new()), None);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ancestors_walk_up_from_a_child_to_this_process() {
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let peer = identity::read(child.id()).unwrap();
        let chain = ancestors(peer);
        let stale = ancestors(Identity { birth_micros: peer.birth_micros + 1, ..peer });
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(chain[0], child.id());
        assert_eq!(chain[1], std::process::id());
        assert!(chain.len() <= 32 && !chain.contains(&1));
        assert!(stale.is_empty(), "a reused pid is not the peer the socket named");
    }
}
