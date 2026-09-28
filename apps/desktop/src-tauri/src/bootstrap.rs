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
//! The function takes already-resolved paths so it can be exercised from tests
//! without a Tauri AppHandle.
//!
//! The proxy's token provider is a device-key minter (see [`crate::device_token`]):
//! on every upstream (re)connect it signs a fresh short-lived EdDSA token with
//! the CLI device key, so reconnects never depend on the webview's staleable
//! Clerk JWT. The one place the Clerk JWT is still needed is the initial,
//! interactive `/pair` call below (authenticated as the user).

use std::path::PathBuf;
use std::sync::Arc;

use crate::device_token::{mint_device_token, DEVICE_TOKEN_LIFETIME_SECS};
use crate::keychain::{delete, load_or_generate, DeviceIdentity, KeychainSlot};
use crate::log;
use crate::pair_client::{
    is_device_conflict, pair as call_pair, DesktopPermissions, PairRequest, PairResponse,
};
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
/// passes `"company.thelightbulb.desktop"`. Tests can pass a unique value to avoid
/// stomping on the real entries.
pub async fn bootstrap_desktop_node_host(
    api_base: &str,
    jwt: &str,
    keychain_service: &str,
    cli_client_id: String,
    state_dir: PathBuf,
    binary: PathBuf,
    permissions: Option<DesktopPermissions>,
) -> Result<DesktopNodeHostHandles, String> {
    // 1. Load or generate Keychain identities. The Mac identity is the
    //    Tauri-process clientId (openclaw-macos); the CLI identity is
    //    what the subprocess uses (clientId we pass through). They're
    //    persisted under different account slots in Keychain.
    let mut mac_id = load_or_generate(KeychainSlot::Mac, keychain_service)?;
    let mut cli_id = load_or_generate(KeychainSlot::Cli, keychain_service)?;

    // 2. Exchange the public keys for a gateway URL + token.
    let request = |mac: &DeviceIdentity, cli: &DeviceIdentity| PairRequest {
        tauri_mac_pub_key: mac.public_key_pem.clone(),
        cli_pub_key: cli.public_key_pem.clone(),
        permissions: permissions.clone(),
    };
    let pair_resp = match call_pair(api_base, jwt, &request(&mac_id, &cli_id)).await {
        // The Keychain identity is per app, not per Clerk user: a Mac handed to another
        // member still holds the previous member's keys, and the server refuses to relabel
        // their devices. Mint fresh keys for this member and pair once more, carrying the
        // same consent (no new dialog).
        Err(e) if is_device_conflict(&e) => {
            log("[pair] this Mac's keys are paired to another member; minting new keys and pairing again");
            for slot in [KeychainSlot::Mac, KeychainSlot::Cli] {
                let _ = delete(slot, keychain_service);
            }
            mac_id = load_or_generate(KeychainSlot::Mac, keychain_service)?;
            cli_id = load_or_generate(KeychainSlot::Cli, keychain_service)?;
            call_pair(api_base, jwt, &request(&mac_id, &cli_id)).await?
        }
        other => other?,
    };

    start_from_pair(&cli_id, pair_resp, cli_client_id, state_dir, binary).await
}

/// The post-pair bootstrap leg: device-token provider + loopback WSS proxy +
/// openclaw subprocess. Split from [`bootstrap_desktop_node_host`] because
/// `/pair` is the ONLY leg that needs a webview Clerk JWT — an app relaunch
/// replays this leg from PERSISTED pair state (see [`crate::pair_state`]),
/// so the desktop-node surface comes up with no webview involved.
pub async fn start_from_pair(
    cli_id: &crate::keychain::DeviceIdentity,
    pair_resp: PairResponse,
    cli_client_id: String,
    state_dir: PathBuf,
    binary: PathBuf,
) -> Result<DesktopNodeHostHandles, String> {
    // 3. Build the device-token provider and start the loopback WSS proxy. The
    //    provider is invoked on every upstream-open (per reconnect), so each
    //    reconnect signs a FRESH short-lived device token with the CLI device
    //    key — no dependence on the webview's (staleable) Clerk JWT. Identity
    //    (device id + owner) comes from the pair response we just received.
    let token_key_pem = cli_id.private_key_pem.clone();
    let token_device_id = pair_resp.device_id_cli.clone();
    let token_owner_id = pair_resp.owner_id.clone();
    let token_provider: JwtProvider = Arc::new(move || {
        match mint_device_token(
            &token_key_pem,
            &token_device_id,
            &token_owner_id,
            DEVICE_TOKEN_LIFETIME_SECS,
        ) {
            Ok(token) => token,
            Err(e) => {
                // Empty token -> upstream 403 -> the subprocess reconnect loop
                // retries (same failure mode the poisoned-JWT-lock path had).
                log(&format!("[bootstrap] device token mint failed: {e}"));
                String::new()
            }
        }
    });
    let proxy = start_proxy(pair_resp.relay_url.clone(), cli_client_id, token_provider).await?;
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
