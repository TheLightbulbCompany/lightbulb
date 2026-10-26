//! Settings → "Share every new session": one guarded block in ~/.zshrc and ~/.bashrc that
//! defines `claude` and `codex` shell functions calling `lightbulb claude|codex`, which shares
//! the session live or runs the real tool (`cli.rs`). Off removes exactly that block.

use std::path::Path;

const BEGIN: &str = "# >>> lightbulb live sessions >>>";
const END: &str = "# <<< lightbulb live sessions <<<";
const RC_FILES: &[&str] = &[".zshrc", ".bashrc"];
/// Bash login shells read the first of these that exists and never ~/.bashrc, so the block goes
/// there too. None is created: a new ~/.bash_profile would stop bash reading an existing ~/.profile.
const BASH_LOGIN_FILES: &[&str] = &[".bash_profile", ".bash_login", ".profile"];

/// The startup files the block goes into when turned on: zsh, bash interactive, and the bash
/// login file this home uses now.
fn rc_files(home: &Path) -> Vec<&'static str> {
    let mut files = RC_FILES.to_vec();
    files.extend(BASH_LOGIN_FILES.iter().find(|f| home.join(f).exists()));
    files
}

/// Every file the block may be in: off and the status read all of them, since the bash login
/// file in use can change after the block was written.
fn all_rc_files() -> Vec<&'static str> {
    RC_FILES.iter().chain(BASH_LOGIN_FILES).copied().collect()
}

/// The block. Interactive shells only (bash reads ~/.bashrc for some non-interactive ones), and
/// the real tool whenever stdin or stdout is not a terminal: Claude Code copies shell functions
/// into its agents' shells, which have none. `function name {` so an alias of the same name is
/// not expanded where the function is defined. Only the app's own link is used (it points at
/// `…/Contents/MacOS/lightbulb-desktop`, see `cli::install`), never a `lightbulb` found on PATH:
/// on 2026-10-23 the network CLI took that name and every `claude` printed "Unknown command".
/// Anything else, or no link, runs the real tool.
pub fn block() -> String {
    let function = |tool: &str| {
        format!(
            "    function {tool} {{ if [ -t 0 ] && [ -t 1 ]; then local lb=\"$HOME/.local/bin/lightbulb\"; case \"$(readlink \"$lb\" 2>/dev/null)\" in */lightbulb-desktop) if [ -x \"$lb\" ]; then \"$lb\" {tool} \"$@\"; return; fi;; esac; fi; command {tool} \"$@\"; }}\n"
        )
    };
    format!(
        "{BEGIN}\n# Added by the Lightbulb app (Settings → Share every new session). Turning it off removes this block.\ncase $- in\n  *i*)\n{}{}    ;;\nesac\n{END}\n",
        function("claude"),
        function("codex")
    )
}

/// `text` with the block removed, then appended when `on`. Everything else stays as it was.
pub fn with_block(text: &str, on: bool) -> String {
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
    if on {
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(&block());
    }
    out
}

/// At launch: rewrite a block that is on, so an older block gets this version's text.
pub fn refresh_at_launch() {
    let Ok(home) = home() else { return };
    if is_on(&home) {
        if let Err(e) = set(&home, true) {
            crate::log(&format!("[shell-integration] refresh: {e}"));
        }
    }
}

pub fn is_on(home: &Path) -> bool {
    all_rc_files()
        .iter()
        .any(|rc| std::fs::read_to_string(home.join(rc)).is_ok_and(|t| t.lines().any(|l| l.trim_end() == BEGIN)))
}

/// Add (creating a missing file) or remove the block in each rc file. Idempotent.
pub fn set(home: &Path, on: bool) -> Result<(), String> {
    for rc in if on { rc_files(home) } else { all_rc_files() } {
        let path = home.join(rc);
        let old = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && on => String::new(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(format!("~/{rc}: {e}")),
        };
        let new = with_block(&old, on);
        if new != old {
            // Through a symlink (a dotfiles manager's) to the real file, keeping its mode.
            std::fs::write(&path, new).map_err(|e| format!("~/{rc}: {e}"))?;
        }
    }
    crate::log(&format!("[shell-integration] {}", if on { "on" } else { "off" }));
    Ok(())
}

fn home() -> Result<std::path::PathBuf, String> {
    std::env::var_os("HOME").map(Into::into).ok_or_else(|| "No home folder.".to_string())
}

#[tauri::command]
pub fn shell_integration_status() -> bool {
    home().is_ok_and(|h| is_on(&h))
}

#[tauri::command]
pub fn shell_integration_set(enabled: bool) -> Result<bool, String> {
    let home = home()?;
    set(&home, enabled)?;
    Ok(is_on(&home))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_block_routes_only_interactive_terminals_through_lightbulb() {
        let b = block();
        assert!(b.starts_with(BEGIN) && b.trim_end().ends_with(END));
        assert!(b.contains("case $- in\n  *i*)"));
        // The shim by its install path first: ~/.local/bin is often not on PATH yet.
        // Only the app's own link, by its install path (~/.local/bin is often not on PATH yet).
        assert!(b.contains(r#"function claude { if [ -t 0 ] && [ -t 1 ]; then local lb="$HOME/.local/bin/lightbulb"; case "$(readlink "$lb" 2>/dev/null)" in */lightbulb-desktop) if [ -x "$lb" ]; then "$lb" claude "$@"; return; fi;; esac; fi; command claude "$@"; }"#));
        assert!(b.contains(r#"then "$lb" codex "$@"; return; fi;; esac; fi; command codex "$@"; }"#));
        assert!(!b.contains("command -v lightbulb"), "a lightbulb on PATH may be another program");
    }

    /// The block in a real zsh and bash: a link to another program, or none, runs the real
    /// tool; the app's link routes. BSD `script` gives the shell a terminal so `-t 0/1` hold.
    #[cfg(target_os = "macos")]
    #[test]
    fn shell_block_falls_back_to_the_real_tool_unless_the_link_is_the_app() {
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join("bin");
        std::fs::create_dir_all(h.join(".local/bin")).unwrap();
        std::fs::create_dir_all(&bin).unwrap();
        let script = |p: &Path, body: &str| {
            std::fs::write(p, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(p, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        };
        script(&bin.join("claude"), "echo real-claude \"$@\"");
        script(&bin.join("network"), "echo network \"$@\"");
        script(&bin.join("lightbulb-desktop"), "echo app \"$@\"");
        std::fs::write(h.join("rc"), block()).unwrap();
        let link = h.join(".local/bin/lightbulb");
        let run = |shell: &str| {
            let cmd = format!("{shell} -ic '. \"$HOME/rc\"; claude hi' 2>/dev/null");
            let out = std::process::Command::new("script")
                .args(["-q", "/dev/null", "/bin/sh", "-c", &cmd])
                .env("HOME", h)
                .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
                .env("ZDOTDIR", h)
                .stdin(std::process::Stdio::null())
                .output()
                .unwrap();
            String::from_utf8_lossy(&out.stdout).replace('\r', "")
        };
        for shell in ["/bin/zsh", "/bin/bash"] {
            if !Path::new(shell).exists() || !Path::new("/usr/bin/script").exists() {
                continue;
            }
            let _ = std::fs::remove_file(&link);
            assert!(run(shell).contains("real-claude hi"), "{shell}: no link");
            std::os::unix::fs::symlink(bin.join("network"), &link).unwrap();
            assert!(run(shell).contains("real-claude hi"), "{shell}: another program's link");
            std::fs::remove_file(&link).unwrap();
            std::os::unix::fs::symlink(bin.join("lightbulb-desktop"), &link).unwrap();
            assert!(run(shell).contains("app claude hi"), "{shell}: the app's link");
        }
    }

    #[test]
    fn shell_block_adds_once_and_removes_exactly_itself() {
        let mine = "export A=1\nalias ll='ls -l'";
        let on = with_block(mine, true);
        assert_eq!(on, format!("{mine}\n{}", block()));
        assert_eq!(with_block(&on, true), on, "idempotent");
        assert_eq!(with_block(&on, false), format!("{mine}\n"));
        let around = format!("a\n{}b\n", block());
        assert_eq!(with_block(&around, false), "a\nb\n", "lines after the block stay");
        assert_eq!(with_block("", true), block());
        assert_eq!(with_block("x\n", false), "x\n");
    }

    #[test]
    fn shell_set_creates_and_cleans_only_the_two_rc_files() {
        let home = tempfile::tempdir().unwrap();
        std::fs::write(home.path().join(".zshrc"), "export A=1\n").unwrap();
        set(home.path(), true).unwrap();
        assert!(is_on(home.path()));
        assert_eq!(std::fs::read_to_string(home.path().join(".bashrc")).unwrap(), block(), "created");
        set(home.path(), true).unwrap();
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), format!("export A=1\n{}", block()));
        set(home.path(), false).unwrap();
        assert!(!is_on(home.path()));
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), "export A=1\n");
        assert!(!home.path().join(".profile").exists());

        // A bash login file that exists gets the block too (login shells skip ~/.bashrc);
        // none is ever created.
        let login = tempfile::tempdir().unwrap();
        std::fs::write(login.path().join(".profile"), "PATH=x\n").unwrap();
        set(login.path(), true).unwrap();
        assert_eq!(std::fs::read_to_string(login.path().join(".profile")).unwrap(), format!("PATH=x\n{}", block()));
        assert!(!login.path().join(".bash_profile").exists());
        // A ~/.bash_profile created later does not strand the block in ~/.profile.
        std::fs::write(login.path().join(".bash_profile"), "").unwrap();
        set(login.path(), false).unwrap();
        assert_eq!(std::fs::read_to_string(login.path().join(".profile")).unwrap(), "PATH=x\n");
        assert!(!is_on(login.path()));

        let empty = tempfile::tempdir().unwrap();
        set(empty.path(), false).unwrap();
        assert!(!empty.path().join(".zshrc").exists(), "off never creates a file");
    }
}
