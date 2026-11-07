//! `lightbulb-core`: the parts of the desktop client that build without Tauri.
//! The app (`lightbulb-desktop`) re-exports these modules, so its `crate::…` paths keep working.

pub mod device_token;
pub mod entry;
#[cfg(unix)]
pub mod holder;
pub mod keychain;
pub mod os;
pub mod pair_client;
pub mod pair_state;

/// File-based logger for macOS GUI apps (stdout not visible). Tests print only: their lines
/// ("relay unavailable or pairing revoked", fake session ids) sat in the installed app's own log.
pub fn log(msg: &str) {
    #[cfg(not(test))]
    {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("/tmp/lightbulb-desktop.log") {
            let _ = writeln!(f, "{}", msg);
        }
    }
    println!("{}", msg);
}
