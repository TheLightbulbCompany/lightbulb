//! One-shot bootstrap that wires the new desktop-node-host architecture
//! together on the first Clerk JWT:
//!
//!   Keychain identities --> /api/v1/desktop/pair
//!                              |
//!                              v
//!                       loopback WSS proxy  --(127.0.0.1:N)--> openclaw subprocess
//!
//! Kept in its own module so `lib.rs::send_auth_token` stays thin and so the
//! pure logic (URL construction, identity-file shape, etc.) is greppable.
//! The function takes already-resolved paths + a JwtProvider so it can be
//! exercised from tests without a Tauri AppHandle.
//!
//! Once `proxy_handle` / `subprocess_handle` are stored on `AuthState`, JWT
//! rotation is a single shared-Arc write — the proxy's JwtProvider closure
//! reads the new value on the next upstream connect and no subprocess
//! restart is needed.

use std::path::PathBuf;
use std::sync::Arc;

use crate::keychain::{load_or_generate, KeychainSlot};
use crate::pair_client::{pair as call_pair, PairRequest, PairResponse};
use crate::proxy::{start_proxy, JwtProvider, ProxyHandle};
use crate::subprocess::{spawn as spawn_subprocess, SubprocessHandle};

/// Handles + pairing response surfaced by [`bootstrap_desktop_node_host`].
///
/// The caller is expected to stash these on long-lived state (e.g.
/// `AuthState`) so the proxy and subprocess survive across the
/// Tauri command invocation that triggered the bootstrap.
pub struct DesktopNodeHostHandles {
    pub proxy_handle: ProxyHandle,
    pub subprocess_handle: SubprocessHandle,
    pub pair_response: PairResponse,
}

/// Resolve identities, call /pair, start the proxy, spawn the subprocess.
///
/// The function is intentionally synchronous in its argument shape (no
/// `tauri::AppHandle`) so it can be unit-tested with a stand-in binary and
/// a fake api_base served by wiremock. The Tauri integration in `lib.rs`
/// resolves `state_dir` and `binary` from `AppHandle` and then calls us.
///
/// `keychain_service` is the macOS Keychain service name — production code
/// passes `"co.isol8.desktop"`. Tests can pass a unique value to avoid
/// stomping on the real entries.
pub async fn bootstrap_desktop_node_host(
    api_base: &str,
    jwt: &str,
    keychain_service: &str,
    cli_client_id: String,
    state_dir: PathBuf,
    binary: PathBuf,
    jwt_provider: JwtProvider,
) -> Result<DesktopNodeHostHandles, String> {
    // 1. Load or generate Keychain identities. The Mac identity is the
    //    Tauri-process clientId (openclaw-macos); the CLI identity is
    //    what the subprocess uses (clientId we pass through). They're
    //    persisted under different account slots in Keychain.
    let mac_id = load_or_generate(KeychainSlot::Mac, keychain_service)?;
    let cli_id = load_or_generate(KeychainSlot::Cli, keychain_service)?;

    // 2. Exchange the public keys for a gateway URL + token.
    let pair_resp = call_pair(
        api_base,
        jwt,
        &PairRequest {
            tauri_mac_pub_key: mac_id.public_key_pem.clone(),
            cli_pub_key: cli_id.public_key_pem.clone(),
        },
    )
    .await?;

    // 3. Start the loopback WSS proxy. The JwtProvider is invoked on every
    //    upstream-open (not per-message), so JWT rotation lands at the
    //    next reconnect.
    let proxy = start_proxy(
        pair_resp.gateway_url.clone(),
        cli_client_id,
        jwt_provider,
    )
    .await?;
    let proxy_port = proxy.local_port();

    // 4. Spawn the openclaw subprocess pointed at the loopback proxy.
    let sub = spawn_subprocess(
        binary,
        proxy_port,
        state_dir,
        &cli_id.private_key_pem,
        &cli_id.public_key_pem,
        &pair_resp.gateway_token,
    )
    .await?;

    Ok(DesktopNodeHostHandles {
        proxy_handle: proxy,
        subprocess_handle: sub,
        pair_response: pair_resp,
    })
}

/// Build a JwtProvider closure backed by a shared `Arc<RwLock<String>>`.
///
/// Extracted so `send_auth_token` reads cleanly and so we can unit-test
/// that the closure reflects writes to the underlying Arc.
pub fn jwt_provider_from_arc(jwt: Arc<std::sync::RwLock<String>>) -> JwtProvider {
    Arc::new(move || {
        jwt.read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::RwLock;

    #[test]
    fn jwt_provider_reads_current_value() {
        let jwt = Arc::new(RwLock::new("initial".to_string()));
        let provider = jwt_provider_from_arc(jwt.clone());
        assert_eq!(provider(), "initial");

        *jwt.write().unwrap() = "rotated".to_string();
        assert_eq!(provider(), "rotated");
    }

    #[test]
    fn jwt_provider_survives_poisoned_lock() {
        // A poisoned RwLock should not panic the provider — we return
        // empty string and let the upstream-connect attempt fail with
        // a 401, which the reconnect loop already handles.
        let jwt = Arc::new(RwLock::new("token".to_string()));
        let jwt_clone = jwt.clone();
        let _ = std::thread::spawn(move || {
            let _guard = jwt_clone.write().unwrap();
            panic!("poison the lock");
        })
        .join();

        let provider = jwt_provider_from_arc(jwt);
        // Read on poisoned lock returns Err; we map to empty.
        assert_eq!(provider(), "");
    }
}
