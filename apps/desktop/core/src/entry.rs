//! The internal subcommands every Lightbulb binary answers before anything reads its words as
//! CLI commands. A binary named `lightbulb` takes every word as a CLI command, so
//! `lightbulb holder <sock>` (what `holder::manager` spawns from `current_exe`) must be caught here first.

/// `Some(exit code)` when `argv` names an internal subcommand; `None` leaves it to the caller.
pub fn dispatch(argv: &[String]) -> Option<i32> {
    #[cfg(unix)]
    match argv.get(1).map(String::as_str) {
        Some("print-env") => {
            crate::holder::run::print_environment();
            return Some(0);
        }
        // `holder <socket> [--size=<cols>x<rows>] -- <argv…>`: one held session, in its own process.
        Some("holder") => {
            let size = argv.get(3).and_then(|a| crate::holder::run::parse_size(a));
            let dash = if size.is_some() { 4 } else { 3 };
            let (Some(socket), Some(command)) = (argv.get(2), argv.get(dash + 1..).filter(|a| argv.get(dash).map(String::as_str) == Some("--") && !a.is_empty())) else {
                return Some(2);
            };
            let cwd = std::env::current_dir().unwrap_or_else(|_| "/".into());
            return Some(match crate::holder::run::run(std::path::Path::new(socket), command, &cwd, size) {
                Ok(()) => 0,
                Err(_) => 1,
            });
        }
        _ => {}
    }
    let _ = argv;
    None
}

#[cfg(test)]
mod tests {
    use super::dispatch;

    fn words(w: &[&str]) -> Vec<String> {
        w.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn leaves_cli_words_alone() {
        assert_eq!(dispatch(&words(&["lightbulb", "claude"])), None);
        assert_eq!(dispatch(&words(&["lightbulb", "cli", "holder"])), None);
        assert_eq!(dispatch(&words(&["lightbulb"])), None);
    }

    #[cfg(unix)]
    #[test]
    fn holder_is_caught_whatever_the_binary_is_called() {
        for exe in ["lightbulb", "/Users/me/.local/bin/lightbulb", "lightbulb-desktop"] {
            assert_eq!(dispatch(&words(&[exe, "holder"])), Some(2));
            assert_eq!(dispatch(&words(&[exe, "holder", "/tmp/x.sock", "cat"])), Some(2));
            assert_eq!(dispatch(&words(&[exe, "holder", "/tmp/x.sock", "--"])), Some(2));
        }
    }
}
