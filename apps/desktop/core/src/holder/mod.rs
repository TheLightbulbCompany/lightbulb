//! Held sessions: a small process per session (`lightbulb[-desktop] holder …`) owns a pty running
//! the unmodified Claude Code or Codex, outlives the app, and serves the app's panel terminal and live viewers over a unix socket.
//! Spec: docs/superpowers/specs/2026-10-02-managed-session-sharing.md §5.1-5.2.
//! The app's panel terminal (`holder/panel.rs`) stays in the app and writes `shown`.

pub mod identity;
pub mod manager;
pub mod model;
pub mod protocol;
pub mod run;
pub mod shown;
