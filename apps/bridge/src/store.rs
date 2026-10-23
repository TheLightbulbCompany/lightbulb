//! Keys from Secrets Manager, `state.db` to and from S3 (`bridge-state/<owner>/state.db`),
//! and the debounced snapshot after state-writing events.

use crate::{log, Fut, Result};
use lightbulb_connect::embed::Instance;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::future::Future;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::broadcast::{error::RecvError, Receiver};
use tokio::time::Instant;

pub trait Secrets: Send + Sync {
    /// The secret string of `name`.
    fn get(&self, name: &str) -> Fut<'_, String>;
}

pub trait Blobs: Send + Sync {
    /// The object at `key` in `OWNER_STATE_BUCKET`, `None` when there is none.
    fn get(&self, key: &str) -> Fut<'_, Option<Vec<u8>>>;
    fn put(&self, key: &str, bytes: Vec<u8>) -> Fut<'_, ()>;
}

/// Where `owner`'s snapshot lives: never under `<owner>/*`, which the runtime can write.
pub fn key(owner: &str) -> String {
    format!("bridge-state/{owner}/state.db")
}

/// One instance's snapshots: its local copy at `path`, put to S3 only while the lease is
/// valid (`until`, which the lease loop keeps), and only when the bytes changed.
pub struct Snap {
    pub blobs: Arc<dyn Blobs>,
    pub owner: String,
    pub path: PathBuf,
    pub until: Arc<Mutex<Instant>>,
    /// The SHA-256 of the bytes last put (or restored).
    pub sent: Mutex<Option<[u8; 32]>>,
}

impl Snap {
    /// A consistent copy of `instance`'s `state.db` (`snapshot {path}`), then `upload`.
    pub async fn take(&self, instance: &Instance) -> Result<()> {
        let r = instance
            .call(json!({"id": 1, "op": "snapshot", "path": self.path}))
            .await;
        if r["ok"] != true {
            return Err(format!("snapshot refused: {}", r["error"]).into());
        }
        self.upload().await
    }

    /// Puts the local copy as the owner's snapshot, unless the lease's validity has passed or
    /// S3 already has these bytes.
    pub async fn upload(&self) -> Result<()> {
        let owner = &self.owner;
        if Instant::now() >= *self.until.lock().unwrap() {
            log!("snapshot owner={owner} skipped: the lease is no longer surely ours");
            return Ok(());
        }
        let t0 = Instant::now();
        let path = self.path.clone();
        let bytes = crate::blocking(move || Ok(std::fs::read(path)?)).await?;
        let sum: [u8; 32] = Sha256::digest(&bytes).into();
        if *self.sent.lock().unwrap() == Some(sum) {
            return Ok(());
        }
        let n = bytes.len();
        self.blobs.put(&key(owner), bytes).await?;
        *self.sent.lock().unwrap() = Some(sum);
        log!(
            "snapshot owner={owner} bytes={n} {}ms",
            t0.elapsed().as_millis()
        );
        Ok(())
    }
}

/// Runs `snap` at once when `first`, then 1 s after the first state-writing event of each
/// burst (the burst's later events ride along), until the instance shuts down. Stream bytes
/// write no state.
pub async fn debounce<F, Fu>(mut rx: Receiver<Value>, first: bool, mut snap: F)
where
    F: FnMut() -> Fu,
    Fu: Future<Output = ()>,
{
    if first {
        snap().await;
    }
    loop {
        match rx.recv().await {
            Ok(e) => match e["event"].as_str() {
                Some("shutdown") => return,
                Some("stream" | "stream_input") => continue,
                _ => {}
            },
            // Lagged: something was missed, so something was written.
            Err(RecvError::Lagged(_)) => {}
            Err(RecvError::Closed) => return,
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
        while let Ok(e) = rx.try_recv() {
            if e["event"] == "shutdown" {
                return;
            }
        }
        snap().await;
    }
}

/// Secrets Manager and S3, as the task role reaches them.
pub struct Aws {
    pub secrets: aws_sdk_secretsmanager::Client,
    pub s3: aws_sdk_s3::Client,
    pub bucket: String,
}

impl Secrets for Aws {
    fn get(&self, name: &str) -> Fut<'_, String> {
        let name = name.to_string();
        Box::pin(async move {
            let r = self
                .secrets
                .get_secret_value()
                .secret_id(name)
                .send()
                .await?;
            Ok(r.secret_string()
                .ok_or("the secret has no string")?
                .to_string())
        })
    }
}

impl Blobs for Aws {
    fn get(&self, key: &str) -> Fut<'_, Option<Vec<u8>>> {
        let key = key.to_string();
        Box::pin(async move {
            match self
                .s3
                .get_object()
                .bucket(&self.bucket)
                .key(key)
                .send()
                .await
            {
                Ok(o) => Ok(Some(o.body.collect().await?.to_vec())),
                Err(e) if e.as_service_error().is_some_and(|e| e.is_no_such_key()) => Ok(None),
                Err(e) => Err(e.into()),
            }
        })
    }

    fn put(&self, key: &str, bytes: Vec<u8>) -> Fut<'_, ()> {
        let key = key.to_string();
        Box::pin(async move {
            self.s3
                .put_object()
                .bucket(&self.bucket)
                .key(key)
                .body(bytes.into())
                .send()
                .await?;
            Ok(())
        })
    }
}
