//! Settings → "Share every session live": one guarded block in ~/.zshrc and ~/.bashrc that
//! defines `claude` and `codex` shell functions calling `lightbulb claude|codex`, which shares
//! the session live or runs the real tool (`cli.rs`). Off removes exactly that block.

use std::path::Path;

const BEGIN: &str = "# >>> lightbulb live sessions >>>";
const END: &str = "# <<< lightbulb live sessions <<<";
const RC_FILES: &[&str] = &[".zshrc", ".bashrc"];
/// Bash login shells read the first of these that exists and never ~/.bashrc, so the block goes
/// there too. None is created: a new ~/.bash_profile would stop bash reading an existing ~/.profile.
const BASH_LOGIN_FILES: &[&str] = &[".bash_profile", ".bash_login", ".profile"];

/// Every startup file the block belongs in for this home: zsh, bash interactive, bash login.
fn rc_files(home: &Path) -> Vec<&'static str> {
    let mut files = RC_FILES.to_vec();
    files.extend(BASH_LOGIN_FILES.iter().find(|f| home.join(f).exists()));
    files
}

/// The block. Interactive shells only (bash reads ~/.bashrc for some non-interactive ones), and
/// the real tool whenever stdin or stdout is not a terminal: Claude Code copies shell functions
/// into its agents' shells, which have none. `function name {` so an alias of the same name is
/// not expanded where the function is defined.
pub fn block() -> String {
    let function = |tool: &str| {
        format!(
            "    function {tool} {{ if [ -t 0 ] && [ -t 1 ]; then local lb=\"$HOME/.local/bin/lightbulb\"; [ -x \"$lb\" ] || lb=\"$(command -v lightbulb 2>/dev/null)\"; if [ -n \"$lb\" ]; then \"$lb\" {tool} \"$@\"; return; fi; fi; command {tool} \"$@\"; }}\n"
        )
    };
    format!(
        "{BEGIN}\n# Added by the Lightbulb app (Settings → Share every session live). Turning it off removes this block.\ncase $- in\n  *i*)\n{}{}    ;;\nesac\n{END}\n",
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

pub fn is_on(home: &Path) -> bool {
    rc_files(home)
        .iter()
        .any(|rc| std::fs::read_to_string(home.join(rc)).is_ok_and(|t| t.lines().any(|l| l.trim_end() == BEGIN)))
}

/// Add (creating a missing file) or remove the block in each rc file. Idempotent.
pub fn set(home: &Path, on: bool) -> Result<(), String> {
    for rc in rc_files(home) {
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
        assert!(b.contains(r#"function claude { if [ -t 0 ] && [ -t 1 ]; then local lb="$HOME/.local/bin/lightbulb"; [ -x "$lb" ] || lb="$(command -v lightbulb 2>/dev/null)"; if [ -n "$lb" ]; then "$lb" claude "$@"; return; fi; fi; command claude "$@"; }"#));
        assert!(b.contains(r#"then "$lb" codex "$@"; return; fi; fi; command codex "$@"; }"#));
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
        set(login.path(), false).unwrap();
        assert_eq!(std::fs::read_to_string(login.path().join(".profile")).unwrap(), "PATH=x\n");

        let empty = tempfile::tempdir().unwrap();
        set(empty.path(), false).unwrap();
        assert!(!empty.path().join(".zshrc").exists(), "off never creates a file");
    }
}
