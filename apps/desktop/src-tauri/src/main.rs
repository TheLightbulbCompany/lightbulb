#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    // `holder` and `print-env` first: under the ~/.local/bin/lightbulb name every word is a CLI word.
    if let Some(code) = lightbulb_core::entry::dispatch(&argv) {
        std::process::exit(code);
    }
    // `lightbulb …` (the ~/.local/bin link) or `lightbulb-desktop cli …`: the command, never the app.
    #[cfg(unix)]
    if let Some(args) = lightbulb_desktop::cli::cli_args(&argv) {
        std::process::exit(lightbulb_desktop::cli::main(args));
    }
    #[cfg(unix)]
    if argv.get(1).map(String::as_str) == Some("serve") {
        lightbulb_desktop::headless::serve();
    }
    // The retired app MCP server: a provider config an older release wrote still starts it.
    // It fails at once, and the launch's MCP reconcile removes that entry.
    if argv.get(1).map(String::as_str) == Some("session-mcp") {
        std::process::exit(1);
    }
    if argv.get(1).map(String::as_str) == Some("local-session-hook") {
        // argv is untrusted: only the providers we install hooks for.
        let provider = match argv.get(2).map(String::as_str) {
            Some("codex") => "codex",
            Some("cursor") => "cursor",
            Some("grok") => "grok",
            _ => "claude",
        };
        lightbulb_desktop::local_sessions::enrollment::hook(provider);
        return;
    }
    lightbulb_desktop::run()
}
