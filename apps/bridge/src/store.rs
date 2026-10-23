//! Keys from Secrets Manager, `state.db` to and from S3 (`bridge-state/<owner>/state.db`),
//! and the debounced snapshot after state-writing events.

use crate::{log, Fut, Result};
use lightbulb_connect::embed::Instance;
use serde_json::{json, Value};
use std::future::Future;
use std::path::Path;
use std::time::{Duration, Instant};
use tokio::sync::broadcast::{error::RecvError, Receiver};

pub trait Secrets: Send + Sync {
    /// The secret string of `name`.
    fn get(&self, name: &str) -> Fut<'_, String>;
}

pub trait Blobs: Send + Sync {
    /// The object at `key` in `OWNER_STATE_BUCKET`, `None` when there is none.
    fn get(&self, key: &str) -> Fut<'_, Option<Vec<u8>>>;
    fn put(&self, key: &str, bytes: Vec<u8>) -> Fut<'_, ()>;
}

/// Every Secrets Manager and S3 call's time limit.
pub const CALL_TIMEOUT: Duration = Duration::from_secs(10);

/// `f`, or an error once `CALL_TIMEOUT` has passed.
pub async fn within<T>(f: Fut<'_, T>) -> Result<T> {
    tokio::time::timeout(CALL_TIMEOUT, f)
        .await
        .map_err(|_| "timed out")?
}

/// Where `owner`'s snapshot lives: never under `<owner>/*`, which the runtime can write.
pub fn key(owner: &str) -> String {
    format!("bridge-state/{owner}/state.db")
}

/// A consistent copy of `instance`'s `state.db` (`snapshot {path}`, at `path`), put to S3.
pub async fn snapshot(
    blobs: &dyn Blobs,
    owner: &str,
    instance: &Instance,
    path: &Path,
) -> Result<()> {
    let r = instance
        .call(json!({"id": 1, "op": "snapshot", "path": path}))
        .await;
    if r["ok"] != true {
        return Err(format!("snapshot refused: {}", r["error"]).into());
    }
    upload(blobs, owner, path).await
}

/// Puts the file at `path` as `owner`'s snapshot.
pub async fn upload(blobs: &dyn Blobs, owner: &str, path: &Path) -> Result<()> {
    let t0 = Instant::now();
    let bytes = std::fs::read(path)?;
    let n = bytes.len();
    within(blobs.put(&key(owner), bytes)).await?;
    log!(
        "snapshot owner={owner} bytes={n} {}ms",
        t0.elapsed().as_millis()
    );
    Ok(())
}

/// Whether event `e` wrote state: all but stream bytes and the instance's own shutdown.
fn writes_state(e: &Value) -> bool {
    !matches!(
        e["event"].as_str(),
        Some("shutdown" | "stream" | "stream_input")
    )
}

/// Runs `snap` 1 s after the first state-writing event of a burst (the burst's later events
/// ride along), until the instance shuts down.
pub async fn debounce<F, Fu>(mut rx: Receiver<Value>, mut snap: F)
where
    F: FnMut() -> Fu,
    Fu: Future<Output = ()>,
{
    loop {
        match rx.recv().await {
            Ok(e) if e["event"] == "shutdown" => return,
            Ok(e) if !writes_state(&e) => continue,
            // Lagged: something was missed, so something was written.
            Ok(_) | Err(RecvError::Lagged(_)) => {}
            Err(RecvError::Closed) => return,
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
        loop {
            match rx.try_recv() {
                Ok(e) if e["event"] == "shutdown" => return,
                Err(tokio::sync::broadcast::error::TryRecvError::Empty) => break,
                Err(tokio::sync::broadcast::error::TryRecvError::Closed) => return,
                _ => {}
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
