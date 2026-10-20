#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // `lightbulb …` (the ~/.local/bin link) or `lightbulb-desktop cli …`: the command, never the app.
    #[cfg(unix)]
    {
        let argv: Vec<String> = std::env::args().collect();
        if let Some(args) = lightbulb_desktop::cli::cli_args(&argv) {
            std::process::exit(lightbulb_desktop::cli::main(args));
        }
    }
    #[cfg(unix)]
    if std::env::args().nth(1).as_deref() == Some("print-env") {
        lightbulb_desktop::holder::run::print_environment();
        return;
    }
    #[cfg(unix)]
    if std::env::args().nth(1).as_deref() == Some("serve") {
        lightbulb_desktop::headless::serve();
    }
    #[cfg(unix)]
    if std::env::args().nth(1).as_deref() == Some("holder") {
        // `holder <socket> [--size=<cols>x<rows>] -- <argv…>`: one held session, in its own process.
        let args: Vec<String> = std::env::args().collect();
        let size = args.get(3).and_then(|a| lightbulb_desktop::holder::run::parse_size(a));
        let dash = if size.is_some() { 4 } else { 3 };
        let (Some(socket), Some(argv)) = (args.get(2), args.get(dash + 1..).filter(|a| args.get(dash).map(String::as_str) == Some("--") && !a.is_empty())) else {
            std::process::exit(2);
        };
        let cwd = std::env::current_dir().unwrap_or_else(|_| "/".into());
        let code = match lightbulb_desktop::holder::run::run(std::path::Path::new(socket), argv, &cwd, size) {
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
