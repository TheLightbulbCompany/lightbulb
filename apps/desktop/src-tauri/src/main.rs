#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(unix)]
    if std::env::args().nth(1).as_deref() == Some("holder") {
        // `holder <socket> -- <argv…>`: one held session, in its own process.
        let args: Vec<String> = std::env::args().collect();
        let (Some(socket), Some(argv)) = (args.get(2), args.get(4..).filter(|a| args.get(3).map(String::as_str) == Some("--") && !a.is_empty())) else {
            std::process::exit(2);
        };
        let cwd = std::env::current_dir().unwrap_or_else(|_| "/".into());
        let code = match lightbulb_desktop::holder::run::run(std::path::Path::new(socket), argv, &cwd) {
            Ok(()) => 0,
            Err(_) => 1,
        };
        std::process::exit(code);
    }
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
