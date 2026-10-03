//! Held sessions: a small process per session (`lightbulb-desktop holder …`) owns a pty running
//! the unmodified Claude Code or Codex, outlives the app, and serves windows over a unix socket.
//! Spec: docs/superpowers/specs/2026-10-02-managed-session-sharing.md §5.1-5.2.

pub mod manager;
pub mod model;
pub mod protocol;
pub mod run;
pub mod window;
