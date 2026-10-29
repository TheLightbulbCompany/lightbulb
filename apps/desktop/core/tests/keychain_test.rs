use lightbulb_core::keychain::{
    delete, generate_keypair, load_or_generate, KeychainSlot,
};

#[test]
fn generate_keypair_produces_pem_pair() {
    let id = generate_keypair();
    assert!(id.private_key_pem.contains("BEGIN PRIVATE KEY"));
    assert!(id.public_key_pem.contains("BEGIN PUBLIC KEY"));
    assert!(id.public_key_pem.len() > 100);
}

#[test]
fn generate_keypair_produces_distinct_keys() {
    let a = generate_keypair();
    let b = generate_keypair();
    assert_ne!(a.private_key_pem, b.private_key_pem);
    assert_ne!(a.public_key_pem, b.public_key_pem);
}

#[test]
#[ignore = "requires interactive Keychain access; run locally with --include-ignored"]
fn load_or_generate_persists_across_calls() {
    let slot = KeychainSlot::Mac;
    let service = "company.thelightbulb.desktop.test";
    let _ = delete(slot, service); // best-effort cleanup
    let first = load_or_generate(slot, service).unwrap();
    let second = load_or_generate(slot, service).unwrap();
    assert_eq!(first.public_key_pem, second.public_key_pem);
    delete(slot, service).ok();
}
