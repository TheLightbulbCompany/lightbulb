//! One embedded engine instance per connected workspace: its key from Secrets Manager, its
//! `state.db` from S3, a fresh home on this task's disk. Instances run only while this task
//! holds the lease (`set_active`).

use crate::{log, store, Cfg, Result};
use lightbulb_connect::embed::{self, EmbedOpts, Instance};
use lightbulb_connect::roster::MemberKind;
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::task::{JoinHandle, JoinSet};

/// Workspaces starting at once during a boot.
const BOOT_CONCURRENCY: usize = 8;
/// The backend list's time limit.
const LIST_TIMEOUT: Duration = Duration::from_secs(10);
/// All of a `stop_all`: under ECS's 30 s stop timeout, with room for the lease release.
const STOP_DEADLINE: Duration = Duration::from_secs(20);

/// Starts that failed since this task began (a log field until Task 12's metrics).
static START_FAILURES: AtomicU64 = AtomicU64::new(0);

/// A connected workspace, as `GET /api/v1/internal/bridge/workspaces` lists it.
#[derive(Clone, Debug, Deserialize)]
pub struct Workspace {
    pub owner: String,
    /// Its display name, the computer name peers see; the owner id when the backend sends none.
    #[serde(default)]
    pub name: Option<String>,
}

pub struct Instances {
    cfg: Arc<Cfg>,
    live: tokio::sync::Mutex<BTreeMap<String, Live>>,
    /// Owners whose start is under way (outside the `live` lock).
    starting: Mutex<BTreeSet<String>>,
    /// Whether this task holds the lease: set and cleared by `lease::run`.
    active: AtomicBool,
}

struct Live {
    instance: Arc<Instance>,
    /// `home/` (the engine's) and `state.db` (its snapshots) beside it.
    dir: PathBuf,
    snapper: JoinHandle<()>,
}

/// An owner's place in `starting`, given back however the start ends (an abort included).
struct Claim<'a>(&'a Instances, String);
impl Drop for Claim<'_> {
    fn drop(&mut self) {
        self.0.starting.lock().unwrap().remove(&self.1);
    }
}

impl Instances {
    pub fn new(cfg: Arc<Cfg>) -> Arc<Instances> {
        Arc::new(Instances {
            cfg,
            live: Default::default(),
            starting: Default::default(),
            active: AtomicBool::new(false),
        })
    }

    pub(crate) fn set_active(&self, on: bool) {
        self.active.store(on, Ordering::SeqCst);
    }

    /// Starts every connected workspace's instance, up to `BOOT_CONCURRENCY` at once. `Err`
    /// only when the list cannot be read; a workspace that fails to start is logged and left
    /// out.
    pub async fn boot(self: &Arc<Self>) -> Result<()> {
        let list = workspaces(&self.cfg).await?;
        let gate = Arc::new(tokio::sync::Semaphore::new(BOOT_CONCURRENCY));
        let mut starts = JoinSet::new();
        for w in list {
            let (me, gate) = (self.clone(), gate.clone());
            starts.spawn(async move {
                let _permit = gate.acquire_owned().await;
                let owner = if valid_owner(&w.owner) {
                    w.owner.clone()
                } else {
                    format!("<invalid, {} bytes>", w.owner.len())
                };
                if let Err(e) = me.add(w).await {
                    let n = START_FAILURES.fetch_add(1, Ordering::Relaxed) + 1;
                    log!("instance owner={owner} start failed start_failures={n}: {e}");
                }
            });
        }
        starts.join_all().await;
        Ok(())
    }

    pub async fn get(&self, owner: &str) -> Option<Arc<Instance>> {
        self.live
            .lock()
            .await
            .get(owner)
            .map(|l| l.instance.clone())
    }

    pub async fn owners(&self) -> Vec<String> {
        self.live.lock().await.keys().cloned().collect()
    }

    /// Starts `w`'s instance (a no-op while it runs or starts). Refused without the lease.
    pub async fn add(&self, w: Workspace) -> Result<()> {
        if !valid_owner(&w.owner) {
            return Err("not an owner id".into());
        }
        let _claim = {
            let live = self.live.lock().await;
            let mut starting = self.starting.lock().unwrap();
            if live.contains_key(&w.owner) || !starting.insert(w.owner.clone()) {
                return Ok(());
            }
            Claim(self, w.owner.clone())
        };
        if !self.active.load(Ordering::SeqCst) {
            return Err("this task does not hold the lease".into());
        }
        let l = start(&self.cfg, &w).await?;
        let mut live = self.live.lock().await;
        // The lease may have gone while it started: then it never runs.
        if !self.active.load(Ordering::SeqCst) {
            drop(live);
            stop(self.cfg.clone(), w.owner, l, false).await;
            return Err("the lease was lost during the start".into());
        }
        live.insert(w.owner, l);
        Ok(())
    }

    /// Stops `owner`'s instance and puts its final snapshot. Whether one ran.
    pub async fn remove(&self, owner: &str) -> bool {
        let l = self.live.lock().await.remove(owner);
        match l {
            Some(l) => {
                stop(self.cfg.clone(), owner.to_string(), l, true).await;
                true
            }
            None => false,
        }
    }

    /// Stops every instance at once, within `STOP_DEADLINE` (one past it is dropped: its
    /// endpoint closes, with no final snapshot). `flush` puts each one's final snapshot, only
    /// while this task surely holds the lease: a stale copy must never overwrite the next
    /// holder's.
    pub async fn stop_all(&self, flush: bool) {
        let all = std::mem::take(&mut *self.live.lock().await);
        // No debounced snapshot from here on, for any of them.
        for l in all.values() {
            l.snapper.abort();
        }
        let mut stops = JoinSet::new();
        for (owner, l) in all {
            stops.spawn(stop(self.cfg.clone(), owner, l, flush));
        }
        if tokio::time::timeout(STOP_DEADLINE, stops.join_all())
            .await
            .is_err()
        {
            log!(
                "stop_all past {}s: the rest dropped",
                STOP_DEADLINE.as_secs()
            );
        }
    }
}

/// `GET {BACKEND_INTERNAL_URL}/api/v1/internal/bridge/workspaces` -> `[Workspace]`, within
/// `LIST_TIMEOUT`. Its errors quote nothing from the body.
async fn workspaces(cfg: &Cfg) -> Result<Vec<Workspace>> {
    let url = format!("{}/api/v1/internal/bridge/workspaces", cfg.backend_url);
    let token = cfg.token.clone();
    let body = tokio::task::spawn_blocking(move || -> Result<String> {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(LIST_TIMEOUT))
            .build()
            .into();
        Ok(agent
            .get(&url)
            .header(crate::http::HEADER, &token)
            .call()?
            .body_mut()
            .read_to_string()?)
    })
    .await??;
    serde_json::from_str(&body).map_err(|e| {
        format!(
            "bad workspaces list ({:?} at {}:{})",
            e.classify(),
            e.line(),
            e.column()
        )
        .into()
    })
}

/// An owner id is a path segment, a secret name's and an S3 key's: `[A-Za-z0-9_-]{1,64}`.
fn valid_owner(o: &str) -> bool {
    (1..=64).contains(&o.len())
        && o.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn write_0600(path: &Path, bytes: &[u8]) -> Result<()> {
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?
        .write_all(bytes)?;
    Ok(())
}

async fn start(cfg: &Arc<Cfg>, w: &Workspace) -> Result<Live> {
    let t0 = Instant::now();
    let owner = &w.owner;
    let dir = cfg.scratch.join(owner);
    let home = dir.join("home");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&home)?;
    let name = format!("lightbulb/{}/network/bridge/{owner}", cfg.env);
    let secret = store::within(cfg.secrets.get(&name)).await?;
    let seed = data_encoding::BASE64
        .decode(secret.trim().as_bytes())
        .ok()
        .filter(|s| s.len() == 32)
        .ok_or("the workspace key is not a base64 32-byte seed")?;
    write_0600(&home.join("identity.key"), &seed)?;
    let restored = match store::within(cfg.blobs.get(&store::key(owner))).await? {
        // An empty or foreign object is no snapshot: a fresh network would fork it off its own.
        Some(db) if !db.starts_with(b"SQLite format 3\0") => {
            return Err(format!("the snapshot is not a database ({} bytes)", db.len()).into())
        }
        Some(db) => {
            write_0600(&home.join("state.db"), &db)?;
            Some(db.len())
        }
        None => None,
    };
    let snap = dir.join("state.db");
    let opts = EmbedOpts {
        kind: MemberKind::Cloud,
        relay: cfg.relay.as_deref().map(str::parse).transpose()?,
        issuer: cfg.issuer.clone(),
        snapshot: Some(snap.clone()),
        name: w.name.clone().unwrap_or_else(|| owner.clone()),
    };
    let instance = Arc::new(embed::start(&home, opts).await?);
    // A new network's genesis is saved at once, not at its first event.
    if restored.is_none() {
        if let Err(e) = store::snapshot(&*cfg.blobs, owner, &instance, &snap).await {
            log!("snapshot owner={owner} first failed: {e}");
        }
    }
    // The snapper holds the instance weakly: `stop` must be able to take it back.
    let weak = Arc::downgrade(&instance);
    let (cfg2, owner2) = (cfg.clone(), owner.clone());
    let snapper = tokio::spawn(store::debounce(instance.events(), move || {
        let (weak, cfg, owner, snap) = (weak.clone(), cfg2.clone(), owner2.clone(), snap.clone());
        async move {
            let Some(i) = weak.upgrade() else { return };
            if let Err(e) = store::snapshot(&*cfg.blobs, &owner, &i, &snap).await {
                log!("snapshot owner={owner} failed: {e}");
            }
        }
    }));
    log!(
        "instance owner={owner} started restored_bytes={} {}ms",
        restored.map_or("none".into(), |n| n.to_string()),
        t0.elapsed().as_millis()
    );
    Ok(Live {
        instance,
        dir,
        snapper,
    })
}

async fn stop(cfg: Arc<Cfg>, owner: String, l: Live, flush: bool) {
    l.snapper.abort();
    let _ = l.snapper.await;
    // A route mid-call holds a clone for that call; wait it out, up to 5 s.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut a = l.instance;
    let instance = loop {
        match Arc::try_unwrap(a) {
            Ok(i) => break Some(i),
            Err(b) if Instant::now() < deadline => {
                a = b;
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            Err(_) => break None,
        }
    };
    match instance {
        // `shutdown` leaves the final snapshot at `dir/state.db`.
        Some(i) => {
            i.shutdown().await;
            if flush {
                if let Err(e) = store::upload(&*cfg.blobs, &owner, &l.dir.join("state.db")).await {
                    log!("snapshot owner={owner} final failed: {e}");
                }
            }
        }
        None => log!("instance owner={owner} busy at stop: dropped, no final snapshot"),
    }
    let _ = std::fs::remove_dir_all(&l.dir);
    log!("instance owner={owner} stopped flush={flush}");
}
