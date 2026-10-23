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
