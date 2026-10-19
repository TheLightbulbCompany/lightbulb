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

pub(super) struct SocketGuard {
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

pub(super) fn bind(name: &str) -> Result<(UnixListener, SocketGuard)> {
    let path = enrollment::hook_path().with_file_name(name);
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

pub(super) fn peer_pid(socket: &UnixStream) -> Result<u32> {
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

pub(super) fn parent(pid: u32) -> Result<Process> {
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

/// Read by the agent that called the tool: it says what to do next, not only what failed.
const SOURCE_UNIDENTIFIED: &str = "source_unidentified: the local Lightbulb tools could not match this \
    conversation to one shared session on this Mac. This happens in the Codex or Claude app, in IDE \
    sessions, when the session is set to Only me, or when the Lightbulb Mac app does not recognise \
    this app version. Use the \"lightbulb-hosted\" MCP server instead \
    (https://api.thelightbulb.company/mcp, sign in with OAuth). It works without a local session.";

/// Every mirror of a session that has not ended and whose own process is still that process,
/// with its key, whatever its sharing or verification state.
fn running_mirrors(scope: &Path) -> Result<Vec<(String, Mirror)>> {
    let mut running = Vec::new();
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
            || process(mirror.native.process.pid).ok().as_ref() != Some(&mirror.native.process)
        {
            continue;
        }
        running.push((key.to_owned(), mirror));
    }
    Ok(running)
}

/// Shared and unrestricted. (A withheld session is no source: it reads and sends nothing
/// through its tools.)
fn live(key: &str, mirror: &Mirror) -> bool {
    !super::transport::stopped(key, mirror) && mirror.native.restriction.is_none()
}

/// Every mirror of a running, shared, unrestricted session whose own process is still that process.
fn live_mirrors(scope: &Path) -> Result<Vec<Mirror>> {
    Ok(running_mirrors(scope)?
        .into_iter()
        .filter(|(key, mirror)| live(key, mirror))
        .map(|(_, mirror)| mirror)
        .collect())
}

const BROWSER_AMBIGUOUS: &str = "More than one session runs in this process (the Codex app, or one Codex \
    service for several conversations), so the Lightbulb browser cannot tell which one is asking";

/// The session a browser connection belongs to: the one in the NEAREST process, on the chain from
/// the peer upward (closest first), that runs any session at all, shared or not. That session owns
/// its descendants, so a session started inside another one browses as itself, and an Only-me or
/// unverified one is refused rather than answered as the shared session around it (never the
/// wrong session's browser). A nearest process with two sessions, or a Codex process that serves
/// many conversations, cannot say which one is asking and is refused.
pub(super) fn session_of_ancestors(scope: &Path, ancestors: &[u32]) -> Result<String> {
    let running = running_mirrors(scope)?;
    let not_shared = || crate::session_browser::NOT_SHARED.to_string();
    let Some(nearest) = ancestors.iter().find_map(|pid| {
        let here: Vec<_> = running.iter().filter(|(_, m)| m.native.process.pid == *pid).collect();
        (!here.is_empty()).then_some(here)
    }) else {
        return Err(not_shared());
    };
    match nearest.as_slice() {
        [(_, m)] if m.native.provider == "codex" && super::codex::serves_many(&m.native) => Err(BROWSER_AMBIGUOUS.into()),
        [(key, m)] if live(key, m) => m.session_id.clone().ok_or_else(not_shared),
        [_] => Err(not_shared()),
        _ => Err(BROWSER_AMBIGUOUS.into()),
    }
}

/// The session still runs and is shared (a browser connection's 30 s check).
pub(super) fn mirror_live(scope: &Path, session_id: &str) -> bool {
    live_mirrors(scope).is_ok_and(|live| live.iter().any(|m| m.session_id.as_deref() == Some(session_id)))
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
    for mirror in live_mirrors(scope)? {
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
        0 => Err(SOURCE_UNIDENTIFIED.into()),
        _ => Err("source_ambiguous: more than one shared session on this Mac matches this tool call; \
            restart this session's MCP server, or use the \"lightbulb-hosted\" MCP server"
            .into()),
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
        let tool = request["tool"].as_str().unwrap_or("");
        if tool == LIST {
            // The tool list is the backend's (/mcp), so the Mac and hosted servers never drift.
            let token = crate::device_token::mint_device_token(
                &identity.private_key_pem, &enrollment.device_id, &enrollment.owner_id, 90,
            )
            .map_err(|_| "device authentication unavailable")?;
            return mcp(&client, &token, json!({"jsonrpc":"2.0","id":1,"method":"tools/list"})).await;
        }
        let (source, warmed) = source_with_turn(&scope, peer, &request["meta"]).await?;
        if source.provider == "codex" && tool == "send_message" {
            if let Some(turn) = request["meta"]["x-codex-turn-metadata"]["turn_id"].as_str() {
                if Some(turn) != source.active_turn.as_deref() {
                    return Err("Codex native send turn changed".into());
                }
            }
        }
        let call = json!({"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":tool,"arguments":request["arguments"]}});
        let retry_until = tokio::time::Instant::now() + Duration::from_secs(4);
        let mut source = source;
        loop {
            let token = crate::device_token::mint_session_token(
                &identity.private_key_pem,
                &enrollment.device_id,
                &enrollment.owner_id,
                90,
                Some(source_claim(&source)),
            )
            .map_err(|_| "device authentication unavailable")?;
            let reply = mcp(&client, &token, call.clone()).await?;
            if retry_warmed_forbidden(warmed, &reply) && tokio::time::Instant::now() < retry_until {
                tokio::time::sleep(Duration::from_millis(250)).await;
                let current = resolve_source(&scope, peer, &request["meta"])?;
                if !same_source_turn(&source, &current) {
                    return Err("Codex session changed while syncing".into());
                }
                source = current;
                continue;
            }
            return Ok(reply);
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

/// The tool socket's request for the backend's tool list (not a tool name).
pub(super) const LIST: &str = "tools/list";

/// The signed `source` claim: which shared session this Mac says is calling.
fn source_claim(source: &Source) -> Value {
    json!({"sourceSessionId":source.session_id,"sourceGeneration":source.generation,
        "sourceProvider":source.provider,"sourceConversationId":source.conversation,
        "sourceTurnId":source.active_turn,"originItemId":source.item_id})
}

/// One JSON-RPC request to the backend's `/mcp`: the same tools hosted clients use. Answers
/// `{"ok", "result"}` with the MCP result (`content`, `isError`) passed through unchanged.
async fn mcp(client: &reqwest::Client, token: &str, body: Value) -> Result<Value> {
    let url = format!("{}/mcp", crate::LIGHTBULB_API_BASE.trim_end_matches('/'));
    let response = client
        .post(url)
        .bearer_auth(token)
        .header("Accept", "application/json, text/event-stream")
        .json(&body)
        .send()
        .await
        .map_err(|_| "Lightbulb unavailable")?;
    if !response.status().is_success() {
        return Err(format!("Lightbulb refused this Mac ({})", response.status().as_u16()));
    }
    let reply: Value = response.json().await.map_err(|_| "invalid Lightbulb response")?;
    match reply.get("result") {
        Some(result) => Ok(json!({"ok": result["isError"] != true, "result": result})),
        None => Err(reply["error"]["message"].as_str().unwrap_or("invalid Lightbulb response").to_owned()),
    }
}

/// A warmed first-turn Codex source can be ready locally before the backend has persisted its
/// session row: a forbidden answer then is transient, for every tool that posts from the source.
fn retry_warmed_forbidden(warmed: bool, reply: &Value) -> bool {
    warmed
        && reply["ok"] == false
        && reply["result"]["content"][0]["text"].as_str().is_some_and(|t| t.starts_with("LOCAL_SESSION_FORBIDDEN"))
}

pub(super) async fn serve(
    scope: PathBuf,
    enrollment: Enrollment,
    identity: crate::keychain::DeviceIdentity,
) {
    let (listener, _guard) = match bind("tool.sock") {
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

    fn persisted(scope: &Path, key: &str, sid: &str, shared: bool) {
        let mut mirror = super::super::transport::tests::test_mirror("22222222-2222-4222-8222-222222222222", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some(sid.into());
        mirror.shared = shared;
        mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
    }

    #[test]
    fn a_browser_connection_finds_its_shared_session_among_its_ancestors() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        // This test process is the session (test_mirror's process); a child stands in for the CDP client.
        let key = "b0b0feed".repeat(8); // its own: pending asks are process-wide
        persisted(scope.path(), &key, "session-b", true);
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let me = std::process::id();
        let found = session_of_ancestors(scope.path(), &[client.id(), me]);
        let alone = session_of_ancestors(scope.path(), &[client.id()]);
        let still = mirror_live(scope.path(), "session-b");
        persisted(scope.path(), &key, "session-b", false);
        let unshared = session_of_ancestors(scope.path(), &[client.id(), me]);
        let gone = mirror_live(scope.path(), "session-b");
        client.kill().unwrap();
        client.wait().unwrap();
        assert_eq!(found.unwrap(), "session-b");
        assert_eq!(alone.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert_eq!(unshared.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert!(still && !gone);
    }

    #[test]
    fn two_shared_sessions_in_one_process_are_refused_as_ambiguous() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        persisted(scope.path(), &"c0c0feed".repeat(8), "session-c", true);
        persisted(scope.path(), &"d0d0feed".repeat(8), "session-d", true);
        let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
        assert!(refused.contains("cannot tell which"), "{refused}");
    }

    #[test]
    fn a_shared_session_beside_an_only_me_or_unverified_one_is_never_answered() {
        use std::os::unix::fs::PermissionsExt;
        for neighbour in ["only-me", "unverified"] {
            let scope = tempfile::tempdir().unwrap();
            fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
            persisted(scope.path(), &"e0e0feed".repeat(8), "session-e", true);
            let key = "f0f0feed".repeat(8);
            persisted(scope.path(), &key, "session-f", neighbour == "unverified");
            if neighbour == "unverified" {
                let path = scope.path().join(format!("{key}.json"));
                let mut mirror: Mirror = private_read(&path).unwrap();
                mirror.native.restriction = Some("runtime-unverified".into());
                mirror.persist(&path).unwrap();
            }
            // Either session's client has this same process for an ancestor: neither is answered.
            let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
            assert!(refused.contains("cannot tell which"), "{neighbour}: {refused}");
        }
    }

    #[test]
    fn a_codex_process_serving_many_conversations_is_refused_with_one_mirrored() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = scope.path().join(format!("{}.json", "a1a1feed".repeat(8)));
        let mut mirror = super::super::transport::tests::test_mirror("33333333-3333-4333-8333-333333333333", "gen-1");
        mirror.native.provider = "codex".into();
        mirror.session_id = Some("session-g".into());
        assert!(mirror.native.app, "the Codex app's own conversation");
        mirror.persist(&path).unwrap();
        let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
        assert!(refused.contains("cannot tell which"), "{refused}");
    }

    /// A Claude session mirrored on `pid` (a spawned child, so it can sit below this process).
    fn persisted_on(scope: &Path, key: &str, sid: &str, shared: bool, pid: u32) {
        let mut mirror = super::super::transport::tests::test_mirror("44444444-4444-4444-8444-444444444444", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.native.process = process(pid).unwrap();
        mirror.session_id = Some(sid.into());
        mirror.shared = shared;
        mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
    }

    #[test]
    fn a_session_started_inside_another_browses_as_itself_never_as_the_outer_one() {
        use std::os::unix::fs::PermissionsExt;
        let me = std::process::id();
        let mut inner = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        // The chain: CDP client -> inner session -> this process (the outer session).
        let chain = [client.id(), inner.id(), me];
        let resolve = |inner_shared: Option<bool>| {
            let scope = tempfile::tempdir().unwrap();
            fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
            persisted(scope.path(), &"a2a2feed".repeat(8), "outer", true);
            if let Some(shared) = inner_shared {
                persisted_on(scope.path(), &"b2b2feed".repeat(8), "inner", shared, inner.id());
            }
            session_of_ancestors(scope.path(), &chain)
        };
        let both_shared = resolve(Some(true));
        let inner_only_me = resolve(Some(false));
        let no_inner = resolve(None);
        client.kill().unwrap();
        client.wait().unwrap();
        inner.kill().unwrap();
        inner.wait().unwrap();
        assert_eq!(both_shared.unwrap(), "inner");
        assert_eq!(inner_only_me.unwrap_err(), crate::session_browser::NOT_SHARED, "never the outer session");
        assert_eq!(no_inner.unwrap(), "outer");
    }

    #[test]
    fn a_plain_sessions_own_mcp_call_resolves_it_as_the_source() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        // This test process is the session; its child stands in for the session's MCP server.
        let mut mirror = super::super::transport::tests::test_mirror("11111111-1111-4111-8111-111111111111", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some("session-a".into());
        assert!(mirror.native.holder.is_none() && mirror.native.restriction.is_none(), "a verified session with no holder");
        let key = "5eedfeed".repeat(8); // its own: pending asks are process-wide
        let path = scope.path().join(format!("{key}.json"));
        mirror.persist(&path).unwrap();
        let mut mcp = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let source = resolve_source(scope.path(), mcp.id(), &json!({})).map(|s| s.session_id);
        // Stop pressed, not applied by the relay loop yet: the stored mirror still says shared.
        super::super::transport::ask_share(&key, false);
        let stopping = resolve_source(scope.path(), mcp.id(), &json!({})).is_err();
        super::super::transport::ask_share(&key, true);
        mirror.shared = false;
        mirror.persist(&path).unwrap();
        let stopped = resolve_source(scope.path(), mcp.id(), &json!({})).map(|s| s.session_id);
        mcp.kill().unwrap();
        mcp.wait().unwrap();
        assert_eq!(source.unwrap(), "session-a", "no holder needed to be this session's MCP source");
        assert!(stopping, "a stop the relay loop has not applied yet already counts");
        assert!(stopped.is_err(), "a session that is not shared reads and sends nothing through its tools");
        let unidentified = stopped.unwrap_err().to_string();
        assert!(unidentified.starts_with("source_unidentified:") && unidentified.contains("https://api.thelightbulb.company/mcp"));
    }

    #[test]
    fn warmed_source_retries_a_transient_forbidden_for_every_post() {
        let forbidden = json!({"ok":false,"result":{"isError":true,"content":[{"type":"text","text":"LOCAL_SESSION_FORBIDDEN: not current"}]}});
        assert!(retry_warmed_forbidden(true, &forbidden), "use_browser shares this retry");
        assert!(!retry_warmed_forbidden(false, &forbidden), "a settled source is really forbidden");
        let ok = json!({"ok":true,"result":{"isError":false,"content":[{"type":"text","text":"LOCAL_SESSION_FORBIDDEN"}]}});
        assert!(!retry_warmed_forbidden(true, &ok));
        let other = json!({"ok":false,"result":{"isError":true,"content":[{"type":"text","text":"OTHER: no"}]}});
        assert!(!retry_warmed_forbidden(true, &other));
    }

    #[test]
    fn the_source_claim_is_the_backends_native_source_shape() {
        let source = Source {
            session_id: "s".into(), generation: "g".into(), provider: "codex".into(),
            conversation: "c".into(), active_turn: Some("t".into()), item_id: None,
        };
        assert_eq!(source_claim(&source), json!({"sourceSessionId":"s","sourceGeneration":"g","sourceProvider":"codex",
            "sourceConversationId":"c","sourceTurnId":"t","originItemId":null}));
    }

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
