//! Integration tests for the subprocess supervisor.
//!
//! These tests use `/bin/sleep` as a stand-in for the real openclaw shim —
//! it accepts whatever args the supervisor passes and just blocks. That lets
//! us assert on the identity-file side effects and the non-blocking spawn
//! behavior without needing a real openclaw install on the test machine.

use isol8_desktop::keychain::generate_keypair;
use isol8_desktop::subprocess::spawn;
use sha2::{Digest, Sha256};
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

#[tokio::test]
async fn spawn_writes_identity_file_with_device_id() {
    let tempdir = TempDir::new().unwrap();
    let id = generate_keypair();

    let handle = spawn(
        std::path::PathBuf::from("/bin/sleep"),
        12345,
        tempdir.path().to_path_buf(),
        &id.private_key_pem,
        &id.public_key_pem,
        "tok_gateway",
    )
    .await
    .unwrap();

    let identity_path = tempdir.path().join("identity/device.json");
    assert!(identity_path.exists(), "identity not written");
    let content = std::fs::read_to_string(&identity_path).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&content).unwrap();

    assert_eq!(parsed["version"], 1);
    assert!(parsed["publicKeyPem"]
        .as_str()
        .unwrap()
        .contains("BEGIN PUBLIC KEY"));
    assert!(parsed["privateKeyPem"]
        .as_str()
        .unwrap()
        .contains("BEGIN PRIVATE KEY"));

    // deviceId MUST be present and equal sha256(raw_pubkey_bytes).hex —
    // upstream openclaw's normalizeStoredIdentity (device-identity.ts:128-161)
    // treats a missing deviceId as `recognized-invalid` and regenerates the
    // keypair, discarding the Keychain identity.
    let device_id = parsed["deviceId"].as_str().expect("deviceId missing");
    assert_eq!(device_id.len(), 64, "deviceId must be sha256 hex (64 chars)");

    // Cross-verify: parse the PEM ourselves, strip SPKI, sha256.
    use ed25519_dalek::pkcs8::DecodePublicKey;
    let pubkey = ed25519_dalek::VerifyingKey::from_public_key_pem(&id.public_key_pem).unwrap();
    let raw_pub = pubkey.to_bytes();
    let mut hasher = Sha256::new();
    hasher.update(raw_pub);
    let expected_hex = hex::encode(hasher.finalize());
    assert_eq!(device_id, expected_hex);

    // chmod 600
    let perms = std::fs::metadata(&identity_path).unwrap().permissions();
    let mode = perms.mode() & 0o777;
    assert_eq!(mode, 0o600);

    handle.shutdown().await;
}

#[tokio::test]
async fn spawn_returns_quickly_does_not_block() {
    let tempdir = TempDir::new().unwrap();
    let id = generate_keypair();
    let start = std::time::Instant::now();
    let handle = spawn(
        std::path::PathBuf::from("/bin/sleep"),
        54321,
        tempdir.path().to_path_buf(),
        &id.private_key_pem,
        &id.public_key_pem,
        "tok",
    )
    .await
    .unwrap();
    assert!(start.elapsed() < std::time::Duration::from_secs(2));
    handle.shutdown().await;
}

#[tokio::test]
async fn spawn_forwards_subprocess_stderr_to_log() {
    // Smoke test for the stdio-piping plumbing. We can't easily intercept
    // /tmp/isol8-desktop.log from the test harness, so this just verifies
    // that spawning a process that produces stderr + exits doesn't crash
    // the parent (e.g. by panicking on a closed pipe in the forwarding
    // tasks). /bin/sh with no args reads stdin then exits — close enough
    // to exercise both the stdout and stderr pipes.
    let tempdir = TempDir::new().unwrap();
    let id = generate_keypair();
    let handle = spawn(
        std::path::PathBuf::from("/bin/sh"),
        12345,
        tempdir.path().to_path_buf(),
        &id.private_key_pem,
        &id.public_key_pem,
        "tok",
    )
    .await
    .unwrap();
    // Confirm spawn returned cleanly and shutdown is idempotent.
    handle.shutdown().await;
}
