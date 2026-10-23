//! The bridge skeleton: one embedded engine instance per connected workspace, its key from
//! Secrets Manager, its `state.db` from and to S3, and one live bridge at a time (the lease).
//! AWS is faked here; the real clients are exercised only by compile.

use lightbulb_bridge::instances::Instances;
use lightbulb_bridge::store::{self, Blobs, Secrets};
use lightbulb_bridge::{http, lease, Cfg, Fut};
use lightbulb_connect::embed::{self, EmbedOpts, Instance};
use lightbulb_connect::identity::Identity;
use lightbulb_connect::roster::MemberKind;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::oneshot;

const ENV: &str = "test";
const ISSUER: &str = "http://127.0.0.1:9/connect";
const TOKEN: &str = "t0ken-for-tests";

/// The secrets by name, and the names whose read never answers.
#[derive(Default)]
struct FakeSecrets(Mutex<HashMap<String, String>>, Mutex<Vec<String>>);
impl Secrets for FakeSecrets {
    fn get(&self, name: &str) -> Fut<'_, String> {
        if self.1.lock().unwrap().iter().any(|n| n == name) {
            return Box::pin(std::future::pending());
        }
        let v = self.0.lock().unwrap().get(name).cloned();
        Box::pin(async move { v.ok_or_else(|| "no such secret".into()) })
    }
}

#[derive(Default)]
struct FakeBlobs {
    objects: Mutex<HashMap<String, Vec<u8>>>,
    puts: AtomicUsize,
}
impl Blobs for FakeBlobs {
    fn get(&self, key: &str) -> Fut<'_, Option<Vec<u8>>> {
        let v = self.objects.lock().unwrap().get(key).cloned();
        Box::pin(async move { Ok(v) })
    }
    fn put(&self, key: &str, bytes: Vec<u8>) -> Fut<'_, ()> {
        self.puts.fetch_add(1, Ordering::SeqCst);
        self.objects.lock().unwrap().insert(key.into(), bytes);
        Box::pin(async { Ok(()) })
    }
}

/// The containers-table row `__bridge__#lease`, with the same condition as the real put.
#[derive(Default)]
struct FakeLease {
    row: Mutex<Option<(String, i64)>>,
    attempts: Mutex<HashMap<String, usize>>,
}
impl lease::Table for FakeLease {
    fn put(&self, holder: &str, now: i64, expires_at: i64) -> Fut<'_, bool> {
        *self
            .attempts
            .lock()
            .unwrap()
            .entry(holder.into())
            .or_default() += 1;
        let mut row = self.row.lock().unwrap();
        let free = row.as_ref().is_none_or(|(h, e)| *e < now || h == holder);
        if free {
            *row = Some((holder.into(), expires_at));
        }
        Box::pin(async move { Ok(free) })
    }
    fn delete(&self, holder: &str) -> Fut<'_, ()> {
        let mut row = self.row.lock().unwrap();
        if row.as_ref().is_some_and(|(h, _)| h == holder) {
            *row = None;
        }
        Box::pin(async { Ok(()) })
    }
}

impl FakeLease {
    fn attempts(&self, holder: &str) -> usize {
        self.attempts
            .lock()
            .unwrap()
            .get(holder)
            .copied()
            .unwrap_or(0)
    }
}

/// The fakes two bridges share, and a backend that lists `owners`.
struct World {
    secrets: Arc<FakeSecrets>,
    blobs: Arc<FakeBlobs>,
    lease: Arc<FakeLease>,
    backend: String,
    scratch: tempfile::TempDir,
}

/// The seed of `owner`'s workspace key (any 32 bytes are an Ed25519 seed).
fn seed(owner: &str) -> [u8; 32] {
    let mut s = [7u8; 32];
    s[..owner.len().min(32)].copy_from_slice(&owner.as_bytes()[..owner.len().min(32)]);
    s
}

fn seed_b64(owner: &str) -> String {
    data_encoding::BASE64.encode(&seed(owner))
}

/// The member key's short form for `owner`'s seed, by the engine's own loader.
fn short_of(owner: &str) -> String {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(d.path().join("identity.key"), seed(owner)).unwrap();
    Identity::load_or_create(d.path()).unwrap().short
}

async fn world(owners: &[&str]) -> World {
    let secrets = Arc::new(FakeSecrets::default());
    for o in owners {
        secrets
            .0
            .lock()
            .unwrap()
            .insert(format!("lightbulb/{ENV}/network/bridge/{o}"), seed_b64(o));
    }
    let list: Vec<Value> = owners.iter().map(|o| json!({ "owner": o })).collect();
    let app = axum::Router::new().route(
        "/api/v1/internal/bridge/workspaces",
        axum::routing::get(move |h: axum::http::HeaderMap| {
            let list = list.clone();
            async move {
                // The bridge proves itself to the backend's internal door too.
                assert_eq!(h[http::HEADER], TOKEN);
                axum::Json(list)
            }
        }),
    );
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let backend = format!("http://{}", l.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    World {
        secrets,
        blobs: Default::default(),
        lease: Default::default(),
        backend,
        scratch: tempfile::tempdir().unwrap(),
    }
}

fn cfg(w: &World, holder: &str) -> Arc<Cfg> {
    Arc::new(Cfg {
        backend_url: w.backend.clone(),
        token: TOKEN.into(),
        env: ENV.into(),
        relay: None,
        issuer: ISSUER.into(),
        holder: holder.into(),
        scratch: w.scratch.path().join(holder),
        tick: Duration::from_millis(100),
        secrets: w.secrets.clone(),
        blobs: w.blobs.clone(),
        lease: w.lease.clone(),
    })
}

async fn op(i: &Instance, op: &str, body: Value) -> Value {
    let mut req = body;
    req["id"] = 1.into();
    req["op"] = op.into();
    let r = i.call(req).await;
    assert_eq!(r["ok"], true, "{op}: {r}");
    r
}

async fn status(instances: &Instances, owner: &str) -> Value {
    op(
        &instances.get(owner).await.expect(owner),
        "status",
        json!({}),
    )
    .await
}

/// Waits (up to 10 s) until `instances` runs exactly `n`.
async fn running(instances: &Instances, n: usize) {
    for _ in 0..1000 {
        if instances.owners().await.len() == n {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("never {n} instances: {:?}", instances.owners().await);
}

/// A state.db for `owner` as a previous bridge left it: an instance named "before" (its solo
/// network's name), snapshotted. Returns its team id.
async fn left_behind(w: &World, owner: &str) -> String {
    let d = tempfile::tempdir().unwrap();
    let home = d.path().join("home");
    std::fs::create_dir_all(&home).unwrap();
    std::fs::write(home.join("identity.key"), seed(owner)).unwrap();
    let opts = EmbedOpts {
        kind: MemberKind::Cloud,
        relay: None,
        issuer: ISSUER.into(),
        snapshot: None,
        name: "before".into(),
    };
    let i = embed::start(&home, opts).await.unwrap();
    let team = op(&i, "status", json!({})).await["team"]["id"]
        .as_str()
        .unwrap()
        .to_string();
    let snap = d.path().join("snap.db");
    op(&i, "snapshot", json!({ "path": snap })).await;
    i.shutdown().await;
    w.blobs
        .objects
        .lock()
        .unwrap()
        .insert(store::key(owner), std::fs::read(&snap).unwrap());
    team
}

/// Runs a bridge until the returned sender fires (its SIGTERM).
fn bridge(
    cfg: Arc<Cfg>,
) -> (
    Arc<Instances>,
    oneshot::Sender<()>,
    tokio::task::JoinHandle<()>,
) {
    let instances = Instances::new(cfg.clone());
    let (tx, rx) = oneshot::channel::<()>();
    let run = tokio::spawn(lease::run(cfg, instances.clone(), async move {
        let _ = rx.await;
    }));
    (instances, tx, run)
}

#[tokio::test(flavor = "multi_thread")]
async fn boot_restores_each_workspace_from_its_snapshot() {
    let w = world(&["org_a", "org_b"]).await;
    let team_a = left_behind(&w, "org_a").await;
    let team_b = left_behind(&w, "org_b").await;
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 2).await;
    assert_eq!(instances.owners().await, ["org_a", "org_b"]);
    for (owner, team) in [("org_a", team_a), ("org_b", team_b)] {
        let s = status(&instances, owner).await;
        // Its key is the workspace's, from Secrets Manager; its state is the snapshot's.
        assert_eq!(s["member_key_short"], short_of(owner).as_str());
        assert_eq!(s["team"]["id"], team.as_str());
        assert_eq!(s["team"]["name"], "before");
    }
    term.send(()).unwrap();
    run.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn missing_snapshot_starts_an_empty_instance() {
    let w = world(&["org_new"]).await;
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 1).await;
    let s = status(&instances, "org_new").await;
    assert_eq!(s["member_key_short"], short_of("org_new").as_str());
    // A fresh solo network, named for the workspace, saved at once (its genesis).
    assert_eq!(s["team"]["name"], "org_new");
    let first = w.blobs.objects.lock().unwrap()[&store::key("org_new")].clone();
    assert!(first.starts_with(b"SQLite format 3\0"));
    term.send(()).unwrap();
    run.await.unwrap();
    // A workspace whose key cannot be read starts nothing, and boot goes on without it.
    let w = world(&["org_ok", "org_nokey"]).await;
    w.secrets
        .0
        .lock()
        .unwrap()
        .remove(&format!("lightbulb/{ENV}/network/bridge/org_nokey"));
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 1).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(instances.owners().await, ["org_ok"]);
    term.send(()).unwrap();
    run.await.unwrap();
    // An empty object is no snapshot: the workspace stays down, and its object untouched.
    let w = world(&["org_empty"]).await;
    let key = store::key("org_empty");
    w.blobs.objects.lock().unwrap().insert(key.clone(), vec![]);
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(instances.owners().await.is_empty());
    term.send(()).unwrap();
    run.await.unwrap();
    assert!(w.blobs.objects.lock().unwrap()[&key].is_empty());
    // And `add` refuses on a task without the lease.
    let none = Instances::new(cfg(&w, "b9"));
    let ws = serde_json::from_value(json!({"owner": "org_ok"})).unwrap();
    assert!(none.add(ws).await.is_err());
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_is_debounced_and_flushed_on_sigterm() {
    // A burst of state-writing events is one snapshot, 1 s after it; stream bytes are none.
    let (tx, rx) = tokio::sync::broadcast::channel(64);
    let n = Arc::new(AtomicUsize::new(0));
    let n2 = n.clone();
    tokio::spawn(store::debounce(rx, move || {
        let n = n2.clone();
        async move {
            n.fetch_add(1, Ordering::SeqCst);
        }
    }));
    for _ in 0..5 {
        tx.send(json!({"event": "message", "to_session": "friday"}))
            .unwrap();
        tx.send(json!({"event": "stream", "data_b64": "AA=="}))
            .unwrap();
    }
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(n.load(Ordering::SeqCst), 0, "not before the second is up");
    tokio::time::sleep(Duration::from_millis(1000)).await;
    assert_eq!(n.load(Ordering::SeqCst), 1);
    tx.send(json!({"event": "stream", "data_b64": "AA=="}))
        .unwrap();
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert_eq!(n.load(Ordering::SeqCst), 1, "stream bytes write no state");

    // SIGTERM leaves each instance's final state.db in S3, then frees the lease.
    let w = world(&["org_a"]).await;
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 1).await;
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert_eq!(
        w.blobs.puts.load(Ordering::SeqCst),
        1,
        "idle: the genesis only"
    );
    term.send(()).unwrap();
    run.await.unwrap();
    assert_eq!(w.blobs.puts.load(Ordering::SeqCst), 2, "and the final one");
    let db = w.blobs.objects.lock().unwrap()[&store::key("org_a")].clone();
    assert!(db.starts_with(b"SQLite format 3\0"));
    assert!(w.lease.row.lock().unwrap().is_none(), "lease released");
    assert!(instances.owners().await.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn second_bridge_waits_without_the_lease() {
    let w = world(&["org_a"]).await;
    let (one, term1, run1) = bridge(cfg(&w, "b1"));
    running(&one, 1).await;
    let (two, term2, run2) = bridge(cfg(&w, "b2"));
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(two.owners().await.is_empty(), "two bridges, one key");
    assert!(w.lease.attempts("b2") >= 3, "it retries every tick");
    // The first stops (a deploy): the second takes over, from the first's final snapshot.
    term1.send(()).unwrap();
    run1.await.unwrap();
    running(&two, 1).await;
    assert_eq!(w.lease.row.lock().unwrap().as_ref().unwrap().0, "b2");
    term2.send(()).unwrap();
    run2.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn hung_secret_read_blocks_nothing() {
    let w = world(&["org_a", "org_hang"]).await;
    w.secrets
        .1
        .lock()
        .unwrap()
        .push(format!("lightbulb/{ENV}/network/bridge/org_hang"));
    let (one, term1, run1) = bridge(cfg(&w, "b1"));
    // The others boot, and `get` answers while one start hangs.
    running(&one, 1).await;
    let s = tokio::time::timeout(Duration::from_secs(2), status(&one, "org_a")).await;
    assert!(s.is_ok(), "get waited on a hung add");
    // Renewals go on, so a second bridge cannot take the lease meanwhile.
    let before = w.lease.attempts("b1");
    let (two, term2, run2) = bridge(cfg(&w, "b2"));
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(w.lease.attempts("b1") >= before + 4, "renewals stalled");
    assert!(two.owners().await.is_empty());
    assert_eq!(w.lease.row.lock().unwrap().as_ref().unwrap().0, "b1");
    // SIGTERM is not held up by the hung start either.
    term1.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(5), run1)
        .await
        .expect("SIGTERM waited on boot")
        .unwrap();
    term2.send(()).unwrap();
    run2.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn lost_lease_stops_every_instance() {
    let w = world(&["org_a", "org_b"]).await;
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 2).await;
    let puts = w.blobs.puts.load(Ordering::SeqCst);
    *w.lease.row.lock().unwrap() = Some(("b2".into(), i64::MAX));
    running(&instances, 0).await;
    // And it waits: nothing starts while another holds the lease.
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert!(instances.owners().await.is_empty());
    // Another holder is live: our copies are stale and must not overwrite its snapshots.
    assert_eq!(
        w.blobs.puts.load(Ordering::SeqCst),
        puts,
        "no upload after a refusal"
    );
    term.send(()).unwrap();
    run.await.unwrap();
    assert_eq!(w.lease.row.lock().unwrap().as_ref().unwrap().0, "b2");
}

async fn serve() -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(l, http::router(TOKEN.into())).await.unwrap() });
    url
}

/// The status of `method url` with `headers` and `body`.
fn hit(method: &str, url: &str, headers: &[(&str, &str)], body: &str) -> u16 {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .http_status_as_error(false)
        .build()
        .into();
    let r = match method {
        "GET" => headers
            .iter()
            .fold(agent.get(url), |r, (k, v)| r.header(*k, *v))
            .call(),
        _ => headers
            .iter()
            .fold(agent.post(url), |r, (k, v)| r.header(*k, *v))
            .send(body),
    };
    r.unwrap().status().as_u16()
}

#[tokio::test(flavor = "multi_thread")]
async fn internal_routes_refuse_without_the_token() {
    let url = serve().await;
    let codes = tokio::task::spawn_blocking(move || {
        [
            hit("GET", &format!("{url}/health"), &[], ""),
            hit("POST", &format!("{url}/bridge/connect"), &[], "{}"),
            hit(
                "POST",
                &format!("{url}/bridge/connect"),
                &[(http::HEADER, "wrong")],
                "{}",
            ),
            hit(
                "POST",
                &format!("{url}/bridge/connect"),
                &[(http::HEADER, "")],
                "{}",
            ),
            // With the token, past the door (no such route yet).
            hit(
                "POST",
                &format!("{url}/bridge/connect"),
                &[(http::HEADER, TOKEN)],
                "{}",
            ),
        ]
    })
    .await
    .unwrap();
    assert_eq!(codes, [200, 401, 401, 401, 404]);
}

#[tokio::test(flavor = "multi_thread")]
async fn bridge_logs_carry_no_bodies() {
    lightbulb_bridge::capture_logs();
    const MARKER: &str = "MARKER-7f3a9c";
    let url = serve().await;
    tokio::task::spawn_blocking(move || {
        for token in ["", TOKEN] {
            let target = format!("{url}/bridge/connect?ticket={MARKER}");
            hit(
                "POST",
                &target,
                &[(http::HEADER, token), ("x-note", MARKER)],
                &json!({ "text": MARKER }).to_string(),
            );
        }
    })
    .await
    .unwrap();
    // A boot and a snapshot log the owner, never the key it read.
    let w = world(&["org_logs"]).await;
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    running(&instances, 1).await;
    term.send(()).unwrap();
    run.await.unwrap();
    let logs = lightbulb_bridge::logs();
    assert!(
        logs.iter().any(|l| l.contains("/bridge/connect")),
        "{logs:?}"
    );
    assert!(logs.iter().any(|l| l.contains("org_logs")), "{logs:?}");
    for secret in [MARKER, TOKEN, &seed_b64("org_logs")] {
        assert!(
            !logs.iter().any(|l| l.contains(secret)),
            "{secret} in {logs:?}"
        );
    }
}

/// Step 4's memory ceiling, by hand: `BRIDGE_RSS_N=<n> cargo test --test instances
/// idle_rss -- --ignored --nocapture` prints this process's RSS before and after `n` idle
/// instances.
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn idle_rss() {
    let n: usize = std::env::var("BRIDGE_RSS_N").map_or(1, |v| v.parse().unwrap());
    let owners: Vec<String> = (0..n).map(|i| format!("org_rss{i}")).collect();
    let refs: Vec<&str> = owners.iter().map(String::as_str).collect();
    let w = world(&refs).await;
    let rss = || {
        let out = std::process::Command::new("ps")
            .args(["-o", "rss=", "-p", &std::process::id().to_string()])
            .output()
            .unwrap();
        String::from_utf8(out.stdout).unwrap().trim().to_string()
    };
    tokio::time::sleep(Duration::from_secs(5)).await;
    println!("rss_kib before={}", rss());
    let (instances, term, run) = bridge(cfg(&w, "b1"));
    let t = std::time::Instant::now();
    running(&instances, n).await;
    println!("boot of {n}: {:?}", t.elapsed());
    tokio::time::sleep(Duration::from_secs(30)).await;
    println!("rss_kib with {n} idle={}", rss());
    term.send(()).unwrap();
    run.await.unwrap();
}
