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

pub const PROVIDERS: [&str; 2] = ["claude", "codex"];

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
    let mut head = [0u8; 1024];
    let n = stream.peek(&mut head)?;
    let first = String::from_utf8_lossy(&head[..n]);
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
    let mut holder = UnixStream::connect(manager::socket_dir().join(format!("{id}.sock")))?;
    holder.write_all(&encode(HELLO, br#"{"role":"interactive"}"#))?;
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

fn open_window(app: &AppHandle, info: &HolderInfo) -> Result<(), String> {
    let label = format!("terminal-{}", info.id);
    if let Some(window) = app.get_webview_window(&label) {
        return window.set_focus().map_err(|e| e.to_string());
    }
    let server = server()?;
    let key = uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string();
    server.keys.lock().unwrap().insert(key.clone(), info.id.clone());
    let url = format!("http://127.0.0.1:{}/?key={key}", server.port).parse().map_err(|e: url::ParseError| e.to_string())?;
    let program = info.argv.first().map(|p| p.rsplit('/').next().unwrap_or(p)).unwrap_or("session");
    let folder = info.cwd.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
    WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title(format!("{program} — {folder}"))
        .inner_size(1000.0, 680.0)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// From the main webview. `provider` starts a new session after the person picks a folder;
/// `holder_id` opens (or focuses) the window of a running one.
#[tauri::command]
pub async fn terminal_open(app: AppHandle, provider: Option<String>, holder_id: Option<String>) -> Result<Option<HolderInfo>, String> {
    if let Some(id) = holder_id {
        let info = manager::list().into_iter().find(|h| h.id == id).ok_or("That session has ended.")?;
        open_window(&app, &info)?;
        return Ok(Some(info));
    }
    let provider = provider.ok_or("Choose Claude Code or Codex.")?;
    if !PROVIDERS.contains(&provider.as_str()) {
        return Err("Only Claude Code and Codex sessions can be started here.".into());
    }
    let picker_app = app.clone();
    let folder = tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_dialog::DialogExt;
        picker_app.dialog().file().set_title("Choose a folder for this session").blocking_pick_folder()
    })
    .await
    .map_err(|e| e.to_string())?;
    let Some(folder) = folder else { return Ok(None) };
    let folder = folder.into_path().map_err(|e| e.to_string())?;
    let info = tauri::async_runtime::spawn_blocking(move || manager::start(&folder, &[provider]))
        .await
        .map_err(|e| e.to_string())??;
    open_window(&app, &info)?;
    Ok(Some(info))
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
    fn terminal_assets_are_served_and_nothing_else() {
        assert_eq!(asset("/").unwrap().0, "text/html; charset=utf-8");
        assert!(asset("/xterm.js").is_some() && asset("/addon-fit.js").is_some());
        assert!(asset("/../Cargo.toml").is_none() && asset("/ws").is_none());
    }
}
