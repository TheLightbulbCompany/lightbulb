//! One embedded engine instance per connected workspace: its key from Secrets Manager, its
//! `state.db` from S3, a fresh home on this task's disk.

use crate::{log, store, Cfg, Result};
use lightbulb_connect::embed::{self, EmbedOpts, Instance};
use lightbulb_connect::roster::MemberKind;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::task::JoinHandle;

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
}

struct Live {
    instance: Arc<Instance>,
    /// `home/` (the engine's) and `state.db` (its snapshots) beside it.
    dir: PathBuf,
    snapper: JoinHandle<()>,
}

impl Instances {
    pub fn new(cfg: Arc<Cfg>) -> Arc<Instances> {
        Arc::new(Instances {
            cfg,
            live: Default::default(),
        })
    }

    /// Starts every connected workspace's instance. `Err` only when the list cannot be read;
    /// a workspace that fails to start is logged and left out.
    pub async fn boot(&self) -> Result<()> {
        for w in workspaces(&self.cfg).await? {
            let owner = w.owner.clone();
            if let Err(e) = self.add(w).await {
                log!("instance owner={owner} start failed: {e}");
            }
        }
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

    /// Starts `w`'s instance (a no-op while it runs).
    pub async fn add(&self, w: Workspace) -> Result<()> {
        let mut live = self.live.lock().await;
        if live.contains_key(&w.owner) {
            return Ok(());
        }
        let l = start(&self.cfg, &w).await?;
        live.insert(w.owner, l);
        Ok(())
    }

    /// Stops `owner`'s instance and puts its final snapshot. Whether one ran.
    pub async fn remove(&self, owner: &str) -> bool {
        let l = self.live.lock().await.remove(owner);
        match l {
            Some(l) => {
                stop(&self.cfg, owner, l, true).await;
                true
            }
            None => false,
        }
    }

    /// Stops every instance; `flush` puts each one's final snapshot (only while this task
    /// surely holds the lease: a stale copy must never overwrite the next holder's).
    pub async fn stop_all(&self, flush: bool) {
        let all = std::mem::take(&mut *self.live.lock().await);
        for (owner, l) in all {
            stop(&self.cfg, &owner, l, flush).await;
        }
    }
}

/// `GET {BACKEND_INTERNAL_URL}/api/v1/internal/bridge/workspaces` -> `[Workspace]`.
async fn workspaces(cfg: &Cfg) -> Result<Vec<Workspace>> {
    let url = format!("{}/api/v1/internal/bridge/workspaces", cfg.backend_url);
    let token = cfg.token.clone();
    let body = tokio::task::spawn_blocking(move || -> Result<String> {
        Ok(ureq::get(&url)
            .header(crate::http::HEADER, &token)
            .call()?
            .body_mut()
            .read_to_string()?)
    })
    .await??;
    Ok(serde_json::from_str(&body)?)
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
    if !valid_owner(owner) {
        return Err("not an owner id".into());
    }
    let dir = cfg.scratch.join(owner);
    let home = dir.join("home");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&home)?;
    let secret = cfg
        .secrets
        .get(&format!("lightbulb/{}/network/bridge/{owner}", cfg.env))
        .await?;
    let seed = data_encoding::BASE64
        .decode(secret.trim().as_bytes())
        .ok()
        .filter(|s| s.len() == 32)
        .ok_or("the workspace key is not a base64 32-byte seed")?;
    write_0600(&home.join("identity.key"), &seed)?;
    let restored = match cfg.blobs.get(&store::key(owner)).await? {
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

async fn stop(cfg: &Cfg, owner: &str, l: Live, flush: bool) {
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
                if let Err(e) = store::upload(&*cfg.blobs, owner, &l.dir.join("state.db")).await {
                    log!("snapshot owner={owner} final failed: {e}");
                }
            }
        }
        None => log!("instance owner={owner} busy at stop: dropped, no final snapshot"),
    }
    let _ = std::fs::remove_dir_all(&l.dir);
    log!("instance owner={owner} stopped flush={flush}");
}
