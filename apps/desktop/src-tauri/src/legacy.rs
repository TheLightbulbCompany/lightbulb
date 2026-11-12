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
/// `<app_data>/agent-chrome`, the old agent Chrome's profile.
const AGENT_CHROME: &str = "agent-chrome";
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
            let path = std::env::var_os("PATH").unwrap_or_default();
            let this = std::env::current_exe().and_then(std::fs::canonicalize).ok();
            let Some(real) = find_tool(tool, &path, this.as_deref()) else {
                eprintln!("{tool}: not found on PATH");
                return Some(127);
            };
            let err = std::process::Command::new(&real).arg0(tool).args(&rest[1..]).exec();
            eprintln!("{tool}: {err}");
            Some(127)
        }
        _ => {
            eprintln!("The Lightbulb app no longer has a `lightbulb` command.");
            Some(1)
        }
    }
}

/// The first executable `tool` on `path` that is not this app (`this`, canonical): the old shell
/// function must never resolve back to the app it is calling from.
#[cfg(unix)]
fn find_tool(tool: &str, path: &std::ffi::OsStr, this: Option<&Path>) -> Option<std::path::PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::split_paths(path).map(|dir| dir.join(tool)).find(|cand| {
        let Ok(canon) = std::fs::canonicalize(cand) else { return false };
        let executable = std::fs::metadata(&canon).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0);
        executable && Some(canon.as_path()) != this
    })
}

/// At launch: stop and remove the `serve` launch agent (KeepAlive would restart it forever), take
/// the shell block out of the startup files, and delete the old agent Chrome's profile (it holds
/// workspace sign-in cookies and nothing else can wipe it now). Logs; never fails the launch.
pub fn clean(app_data: Option<std::path::PathBuf>) {
    if let Some(dir) = app_data.map(|d| d.join(AGENT_CHROME)) {
        match std::fs::remove_dir_all(&dir) {
            Ok(()) => crate::log("[legacy] removed the old agent Chrome profile"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => crate::log(&format!("[legacy] agent Chrome profile: {e}")),
        }
    }
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
            match replace(&path, &kept) {
                Ok(()) => crate::log(&format!("[legacy] removed the session block from ~/{name}")),
                Err(e) => crate::log(&format!("[legacy] ~/{name}: {e}")),
            }
        }
    }
}

/// Replaces the file `path` resolves to (a dotfiles symlink stays a symlink) with `text`: a
/// temp file beside it with the same mode, then a rename, so a crash never leaves it half written.
fn replace(path: &Path, text: &str) -> std::io::Result<()> {
    let target = std::fs::canonicalize(path)?;
    let name = target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = target.with_file_name(format!(".{name}.lightbulb-tmp"));
    std::fs::write(&tmp, text)?;
    let done = std::fs::metadata(&target)
        .and_then(|m| std::fs::set_permissions(&tmp, m.permissions()))
        .and_then(|()| std::fs::rename(&tmp, &target));
    if done.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    done
}

/// `text` without the old app's guarded block; every other line exactly as it was. A BEGIN with
/// no END is not our block: the text stays as it is.
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
    if inside {
        return text.to_owned();
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

    #[test]
    fn a_begin_with_no_end_changes_nothing() {
        let rc = format!("export A=1\n{BEGIN}\nfunction claude {{ x; }}\nalias b=c\n");
        assert_eq!(without_block(&rc), rc);
    }

    #[cfg(unix)]
    #[test]
    fn the_real_tool_is_never_this_app() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("lb-legacy-{}", std::process::id()));
        let (ours, theirs) = (dir.join("ours"), dir.join("theirs"));
        for d in [&ours, &theirs] {
            std::fs::create_dir_all(d).unwrap();
            let f = d.join("claude");
            std::fs::write(&f, "#!/bin/sh\n").unwrap();
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let path = std::env::join_paths([&ours, &theirs]).unwrap();
        let this = std::fs::canonicalize(ours.join("claude")).unwrap();
        let found = find_tool("claude", &path, Some(&this)).unwrap();
        assert_eq!(std::fs::canonicalize(found).unwrap(), std::fs::canonicalize(theirs.join("claude")).unwrap());
        let only_ours = std::env::join_paths([&ours]).unwrap();
        assert_eq!(find_tool("claude", &only_ours, Some(&this)), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_rc_file_is_replaced_whole_with_its_mode() {
        let dir = std::env::temp_dir().join(format!("lb-legacy-rc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let rc = dir.join(".zshrc");
        std::fs::write(&rc, "old\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&rc, std::fs::Permissions::from_mode(0o640)).unwrap();
        }
        replace(&rc, "new\n").unwrap();
        assert_eq!(std::fs::read_to_string(&rc).unwrap(), "new\n");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&rc).unwrap().permissions().mode() & 0o777, 0o640);
        }
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1, "no temp file left");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
