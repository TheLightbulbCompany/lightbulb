#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args().nth(1).as_deref() == Some("session-mcp") {
        lightbulb_desktop::local_sessions::serve_mcp();
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("local-session-hook") {
        // argv is untrusted: only the two providers we install hooks for.
        let provider = match std::env::args().nth(2).as_deref() {
            Some("codex") => "codex",
            _ => "claude",
        };
        lightbulb_desktop::local_sessions::enrollment::hook(provider);
        return;
    }
    lightbulb_desktop::run()
}
