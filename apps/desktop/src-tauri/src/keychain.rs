//! Ed25519 keypair management with macOS Keychain persistence.
//!
//! Backend never sees the private key (security review HIGH finding D).
//! Keys live in Keychain entries under co.isol8.desktop.device.{mac,cli}.

use base64::Engine;
use ed25519_dalek::pkcs8::{
    spki::der::pem::LineEnding,
    EncodePublicKey,
};
use ed25519_dalek::{SigningKey, VerifyingKey};
use rand::rngs::OsRng;
use security_framework::passwords::{
    delete_generic_password, get_generic_password, set_generic_password,
};

const PUBLIC_SEPARATOR: &str = "---PUBLIC_SEPARATOR---\n";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeychainSlot {
    /// Tauri-mac process identity (clientId: openclaw-macos)
    Mac,
    /// openclaw CLI subprocess identity (clientId: node-host)
    Cli,
}

impl KeychainSlot {
    fn account_suffix(self) -> &'static str {
        match self {
            KeychainSlot::Mac => "device.mac",
            KeychainSlot::Cli => "device.cli",
        }
    }
}

#[derive(Debug, Clone)]
pub struct DeviceIdentity {
    pub private_key_pem: String,
    pub public_key_pem: String,
}

/// Encode the 32-byte raw Ed25519 private key as PKCS#8 v1 PEM.
///
/// ed25519-dalek's built-in `to_pkcs8_pem` emits PKCS#8 v2 (OneAsymmetricKey,
/// RFC 5958) which embeds the public key alongside the private — 81 bytes
/// DER. Node.js's OpenSSL DECODER rejects that format with
/// `error:1E08010C:DECODER routines::unsupported`, so when openclaw's
/// `loadOrCreateDeviceIdentity` calls `crypto.createPrivateKey` on our PEM
/// it falls through to the catch branch, marks the stored identity as
/// `recognized-invalid`, and regenerates a fresh in-memory key — which
/// of course isn't in the backend's paired.json. Result: every CLI
/// connect signs with a brand-new deviceId and gets NOT_PAIRED.
///
/// PKCS#8 v1 (RFC 5208) for Ed25519 is the 48-byte form Node accepts:
///   SEQUENCE (length 46) {
///     INTEGER 0                       -- version
///     SEQUENCE (length 5) {           -- AlgorithmIdentifier
///       OID 1.3.101.112               -- Ed25519
///     }
///     OCTET STRING (length 34) {      -- PrivateKey wrapper
///       OCTET STRING (length 32) {    -- the actual 32 raw bytes
///         <raw private key>
///       }
///     }
///   }
fn signing_key_to_pkcs8_v1_pem(signing: &SigningKey) -> String {
    let raw = signing.to_bytes();
    let mut der = Vec::with_capacity(48);
    der.extend_from_slice(&[
        0x30, 0x2e,                          // SEQUENCE, len 46
        0x02, 0x01, 0x00,                    // INTEGER 0 (v1)
        0x30, 0x05,                          // SEQUENCE (AlgorithmIdentifier)
        0x06, 0x03, 0x2b, 0x65, 0x70,        //   OID 1.3.101.112 (Ed25519)
        0x04, 0x22,                          // OCTET STRING, len 34
        0x04, 0x20,                          //   OCTET STRING, len 32
    ]);
    der.extend_from_slice(&raw);
    debug_assert_eq!(der.len(), 48);
    let b64 = base64::engine::general_purpose::STANDARD.encode(&der);
    let body = b64
        .as_bytes()
        .chunks(64)
        .map(|c| std::str::from_utf8(c).expect("base64 is ascii"))
        .collect::<Vec<_>>()
        .join("\n");
    format!("-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----\n", body)
}

pub fn generate_keypair() -> DeviceIdentity {
    let mut csprng = OsRng;
    let signing = SigningKey::generate(&mut csprng);
    let verifying: VerifyingKey = signing.verifying_key();
    DeviceIdentity {
        private_key_pem: signing_key_to_pkcs8_v1_pem(&signing),
        public_key_pem: verifying.to_public_key_pem(LineEnding::LF).unwrap(),
    }
}

pub fn load_or_generate(slot: KeychainSlot, service: &str) -> Result<DeviceIdentity, String> {
    let account = slot.account_suffix();
    match get_generic_password(service, account) {
        Ok(bytes) => parse_stored(&bytes),
        Err(_) => {
            let id = generate_keypair();
            let stored = format!(
                "{}{}{}",
                id.private_key_pem, PUBLIC_SEPARATOR, id.public_key_pem
            );
            set_generic_password(service, account, stored.as_bytes())
                .map_err(|e| format!("keychain set: {e}"))?;
            Ok(id)
        }
    }
}

pub fn delete(slot: KeychainSlot, service: &str) -> Result<(), String> {
    delete_generic_password(service, slot.account_suffix())
        .map_err(|e| format!("keychain delete: {e}"))
}

fn parse_stored(bytes: &[u8]) -> Result<DeviceIdentity, String> {
    let pem = std::str::from_utf8(bytes).map_err(|e| format!("utf8: {e}"))?;
    let mut parts = pem.split(PUBLIC_SEPARATOR);
    let private_key_pem = parts
        .next()
        .ok_or("malformed keychain entry: no private part")?
        .trim_end_matches('\n')
        .to_string();
    let public_key_pem = parts
        .next()
        .ok_or("malformed keychain entry: no public part")?
        .to_string();
    Ok(DeviceIdentity {
        private_key_pem,
        public_key_pem,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_stored_roundtrip() {
        let id = generate_keypair();
        let stored = format!("{}{}{}", id.private_key_pem, PUBLIC_SEPARATOR, id.public_key_pem);
        let parsed = parse_stored(stored.as_bytes()).unwrap();
        assert_eq!(parsed.private_key_pem, id.private_key_pem.trim_end_matches('\n'));
        assert_eq!(parsed.public_key_pem, id.public_key_pem);
    }

    #[test]
    fn parse_stored_rejects_no_separator() {
        let r = parse_stored(b"no separator here");
        assert!(r.is_err());
    }

    /// Lock in PKCS#8 v1 (48-byte DER) — Node's `crypto.createPrivateKey`
    /// rejects v2 (OneAsymmetricKey, 81 bytes) which is what
    /// ed25519-dalek's `to_pkcs8_pem` emits by default. If this assertion
    /// ever fires, openclaw's `loadOrCreateDeviceIdentity` will mark our
    /// stored identity as recognized-invalid and regenerate a fresh
    /// in-memory key on every connect — silently breaking the CLI
    /// node-host handshake (deviceId in paired.json won't match what the
    /// subprocess signs with).
    #[test]
    fn private_key_pem_is_pkcs8_v1_48_bytes() {
        use base64::Engine;
        let id = generate_keypair();
        let body: String = id
            .private_key_pem
            .lines()
            .filter(|l| !l.starts_with("-----"))
            .collect();
        let der = base64::engine::general_purpose::STANDARD
            .decode(body.trim())
            .expect("valid base64");
        assert_eq!(der.len(), 48, "must be PKCS#8 v1 (48 bytes), got {}", der.len());
        // version byte must be 0 (v1), not 1 (v2)
        assert_eq!(der[4], 0x00, "must be PKCS#8 v1, got version {}", der[4]);
        // OID Ed25519 (1.3.101.112)
        assert_eq!(&der[7..12], &[0x06, 0x03, 0x2b, 0x65, 0x70]);
    }
}
