//! Persisted `/desktop/pair` state so an app relaunch can start the node
//! host WITHOUT waiting for the webview to deliver a Clerk JWT.
//!
//! Before this existed, `start_node_host` was only reachable from the
//! bootstrap spawned by `send_auth_token` — which only fires once the
//! foreground webview resolves a Clerk session. An app launched backgrounded
//! (login item, relaunch after update) never started the desktop-node
//! surface at all. The `/pair` call is the ONLY leg of the bootstrap that
//! needs a Clerk JWT; everything after it authenticates with self-minted
//! device tokens (see `device_token`). So we persist the pair-response
//! essentials here and replay the post-pair leg on launch.
//!
//! Lives at `<app_data_dir>/pair_state.json`, chmod 0600. Holds device ids,
//! the owner binding, and the gateway URL + token. The device PRIVATE keys
//! stay in the macOS Keychain (`keychain.rs`) — the gateway token alone is
//! not a usable credential: both the WS authorizer's device-token path and
//! the upstream signed-connect verifier require a signature from the
//! Keychain-held key. (The CLI identity file at
//! `openclaw-state/identity/device.json` already persists strictly more
//! than this, at the same 0600 bar.)
//!
//! Staleness: if the Keychain entries are wiped, `load_or_generate` mints
//! fresh keys with new derived device ids — `matches_identities` then fails,
//! the caller discards this file, and the webview `/pair` path re-pairs.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::pair_client::PairResponse;

/// Current on-disk pairing version. Bump when the JSON shape or server-side
/// command grant contract changes; `load`
/// treats any other version as absent so an old binary never misreads a
/// newer shape (and vice versa) — the webview pair path just re-pairs.
///
/// 1 -> 2: the shape is UNCHANGED. This bump exists to force exactly that
/// re-pair, because the SERVER-side command surface changed and there is no
/// other way to reach an already-paired Mac.
///
/// 2 -> 3: adds `relay_url` (the CLI node's loopback proxy now dials the raw
/// WS relay on `bv-` instead of `gateway_url`'s API Gateway door — see the
/// machine relay design). A pre-3 file has no `relay_url` key, so
/// `serde_json` already refuses to deserialize it as the current shape; the
/// version bump documents that refusal instead of leaving it as an accident
/// of a missing required field.
///
/// 3 -> 4: shape unchanged; force the signed app to re-pair on foreground
/// Clerk sign-in so the server grants native approval/cancel and CLI execution
/// commands added after the 0.4.150 pair. Retaining v3 would start from the
/// old pair response and rotate the JWT without calling `/desktop/pair`.
///
/// `POST /desktop/pair` writes the server-side `paired_devices` grants in the
/// owner's DynamoDB row, and `lib.rs::run_bootstrap` is the app's pairing
/// path. A normal relaunch instead replays this persisted state
/// (`lib.rs::start_from_persisted_state`), so a schema bump is how an existing
/// install obtains newly granted commands. V2 added local MCP; V4 adds
/// selected-Mac execution approvals and file policy.
///
/// 4 -> 5: shape unchanged; re-pair so the server grants the owner-only
/// `session.options.v1` / `session.start.v1` native commands (start a Claude
/// Code / Codex session on this Mac from Lightbulb).
///
/// Discarding this file costs one re-pair round trip on next launch and
/// nothing else: device identity lives in the Keychain, not here.
const STORE_VERSION: u32 = 5;

const FILE_NAME: &str = "pair_state.json";

/// The pair-response essentials the post-pair bootstrap leg consumes, plus
/// the display name the node advertises on connect.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PairState {
    pub version: u32,
    pub owner_id: String,
    pub device_id_mac: String,
    pub device_id_cli: String,
    pub gateway_url: String,
    pub relay_url: String,
    pub gateway_token: String,
    pub display_name: String,
    /// The switches `/desktop/pair` last granted on the server. The server grant
    /// changes only on a pair, so a Settings change (or a file written before this
    /// field existed: `None`) re-pairs once from `send_auth_token`; see
    /// `lib.rs::repair_if_permissions_changed`.
    #[serde(default)]
    pub paired_permissions: Option<crate::pair_client::DesktopPermissions>,
}

impl PairState {
    pub fn from_pair_response(
        resp: &PairResponse,
        display_name: &str,
        paired_permissions: Option<crate::pair_client::DesktopPermissions>,
    ) -> Self {
        Self {
            version: STORE_VERSION,
            owner_id: resp.owner_id.clone(),
            device_id_mac: resp.device_id_mac.clone(),
            device_id_cli: resp.device_id_cli.clone(),
            gateway_url: resp.gateway_url.clone(),
            relay_url: resp.relay_url.clone(),
            gateway_token: resp.gateway_token.clone(),
            display_name: display_name.to_string(),
            paired_permissions,
        }
    }

    /// Reconstruct the `PairResponse` shape the start paths consume. The
    /// per-device command allowlists are not persisted — nothing client-side
    /// reads them (the server enforces them) — so they come back empty.
    pub fn to_pair_response(&self) -> PairResponse {
        PairResponse {
            gateway_url: self.gateway_url.clone(),
            relay_url: self.relay_url.clone(),
            gateway_token: self.gateway_token.clone(),
            owner_id: self.owner_id.clone(),
            device_id_mac: self.device_id_mac.clone(),
            device_id_cli: self.device_id_cli.clone(),
            commands_mac: Vec::new(),
            commands_cli: Vec::new(),
        }
    }

    /// True when the persisted device ids still match the identities in the
    /// Keychain (deviceId = sha256 of the raw public key, same derivation
    /// the backend used at pair time). False means the keys were regenerated
    /// since we paired — the persisted state is stale and must be discarded.
    pub fn matches_identities(&self, mac_public_key_pem: &str, cli_public_key_pem: &str) -> bool {
        matches!(
            crate::device_token::derive_device_id(mac_public_key_pem),
            Ok(ref id) if *id == self.device_id_mac
        ) && matches!(
            crate::device_token::derive_device_id(cli_public_key_pem),
            Ok(ref id) if *id == self.device_id_cli
        )
    }
}

fn store_file(dir: &Path) -> PathBuf {
    dir.join(FILE_NAME)
}

/// Atomic write (tmp + rename, same convention as `file_access.rs`),
/// chmod 0600 before the rename so the token bytes are never readable by
/// other users, even transiently.
pub fn save(dir: &Path, state: &PairState) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("create app data dir: {e}"))?;
    let path = store_file(dir);
    let tmp = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(state).map_err(|e| format!("serialize: {e}"))?;
    std::fs::write(&tmp, bytes).map_err(|e| format!("write: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("chmod: {e}"))?;
    }
    std::fs::rename(&tmp, &path).map_err(|e| format!("rename: {e}"))?;
    Ok(())
}

/// `None` when the file is missing, unreadable, corrupt, or a different
/// schema version. A bad file must never block startup — it just means no
/// standalone start this launch; the webview pair path takes over.
pub fn load(dir: &Path) -> Option<PairState> {
    let bytes = std::fs::read(store_file(dir)).ok()?;
    let state: PairState = serde_json::from_slice(&bytes).ok()?;
    (state.version == STORE_VERSION).then_some(state)
}

/// Remove the persisted state (stale-identity discard). Idempotent.
pub fn clear(dir: &Path) {
    let _ = std::fs::remove_file(store_file(dir));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> PairState {
        PairState {
            version: STORE_VERSION,
            owner_id: "org_1".into(),
            device_id_mac: "mac-id".into(),
            device_id_cli: "cli-id".into(),
            gateway_url: "wss://ws-dev.thelightbulb.company".into(),
            relay_url: "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws".into(),
            gateway_token: "gw-tok".into(),
            display_name: "Test Mac".into(),
            paired_permissions: None,
        }
    }

    #[test]
    fn save_load_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let state = sample();
        save(dir.path(), &state).unwrap();
        assert_eq!(load(dir.path()), Some(state));
    }

    #[test]
    fn load_missing_file_is_none() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load(dir.path()), None);
    }

    #[test]
    fn load_corrupt_file_is_none() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(store_file(dir.path()), b"not json{{").unwrap();
        assert_eq!(load(dir.path()), None);
    }

    #[test]
    fn load_unknown_version_is_none() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = sample();
        state.version = STORE_VERSION + 1;
        save(dir.path(), &state).unwrap();
        assert_eq!(load(dir.path()), None);
    }

    /// The v1 -> v2 bump re-paired an already-paired Mac, which is how that
    /// existing install was granted `mcp.tools.call.v1`.
    /// Deliberately a literal `1`, not `STORE_VERSION - 1`: this pins the real
    /// upgrade path and must keep meaning that even after the next bump.
    /// The sibling test above only covers a NEWER file (forward-incompat).
    #[test]
    fn load_discards_a_v1_file_so_the_mac_re_pairs() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = sample();
        state.version = 1;
        save(dir.path(), &state).unwrap();
        assert_eq!(
            load(dir.path()),
            None,
            "a v1 pair_state must be discarded, or the Mac replays persisted \
             state forever and never re-pairs"
        );
    }

    #[test]
    fn load_discards_a_v3_file_so_the_mac_re_pairs_for_execution_grants() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = sample();
        state.version = 3; // installed 0.4.150 persisted this exact version
        save(dir.path(), &state).unwrap();
        assert_eq!(load(dir.path()), None);
    }

    #[test]
    fn load_discards_a_v4_file_so_the_mac_re_pairs_for_session_start() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = sample();
        state.version = 4;
        save(dir.path(), &state).unwrap();
        assert_eq!(load(dir.path()), None);
    }

    #[test]
    fn clear_removes_and_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        save(dir.path(), &sample()).unwrap();
        clear(dir.path());
        assert_eq!(load(dir.path()), None);
        clear(dir.path()); // second call must not panic
    }

    #[cfg(unix)]
    #[test]
    fn saved_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        save(dir.path(), &sample()).unwrap();
        let mode = std::fs::metadata(store_file(dir.path()))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600, "token file must be 0600");
    }

    #[test]
    fn pair_response_round_trip_preserves_essentials() {
        let resp = sample().to_pair_response();
        let state = PairState::from_pair_response(&resp, "Test Mac", None);
        assert_eq!(state, sample());
    }

    /// A file written before `paired_permissions` existed still loads (no forced
    /// re-pair at launch); `None` makes the first webview token re-pair once.
    #[test]
    fn a_file_without_paired_permissions_loads_as_none() {
        let dir = tempfile::tempdir().unwrap();
        let mut json = serde_json::to_value(sample()).unwrap();
        json.as_object_mut().unwrap().remove("paired_permissions");
        std::fs::write(store_file(dir.path()), json.to_string()).unwrap();
        assert_eq!(load(dir.path()).unwrap().paired_permissions, None);
    }

    /// The Keychain-staleness gate: persisted ids derived from the SAME keys
    /// match; a regenerated keypair (fresh ids) does not.
    #[test]
    fn matches_identities_tracks_the_keychain_keys() {
        let mac = crate::keychain::generate_keypair();
        let cli = crate::keychain::generate_keypair();
        let mut state = sample();
        state.device_id_mac =
            crate::device_token::derive_device_id(&mac.public_key_pem).unwrap();
        state.device_id_cli =
            crate::device_token::derive_device_id(&cli.public_key_pem).unwrap();

        assert!(state.matches_identities(&mac.public_key_pem, &cli.public_key_pem));

        let other = crate::keychain::generate_keypair();
        assert!(!state.matches_identities(&other.public_key_pem, &cli.public_key_pem));
        assert!(!state.matches_identities(&mac.public_key_pem, &other.public_key_pem));
        assert!(!state.matches_identities("not a pem", &cli.public_key_pem));
    }
}
