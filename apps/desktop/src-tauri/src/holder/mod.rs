//! Held sessions live in core (`lightbulb_core::holder`). The app adds only the panel terminal,
//! a Tauri child webview, which writes `shown` for the relay to read.

pub use lightbulb_core::holder::{identity, manager, model, protocol, run, shown};
pub mod panel;
