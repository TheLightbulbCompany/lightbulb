#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    // A hook, launch agent or shell function an older release installed: never the window.
    if let Some(code) = lightbulb_desktop::legacy::argv(&argv) {
        std::process::exit(code);
    }
    lightbulb_desktop::run()
}
