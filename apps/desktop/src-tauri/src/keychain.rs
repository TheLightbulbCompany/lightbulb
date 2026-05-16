//! Ed25519 keypair management with macOS Keychain persistence.
//!
//! Backend never sees the private key (security review HIGH finding D).
//! Keys live in Keychain entries under co.isol8.desktop.device.{mac,cli}.

use ed25519_dalek::pkcs8::{
    spki::der::pem::LineEnding,
    EncodePrivateKey, EncodePublicKey,
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

pub fn generate_keypair() -> DeviceIdentity {
    let mut csprng = OsRng;
    let signing = SigningKey::generate(&mut csprng);
    let verifying: VerifyingKey = signing.verifying_key();
    DeviceIdentity {
        private_key_pem: signing.to_pkcs8_pem(LineEnding::LF).unwrap().to_string(),
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
}
