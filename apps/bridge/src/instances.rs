//! One embedded engine instance per connected workspace: its key from Secrets Manager, its
//! `state.db` from S3, a fresh home on this task's disk. Instances run only while this task
//! holds the lease (`set_active`), and upload only while it is surely valid (`set_until`).

use crate::store::{self, Snap};
use crate::{blocking, log, Cfg, Result};
use lightbulb_connect::embed::{self, EmbedOpts, Instance};
use lightbulb_connect::roster::MemberKind;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Notify;
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::Instant;

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
    /// Until when an upload cannot overwrite a newer holder's: kept by `lease::run`.
    until: Arc<Mutex<Instant>>,
}

struct Live {
    instance: Arc<Instance>,
    /// Rung each time a `Lent` is given back.
    returned: Arc<Notify>,
    snap: Arc<Snap>,
    snapper: JoinHandle<()>,
}

/// An instance lent to a caller (a route) for one call.
pub struct Lent(Arc<Instance>, Arc<Notify>);

impl std::ops::Deref for Lent {
    type Target = Instance;
    fn deref(&self) -> &Instance {
        &self.0
    }
}

impl Drop for Lent {
    fn drop(&mut self) {
        self.1.notify_one();
    }
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
            until: Arc::new(Mutex::new(Instant::now())),
        })
    }

    pub(crate) fn set_active(&self, on: bool) {
        self.active.store(on, Ordering::SeqCst);
    }

    pub(crate) fn set_until(&self, t: Instant) {
        *self.until.lock().unwrap() = t;
    }

    /// Starts every connected workspace's instance, up to `BOOT_CONCURRENCY` at once. `Err`
    /// only when the list cannot be read; a workspace that fails to start is left out.
    pub async fn boot(self: &Arc<Self>) -> Result<()> {
        let list = workspaces(&self.cfg).await?;
        let gate = Arc::new(tokio::sync::Semaphore::new(BOOT_CONCURRENCY));
        let mut starts = JoinSet::new();
        for w in list {
            let (me, gate) = (self.clone(), gate.clone());
            starts.spawn(async move {
                let _permit = gate.acquire_owned().await;
                let _ = me.add(w).await;
            });
        }
        starts.join_all().await;
        Ok(())
    }

    pub async fn get(&self, owner: &str) -> Option<Lent> {
        self.live
            .lock()
            .await
            .get(owner)
            .map(|l| Lent(l.instance.clone(), l.returned.clone()))
    }

    pub async fn owners(&self) -> Vec<String> {
        self.live.lock().await.keys().cloned().collect()
    }

    /// Starts `w`'s instance (a no-op while it runs or starts). Refused without the lease.
    /// A failure is logged here, by a label that quotes no invalid owner.
    pub async fn add(&self, w: Workspace) -> Result<()> {
        let (label, r) = if valid_owner(&w.owner) {
            (w.owner.clone(), self.try_add(w).await)
        } else {
            let label = format!("<invalid, {} bytes>", w.owner.len());
            (label, Err("not an owner id".into()))
        };
        if let Err(e) = &r {
            let n = START_FAILURES.fetch_add(1, Ordering::Relaxed) + 1;
            log!("instance owner={label} start failed start_failures={n}: {e}");
        }
        r
    }

    async fn try_add(&self, w: Workspace) -> Result<()> {
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
        let (instance, restored) = start(&self.cfg, &w).await?;
        let mut live = self.live.lock().await;
        // The lease may have gone while it started: then it never runs, and uploads nothing.
        if !self.active.load(Ordering::SeqCst) {
            drop(live);
            instance.shutdown().await;
            let d = dir(&self.cfg, &w.owner);
            let _ = blocking(move || Ok(std::fs::remove_dir_all(d)?)).await;
            return Err("the lease was lost during the start".into());
        }
        let instance = Arc::new(instance);
        let snap = Arc::new(Snap {
            blobs: self.cfg.blobs.clone(),
            owner: w.owner.clone(),
            path: dir(&self.cfg, &w.owner).join("state.db"),
            until: self.until.clone(),
            sent: Mutex::new(restored),
        });
        // A new network's genesis goes up at once: only now, with the lease re-checked. The
        // snapper holds the instance weakly: `stop` must be able to take it back.
        let (weak, s) = (Arc::downgrade(&instance), snap.clone());
        let first = restored.is_none();
        let snapper = tokio::spawn(store::debounce(instance.events(), first, move || {
            let (weak, s) = (weak.clone(), s.clone());
            async move {
                let Some(i) = weak.upgrade() else { return };
                if let Err(e) = s.take(&i).await {
                    log!("snapshot owner={} failed: {e}", s.owner);
                }
            }
        }));
        let returned = Default::default();
        live.insert(
            w.owner,
            Live {
                instance,
                returned,
                snap,
                snapper,
            },
        );
        Ok(())
    }

    /// Stops `owner`'s instance and puts its final snapshot. Whether one ran.
    pub async fn remove(&self, owner: &str) -> bool {
        let l = self.live.lock().await.remove(owner);
        match l {
            Some(l) => {
                stop(self.cfg.clone(), owner.to_string(), l).await;
                true
            }
            None => false,
        }
    }

    /// Stops every instance at once, within `STOP_DEADLINE` (one past it is dropped: its
    /// endpoint closes, with no final snapshot). Each final snapshot goes up only while the
    /// lease is still surely ours (`set_until`).
    pub async fn stop_all(&self) {
        let all = std::mem::take(&mut *self.live.lock().await);
        // No debounced snapshot from here on, for any of them.
        for l in all.values() {
            l.snapper.abort();
        }
        let mut stops = JoinSet::new();
        for (owner, l) in all {
            stops.spawn(stop(self.cfg.clone(), owner, l));
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
    let body = blocking(move || {
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
    .await?;
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

/// `owner`'s dir on this task: `home/` (the engine's) and `state.db` (its snapshots).
fn dir(cfg: &Cfg, owner: &str) -> PathBuf {
    cfg.scratch.join(owner)
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

/// Starts `w`'s instance in a fresh home, with the SHA-256 of the snapshot it restored.
async fn start(cfg: &Cfg, w: &Workspace) -> Result<(Instance, Option<[u8; 32]>)> {
    let t0 = Instant::now();
    let owner = &w.owner;
    let dir = dir(cfg, owner);
    let home = dir.join("home");
    let name = format!("lightbulb/{}/network/bridge/{owner}", cfg.env);
    let (secret, db) = tokio::join!(cfg.secrets.get(&name), cfg.blobs.get(&store::key(owner)));
    let seed = data_encoding::BASE64
        .decode(secret?.trim().as_bytes())
        .ok()
        .filter(|s| s.len() == 32)
        .ok_or("the workspace key is not a base64 32-byte seed")?;
    let db = db?;
    // An empty or foreign object is no snapshot: a fresh network would fork it off its own.
    if let Some(db) = db.as_ref().filter(|d| !d.starts_with(b"SQLite format 3\0")) {
        return Err(format!("the snapshot is not a database ({} bytes)", db.len()).into());
    }
    let restored: Option<(usize, [u8; 32])> =
        db.as_ref().map(|d| (d.len(), Sha256::digest(d).into()));
    let (d, h) = (dir.clone(), home.clone());
    blocking(move || {
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&h)?;
        write_0600(&h.join("identity.key"), &seed)?;
        if let Some(db) = db {
            write_0600(&h.join("state.db"), &db)?;
        }
        Ok(())
    })
    .await?;
    let opts = EmbedOpts {
        kind: MemberKind::Cloud,
        relay: cfg.relay.as_deref().map(str::parse).transpose()?,
        issuer: cfg.issuer.clone(),
        snapshot: Some(dir.join("state.db")),
        name: w.name.clone().unwrap_or_else(|| owner.clone()),
    };
    let instance = embed::start(&home, opts).await?;
    log!(
        "instance owner={owner} started restored_bytes={} {}ms",
        restored.map_or("none".into(), |(n, _)| n.to_string()),
        t0.elapsed().as_millis()
    );
    Ok((instance, restored.map(|(_, sum)| sum)))
}

async fn stop(cfg: Arc<Cfg>, owner: String, l: Live) {
    l.snapper.abort();
    let _ = l.snapper.await;
    // A route mid-call holds a `Lent` for that call; wait for it, up to 5 s.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut a = l.instance;
    let instance = loop {
        match Arc::try_unwrap(a) {
            Ok(i) => break Some(i),
            Err(b) => {
                a = b;
                if tokio::time::timeout_at(deadline, l.returned.notified())
                    .await
                    .is_err()
                {
                    break Arc::try_unwrap(a).ok();
                }
            }
        }
    };
    match instance {
        // `shutdown` leaves the final snapshot at `snap.path`.
        Some(i) => {
            i.shutdown().await;
            if let Err(e) = l.snap.upload().await {
                log!("snapshot owner={owner} final failed: {e}");
            }
        }
        None => log!("instance owner={owner} busy at stop: dropped, no final snapshot"),
    }
    let d = dir(&cfg, &owner);
    let _ = blocking(move || Ok(std::fs::remove_dir_all(d)?)).await;
    log!("instance owner={owner} stopped");
}
