//! Local authority boundary for native-session MCP calls.
//!
//! The MCP child never receives the Mac's signing key. This listener learns the
//! caller's PID from the Unix socket, binds it to one live mirrored session, and
//! sends only that verified source to the backend with a short-lived device JWT.

use super::*;
use serde_json::{json, Value};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};

const MAX_TOOL_FRAME: usize = 12 * 1024;

struct SocketGuard {
    path: PathBuf,
    inode: u64,
}

impl Drop for SocketGuard {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.path).is_ok_and(|m| m.ino() == self.inode) {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn bind() -> Result<(UnixListener, SocketGuard)> {
    let path = enrollment::hook_path().with_file_name("tool.sock");
    private_dir(path.parent().ok_or("tool socket directory unavailable")?)?;
    if let Ok(meta) = fs::symlink_metadata(&path) {
        if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } {
            return Err("unsafe tool socket".into());
        }
        fs::remove_file(&path).map_err(|_| "tool socket unavailable")?;
    }
    let listener = UnixListener::bind(&path).map_err(|_| "tool socket unavailable")?;
    let inode = fs::symlink_metadata(&path)
        .map_err(|_| "tool socket unavailable")?
        .ino();
    Ok((listener, SocketGuard { path, inode }))
}

fn peer_pid(socket: &UnixStream) -> Result<u32> {
    let fd = socket.as_raw_fd();
    let (mut uid, mut gid) = (0 as libc::uid_t, 0 as libc::gid_t);
    if unsafe { libc::getpeereid(fd, &mut uid, &mut gid) } != 0 || uid != unsafe { libc::geteuid() }
    {
        return Err("tool peer is not this Mac user".into());
    }
    let mut pid = 0 as libc::pid_t;
    let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_LOCAL,
            libc::LOCAL_PEERPID,
            &mut pid as *mut _ as *mut libc::c_void,
            &mut len,
        )
    } != 0
        || len as usize != std::mem::size_of::<libc::pid_t>()
        || pid <= 0
    {
        return Err("tool peer process unavailable".into());
    }
    Ok(pid as u32)
}

fn parent(pid: u32) -> Result<Process> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of_val(&info) as i32;
    if unsafe {
        libc::proc_pidinfo(
            pid as i32,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut info as *mut _ as *mut _,
            size,
        )
    } != size
        || info.pbi_pid != pid
        || info.pbi_uid != unsafe { libc::geteuid() }
    {
        return Err("tool peer process changed".into());
    }
    process(info.pbi_ppid)
}

#[derive(Clone)]
struct Source {
    session_id: String,
    generation: String,
    provider: String,
    conversation: String,
    active_turn: Option<String>,
    item_id: Option<String>,
}

fn resolve_source(scope: &Path, peer: u32, meta: &Value) -> Result<Source> {
    let parent = parent(peer)?;
    let thread = meta["threadId"]
        .as_str()
        .filter(|id| uuid::Uuid::parse_str(id).is_ok());
    let codex_host = parent
        .executable
        .file_name()
        .is_some_and(|name| name == "codex");
    let mut found = Vec::new();
    for entry in fs::read_dir(scope).map_err(|_| "local session state unavailable")? {
        let path = entry.map_err(|_| "local session state unavailable")?.path();
        let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|e| e != "json")
            || key.len() != 64
            || !key.bytes().all(|b| b.is_ascii_hexdigit())
        {
            continue;
        }
        let Ok(mirror) = private_read::<Mirror>(&path) else {
            continue;
        };
        if mirror.ended_at.is_some()
            || mirror.native.restriction.is_some()
            || process(mirror.native.process.pid).ok().as_ref() != Some(&mirror.native.process)
        {
            continue;
        }
        let matches = match mirror.native.provider.as_str() {
            "claude" => mirror.native.process == parent,
            "codex" => {
                codex_host
                    && mirror.native.process == parent
                    && thread == Some(mirror.native.conversation.as_str())
            }
            _ => false,
        };
        if matches {
            if let Some(session_id) = &mirror.session_id {
                found.push(Source {
                    session_id: session_id.clone(),
                    generation: mirror.generation.clone(),
                    provider: mirror.native.provider.clone(),
                    conversation: mirror.native.conversation.clone(),
                    active_turn: mirror.native.active_turn.clone(),
                    item_id: meta["itemId"]
                        .as_str()
                        .filter(|v| v.len() <= 128)
                        .map(str::to_owned),
                });
            }
        }
    }
    match found.len() {
        1 => Ok(found.remove(0)),
        _ => Err("source_ambiguous: this tool needs one verified native session".into()),
    }
}

fn same_source_turn(expected: &Source, current: &Source) -> bool {
    expected.session_id == current.session_id
        && expected.generation == current.generation
        && expected.provider == current.provider
        && expected.conversation == current.conversation
        && expected.active_turn == current.active_turn
}

/// A Codex prompt hook marks the mirror busy before the next discovery sweep
/// reads the rollout's exact turn ID. Wake that existing verified sweep, then
/// wait for its mirror update rather than sending a tool with a null turn.
async fn source_with_turn(scope: &Path, peer: u32, meta: &Value) -> Result<(Source, bool)> {
    let initial = resolve_source(scope, peer, meta)?;
    if initial.provider != "codex" || initial.active_turn.is_some() {
        return Ok((initial, false));
    }
    let wake = tokio::net::UnixDatagram::unbound().map_err(|_| "Codex session is syncing; retry")?;
    wake.send_to(b"?", enrollment::hook_path())
        .await
        .map_err(|_| "Codex session is syncing; retry")?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(7);
    while tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(200)).await;
        let current = resolve_source(scope, peer, meta)?;
        if current.session_id != initial.session_id
            || current.generation != initial.generation
            || current.conversation != initial.conversation
        {
            return Err("Codex session changed while syncing".into());
        }
        if current.active_turn.is_some() {
            return Ok((current, true));
        }
    }
    Err("Codex session is syncing; retry the tool shortly".into())
}

async fn read_frame(socket: &mut UnixStream) -> Result<Value> {
    let mut bytes = Vec::new();
    loop {
        let mut byte = [0u8; 1];
        let n = socket
            .read(&mut byte)
            .await
            .map_err(|_| "tool socket read failed")?;
        if n == 0 || bytes.len() >= MAX_TOOL_FRAME {
            return Err("invalid tool request".into());
        }
        if byte[0] == b'\n' {
            break;
        }
        bytes.push(byte[0]);
    }
    serde_json::from_slice(&bytes).map_err(|_| "invalid tool request".into())
}

async fn answer(
    mut socket: UnixStream,
    scope: PathBuf,
    enrollment: Enrollment,
    identity: crate::keychain::DeviceIdentity,
    client: reqwest::Client,
) {
    let result: Result<Value> = async {
        let peer = peer_pid(&socket)?;
        let request = tokio::time::timeout(Duration::from_secs(2), read_frame(&mut socket))
            .await
            .map_err(|_| "tool socket timed out")??;
        let object = request.as_object().ok_or("invalid tool request")?;
        if object.len() != 3
            || !["tool", "arguments", "meta"]
                .iter()
                .all(|key| object.contains_key(*key))
        {
            return Err("invalid tool request".into());
        }
        let (source, warmed) = source_with_turn(&scope, peer, &request["meta"]).await?;
        if source.provider == "codex" && request["tool"] == "send" {
            if let Some(turn) = request["meta"]["x-codex-turn-metadata"]["turn_id"].as_str() {
                if Some(turn) != source.active_turn.as_deref() {
                    return Err("Codex native send turn changed".into());
                }
            }
        }
        let token = crate::device_token::mint_device_token(
            &identity.private_key_pem,
            &enrollment.device_id,
            &enrollment.owner_id,
            90,
        )
        .map_err(|_| "device authentication unavailable")?;
        // This route only prepares a private intent. An older backend returns
        // 404 here, which is safe; it must never fall through to immediate send.
        let route = if source.provider == "codex" && request["tool"] == "send" {
            "native-session-tools/prepare-send"
        } else {
            "native-session-tools"
        };
        let body = json!({"sourceSessionId":source.session_id.clone(),"sourceGeneration":source.generation.clone(),
            "sourceProvider":source.provider.clone(),"sourceConversationId":source.conversation.clone(),
            "sourceTurnId":source.active_turn.clone(),"originItemId":source.item_id.clone(),
            "tool":request["tool"],"arguments":request["arguments"]});
        let retry_until = tokio::time::Instant::now() + Duration::from_secs(4);
        loop {
            let response = client
                .post(format!(
                    "{}/api/v1/messaging/{route}",
                    crate::LIGHTBULB_API_BASE.trim_end_matches('/'),
                ))
                .bearer_auth(&token)
                .json(&body)
                .send()
                .await
                .map_err(|_| "Lightbulb unavailable")?;
            let ok = response.status().is_success();
            let data: Value = response
                .json()
                .await
                .map_err(|_| "invalid Lightbulb response")?;
            if warmed && !ok && data["code"] == "LOCAL_SESSION_FORBIDDEN"
                && tokio::time::Instant::now() < retry_until
            {
                tokio::time::sleep(Duration::from_millis(250)).await;
                let current = resolve_source(&scope, peer, &request["meta"])?;
                if !same_source_turn(&source, &current) {
                    return Err("Codex session changed while syncing".into());
                }
                continue;
            }
            return Ok(json!({"ok":ok,"data":data}));
        }
    }
    .await;
    let reply = match result {
        Ok(value) => value,
        Err(reason) => json!({"ok":false,"error":reason}),
    };
    if let Ok(mut bytes) = serde_json::to_vec(&reply) {
        bytes.push(b'\n');
        let _ = socket.write_all(&bytes).await;
    }
}

pub(super) async fn serve(
    scope: PathBuf,
    enrollment: Enrollment,
    identity: crate::keychain::DeviceIdentity,
) {
    let (listener, _guard) = match bind() {
        Ok(value) => value,
        Err(reason) => {
            crate::log(&format!(
                "[local-sessions] native tool unavailable: {reason}"
            ));
            return;
        }
    };
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(8))
        .build()
    {
        Ok(client) => client,
        Err(_) => return,
    };
    loop {
        let Ok((socket, _)) = listener.accept().await else {
            break;
        };
        tokio::spawn(answer(
            socket,
            scope.clone(),
            enrollment.clone(),
            identity.clone(),
            client.clone(),
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_tool_retry_keeps_the_same_registered_turn() {
        let expected = Source {
            session_id: "session-a".into(),
            generation: "generation-a".into(),
            provider: "codex".into(),
            conversation: "thread-a".into(),
            active_turn: Some("turn-a".into()),
            item_id: None,
        };
        assert!(same_source_turn(&expected, &expected));
        let mut next = expected.clone();
        next.active_turn = Some("turn-b".into());
        assert!(!same_source_turn(&expected, &next));
        next = expected.clone();
        next.generation = "generation-b".into();
        assert!(!same_source_turn(&expected, &next));
        next = expected.clone();
        next.conversation = "thread-b".into();
        assert!(!same_source_turn(&expected, &next));
    }
}
