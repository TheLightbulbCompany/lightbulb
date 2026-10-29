//! The no-Tauri guard for the files still to move into `lightbulb-core`
//! (docs/superpowers/plans/2026-10-21-lightbulb-core-crate-split.md §4.3).
//! `OFFENDERS` is today's list; each decoupling PR shortens it, and the big move deletes this
//! test, because core's Cargo.toml has no tauri and the compiler then enforces the rule.

const TO_CORE: &[(&str, &str)] = &[
    ("cli.rs", include_str!("cli.rs")),
    ("headless.rs", include_str!("headless.rs")),
    ("local_sessions.rs", include_str!("local_sessions.rs")),
    ("local_sessions/approval.rs", include_str!("local_sessions/approval.rs")),
    ("local_sessions/claude.rs", include_str!("local_sessions/claude.rs")),
    ("local_sessions/codex.rs", include_str!("local_sessions/codex.rs")),
    ("local_sessions/enrollment.rs", include_str!("local_sessions/enrollment.rs")),
    ("local_sessions/hooked.rs", include_str!("local_sessions/hooked.rs")),
    ("local_sessions/lifecycle.rs", include_str!("local_sessions/lifecycle.rs")),
    ("local_sessions/mod_socket.rs", include_str!("local_sessions/mod_socket.rs")),
    ("local_sessions/native_mcp.rs", include_str!("local_sessions/native_mcp.rs")),
    ("local_sessions/native_tools.rs", include_str!("local_sessions/native_tools.rs")),
    ("local_sessions/plugin.rs", include_str!("local_sessions/plugin.rs")),
    ("local_sessions/presence.rs", include_str!("local_sessions/presence.rs")),
    ("local_sessions/remote_input.rs", include_str!("local_sessions/remote_input.rs")),
    ("local_sessions/resume.rs", include_str!("local_sessions/resume.rs")),
    ("local_sessions/screen.rs", include_str!("local_sessions/screen.rs")),
    ("local_sessions/session_states.rs", include_str!("local_sessions/session_states.rs")),
    ("local_sessions/snapshot.rs", include_str!("local_sessions/snapshot.rs")),
    ("local_sessions/start_links.rs", include_str!("local_sessions/start_links.rs")),
    ("local_sessions/stats.rs", include_str!("local_sessions/stats.rs")),
    ("local_sessions/transport.rs", include_str!("local_sessions/transport.rs")),
    ("local_sessions/workspace.rs", include_str!("local_sessions/workspace.rs")),
    ("session_continue.rs", include_str!("session_continue.rs")),
    ("session_pickup.rs", include_str!("session_pickup.rs")),
    ("shell_integration.rs", include_str!("shell_integration.rs")),
];

const FORBIDDEN: &[&str] = &[
    "tauri",
    "agent_chrome",
    "node_client",
    "node_invoke",
    "session_browser::",
    "desktop_permissions",
    "AuthState",
    "focus_or_recreate_main_window",
    "holder::panel",
];

/// Today's offenders, as (file, term). Shorten it as files decouple; never add to it.
const OFFENDERS: &[(&str, &str)] = &[
    ("cli.rs", "tauri"),
    ("headless.rs", "tauri"),
    ("headless.rs", "node_client"),
    ("local_sessions/enrollment.rs", "tauri"),
    ("local_sessions/enrollment.rs", "node_client"),
    ("local_sessions/enrollment.rs", "AuthState"),
    ("local_sessions/native_mcp.rs", "session_browser::"),
    ("local_sessions/native_tools.rs", "session_browser::"),
    ("local_sessions/resume.rs", "tauri"),
    ("session_pickup.rs", "tauri"),
    ("shell_integration.rs", "tauri"),
];

#[test]
fn files_bound_for_core_name_no_app_module() {
    let found: Vec<(&str, &str)> = TO_CORE
        .iter()
        .flat_map(|(file, src)| FORBIDDEN.iter().filter(|t| src.contains(**t)).map(move |t| (*file, *t)))
        .collect();
    let new: Vec<_> = found.iter().filter(|f| !OFFENDERS.contains(f)).collect();
    assert!(new.is_empty(), "a file bound for lightbulb-core now names an app module: {new:?}");
    let gone: Vec<_> = OFFENDERS.iter().filter(|o| !found.contains(o)).collect();
    assert!(gone.is_empty(), "fixed: remove these from OFFENDERS: {gone:?}");
}
