//! What releases before the voice shell left on a Mac that still calls this binary: Claude and
//! Codex hooks (`local-session-hook`), the `serve` launch agent, the `claude`/`codex` shell
//! functions (`cli claude …`) and the old `~/.local/bin/lightbulb` link. None of them may open a
//! window. The launch agent and the shell block are removed at launch; a hook stays where its
//! provider keeps it and exits at once.
//!
//! ponytail: hook entries in ~/.claude, ~/.codex, … are left in place (they cost one process
//! start each); remove them here if anyone notices.

use std::path::Path;

const WORDS: &[&str] = &["serve", "local-session-hook", "session-mcp", "holder", "print-env", "cli"];
const LAUNCH_AGENT: &str = "Library/LaunchAgents/company.thelightbulb.serve.plist";
const BEGIN: &str = "# >>> lightbulb live sessions >>>";
const END: &str = "# <<< lightbulb live sessions <<<";
const RC_FILES: &[&str] = &[".zshrc", ".bashrc", ".bash_profile", ".bash_login", ".profile"];

/// `Some(exit code)` for a call from something an older release installed; `None` opens the app.
pub fn argv(argv: &[String]) -> Option<i32> {
    let as_link = argv.first().and_then(|a| Path::new(a).file_name()).is_some_and(|n| n == "lightbulb");
    let word = argv.get(1).map(String::as_str);
    let rest = if as_link {
        &argv[1..]
    } else if word == Some("cli") {
        &argv[2..]
    } else if word.is_some_and(|w| WORDS.contains(&w)) {
        return Some(0);
    } else {
        return None;
    };
    match rest.first().map(String::as_str) {
        // A shell that still has the old function: run the real tool, as the function did when
        // it could not share.
        #[cfg(unix)]
        Some(tool @ ("claude" | "codex")) => {
            use std::os::unix::process::CommandExt;
            let err = std::process::Command::new(tool).args(&rest[1..]).exec();
            eprintln!("{tool}: {err}");
            Some(127)
        }
        _ => {
            eprintln!("The Lightbulb app no longer has a `lightbulb` command.");
            Some(1)
        }
    }
}

/// At launch: stop and remove the `serve` launch agent (KeepAlive would restart it forever) and
/// take the shell block out of the startup files. Logs; never fails the launch.
pub fn clean() {
    let Some(home) = std::env::var_os("HOME").map(std::path::PathBuf::from) else { return };
    let agent = home.join(LAUNCH_AGENT);
    if agent.exists() {
        #[cfg(unix)]
        let _ = std::process::Command::new("/bin/launchctl")
            .arg("bootout")
            .arg(format!("gui/{}", unsafe { libc::getuid() }))
            .arg(&agent)
            .status();
        match std::fs::remove_file(&agent) {
            Ok(()) => crate::log("[legacy] removed the serve launch agent"),
            Err(e) => crate::log(&format!("[legacy] serve launch agent: {e}")),
        }
    }
    for name in RC_FILES {
        let path = home.join(name);
        let Ok(text) = std::fs::read_to_string(&path) else { continue };
        let kept = without_block(&text);
        if kept != text {
            match std::fs::write(&path, kept) {
                Ok(()) => crate::log(&format!("[legacy] removed the session block from ~/{name}")),
                Err(e) => crate::log(&format!("[legacy] ~/{name}: {e}")),
            }
        }
    }
}

/// `text` without the old app's guarded block; every other line exactly as it was.
fn without_block(text: &str) -> String {
    let mut out = String::new();
    let mut inside = false;
    for line in text.split_inclusive('\n') {
        match line.trim_end() {
            BEGIN => inside = true,
            END if inside => inside = false,
            _ if !inside => out.push_str(line),
            _ => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(w: &[&str]) -> Vec<String> {
        w.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn a_plain_launch_opens_the_app() {
        let exe = "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop";
        assert_eq!(argv(&words(&[exe])), None);
        assert_eq!(argv(&words(&[exe, "-psn_0_1234"])), None);
        assert_eq!(argv(&words(&[exe, "flickmeon://auth?ticket=x"])), None);
    }

    #[test]
    fn old_installs_never_open_a_window() {
        let exe = "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop";
        for w in ["serve", "local-session-hook", "session-mcp", "holder", "print-env"] {
            assert_eq!(argv(&words(&[exe, w, "claude"])), Some(0), "{w}");
        }
        assert_eq!(argv(&words(&[exe, "cli", "status"])), Some(1));
        assert_eq!(argv(&words(&["/Users/a/.local/bin/lightbulb"])), Some(1));
    }

    #[test]
    fn the_shell_block_comes_out_and_nothing_else() {
        let rc = format!("export A=1\n{BEGIN}\ncase $- in\n  *i*)\n    function claude {{ x; }}\n    ;;\nesac\n{END}\nalias b=c\n");
        assert_eq!(without_block(&rc), "export A=1\nalias b=c\n");
        assert_eq!(without_block("export A=1\n"), "export A=1\n");
    }
}
