//! What the app's panel terminal shows. The panel writes it; the relay's attach reaper reads it.

use super::manager::HolderInfo;
use std::sync::Mutex;

/// What the panel shows and its holder, so a moving panel is not looked up again.
pub static SHOWN: Mutex<Option<(String, HolderInfo)>> = Mutex::new(None);

/// The holder the panel's terminal shows now, if any (the attach reaper never closes it).
pub fn shown_holder() -> Option<String> {
    SHOWN.lock().ok()?.as_ref().map(|(_, h)| h.id.clone())
}
