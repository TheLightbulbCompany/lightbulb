# Local-Session Relay Reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the desktop mirror from wedging on a lost batch ack, stop one bad session from taking the relay connection down for every other session, bound the reconnect rate, keep the supervisor alive, and log enough on both sides to diagnose the next incident.

**Architecture:** Four surgical changes in the desktop transport loop (cursor reconciliation, per-connection quarantine set, lifetime-based backoff, restart-forever supervisor) plus reason-bearing conflict logging and a `sessionId` on the backend's error frame. No wire-shape change beyond one optional field.

**Tech Stack:** Rust (tokio, tokio-tungstenite, futures-util, serde_json) in `apps/desktop/src-tauri`; Python (FastAPI, pydantic v2, pytest + moto) in `apps/backend`.

**Spec:** `docs/superpowers/specs/2026-09-12-local-session-relay-reconcile-design.md`

## Global Constraints

- Branch `fix/local-session-relay-reconcile` in the worktree `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/native-session-terminal-view`. Plain git commands only, never `git stash`, never `cd` out of the worktree, never push.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N4GeYnc2LzA3vT6uiWFg13
  ```
- Desktop tests, run SEPARATELY (a combined filter runs zero probe tests):
  ```
  TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib local_sessions
  TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --example local_session_probe
  ```
  Two pre-existing `warning: unused import: Manager` lines are baseline.
- Backend tests: `cd apps/backend && uv run pytest tests/unit/services/test_local_session_relay.py tests/unit/services/test_local_sessions.py tests/unit/routers/test_websocket_chat.py -q`.
- Log lines carry ids, sequence numbers, codes, reasons and durations only. Never transcript text, prompts, file paths, tokens, or the raw frame.
- Desktop log prefix is exactly `[local-sessions]`; write through `crate::log` (`lib.rs`, appends to `/tmp/lightbulb-desktop.log`). Session and mirror ids in log lines are truncated to their first 8 characters.
- The `local.session.error` frame keeps `code`; `sessionId` is added only when the rejected frame carried a string `sessionId` of at most 64 characters.
- Protocol faults (`unsolicited session acknowledgment`, `mismatched session acknowledgment`, `invalid session ID`, `missing upload cursor`, `missing server time`, `stale time acknowledgment`, `unsupported session frame`, `oversized session frame`, `invalid session frame`) still close the connection. Only per-session verdicts and error frames quarantine.
- Existing tests must keep passing unchanged except where a step below says to edit them.

---

### Task 1: Backend — reasoned conflicts, registration log, `sessionId` on the error frame

**Files:**
- Modify: `apps/backend/core/services/local_session_relay.py` (`_conflict` at line 99, every `raise _conflict()` site: lines 188, 224, 229, 353, 361, 369, 422, 426, 452, 455; the registration response block around line 255)
- Modify: `apps/backend/routers/websocket_chat.py:270-272`
- Test: `apps/backend/tests/unit/routers/test_websocket_chat.py`, `apps/backend/tests/unit/services/test_local_session_relay.py`

**Interfaces:**
- Produces: `_conflict(reason: str, **fields) -> ApiProblem` (code unchanged: `LOCAL_SESSION_CONFLICT`); error frame `{"type": "local.session.error", "code": ..., "sessionId"?: str}`.

- [ ] **Step 1: Write the failing router test** (append to `tests/unit/routers/test_websocket_chat.py`, mirroring `test_local_session_raw_envelope_limit_precedes_ingestion`)

```python
@pytest.mark.asyncio
async def test_local_session_error_frame_names_the_rejected_session(app, connection_service):
    from core.services.messaging import ApiProblem

    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed."))
    machine.management.send_message_async = AsyncMock()
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=machine):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1"},
                json={"type": "local.session.events", "sessionId": "abc-123", "batchId": "b", "afterSeq": 5, "events": []},
            )
    assert response.status_code == 200
    assert machine.management.send_message_async.call_args.args == (
        "conn-1",
        {"type": "local.session.error", "code": "LOCAL_SESSION_CONFLICT", "sessionId": "abc-123"},
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat.py -q -k names_the_rejected_session`
Expected: FAIL — the frame has no `sessionId`.

- [ ] **Step 3: Write the failing relay test** (append to `tests/unit/services/test_local_session_relay.py`; `start`, `relay`, `transport` helpers already exist in that file)

```python
@pytest.mark.asyncio
async def test_cursor_conflict_is_logged_with_reason_and_seqs_but_never_text(transport, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="core.services.local_session_relay")
    sid = await start(transport)
    registered = next(r for r in caplog.records if r.getMessage() == "local session registered")
    assert registered.levelno == logging.INFO and registered.sid == sid and registered.cursor == 0
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "batch-1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": "private transcript", "truncated": False}],
    }
    await relay(transport).handle_message("conn-1", body)
    # afterSeq 5 has no stored row, so this is a cursor mismatch (row cursor 1), not a batch-hash replay.
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", {**body, "batchId": "batch-new", "afterSeq": 5})
    conflict = next(r for r in caplog.records if "conflict" in r.getMessage())
    assert conflict.levelno == logging.WARNING
    assert conflict.reason == "cursor" and conflict.expected == 1 and conflict.got == 5 and conflict.sid == sid
    assert "private transcript" not in caplog.text
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_local_session_relay.py -q -k cursor_conflict_is_logged`
Expected: FAIL — `_conflict()` takes no arguments / no record has `reason`.

- [ ] **Step 5: Implement**

In `local_session_relay.py` replace `_conflict`:

```python
def _conflict(reason: str, **fields: object) -> ApiProblem:
    # Ids, seqs and reasons only — never frame content.
    logger.warning("local session conflict: %s", reason, extra={"reason": reason, **fields})
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed; reconcile before retrying.")
```

Then give every `raise _conflict()` a reason, reading the condition above each site: line 188 in `_current` → `_conflict("session lookup", sid=sid, cid=cid)`; 224 → `_conflict("older connection", sid=sid, cid=cid)`; 229 → `_conflict("generation chain", sid=sid, cid=cid)`; 353 → `_conflict("revision contention", cid=cid)`; 361 → `_conflict("batch hash", sid=sid, first_seq=first_seq)`; 369 → `_conflict("cursor", sid=sid, expected=row.get("eventSeq", 0), got=message.after_seq)`; 422, 426, 452, 455 → a two-word reason naming the failed check (for example `"receipt command"`, `"receipt generation"`, `"receipt state"`, `"reconcile generation"`), each with `sid=sid` where `sid` is in scope. Use `sid=row["sessionId"]` in `_events`. `extra` keys must be `reason`, `sid`, `cid`, `expected`, `got`, `first_seq` only (never `message`, `name`, `args`).

After the registration `response = {...}` block (line ~255) add:

```python
                    logger.info(
                        "local session registered",
                        extra={"sid": sid, "cid": cid, "cursor": row["eventSeq"], "resumed": previous is not None},
                    )
```

In `routers/websocket_chat.py` replace the `except ApiProblem as exc:` branch:

```python
        except ApiProblem as exc:
            response = {"type": "local.session.error", "code": exc.error.code}
            sid = body.get("sessionId") if isinstance(body, dict) else None
            if isinstance(sid, str) and len(sid) <= 64:
                response["sessionId"] = sid
            logger.warning(
                "local session frame rejected",
                extra={"code": exc.error.code, "frame": body.get("type") if isinstance(body, dict) else None, "sid": sid, "cid": x_connection_id},
            )
```

- [ ] **Step 6: Run the backend suites**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_local_session_relay.py tests/unit/services/test_local_sessions.py tests/unit/routers/test_websocket_chat.py -q`
Expected: all pass (previous count + 2).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/local_session_relay.py apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat.py apps/backend/tests/unit/services/test_local_session_relay.py
git commit -m "fix(relay): name every local-session conflict and the rejected session"
```

---

### Task 2: Desktop — reconcile the registration cursor (lost ack, or resync with a marker)

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (`impl Mirror`, after `acknowledge` at ~line 1178)
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (ack handler at lines ~476-506; test module)
- Test: `apps/desktop/src-tauri/src/local_sessions/transport.rs` tests

**Interfaces:**
- Produces: `Mirror::resync(&mut self, after: u64)`; `pub(super) fn relay_log(msg: &str)`; test helpers `test_native() -> NativeSession`, `test_host(dir: &Path, mirrors: Vec<(&str, Mirror)>) -> Host`, `const SID: &str`.
- Consumes: `Mirror::acknowledge(batch_id, after_seq)` (unchanged).

- [ ] **Step 1: Add test helpers to the transport test module** (place them just inside `mod tests`, before the first test)

```rust
    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
    const SID_B: &str = "bbbbcccc-dddd-4eee-8fff-000011112222";

    fn test_native(conversation: &str) -> NativeSession {
        NativeSession {
            provider: "codex".into(),
            conversation: conversation.into(),
            profile: "f".repeat(64),
            process: process(std::process::id()).unwrap(),
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "0.149.0".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
        }
    }

    fn test_mirror(conversation: &str, generation: &str) -> Mirror {
        Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: test_native(conversation),
            generation: generation.into(),
            previous_generation: None,
            session_id: None,
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
        }
    }

    fn test_host(dir: &Path, mirrors: Vec<(&str, Mirror)>) -> Host {
        Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.into(),
            home: dir.into(),
            mirrors: mirrors.into_iter().map(|(k, m)| (k.to_string(), m)).collect(),
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    async fn next_frame(socket: &mut WebSocketStream<tokio::net::TcpStream>) -> Value {
        let frame = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await
            .expect("frame within 2s")
            .expect("stream open")
            .expect("frame ok");
        serde_json::from_str(frame.to_text().unwrap()).unwrap()
    }

    async fn reply(socket: &mut WebSocketStream<tokio::net::TcpStream>, body: Value) {
        socket.send(Message::Text(body.to_string().into())).await.unwrap();
    }
```

(`Path`, `Instant`, `Arc`, `AtomicBool`, `WebSocketStream`, `Message`, `StreamExt`, `SinkExt`, `json`, `Value`, `Mirror`, `Cursor`, `RegistrationState`, `NativeSession`, `workspace`, `process`, `private_read` are already in scope of the test module via `use super::*;` — add any missing `use` next to the existing test imports.)

- [ ] **Step 2: Write the failing lost-ack test**

```rust
    #[tokio::test]
    async fn local_sessions_registration_ack_reconciles_a_lost_batch_ack() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            // The relay applied the 5-event batch whose ACK never reached this Mac.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":383,"serverTime":100,"reconcile":null})).await;
            let next = tokio::time::timeout(Duration::from_millis(1500), socket.next()).await;
            assert!(next.is_err(), "an already-applied batch must not be re-sent: {next:?}");
            socket.close(None).await.unwrap();
        });
        let events: Vec<Value> = (0..5)
            .map(|i| json!({"kind":"text","role":"assistant","text":format!("line {i}"),"truncated":false}))
            .collect();
        let mut mirror = test_mirror("native", "g1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("g1".into());
        mirror.after_seq = 378;
        mirror.queue = events.clone();
        mirror.pending = Some(json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
            "batchId":"b1","afterSeq":378,"events":events}));
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 383);
        assert!(stored.pending.is_none());
        assert!(stored.queue.is_empty());
        assert_eq!(stored.registration.acknowledged.as_deref(), Some("g1"));
    }
```

- [ ] **Step 3: Write the failing resync test**

```rust
    #[tokio::test]
    async fn local_sessions_irreconcilable_cursor_resyncs_and_mirrors_a_gap_marker() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let persisted = dir.path().join("local.json");
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            // Neither 378 nor 383: something else wrote this session's history.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":400,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            assert_eq!(batch["afterSeq"], 400);
            assert_eq!(batch["events"].as_array().unwrap().len(), 1);
            assert_eq!(batch["events"][0]["role"], "system");
            assert!(batch["events"][0]["text"].as_str().unwrap().contains("could not mirror"));
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":batch["batchId"],"afterSeq":401})).await;
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    tick.tick().await;
                    let m: Mirror = private_read(&persisted).unwrap();
                    if m.after_seq == 401 && m.pending.is_none() {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            socket.close(None).await.unwrap();
        });
        let events: Vec<Value> = (0..5)
            .map(|i| json!({"kind":"text","role":"assistant","text":format!("line {i}"),"truncated":false}))
            .collect();
        let mut mirror = test_mirror("native", "g1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("g1".into());
        mirror.after_seq = 378;
        mirror.queue = events.clone();
        mirror.pending = Some(json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
            "batchId":"b1","afterSeq":378,"events":events}));
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 401);
        assert!(stored.pending.is_none() && stored.queue.is_empty());
    }
```

- [ ] **Step 4: Run both to verify they fail**

Run the desktop lib command from Global Constraints with `-- reconciles_a_lost_batch_ack resyncs_and_mirrors` appended.
Expected: both FAIL (today the connection errors with `upload cursor conflict` and nothing is persisted at 383/401).

- [ ] **Step 5: Implement `Mirror::resync` and `relay_log`**

In `local_sessions.rs`, inside `impl Mirror` after `acknowledge`:

```rust
    /// Adopt the relay's cursor when neither "nothing applied" nor "the pending batch was
    /// applied" explains it. The local backlog is dropped; one system marker tells viewers.
    fn resync(&mut self, after: u64) {
        self.after_seq = after;
        self.pending = None;
        self.queue.clear();
        self.queue.push(json!({
            "kind": "text",
            "role": "system",
            "text": "[Lightbulb could not mirror some output: this Mac and the relay disagreed about the transcript cursor]",
            "truncated": false
        }));
    }
```

In `transport.rs` (module level, near `send`):

```rust
pub(super) fn relay_log(msg: &str) {
    crate::log(&format!("[local-sessions] {msg}"));
}

fn short(id: &str) -> &str {
    &id[..id.len().min(8)]
}
```

- [ ] **Step 6: Replace the cursor check in the ack handler**

In `connection`, the `Some("local.session.registered" | "local.session.heartbeat.ack")` arm currently has:

```rust
                            let after = body["afterSeq"].as_u64().ok_or("missing upload cursor")?;
                            if after != mirror.after_seq && !(mirror.pending.is_some() && after == mirror.after_seq + 1) {return Err("upload cursor conflict".into());}
```

Replace those two lines with:

```rust
                            let after = body["afterSeq"].as_u64().ok_or("missing upload cursor")?;
                            let pending_id = mirror.pending.as_ref().and_then(|p| p["batchId"].as_str()).unwrap_or("").to_owned();
                            let pending_count = mirror.pending.as_ref().and_then(|p| p["events"].as_array()).map(|e| e.len() as u64).unwrap_or(0);
                            if after == mirror.after_seq {
                                // Nothing was applied since the last ACK.
                            } else if pending_count > 0 && after == mirror.after_seq + pending_count {
                                // The relay applied the in-flight batch; its ACK never reached this Mac.
                                mirror.acknowledge(&pending_id, after)?;
                                relay_log(&format!("{} reconciled a lost ack: cursor {after}", short(&key)));
                            } else {
                                relay_log(&format!("{} cursor resync: local {} (+{pending_count} pending) relay {after}", short(&key), mirror.after_seq));
                                mirror.resync(after);
                            }
```

Everything after (`mirror.session_id = Some(sid.into()); if is_register {mirror.registered();} ...`) stays. `persist_changed` already follows and will write the reconciled state.

- [ ] **Step 7: Run the two tests, then the whole desktop suite (lib and probe separately)**

Expected: the two new tests pass; the existing `local_sessions_transport_registers_then_uploads_and_persists_ack` still passes; probe 9 passed.

- [ ] **Step 8: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/transport.rs
git commit -m "fix(desktop): reconcile a lost batch ack instead of wedging the relay"
```

---

### Task 3: Desktop — quarantine one session instead of dropping the connection

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (`connection`: registration selection at ~line 388, the ack arm, the `local.session.error` arm)
- Test: same file

**Interfaces:**
- Consumes: `relay_log`, `short`, test helpers from Task 2.
- Produces: a per-connection `quarantined: BTreeSet<String>`.

- [ ] **Step 1: Write the failing stale-generation test**

```rust
    #[tokio::test]
    async fn local_sessions_one_rejected_session_does_not_block_the_others() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a", "keys register in order");
            // A generation this Mac never sent: a per-session verdict, not a broken peer.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"other",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["type"], "local.session.register");
            assert_eq!(second["session"]["generation"], "g-b", "the next session must still register on this connection");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID_B,"generation":"g-b",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let quiet = tokio::time::timeout(Duration::from_millis(1200), socket.next()).await;
            assert!(quiet.is_err(), "the quarantined session must not be retried on this connection: {quiet:?}");
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![
            ("a-session", test_mirror("native-a", "g-a")),
            ("b-session", test_mirror("native-b", "g-b")),
        ]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let a: Mirror = private_read(&dir.path().join("a-session.json")).unwrap();
        let b: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert!(a.session_id.is_none() && a.registration.acknowledged.is_none());
        assert_eq!(b.session_id.as_deref(), Some(SID_B));
    }
```

- [ ] **Step 2: Write the failing error-frame tests**

```rust
    #[tokio::test]
    async fn local_sessions_error_frame_for_the_inflight_registration_quarantines_only_that_session() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a");
            // Old backends send no sessionId; the in-flight RPC identifies the session.
            reply(&mut socket, json!({"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT"})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["session"]["generation"], "g-b");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID_B,"generation":"g-b",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![
            ("a-session", test_mirror("native-a", "g-a")),
            ("b-session", test_mirror("native-b", "g-b")),
        ]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let b: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert_eq!(b.session_id.as_deref(), Some(SID_B));
    }

    #[tokio::test]
    async fn local_sessions_rejected_batch_keeps_the_connection_and_the_pending_batch() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            reply(&mut socket, json!({"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT","sessionId":SID})).await;
            // The client must not close the socket in response.
            let quiet = tokio::time::timeout(Duration::from_millis(1200), socket.next()).await;
            assert!(quiet.is_err(), "a rejected batch must not drop the connection: {quiet:?}");
            socket.close(None).await.unwrap();
        });
        let mut mirror = test_mirror("native", "g1");
        mirror.queue = vec![json!({"kind":"text","role":"assistant","text":"public output","truncated":false})];
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert!(stored.pending.is_some(), "the rejected batch stays pending for the next connection's reconciliation");
        assert_eq!(stored.after_seq, 0);
    }
```

- [ ] **Step 3: Run the three to verify they fail**

Expected: FAIL — today each case returns `Err` and closes the socket before the second registration / within the quiet window.

- [ ] **Step 4: Implement the quarantine set**

At the top of `connection` next to `registered`:

```rust
        let mut quarantined = std::collections::BTreeSet::<String>::new();
```

Registration selection (`if pending_rpc.is_none() { if let Some(key) = self.mirrors.keys().find(|key| !registered.contains_key(*key)) ...`) becomes:

```rust
                    .find(|key| !registered.contains_key(*key) && !quarantined.contains(*key))
```

Add a helper closure-free block used by the two paths below (write it as an inner `fn` at module level):

```rust
fn quarantine(
    quarantined: &mut std::collections::BTreeSet<String>,
    registered: &mut BTreeMap<String, Instant>,
    sent_batches: &mut BTreeMap<String, Instant>,
    key: &str,
    reason: &str,
) {
    relay_log(&format!("{} quarantined for this connection: {reason}", short(key)));
    quarantined.insert(key.to_owned());
    registered.remove(key);
    sent_batches.remove(key);
}
```

In the ack arm, the two per-session verdicts

```rust
                            if body["generation"] != mirror.generation {return Err("stale generation acknowledgment".into());}
                            ...
                            if mirror.session_id.as_ref().is_some_and(|known|known != sid) {return Err("session identity changed".into());}
```

become non-fatal. Restructure the start of the arm so that after `pending_rpc.take()`, the `is_register` check, `sid`, `after` and `server` are parsed (protocol faults keep `return Err`), then:

```rust
                            let verdict = if body["generation"] != mirror.generation {
                                Some("stale generation acknowledgment")
                            } else if mirror.session_id.as_ref().is_some_and(|known| known != sid) {
                                Some("session identity changed")
                            } else {
                                None
                            };
                            if let Some(reason) = verdict {
                                quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, reason);
                                continue;
                            }
```

(`mirror` must be re-borrowed after the `quarantine` call or the borrow checker complains; take `let mirror = self.mirrors.get_mut(&key)...` after the verdict block.) The `before` snapshot, the cursor reconciliation from Task 2, `registered()`, `advance()`, persistence, `registered.insert`, anchor and reconcile handling follow unchanged.

Replace the error arm:

```rust
                        Some("local.session.error") => {
                            let code = body["code"].as_str().unwrap_or("?").to_owned();
                            let named = body["sessionId"].as_str().and_then(|sid| {
                                self.mirrors.iter().find(|(_, m)| m.session_id.as_deref() == Some(sid)).map(|(k, _)| k.clone())
                            });
                            let inflight = pending_rpc.as_ref().map(|(k, ..)| k.clone());
                            let Some(key) = named.or(inflight) else {
                                relay_log(&format!("relay rejected an unattributable frame: {code}"));
                                return Err("local session relay rejected the frame".into());
                            };
                            if pending_rpc.as_ref().is_some_and(|(k, ..)| *k == key) {
                                pending_rpc = None;
                            }
                            quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, &format!("relay rejected a frame: {code}"));
                        },
```

- [ ] **Step 5: Run the three new tests, then the full desktop suite (lib and probe separately)**

Expected: all green. `local_sessions_transport_registers_then_uploads_and_persists_ack` unchanged and green.

- [ ] **Step 6: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/transport.rs
git commit -m "fix(desktop): quarantine one rejected session instead of dropping the relay connection"
```

---

### Task 4: Desktop — lifetime-based backoff, restart-forever supervisor, log lines

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (`supervise` at ~lines 236-327; `connection` open/close logging)
- Test: same file

**Interfaces:**
- Produces: `fn reconnect_delay(previous: u64, lived: Duration) -> u64`; `async fn run_once(&mut self, enrollment: &Enrollment, identity: &crate::keychain::DeviceIdentity, wake: Arc<Notify>, status: Arc<Mutex<String>>)` holding the current discovery + network body; `supervise` keeps its signature and loops over `run_once`.

- [ ] **Step 1: Write the failing backoff unit test**

```rust
    #[test]
    fn local_sessions_reconnect_delay_backs_off_unless_the_connection_lived() {
        assert_eq!(reconnect_delay(1, Duration::from_secs(0)), 2);
        assert_eq!(reconnect_delay(2, Duration::from_secs(5)), 4);
        assert_eq!(reconnect_delay(16, Duration::from_secs(29)), 30);
        assert_eq!(reconnect_delay(30, Duration::from_secs(1)), 30);
        assert_eq!(reconnect_delay(30, Duration::from_secs(30)), 1);
        assert_eq!(reconnect_delay(4, Duration::from_secs(600)), 1);
    }
```

- [ ] **Step 2: Write the failing supervisor test** (the gateway is unreachable, so every `run_once` iteration ends; `supervise` must keep looping and return promptly once `stop` is set)

```rust
    #[tokio::test]
    async fn local_sessions_supervisor_keeps_running_after_the_network_loop_ends_and_honours_stop() {
        let dir = tempfile::tempdir().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home: dir.path().into(),
            mirrors: BTreeMap::new(),
            stop: stop.clone(),
        };
        let enrollment = Enrollment {
            owner_id: "org".into(),
            human_user_id: "alice".into(),
            device_id: "mac".into(),
            gateway_url: "wss://127.0.0.1:1".into(),
            enabled: true,
            paused: false,
            server_revision: None,
            remote_disabled: false,
            attempt: None,
        };
        let identity = crate::keychain::generate_keypair();
        let status = Arc::new(std::sync::Mutex::new(String::new()));
        let wake = Arc::new(tokio::sync::Notify::new());
        let stopper = stop.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(2500)).await;
            stopper.store(true, Ordering::SeqCst);
        });
        tokio::time::timeout(Duration::from_secs(8), host.supervise(enrollment, identity, wake, status.clone()))
            .await
            .expect("supervise returns once stop is set");
        assert_eq!(status.lock().unwrap().as_str(), "disconnected");
    }
```

(`Enrollment` is `pub struct Enrollment` in `local_sessions.rs:216` with exactly those fields; `crate::keychain::generate_keypair()` returns a `DeviceIdentity`.) The test's intent: an unreachable gateway, `stop` flipped after ~2.5 s, `supervise` returns within 8 s, final status `disconnected`.

- [ ] **Step 3: Run both to verify they fail**

Expected: `reconnect_delay` does not exist (compile error); `supervise` today returns after the first connect failure only when `observing_wait` errors, so the second test either hangs to the 8 s timeout or panics on status.

- [ ] **Step 4: Implement**

Module level:

```rust
/// A connection that lived long enough was healthy: start over at one second. A short-lived one
/// (connect-then-die, the 2026-09-12 loop) doubles the wait, capped at 30 s.
fn reconnect_delay(previous: u64, lived: Duration) -> u64 {
    if lived >= Duration::from_secs(30) {
        1
    } else {
        (previous * 2).clamp(1, 30)
    }
}
```

Split `supervise`: move its whole current body (the `(tx, rx)` channel, `discovery`, `network`, and the final `tokio::select!`) into

```rust
    async fn run_once(
        &mut self,
        enrollment: &Enrollment,
        identity: &crate::keychain::DeviceIdentity,
        wake: Arc<tokio::sync::Notify>,
        status: Arc<std::sync::Mutex<String>>,
    )
```

adjusting the borrows (`enrollment.gateway_url`, `enrollment.device_id`, `enrollment.owner_id`, `identity.private_key_pem` are read through the references). Inside the network loop:

- remove the `delay = 1;` line that runs right after a successful connect;
- record `let started = Instant::now();` just before `self.connection(&mut socket, &mut rx).await` and after it compute `let lived = started.elapsed();`;
- after `result` is known: `relay_log(&format!("connection ended after {:.1}s: {}", lived.as_secs_f64(), match &result { Ok(()) => "paused".to_owned(), Err(e) => e.to_string() }));` and replace `delay = (delay * 2).min(30);` with `delay = reconnect_delay(delay, lived);` (a connect failure has `lived == Duration::ZERO`, so it still doubles);
- on a successful connect, before `self.connection(...)`: `relay_log(&format!("connected; {} mirrored sessions", self.mirrors.len()));`.

Then the new `supervise`:

```rust
    pub(super) async fn supervise(
        mut self,
        enrollment: Enrollment,
        identity: crate::keychain::DeviceIdentity,
        wake: Arc<tokio::sync::Notify>,
        status: Arc<std::sync::Mutex<String>>,
    ) {
        use futures_util::FutureExt;
        let mut restart = 1u64;
        loop {
            let outcome = std::panic::AssertUnwindSafe(self.run_once(&enrollment, &identity, wake.clone(), status.clone()))
                .catch_unwind()
                .await;
            if let Ok(mut s) = status.lock() {
                *s = "disconnected".into();
            }
            if self.stop.load(Ordering::SeqCst) {
                return;
            }
            relay_log(&match outcome {
                Ok(()) => format!("supervisor loop ended; restarting in {restart}s"),
                Err(_) => format!("supervisor loop panicked; restarting in {restart}s"),
            });
            tokio::time::sleep(Duration::from_secs(restart)).await;
            restart = (restart * 2).min(60);
        }
    }
```

If `run_once` cannot be made `UnwindSafe` because of a `&mut self` borrow through the wrapper, keep `AssertUnwindSafe` (it is the documented escape hatch) and add a one-line comment saying the host is persisted on every change so a torn iteration loses nothing durable.

Log lines still missing after Tasks 2-3 (add now, all through `relay_log`, ids truncated with `short`): registration acked (`"{} registered as {} at cursor {}"`), heartbeat acks are NOT logged (too chatty), events ack is NOT logged, `capture_batch` failures (`"{} history unreadable: {reason}"`).

- [ ] **Step 5: Run the two new tests, then the full desktop suite (lib and probe separately), then `cargo check`**

Expected: all green; no new warnings beyond the two baseline ones.

- [ ] **Step 6: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/transport.rs
git commit -m "fix(desktop): back off short-lived relay connections and keep the supervisor alive"
```

---

### Task 5: Verify, ship, and confirm against the wedged prod device

**Files:**
- Modify: this plan (tick boxes); PR body.

- [ ] **Step 1: Run every suite once more from the worktree root** (desktop lib and probe separately; backend three files). Record counts.

- [ ] **Step 2: Open the PR** from `fix/local-session-relay-reconcile` against `main` with the spec's §1-§3 as the body (what happened, root causes, the four changes, logging, compatibility) and the test counts. Watch CI (`gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`), including `TestDesktop`. Merge when green.

- [ ] **Step 3: Roll out backend first, then desktop.** After merge: wait for the dev deploy, dispatch `deploy.yml` on `main` when the deploy queue reads 0, watch it; then dispatch `desktop-build.yml` on `main`, watch it, install the `desktop-v0.4.*` DMG on this Mac (`scratchpad/install_dmg.sh <tag>` from the session scratchpad does download, verify, replace, relaunch).

- [ ] **Step 4: Prod acceptance (spec §5).** Within a minute of launch: the record `~/Library/Application Support/company.thelightbulb.desktop/local-sessions/<hash>/0ec3e5c1….json` shows `after_seq ≥ 383` and `pending: null`; a fresh `claude` session (spawn with a clean `env -i` as in the session scratchpad's `live_session.exp`) gets a `session_id` within seconds; `/tmp/lightbulb-desktop.log` shows `[local-sessions] connected`, `registered`, and a `reconciled a lost ack` or `cursor resync` line for the old session; API Gateway `$connect` events in `/aws/api-gateway/lightbulb-prod-websocket` drop back to a handful per five minutes.

- [ ] **Step 5: Delete the spec and this plan** in a follow-up commit on `main` (they are disposable per CLAUDE.md), keeping the PR description as the record.
