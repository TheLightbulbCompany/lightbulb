//! The page's whole reach into this Mac is the `generate_handler!` list in `lib.rs`, and the page
//! is a remote origin. This pins that list: a new command must be added here on purpose, and one
//! that widens what runs on this Mac must ask a native "Allow" (`confirm_native_labelled`) first.

const LIB: &str = include_str!("../src/lib.rs");

/// Every command, and why it widens nothing.
const COMMANDS: &[(&str, &str)] = &[
    ("updater_identity", "read"),
    ("apply_update_manifest", "a signed update; the signature is the check"),
    ("notify::native_notify", "a banner"),
    ("notify::notification_permission", "macOS asks itself"),
    ("voice_key::voice_mic_live", "the mic pill; the one-time ask is native"),
];

fn handler_list() -> Vec<String> {
    let start = LIB.find("tauri::generate_handler![").expect("generate_handler!") + "tauri::generate_handler![".len();
    let end = start + LIB[start..].find(']').unwrap();
    LIB[start..end].split(',').map(str::trim).filter(|c| !c.is_empty()).map(str::to_owned).collect()
}

#[test]
fn every_command_is_classified() {
    let table: Vec<String> = COMMANDS.iter().map(|(c, _)| (*c).to_owned()).collect();
    assert_eq!(handler_list(), table, "every command is classified here, in generate_handler! order");
}
