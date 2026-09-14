#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args().nth(1).as_deref() == Some("local-session-hook") {
        let provider = std::env::args().nth(2).unwrap_or_else(|| "claude".into());
        lightbulb_desktop::local_sessions::enrollment::hook(&provider);
        return;
    }
    lightbulb_desktop::run()
}
