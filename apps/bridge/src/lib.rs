//! The Lightbulb bridge: one embedded Lightbulb Connect instance per connected workspace (its
//! single cloud member), held by one live task at a time.

pub mod http;
pub mod instances;
pub mod lease;
pub mod store;

pub use lightbulb_connect::Result;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// What the AWS seams (`store::Secrets`, `store::Blobs`, `lease::Table`) return.
pub type Fut<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

pub struct Cfg {
    /// `BACKEND_INTERNAL_URL`: the backend's VPC-only door.
    pub backend_url: String,
    /// `BRIDGE_INTERNAL_TOKEN`, both ways.
    pub token: String,
    /// `LIGHTBULB_ENV`, for the secret names.
    pub env: String,
    /// `NETWORK_RELAY_URL`.
    pub relay: Option<String>,
    /// `NETWORK_ISSUER_URL`.
    pub issuer: String,
    /// This task, as the lease names it.
    pub holder: String,
    /// Where the instances' homes live (one fresh dir per workspace).
    pub scratch: PathBuf,
    /// The lease's renew and retry period (10 s).
    pub tick: Duration,
    pub secrets: Arc<dyn store::Secrets>,
    pub blobs: Arc<dyn store::Blobs>,
    pub lease: Arc<dyn lease::Table>,
}

/// Every Secrets Manager, S3 and DynamoDB call's time limit.
pub const CALL_TIMEOUT: Duration = Duration::from_secs(10);

/// An AWS seam whose every call ends within `CALL_TIMEOUT`: `Cfg`'s three are always one.
pub struct Timed<T: ?Sized>(pub Arc<T>);

fn timed<'a, T: Send + 'a>(f: Fut<'a, T>) -> Fut<'a, T> {
    Box::pin(async move {
        tokio::time::timeout(CALL_TIMEOUT, f)
            .await
            .map_err(|_| "timed out")?
    })
}

impl<T: store::Secrets + ?Sized> store::Secrets for Timed<T> {
    fn get(&self, name: &str) -> Fut<'_, String> {
        timed(self.0.get(name))
    }
}

impl<T: store::Blobs + ?Sized> store::Blobs for Timed<T> {
    fn get(&self, key: &str) -> Fut<'_, Option<Vec<u8>>> {
        timed(self.0.get(key))
    }
    fn put(&self, key: &str, bytes: Vec<u8>) -> Fut<'_, ()> {
        timed(self.0.put(key, bytes))
    }
}

impl<T: lease::Table + ?Sized> lease::Table for Timed<T> {
    fn put(&self, holder: &str, now: i64, expires_at: i64) -> Fut<'_, bool> {
        timed(self.0.put(holder, now, expires_at))
    }
    fn delete(&self, holder: &str) -> Fut<'_, ()> {
        timed(self.0.delete(holder))
    }
}

/// `f` (file work) off the runtime's threads, so the lease renewer never waits on a disk.
pub async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T> + Send + 'static,
) -> Result<T> {
    tokio::task::spawn_blocking(f).await?
}

static CAPTURE: Mutex<Option<Vec<String>>> = Mutex::new(None);

/// One log line: ids, sizes, states and timings only, never a body, key, proof or token.
pub fn log(line: String) {
    eprintln!("{line}");
    if let Some(lines) = CAPTURE.lock().unwrap().as_mut() {
        lines.push(line);
    }
}

#[macro_export]
macro_rules! log {
    ($($t:tt)*) => { $crate::log(format!($($t)*)) };
}

/// Tests: keep every log line from now on (`logs`).
#[doc(hidden)]
pub fn capture_logs() {
    CAPTURE.lock().unwrap().get_or_insert_with(Vec::new);
}

#[doc(hidden)]
pub fn logs() -> Vec<String> {
    CAPTURE.lock().unwrap().clone().unwrap_or_default()
}
