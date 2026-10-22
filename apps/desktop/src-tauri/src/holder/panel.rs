//! The live terminal inside the app's session panel (rulings 2026-10-03 §2-3; spec §5.2, §6).
//!
//! The main webview loads the remote site, so the terminal is not part of it. It is a child
//! webview of the main window (`tauri::Window::add_child`, the `unstable` feature), loaded from
//! this app's loopback server and placed over the rect the page reports. It reaches its holder
//! over a WebSocket that needs a one-time key minted here and written only into that child's URL.
//! The page says which session to show and where; it never sees the key or the URL, and no Tauri
//! command carries terminal input (app commands are not ACL-checked, so every command is the
//! page's). A bad page can move, shrink, hide or switch the terminal, all in front of the owner,
//! and nothing more.

use super::manager::{self, HolderInfo};
// What the panel shows lives in core, so the relay can read it.
use super::shown::SHOWN;
use super::protocol::{encode, read_frame, with_offset, DETACHED, EXIT, HELLO, INPUT, OUTPUT, RESIZE};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::sync::{Mutex, OnceLock};
use tauri::{AppHandle, LogicalPosition, LogicalSize, Manager, Url, WebviewUrl};
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::Message;

const PAGE: &[u8] = include_bytes!("../../terminal/index.html");
const SCRIPT: &[u8] = include_bytes!("../../terminal/terminal.js");
const XTERM: &[u8] = include_bytes!("../../terminal/xterm.js");
const XTERM_CSS: &[u8] = include_bytes!("../../terminal/xterm.css");
const FIT: &[u8] = include_bytes!("../../terminal/addon-fit.js");

struct Server {
    port: u16,
    /// Key → holder id. A key is minted per terminal view and only ever appears in that
    /// view's URL, which this process builds.
    keys: Mutex<HashMap<String, String>>,
}

static SERVER: OnceLock<Server> = OnceLock::new();

fn server() -> Result<&'static Server, String> {
    if let Some(s) = SERVER.get() {
        return Ok(s);
    }
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let server = SERVER.get_or_init(|| Server { port, keys: Mutex::new(HashMap::new()) });
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            std::thread::spawn(move || {
                let _ = serve(stream);
            });
        }
    });
    Ok(server)
}

fn asset(path: &str) -> Option<(&'static str, &'static [u8])> {
    Some(match path {
        "/" | "/index.html" => ("text/html; charset=utf-8", PAGE),
        "/terminal.js" => ("text/javascript", SCRIPT),
        "/xterm.js" => ("text/javascript", XTERM),
        "/xterm.css" => ("text/css", XTERM_CSS),
        "/addon-fit.js" => ("text/javascript", FIT),
        _ => return None,
    })
}

/// One connection: a WebSocket upgrade on `/ws`, else a static file.
fn serve(mut stream: TcpStream) -> std::io::Result<()> {
    // Route on the whole request line: TCP may hand over only its first few bytes at first.
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    let mut head = [0u8; 1024];
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let mut n = stream.peek(&mut head)?;
    while n < head.len() && !head[..n].windows(2).any(|w| w == b"\r\n") {
        if n == 0 || std::time::Instant::now() > deadline {
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
        n = stream.peek(&mut head)?;
    }
    let first = String::from_utf8_lossy(&head[..n]);
    // The limit was for reading the request line only; a terminal connection idles for hours.
    stream.set_read_timeout(None)?;
    if first.starts_with("GET /ws") {
        return relay(stream);
    }
    let path = first.split_whitespace().nth(1).unwrap_or("/").split('?').next().unwrap_or("/").to_owned();
    let mut request = [0u8; 8192];
    let _ = stream.read(&mut request)?;
    match asset(&path) {
        Some((kind, body)) => {
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", body.len())?;
            stream.write_all(body)
        }
        None => stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
    }
}

/// The WebSocket must come from this server's own page and carry a key this process minted.
/// A key admits once: the connection it opens spends it.
pub fn admit(origin: Option<&str>, query: Option<&str>, port: u16, keys: &mut HashMap<String, String>) -> Option<String> {
    if origin != Some(format!("http://127.0.0.1:{port}").as_str()) {
        return None;
    }
    let key = url::form_urlencoded::parse(query?.as_bytes()).find(|(k, _)| k == "key")?.1.into_owned();
    keys.remove(&key)
}

enum Said {
    Frame(u8, Vec<u8>),
    End,
    Nothing,
    Closed,
}

/// What the page may say: keystrokes, its size, or End. Nothing else reaches the holder.
fn said(message: tungstenite::Result<Message>) -> Said {
    match message {
        Ok(Message::Binary(bytes)) => match read_frame(&mut bytes.as_ref()) {
            Ok((kind, payload)) if kind == INPUT || kind == RESIZE => Said::Frame(kind, payload),
            _ => Said::Nothing,
        },
        Ok(Message::Text(text)) if text.as_str() == "end" => Said::End,
        Ok(Message::Close(_)) | Err(_) => Said::Closed,
        Ok(_) => Said::Nothing,
    }
}

/// The holder's recent output as one frame for the page (spec §5.6), which writes an OUTPUT
/// payload after its offset: the panel's scrollback is full on open. None when there is none.
fn history_frame(bytes: Option<Vec<u8>>) -> Option<Vec<u8>> {
    bytes.filter(|b| !b.is_empty()).map(|b| encode(OUTPUT, &with_offset(0, &b)))
}

/// Whether the holder's HELLO facts say it serves watch clients: the key `HolderInfo::watch` is
/// read from, which the relay checks before it attaches. `manager::history` asks as one, and a
/// holder that predates them would only cost the panel that call's timeout.
fn serves_watch(facts: &[u8]) -> bool {
    serde_json::from_slice::<Value>(facts).is_ok_and(|v| v["watch"] == true)
}

fn relay(stream: TcpStream) -> std::io::Result<()> {
    let server = server().map_err(std::io::Error::other)?;
    let admitted = std::sync::Arc::new(Mutex::new(None::<String>));
    let slot = admitted.clone();
    let callback = move |req: &Request, resp: Response| -> Result<Response, ErrorResponse> {
        let origin = req.headers().get("origin").and_then(|v| v.to_str().ok());
        let mut keys = server.keys.lock().unwrap();
        match admit(origin, req.uri().query(), server.port, &mut keys) {
            Some(id) => {
                *slot.lock().unwrap() = Some(id);
                Ok(resp)
            }
            None => Err(tungstenite::http::Response::builder().status(403).body(None).unwrap()),
        }
    };
    let mut ws = tungstenite::accept_hdr(stream.try_clone()?, callback).map_err(std::io::Error::other)?;
    let Some(id) = admitted.lock().unwrap().take() else { return Ok(()) };
    let socket = manager::socket_dir().join(format!("{id}.sock"));
    // The page sends its size first; the holder needs it before drawing the snapshot. `join`: a
    // terminal already running the session (`lightbulb claude`) keeps its keyboard and size.
    // The page then gets the holder's facts, its recent output for scrollback, and the screen.
    let mut hello = json!({"role": "interactive", "join": true});
    if let Said::Frame(RESIZE, size) = said(ws.read()) {
        if size.len() == 4 {
            hello["cols"] = u16::from_be_bytes([size[0], size[1]]).into();
            hello["rows"] = u16::from_be_bytes([size[2], size[3]]).into();
        }
    }
    let mut holder = UnixStream::connect(&socket)?;
    holder.write_all(&encode(HELLO, hello.to_string().as_bytes()))?;
    let mut from_holder = holder.try_clone()?;
    let mut to_page = tungstenite::WebSocket::from_raw_socket(stream.try_clone()?, tungstenite::protocol::Role::Server, None);
    // The facts first (a joined page takes the session's size from them), then the recent output
    // as scrollback, then everything else in order: the screen is next.
    let (kind, facts) = read_frame(&mut from_holder)?;
    to_page.send(Message::Binary(encode(kind, &facts).into())).map_err(std::io::Error::other)?;
    let recent = if serves_watch(&facts) { manager::history(&socket, super::run::LOG_CAP) } else { None };
    if let Some(frame) = history_frame(recent) {
        to_page.send(Message::Binary(frame.into())).map_err(std::io::Error::other)?;
    }
    let held = id.clone();
    std::thread::spawn(move || {
        // The teammate the cursor names (S8), told as a text message whenever it changes: their
        // keys echo, so a frame follows each batch. The page times it out itself (`forMs`).
        let mut told = None;
        while let Ok((kind, payload)) = read_frame(&mut from_holder) {
            if to_page.send(Message::Binary(encode(kind, &payload).into())).is_err() || matches!(kind, DETACHED | EXIT) {
                break;
            }
            let now = std::time::Instant::now();
            let typist = crate::local_sessions::remote_input::cursor_typist(&held, now);
            if typist != told {
                let left = crate::local_sessions::remote_input::CURSOR_FOR.saturating_sub(typist.as_ref().map_or(Default::default(), |(_, at)| now.saturating_duration_since(*at)));
                let who = typist.as_ref().map(|((user, name), _)| json!({"userId": user, "name": name, "forMs": left.as_millis() as u64}));
                let _ = to_page.send(Message::Text(json!({"typist": who}).to_string().into()));
                told = typist;
            }
        }
        let _ = to_page.close(None);
    });
    loop {
        match said(ws.read()) {
            Said::Frame(kind, payload) => {
                if kind == INPUT {
                    crate::local_sessions::remote_input::owner_typed(&id);
                }
                holder.write_all(&encode(kind, &payload))?
            }
            Said::End => {
                // The panel's End control: the provider ends and the holder keeps its last picture.
                // The page hides End for an attach terminal; one sent anyway ends only that client.
                match manager::hello(&socket) {
                    Some(info) if manager::attach_job(&info).is_some() => manager::stop_attach(&info),
                    // Its holder stays: the page hears EXIT on this connection, as after `/exit`.
                    Some(info) if manager::end(&info) => continue,
                    _ => {}
                }
                return Ok(());
            }
            Said::Closed => return Ok(()),
            Said::Nothing => {}
        }
    }
}

/// The page's own size, as it reads it (`innerWidth` x `innerHeight`).
#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
pub struct Viewport {
    pub width: f64,
    pub height: f64,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// The page's rect clamped into the window, or None when it is not a drawable terminal.
pub fn fit(rect: Rect, window: (f64, f64)) -> Option<Rect> {
    if ![rect.x, rect.y, rect.width, rect.height].iter().all(|v| v.is_finite()) {
        return None;
    }
    let x = rect.x.clamp(0.0, window.0);
    let y = rect.y.clamp(0.0, window.1);
    let width = rect.width.min(window.0 - x);
    let height = rect.height.min(window.1 - y);
    (width >= 120.0 && height >= 80.0).then_some(Rect { x, y, width, height })
}

/// How far below its parent's top the page starts. The page measures `rect` from its own top, but
/// the terminal is placed in the parent view the two share. Where that parent runs under the title
/// bar and the page does not (seen on macOS 26), an unshifted terminal sat a title bar too high and
/// covered the status line above it. Zero wherever the two agree, and for a size no title bar explains.
pub fn page_top(window: (f64, f64), viewport: Option<Viewport>) -> f64 {
    match viewport {
        // A zoomed page reports a smaller size in both directions: only a page as wide as the
        // window is measured in the window's own units.
        Some(v) if (v.width - window.0).abs() <= 1.0 && v.height > 0.0 && window.1 - v.height > 0.0 && window.1 - v.height <= 80.0 => window.1 - v.height,
        _ => 0.0,
    }
}

/// One log line per distinct measurement, so a misplaced terminal can be read from the log.
fn note_placement(window: (f64, f64), viewport: Option<Viewport>, top: f64) {
    static LAST: Mutex<Option<((f64, f64), Option<Viewport>)>> = Mutex::new(None);
    if let Ok(mut last) = LAST.lock() {
        if *last != Some((window, viewport)) {
            *last = Some((window, viewport));
            crate::log(&format!("[terminal-panel] window {window:?} page {:?} shift {top}", viewport.map(|v| (v.width, v.height))));
        }
    }
}

/// The terminal webview may only ever show its own loopback page.
pub fn loopback_page(url: &Url, port: u16) -> bool {
    url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(port)
}

/// The newest call wins: the page's calls run concurrently and may arrive out of order.
static LAST_AT: Mutex<f64> = Mutex::new(0.0);

fn newest(at: f64) -> bool {
    LAST_AT.lock().is_ok_and(|last| *last == at)
}

#[tauri::command]
pub async fn terminal_panel(
    app: AppHandle,
    webview: tauri::Webview,
    session_id: Option<String>,
    holder_id: Option<String>,
    rect: Option<Rect>,
    viewport: Option<Viewport>,
    at: f64,
) -> Result<Value, String> {
    if webview.label() != "main" {
        return Err("Not available here.".into());
    }
    {
        let mut last = LAST_AT.lock().map_err(|e| e.to_string())?;
        if !(at > *last) {
            return Ok(json!({"stale": true}));
        }
        *last = at;
    }
    let key = match (session_id, holder_id) {
        (Some(sid), None) => format!("s:{sid}"),
        (None, Some(id)) => format!("h:{id}"),
        (None, None) if rect.is_none() => {
            hide(&app);
            return Ok(json!({"live": false}));
        }
        _ => return Err("Say which session to show.".into()),
    };
    let holder = resolve(key).await;
    // A newer call arrived while this one looked the holder up: it decides, not this one.
    if !newest(at) {
        return Ok(json!({"stale": true}));
    }
    let Some(holder) = holder else {
        hide(&app);
        return Ok(json!({"live": false}));
    };
    let window = app.get_window("main").ok_or("The Lightbulb window is not open.")?;
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let size = window.inner_size().map_err(|e| e.to_string())?.to_logical::<f64>(scale);
    // Measured on macOS 26: a window configured 1400x900 (inner) is 1400x900 outside too, so its
    // content view runs under the title bar and `inner_size` includes it; the page is inset below.
    let top = page_top((size.width, size.height), viewport);
    note_placement((size.width, size.height), viewport, top);
    match rect.and_then(|r| fit(Rect { y: r.y + top, ..r }, (size.width, size.height))) {
        Some(r) => show(&app, &window, &holder, r)?,
        None => hide(&app),
    }
    Ok(json!({"live": true}))
}

async fn resolve(key: String) -> Option<HolderInfo> {
    // A holder removes its socket as it exits, so a cached holder without one is gone.
    let cached = SHOWN
        .lock()
        .ok()
        .and_then(|s| s.as_ref().filter(|(k, h)| *k == key && h.socket.exists()).map(|(_, h)| h.clone()));
    if cached.is_some() {
        return cached;
    }
    let lookup = key.clone();
    let holder = tauri::async_runtime::spawn_blocking(move || match lookup.split_once(':') {
        Some(("s", sid)) => crate::local_sessions::screen::holder_for_session(sid, true).map(|(h, _)| h),
        Some(("h", id)) if id.len() == 8 && id.bytes().all(|b| b.is_ascii_hexdigit()) => {
            manager::hello(&manager::socket_dir().join(format!("{id}.sock")))
        }
        _ => None,
    })
    .await
    .ok()
    .flatten();
    if let (Some(h), Ok(mut shown)) = (&holder, SHOWN.lock()) {
        *shown = Some((key, h.clone()));
    }
    holder
}

fn show(app: &AppHandle, window: &tauri::Window, holder: &HolderInfo, r: Rect) -> Result<(), String> {
    let label = format!("terminal-{}", holder.id);
    let (position, size) = (LogicalPosition::new(r.x, r.y), LogicalSize::new(r.width, r.height));
    if let Some(view) = app.get_webview(&label) {
        return view.set_bounds(tauri::Rect { position: position.into(), size: size.into() }).map_err(|e| e.to_string());
    }
    close_views(app);
    let server = server()?;
    let key = uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string();
    {
        let mut keys = server.keys.lock().map_err(|e| e.to_string())?;
        // One terminal at a time: a key the closed view never spent dies with it.
        keys.clear();
        keys.insert(key.clone(), holder.id.clone());
    }
    // A Claude background session's attach terminal: the page shows no End (local panel only).
    let attach = if manager::attach_job(holder).is_some() { "&attach=1" } else { "" };
    let url: Url = format!("http://127.0.0.1:{}/?key={key}{attach}", server.port).parse().map_err(|e: url::ParseError| e.to_string())?;
    let port = server.port;
    let builder = tauri::webview::WebviewBuilder::new(label, WebviewUrl::External(url))
        .on_navigation(move |u| loopback_page(u, port))
        // `window.open` and `target=_blank` are not navigations: refuse them outright.
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny);
    // No programmatic focus: the owner clicks into the terminal to type, so the page placing it
    // can never redirect keystrokes into the session.
    window.add_child(builder, position, size).map_err(|e| e.to_string())?;
    Ok(())
}

/// The site is (re)loading: take the panel's terminal away and restart the call order, since the
/// new page's clock starts over. The session keeps running: only End or the provider ends it.
pub fn close(app: &AppHandle) {
    if let Ok(mut last) = LAST_AT.lock() {
        *last = 0.0;
    }
    hide(app);
}

fn hide(app: &AppHandle) {
    if let Ok(mut shown) = SHOWN.lock() {
        *shown = None;
    }
    // A key dies with its view, spent or not.
    if let Some(server) = SERVER.get() {
        if let Ok(mut keys) = server.keys.lock() {
            keys.clear();
        }
    }
    close_views(app);
}

fn close_views(app: &AppHandle) {
    for (label, view) in app.webviews() {
        if label.starts_with("terminal-") {
            let _ = view.close();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_panel_gets_history_as_one_output_frame_and_nothing_when_there_is_none() {
        use crate::holder::protocol::{split_offset, OUTPUT};
        let frame = history_frame(Some(b"old\nlines".to_vec())).unwrap();
        let (kind, payload) = read_frame(&mut frame.as_slice()).unwrap();
        assert_eq!((kind, split_offset(&payload).unwrap().1), (OUTPUT, &b"old\nlines"[..]));
        assert!(history_frame(Some(vec![])).is_none() && history_frame(None).is_none());
    }

    #[test]
    fn history_is_asked_only_of_a_holder_that_serves_watch_clients() {
        assert!(serves_watch(br#"{"protocol":1,"pid":7,"watch":true,"type":true}"#));
        assert!(!serves_watch(br#"{"protocol":1,"pid":7}"#), "a holder older than watch clients");
        assert!(!serves_watch(br#"{"watch":false}"#) && !serves_watch(br#"{"watch":"true"}"#) && !serves_watch(b"not json"));
    }

    #[test]
    fn terminal_socket_admits_its_own_page_once_with_a_minted_key() {
        let mut keys = HashMap::from([("k1".to_owned(), "ab12cd34".to_owned())]);
        let page = Some("http://127.0.0.1:4100");
        assert_eq!(admit(Some("https://thelightbulb.company"), Some("key=k1"), 4100, &mut keys), None);
        assert_eq!(admit(None, Some("key=k1"), 4100, &mut keys), None);
        assert_eq!(admit(Some("http://127.0.0.1:4101"), Some("key=k1"), 4100, &mut keys), None);
        assert_eq!(admit(page, Some("key=guess"), 4100, &mut keys), None);
        assert_eq!(admit(page, None, 4100, &mut keys), None);
        assert_eq!(admit(page, Some("key=k1"), 4100, &mut keys).as_deref(), Some("ab12cd34"));
        assert_eq!(admit(page, Some("key=k1"), 4100, &mut keys), None, "a key opens one connection");
    }

    #[test]
    fn terminal_panel_rect_stays_inside_the_window_and_refuses_nonsense() {
        let r = |x, y, width, height| Rect { x, y, width, height };
        let window = (1400.0, 900.0);
        assert_eq!(fit(r(300.0, 80.0, 800.0, 600.0), window), Some(r(300.0, 80.0, 800.0, 600.0)));
        assert_eq!(fit(r(-50.0, 80.0, 2000.0, 2000.0), window), Some(r(0.0, 80.0, 1400.0, 820.0)));
        assert_eq!(fit(r(f64::NAN, 0.0, 500.0, 500.0), window), None);
        assert_eq!(fit(r(0.0, 0.0, f64::INFINITY, 500.0), window), None);
        assert_eq!(fit(r(1390.0, 0.0, 500.0, 500.0), window), None, "a sliver is not a terminal");
    }

    #[test]
    fn terminal_panel_starts_where_the_page_does() {
        let page = |width, height| Some(Viewport { width, height });
        let window = (1400.0, 900.0);
        assert_eq!(page_top(window, page(1400.0, 868.0)), 32.0, "the page starts a title bar below the window's top");
        assert_eq!(page_top((1400.0, 868.0), page(1400.0, 868.0)), 0.0);
        assert_eq!(page_top(window, None), 0.0, "an older page sends no size");
        assert_eq!(page_top(window, page(1333.3, 857.1)), 0.0, "a page zoomed to 105% is narrower too: not a title bar");
        assert_eq!(page_top(window, page(1400.0, 400.0)), 0.0, "no title bar is 500 tall");
        assert_eq!(page_top(window, page(1400.0, 950.0)), 0.0);
        assert_eq!(page_top(window, page(1400.0, f64::NAN)), 0.0);
    }

    #[test]
    fn terminal_panel_navigates_only_within_its_own_page() {
        let at = |s: &str| s.parse::<Url>().unwrap();
        assert!(loopback_page(&at("http://127.0.0.1:4100/?key=x"), 4100));
        assert!(!loopback_page(&at("http://127.0.0.1:4101/"), 4100));
        assert!(!loopback_page(&at("http://localhost:4100/"), 4100));
        assert!(!loopback_page(&at("https://thelightbulb.company/chat"), 4100));
    }

    #[test]
    fn the_remote_sites_capability_never_covers_the_terminal_page() {
        let caps: Value = serde_json::from_str(include_str!("../../capabilities/default.json")).unwrap();
        for url in caps["remote"]["urls"].as_array().unwrap() {
            let url = url.as_str().unwrap();
            assert!(!url.contains("127.0.0.1") && !url.contains("localhost"), "{url}");
        }
    }

    #[test]
    fn the_page_may_type_resize_or_end_and_nothing_else() {
        let frame = |kind: u8| Ok(Message::Binary(encode(kind, b"x").into()));
        assert!(matches!(said(frame(INPUT)), Said::Frame(INPUT, _)));
        assert!(matches!(said(frame(RESIZE)), Said::Frame(RESIZE, _)));
        assert!(matches!(said(frame(HELLO)), Said::Nothing));
        assert!(matches!(said(Ok(Message::Text("end".into()))), Said::End));
        assert!(matches!(said(Ok(Message::Text("other".into()))), Said::Nothing));
        assert!(matches!(said(Ok(Message::Close(None))), Said::Closed));
    }

    #[test]
    fn terminal_assets_are_served_and_nothing_else() {
        assert_eq!(asset("/").unwrap().0, "text/html; charset=utf-8");
        assert!(asset("/xterm.js").is_some() && asset("/addon-fit.js").is_some());
        assert!(asset("/../Cargo.toml").is_none() && asset("/ws").is_none());
    }
}
