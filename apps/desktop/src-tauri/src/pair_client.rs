//! HTTPS client for `POST /api/v1/desktop/pair` and
//! `DELETE /api/v1/desktop/devices/{device_id}`.
//!
//! Wire format uses camelCase per the backend contract; the Rust surface is
//! snake_case. `serde(rename = "...")` handles the translation in one place
//! so call sites stay idiomatic.

use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize, Clone, PartialEq, Eq)]
pub struct DesktopPermissions {
    pub screen: bool,
    pub shell: bool,
    pub browser: bool,
    pub files: bool,
}

#[derive(Debug, Serialize)]
pub struct PairRequest {
    #[serde(rename = "tauriMacPubKey")]
    pub tauri_mac_pub_key: String,
    #[serde(rename = "cliPubKey")]
    pub cli_pub_key: String,
    pub permissions: Option<DesktopPermissions>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct PairResponse {
    #[serde(rename = "gatewayUrl")]
    pub gateway_url: String,
    /// The raw WebSocket gateway relay's door (`bv-` CloudFront/ALB). The CLI
    /// node's loopback proxy dials THIS one; `gateway_url` above stays on the
    /// API Gateway door for the local-session relay, which still reads it.
    #[serde(rename = "relayUrl")]
    pub relay_url: String,
    #[serde(rename = "gatewayToken")]
    pub gateway_token: String,
    /// Container-row owner id (personal user or org). The node-host mints its
    /// device token with this as the `owner` claim; the authorizer does
    /// `GetItem(containers, owner_id)` to find the registered device pubkey.
    #[serde(rename = "ownerId")]
    pub owner_id: String,
    #[serde(rename = "deviceIdMac")]
    pub device_id_mac: String,
    #[serde(rename = "deviceIdCli")]
    pub device_id_cli: String,
    #[serde(rename = "commandsMac")]
    pub commands_mac: Vec<String>,
    #[serde(rename = "commandsCli")]
    pub commands_cli: Vec<String>,
}

/// The backend's 409 `detail.code` when a submitted device id is already paired to a
/// different member of the workspace (see `routers/desktop_pair.py`).
pub const DEVICE_CONFLICT: &str = "device_registered_to_another_member";

pub fn is_device_conflict(err: &str) -> bool {
    err.contains(DEVICE_CONFLICT)
}

/// POST the device public keys to the backend and return the gateway
/// connection info (URL + token) plus the assigned device IDs and their
/// per-device command allowlists.
pub async fn pair(
    api_base: &str,
    clerk_jwt: &str,
    req: &PairRequest,
) -> Result<PairResponse, String> {
    let url = format!("{}/api/v1/desktop/pair", api_base.trim_end_matches('/'));
    let client = reqwest::Client::new();
    let resp = client
        .post(&url)
        .bearer_auth(clerk_jwt)
        .json(req)
        .send()
        .await
        .map_err(|e| format!("pair request failed: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("pair returned {status}: {body}"));
    }
    resp.json::<PairResponse>()
        .await
        .map_err(|e| format!("pair response parse: {e}"))
}

/// Revoke a previously paired device. The backend tears down the matching
/// node-host slot and removes the public-key entry.
pub async fn revoke(api_base: &str, clerk_jwt: &str, device_id: &str) -> Result<(), String> {
    let url = format!(
        "{}/api/v1/desktop/devices/{}",
        api_base.trim_end_matches('/'),
        device_id
    );
    let client = reqwest::Client::new();
    let resp = client
        .delete(&url)
        .bearer_auth(clerk_jwt)
        .send()
        .await
        .map_err(|e| format!("revoke request failed: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("revoke returned {status}: {body}"));
    }
    Ok(())
}
