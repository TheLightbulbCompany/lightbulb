//! Integration tests for the subprocess supervisor.
//!
//! These tests use `/bin/sleep` as a stand-in for the real openclaw shim —
//! it accepts whatever args the supervisor passes and just blocks. That lets
//! us assert on the identity-file side effects and the non-blocking spawn
//! behavior without needing a real openclaw install on the test machine.

use isol8_desktop::subprocess::spawn;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

#[tokio::test]
async fn spawn_writes_identity_file_with_keypair() {
    let tempdir = TempDir::new().unwrap();
    let handle = spawn(
        std::path::PathBuf::from("/bin/sleep"),
        12345,
        tempdir.path().to_path_buf(),
        "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n",
        "-----BEGIN PUBLIC KEY-----\nDEF\n-----END PUBLIC KEY-----\n",
        "tok_gateway",
    )
    .await
    .unwrap();

    let identity_path = tempdir.path().join("identity/device.json");
    assert!(identity_path.exists(), "identity not written");
    let content = std::fs::read_to_string(&identity_path).unwrap();
    assert!(content.contains("BEGIN PRIVATE KEY"));
    assert!(content.contains("BEGIN PUBLIC KEY"));

    // chmod 600
    let perms = std::fs::metadata(&identity_path).unwrap().permissions();
    let mode = perms.mode() & 0o777;
    assert_eq!(mode, 0o600);

    handle.shutdown().await;
}

#[tokio::test]
async fn spawn_returns_quickly_does_not_block() {
    let tempdir = TempDir::new().unwrap();
    let start = std::time::Instant::now();
    let handle = spawn(
        std::path::PathBuf::from("/bin/sleep"),
        54321,
        tempdir.path().to_path_buf(),
        "PRIV",
        "PUB",
        "tok",
    )
    .await
    .unwrap();
    assert!(start.elapsed() < std::time::Duration::from_secs(2));
    handle.shutdown().await;
}
