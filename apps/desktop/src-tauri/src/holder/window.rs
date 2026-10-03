//! The terminal window. It is served from a loopback server in this app, not from the remote
//! main webview's origin, and it reaches its session over a WebSocket that needs the window's
//! own random key. No Tauri command can type into a session: the main webview (loaded from the
//! website) can only open a window, and starting a new session always goes through a native
//! folder picker the person clicks.

use super::manager::{self, HolderInfo};
use super::protocol::{encode, read_frame, DETACHED, EXIT, HELLO, INPUT, RESIZE};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::sync::{Mutex, OnceLock};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::Message;

const PAGE: &[u8] = include_bytes!("../../terminal/index.html");
const SCRIPT: &[u8] = include_bytes!("../../terminal/terminal.js");
const XTERM: &[u8] = include_bytes!("../../terminal/xterm.js");
const XTERM_CSS: &[u8] = include_bytes!("../../terminal/xterm.css");
const FIT: &[u8] = include_bytes!("../../terminal/addon-fit.js");

struct Server {
    port: u16,
    /// Window key → holder id. A key is minted per window and only ever appears in that
    /// window's URL, which this process builds.
    keys: Mutex<HashMap<String, String>>,
}

fn server() -> Result<&'static Server, String> {
    static SERVER: OnceLock<Server> = OnceLock::new();
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
pub fn admit(origin: Option<&str>, query: Option<&str>, port: u16, keys: &HashMap<String, String>) -> Option<String> {
    if origin != Some(format!("http://127.0.0.1:{port}").as_str()) {
        return None;
    }
    let key = url::form_urlencoded::parse(query?.as_bytes()).find(|(k, _)| k == "key")?.1.into_owned();
    keys.get(&key).cloned()
}

fn relay(stream: TcpStream) -> std::io::Result<()> {
    let server = server().map_err(std::io::Error::other)?;
    let admitted = std::sync::Arc::new(Mutex::new(None::<String>));
    let slot = admitted.clone();
    let callback = move |req: &Request, resp: Response| -> Result<Response, ErrorResponse> {
        let origin = req.headers().get("origin").and_then(|v| v.to_str().ok());
        let keys = server.keys.lock().unwrap();
        match admit(origin, req.uri().query(), server.port, &keys) {
            Some(id) => {
                *slot.lock().unwrap() = Some(id);
                Ok(resp)
            }
            None => Err(tungstenite::http::Response::builder().status(403).body(None).unwrap()),
        }
    };
    let mut ws = tungstenite::accept_hdr(stream.try_clone()?, callback).map_err(std::io::Error::other)?;
    let Some(id) = admitted.lock().unwrap().take() else { return Ok(()) };
    // The page sends its size first; the holder needs it before drawing the snapshot.
    let mut hello = serde_json::json!({"role": "interactive"});
    if let Ok(Message::Binary(bytes)) = ws.read() {
        if let Ok((RESIZE, size)) = read_frame(&mut bytes.as_ref()) {
            if size.len() == 4 {
                hello["cols"] = u16::from_be_bytes([size[0], size[1]]).into();
                hello["rows"] = u16::from_be_bytes([size[2], size[3]]).into();
            }
        }
    }
    let mut holder = UnixStream::connect(manager::socket_dir().join(format!("{id}.sock")))?;
    holder.write_all(&encode(HELLO, hello.to_string().as_bytes()))?;
    // Holder → window: every frame as one binary message, on its own thread.
    let mut from_holder = holder.try_clone()?;
    let mut to_window = tungstenite::WebSocket::from_raw_socket(stream.try_clone()?, tungstenite::protocol::Role::Server, None);
    std::thread::spawn(move || {
        while let Ok((kind, payload)) = read_frame(&mut from_holder) {
            if to_window.send(Message::Binary(encode(kind, &payload).into())).is_err() || matches!(kind, DETACHED | EXIT) {
                break;
            }
        }
        let _ = to_window.close(None);
    });
    // Window → holder: keystrokes and size only.
    loop {
        match ws.read() {
            Ok(Message::Binary(bytes)) => {
                let Ok((kind, payload)) = read_frame(&mut bytes.as_ref()) else { continue };
                if kind == INPUT || kind == RESIZE {
                    holder.write_all(&encode(kind, &payload))?;
                }
            }
            Ok(Message::Close(_)) | Err(_) => return Ok(()),
            Ok(_) => {}
        }
    }
}

pub(crate) fn open_window(app: &AppHandle, info: &HolderInfo) -> Result<(), String> {
    let label = format!("terminal-{}", info.id);
    if let Some(window) = app.get_webview_window(&label) {
        return window.set_focus().map_err(|e| e.to_string());
    }
    let server = server()?;
    let key = uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string();
    server.keys.lock().unwrap().insert(key.clone(), info.id.clone());
    let url = format!("http://127.0.0.1:{}/?key={key}", server.port).parse().map_err(|e: url::ParseError| e.to_string())?;
    let program = provider_label(&info.argv);
    let folder = info.cwd.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
    WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title(format!("{program} — {folder}"))
        .inner_size(1000.0, 680.0)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// "Claude Code" or "Codex" for a holder's argv. The provider's path is searched, not its file
/// name: Claude Code's binary is `…/claude/versions/<number>`, and a linked start wraps it in `/bin/sh`.
pub fn provider_label(argv: &[String]) -> &'static str {
    // Only the executable: the first message is in argv too, and may mention either name.
    let wrapped = argv.first().is_some_and(|a| a == "/bin/sh") && argv.get(3).is_some_and(|a| a == "lightbulb-start");
    let program = argv.get(if wrapped { 6 } else { 0 }).map(String::as_str).unwrap_or("");
    if program.contains("codex") {
        "Codex"
    } else if program.contains("claude") {
        "Claude Code"
    } else {
        "Session"
    }
}

/// From the main webview: open (or focus) the window of a running session. New sessions start
/// through `session_start::terminal_start`, with the same checks as a remote start.
#[tauri::command]
pub async fn terminal_open(app: AppHandle, holder_id: String) -> Result<HolderInfo, String> {
    let info = tauri::async_runtime::spawn_blocking(manager::list)
        .await
        .map_err(|e| e.to_string())?
        .into_iter()
        .find(|h| h.id == holder_id)
        .ok_or("That session has ended.")?;
    open_window(&app, &info)?;
    Ok(info)
}

/// From the main webview: the running sessions on this Mac (no keys, nothing that can type).
#[tauri::command]
pub async fn terminal_list() -> Vec<HolderInfo> {
    tauri::async_runtime::spawn_blocking(manager::list).await.unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_socket_admits_only_its_own_page_with_a_minted_key() {
        let keys = HashMap::from([("k1".to_owned(), "ab12cd34".to_owned())]);
        let page = Some("http://127.0.0.1:4100");
        assert_eq!(admit(page, Some("key=k1"), 4100, &keys).as_deref(), Some("ab12cd34"));
        assert_eq!(admit(Some("https://thelightbulb.company"), Some("key=k1"), 4100, &keys), None);
        assert_eq!(admit(None, Some("key=k1"), 4100, &keys), None);
        assert_eq!(admit(Some("http://127.0.0.1:4101"), Some("key=k1"), 4100, &keys), None);
        assert_eq!(admit(page, Some("key=guess"), 4100, &keys), None);
        assert_eq!(admit(page, None, 4100, &keys), None);
    }

    #[test]
    fn terminal_window_is_titled_by_provider_even_behind_a_wrapper_or_a_versioned_binary() {
        let argv = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(provider_label(&argv(&["/bin/sh", "-c", "x", "lightbulb-start", "id", "/scope", "/Users/a/.local/share/claude/versions/2.1.288", "--", "hi"])), "Claude Code");
        assert_eq!(provider_label(&argv(&["/opt/homebrew/bin/codex", "--no-daemon"])), "Codex");
        assert_eq!(provider_label(&argv(&["/bin/cat"])), "Session");
        assert_eq!(provider_label(&argv(&["/Users/a/.local/share/claude/versions/2.1.288", "--", "fix the codex integration"])), "Claude Code");
    }

    #[test]
    fn terminal_assets_are_served_and_nothing_else() {
        assert_eq!(asset("/").unwrap().0, "text/html; charset=utf-8");
        assert!(asset("/xterm.js").is_some() && asset("/addon-fit.js").is_some());
        assert!(asset("/../Cargo.toml").is_none() && asset("/ws").is_none());
    }
}
