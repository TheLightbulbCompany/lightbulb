#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args().nth(1).as_deref() == Some("local-session-hook") {
        lightbulb_desktop::local_sessions::enrollment::hook();
        return;
    }
    lightbulb_desktop::run()
}
