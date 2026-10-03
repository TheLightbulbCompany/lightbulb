//! The `lightbulb` command (spec §5.3): `lightbulb claude|codex [any provider arguments]` asks
//! the running app to start a held, shared session in this folder, says who it is shared with,
//! then is that session's terminal.
//!
//! The app does the start (`session_start::cli_start`): the installed tool, the approved
//! folders (this folder or one it is inside), and sharing must be on. The provider's arguments
//! pass through unchanged (owner ruling 2026-10-03); only the forms with no terminal to share
//! are refused. This process only turns words into that request and moves bytes.

/// Codex subcommands that run without the interactive terminal (`resume` and `fork` are TUIs).
const CODEX_NON_TUI: &[&str] = &[
    "exec", "e", "review", "login", "logout", "mcp", "mcp-server", "app-server", "app",
    "completion", "sandbox", "debug", "apply", "a", "cloud", "features", "help",
];

/// Why these arguments cannot be a shared session, if they cannot: the non-interactive forms
/// have no terminal to share. Everything after `--` is the provider's own text, never checked.
// ponytail: a Codex flag whose VALUE is exactly a subcommand name (`--profile review`) is refused
// too; a per-flag value table would fix it if anyone hits it.
pub fn refusal(tool: &str, args: &[String]) -> Option<String> {
    let before_dashes = args.iter().take_while(|a| *a != "--");
    for arg in before_dashes {
        let refused = match tool {
            "claude" => {
                arg == "--print"
                    || arg.starts_with("--print=")
                    || (arg.len() > 1 && arg.starts_with('-') && !arg.starts_with("--") && arg[1..].bytes().all(|b| b.is_ascii_alphabetic()) && arg.contains('p'))
            }
            "codex" => CODEX_NON_TUI.contains(&arg.as_str()),
            _ => false,
        };
        if refused {
            return Some(format!(
                "lightbulb {tool} starts an interactive shared session, and `{tool} {arg}` has no terminal to share. Run plain `{tool} {arg}` instead."
            ));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn cli_refuses_only_the_forms_with_no_terminal_to_share() {
        for ok in [&["--resume", "abc"][..], &["--dangerously-skip-permissions"], &["--continue"],
            &["--permission-mode", "bypassPermissions"], &["--model", "opus", "fix it"], &["--", "-p"], &[]] {
            assert_eq!(refusal("claude", &words(ok)), None, "{ok:?}");
        }
        for no in [&["-p", "hi"][..], &["--print", "hi"], &["-cp", "hi"], &["--print=json"]] {
            assert!(refusal("claude", &words(no)).is_some(), "{no:?}");
        }
        for ok in [&["resume"][..], &["fork"], &["--full-auto", "fix the build"], &["-m", "o3"], &["--", "exec"], &[]] {
            assert_eq!(refusal("codex", &words(ok)), None, "{ok:?}");
        }
        for no in [&["exec", "hi"][..], &["e", "hi"], &["review"], &["-m", "o3", "exec", "hi"], &["login"], &["mcp", "list"], &["app-server"]] {
            assert!(refusal("codex", &words(no)).is_some(), "{no:?}");
        }
    }
}
