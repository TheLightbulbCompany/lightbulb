//! Self-minted device-key WS-upgrade tokens (EdDSA proof-of-possession).
//!
//! The node-host subprocess authenticates its gateway `$connect` with a SHORT
//! (~90s) JWT that this device signs with its Ed25519 Keychain key — instead of
//! the webview's ~60s Clerk JWT, which goes stale when the Mac is idle and 403s
//! the reconnect (dropping phone→Mac agent actions). The AWS Lambda authorizer
//! verifies the signature against the device's registered public key (projected
//! onto the owner's `containers` row at `/desktop/pair`); revocation is
//! un-pairing. The private key never leaves the Keychain — only short signed
//! assertions cross the wire, so a leaked token is useless in ~90s.
//!
//! Minted with the `jsonwebtoken` crate (Algorithm::EdDSA) — no hand-rolled JWS.
//! The claim shape mirrors `apps/infra/lambda/websocket-authorizer/index.py`
//! (`_try_device_token`): {kind:"device", deviceId, owner, iat, exp}, EdDSA.

use std::time::{SystemTime, UNIX_EPOCH};

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde::{Deserialize, Serialize};

/// Default device-token lifetime (exp = now + this). Kept well under the
/// authorizer's 300s ceiling (`DEVICE_TOKEN_MAX_LIFETIME_S`) even after the
/// `iat` backdate below, while giving reconnects headroom.
pub const DEVICE_TOKEN_LIFETIME_SECS: u64 = 90;

/// Backdate `iat` by this much. The authorizer verifies `iat` with only ~30s of
/// forward leeway (PyJWT rejects a future `iat` with ImmatureSignatureError), so
/// a Mac clock that runs fast would mint tokens rejected on every reconnect — a
/// silent 403 loop. Backdating widens the fast-clock tolerance to ~90s. It keeps
/// `exp - iat` (= lifetime + backdate = 150s) under the authorizer's 300s cap.
pub const IAT_BACKDATE_SECS: u64 = 60;

#[derive(Debug, Serialize, Deserialize)]
struct DeviceClaims {
    /// Discriminant the authorizer routes on (must be "device").
    kind: String,
    /// sha256(pubkey)-derived id; the authorizer's key into `paired_devices`.
    #[serde(rename = "deviceId")]
    device_id: String,
    /// Owner container-row id the authorizer does GetItem against.
    owner: String,
    iat: u64,
    exp: u64,
    /// The shared session making this call, for the backend's `/mcp` (signed, so the
    /// device key vouches for which session it is). Absent on every other token.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    source: Option<serde_json::Value>,
}

/// Mint an EdDSA device token signed by `private_key_pem` (the CLI device
/// identity's PKCS#8 key). `device_id` is the paired CLI device id and
/// `owner_id` is the container-row owner — both from the `/pair` response.
pub fn mint_device_token(
    private_key_pem: &str,
    device_id: &str,
    owner_id: &str,
    lifetime_secs: u64,
) -> Result<String, String> {
    mint_session_token(private_key_pem, device_id, owner_id, lifetime_secs, None)
}

/// A device token that also names the calling shared session (`source`), for `/mcp`.
pub fn mint_session_token(
    private_key_pem: &str,
    device_id: &str,
    owner_id: &str,
    lifetime_secs: u64,
    source: Option<serde_json::Value>,
) -> Result<String, String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| format!("system clock before epoch: {e}"))?
        .as_secs();
    let claims = DeviceClaims {
        kind: "device".to_string(),
        device_id: device_id.to_string(),
        owner: owner_id.to_string(),
        // Backdate iat so a fast client clock doesn't trip the authorizer's
        // future-iat rejection; exp is still now + lifetime.
        iat: now.saturating_sub(IAT_BACKDATE_SECS),
        exp: now + lifetime_secs,
        source,
    };
    let key = EncodingKey::from_ed_pem(private_key_pem.as_bytes())
        .map_err(|e| format!("load device signing key: {e}"))?;
    encode(&Header::new(Algorithm::EdDSA), &claims, &key)
        .map_err(|e| format!("mint device token: {e}"))
}

/// Extract the 32 raw Ed25519 public-key bytes from a PEM string. Matches
/// upstream's `derivePublicKeyRaw` (src/infra/device-identity.ts), which
/// strips the 12-byte SPKI prefix `302a300506032b6570032100` from a DER-
/// encoded SPKI to recover the raw key. Going via `VerifyingKey::to_bytes`
/// is equivalent — ed25519-dalek owns that decoding path.
pub fn raw_public_key_from_pem(public_key_pem: &str) -> Result<[u8; 32], String> {
    use ed25519_dalek::pkcs8::DecodePublicKey;
    let verifying = ed25519_dalek::VerifyingKey::from_public_key_pem(public_key_pem)
        .map_err(|e| format!("parse public PEM: {e}"))?;
    Ok(verifying.to_bytes())
}

/// `sha256(rawPub).hex()` — same formula upstream uses to mint
/// `deviceId`. The signed connect's `device.id` MUST equal this so the
/// upstream container can look up the paired entry by deviceId.
/// `pair_state::matches_identities` uses the same derivation to detect a
/// Keychain/persisted-state mismatch.
pub fn derive_device_id(public_key_pem: &str) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let raw = raw_public_key_from_pem(public_key_pem)?;
    let mut hasher = Sha256::new();
    hasher.update(raw);
    Ok(hex::encode(hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keychain::generate_keypair;
    use jsonwebtoken::{decode, DecodingKey, Validation};

    /// Round-trip: a token minted with the device private key must verify
    /// against its public key with the exact EdDSA algorithm + claim shape the
    /// authorizer enforces. This also proves our Keychain PKCS#8 key loads into
    /// jsonwebtoken/ring (the reason for the PKCS#8-v1 format is documented in
    /// keychain.rs).
    #[test]
    fn mint_round_trips_and_verifies_with_public_key() {
        let id = generate_keypair();
        let token = mint_device_token(
            &id.private_key_pem,
            "dev_abc",
            "org_x",
            DEVICE_TOKEN_LIFETIME_SECS,
        )
        .expect("mint should succeed");

        // Header must be EdDSA so the authorizer routes it as a device token.
        let header = jsonwebtoken::decode_header(&token).unwrap();
        assert_eq!(header.alg, Algorithm::EdDSA);

        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.validate_aud = false;
        validation.set_required_spec_claims(&["exp"]);
        let key = DecodingKey::from_ed_pem(id.public_key_pem.as_bytes()).unwrap();
        let data =
            decode::<DeviceClaims>(&token, &key, &validation).expect("verify should succeed");

        assert_eq!(data.claims.kind, "device");
        assert_eq!(data.claims.device_id, "dev_abc");
        assert_eq!(data.claims.owner, "org_x");
        // iat is backdated, so exp - iat = lifetime + backdate...
        assert_eq!(
            data.claims.exp - data.claims.iat,
            DEVICE_TOKEN_LIFETIME_SECS + IAT_BACKDATE_SECS
        );
        // ...and must still stay under the authorizer's 300s ceiling.
        assert!(data.claims.exp - data.claims.iat <= 300);
    }

    /// A different key must NOT verify a token — guards against silently signing
    /// with the wrong identity.
    #[test]
    fn token_does_not_verify_under_a_different_key() {
        let signer = generate_keypair();
        let other = generate_keypair();
        let token = mint_device_token(&signer.private_key_pem, "dev_abc", "org_x", 90).unwrap();

        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.validate_aud = false;
        let key = DecodingKey::from_ed_pem(other.public_key_pem.as_bytes()).unwrap();
        assert!(decode::<DeviceClaims>(&token, &key, &validation).is_err());
    }

    #[test]
    fn rejects_malformed_private_key() {
        assert!(mint_device_token("not a pem", "d", "o", 90).is_err());
    }
}
