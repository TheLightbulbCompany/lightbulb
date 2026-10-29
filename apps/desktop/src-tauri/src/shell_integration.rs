//! Settings → "Share every new session": one guarded block in ~/.zshrc and ~/.bashrc that
//! defines `claude` and `codex` shell functions calling `lightbulb claude|codex`, which shares
//! the session live or runs the real tool (`cli.rs`). Off removes exactly that block.

use std::path::{Path, PathBuf};

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

/// Where the block finds the app when this copy is not installed (a build folder, the disk image).
const DEFAULT_APP: &str = "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop";

/// This app's binary when installed, else the default install path: the block names it
/// directly, never `lightbulb` on PATH or in ~/.local/bin, which another program may own.
fn app_exe() -> PathBuf {
    let home = home().unwrap_or_default();
    std::env::current_exe()
        .and_then(std::fs::canonicalize)
        .ok()
        .and_then(|exe| crate::cli::installed_exe(&exe, &home))
        .unwrap_or_else(|| DEFAULT_APP.into())
}

/// The block. Interactive shells only (bash reads ~/.bashrc for some non-interactive ones), and
/// the real tool whenever stdin or stdout is not a terminal: Claude Code copies shell functions
/// into its agents' shells, which have none. `function name {` so an alias of the same name is
/// not expanded where the function is defined. The app by absolute path, `cli` so it runs as the
/// command (`cli::cli_args`); the app itself runs the real tool when it is not running or cannot
/// share. A missing app runs the real tool here. The app's exit code always stands: the app
/// execs the provider, so retrying on a failure could run the provider twice.
pub fn block(exe: &Path) -> String {
    let app = format!("'{}'", exe.display().to_string().replace('\'', r"'\''"));
    let function = |tool: &str| {
        format!(
            "    function {tool} {{ if [ -t 0 ] && [ -t 1 ] && [ -x {app} ]; then {app} cli {tool} \"$@\"; return; fi; command {tool} \"$@\"; }}\n"
        )
    };
    format!(
        "{BEGIN}\n# Added by the Lightbulb app (Settings → Share every new session). Turning it off removes this block.\ncase $- in\n  *i*)\n{}{}    ;;\nesac\n{END}\n",
        function("claude"),
        function("codex")
    )
}

/// `text` with the block removed, then appended when `on`. Everything else stays as it was.
pub fn with_block(text: &str, on: bool, exe: &Path) -> String {
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
        out.push_str(&block(exe));
    }
    out
}

pub fn is_on(home: &Path) -> bool {
    all_rc_files()
        .iter()
        .any(|rc| std::fs::read_to_string(home.join(rc)).is_ok_and(|t| t.lines().any(|l| l.trim_end() == BEGIN)))
}

/// Add (creating a missing file) or remove the block in each rc file. Idempotent.
pub fn set(home: &Path, on: bool) -> Result<(), String> {
    set_with(home, on, &app_exe())
}

/// At launch, when sharing is on: rewrite a block that differs (an older version's, or one
/// naming another path), so existing installs heal without touching the setting.
pub fn refresh_at_launch() {
    if let Err(e) = home().and_then(|h| refresh(&h, &app_exe())) {
        crate::log(&format!("[shell-integration] refresh: {e}"));
    }
}

fn refresh(home: &Path, exe: &Path) -> Result<(), String> {
    if is_on(home) { set_with(home, true, exe) } else { Ok(()) }
}

fn set_with(home: &Path, on: bool, exe: &Path) -> Result<(), String> {
    for rc in if on { rc_files(home) } else { all_rc_files() } {
        let path = home.join(rc);
        let old = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && on => String::new(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(format!("~/{rc}: {e}")),
        };
        let new = with_block(&old, on, exe);
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

    fn b() -> String {
        block(Path::new(DEFAULT_APP))
    }

    #[test]
    fn shell_block_calls_the_app_by_absolute_path_and_falls_back() {
        let b = b();
        assert!(b.starts_with(BEGIN) && b.trim_end().ends_with(END));
        assert!(b.contains("case $- in\n  *i*)"));
        assert!(b.contains(r#"function claude { if [ -t 0 ] && [ -t 1 ] && [ -x '/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop' ]; then '/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop' cli claude "$@"; return; fi; command claude "$@"; }"#));
        assert!(b.contains(r#"cli codex "$@"; return; fi; command codex "$@"; }"#));
        // Never whatever owns the `lightbulb` name.
        assert!(!b.contains(".local/bin") && !b.contains("command -v"));
        assert!(block(Path::new("/x/it's/lightbulb-desktop")).contains(r"'/x/it'\''s/lightbulb-desktop' cli claude"));
    }

    /// The block in a real zsh and bash (BSD `script` gives it a terminal so `-t 0/1` hold): the
    /// app routes and its exit code stands, even 126/127 (never a second run); a missing or
    /// non-executable app runs the real tool.
    #[cfg(target_os = "macos")]
    #[test]
    fn shell_block_runs_the_real_tool_when_the_app_cannot() {
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join("bin");
        let apps = h.join("My Apps");
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::create_dir_all(&apps).unwrap();
        let script = |p: &Path, body: &str| {
            std::fs::write(p, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(p, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        };
        script(&bin.join("claude"), "echo real-claude \"$@\"");
        let app = apps.join("lightbulb-desktop");
        std::fs::write(h.join("rc"), block(&app)).unwrap();
        let run = |shell: &str| {
            let cmd = format!("{shell} -ic '. \"$HOME/rc\"; claude hi; echo rc=$?' 2>/dev/null");
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
            let _ = std::fs::remove_file(&app);
            assert!(run(shell).contains("real-claude hi"), "{shell}: no app");
            std::fs::write(&app, "not a program").unwrap();
            assert!(run(shell).contains("real-claude hi"), "{shell}: app not executable");
            script(&app, "exit 127");
            let out = run(shell);
            assert!(out.contains("rc=127") && !out.contains("real-claude"), "{shell}: {out}");
            script(&app, "[ \"$1\" = cli ] || exit 99; shift; echo app \"$@\"; exit 3");
            let out = run(shell);
            assert!(out.contains("app claude hi") && out.contains("rc=3"), "{shell}: {out}");
            assert!(!out.contains("real-claude"), "{shell}: the provider's own exit is not a fallback");
        }
    }

    #[test]
    fn shell_block_adds_once_and_removes_exactly_itself() {
        let exe = Path::new(DEFAULT_APP);
        let mine = "export A=1\nalias ll='ls -l'";
        let on = with_block(mine, true, exe);
        assert_eq!(on, format!("{mine}\n{}", b()));
        assert_eq!(with_block(&on, true, exe), on, "idempotent");
        assert_eq!(with_block(&on, false, exe), format!("{mine}\n"));
        let around = format!("a\n{}b\n", b());
        assert_eq!(with_block(&around, false, exe), "a\nb\n", "lines after the block stay");
        assert_eq!(with_block("", true, exe), b());
        assert_eq!(with_block("x\n", false, exe), "x\n");
    }

    /// Launch heals an older block (here the 2026-10 one that called `lightbulb` from PATH) and
    /// leaves a home with sharing off alone.
    #[test]
    fn launch_refresh_rewrites_an_old_block_only_when_on() {
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let home = tempfile::tempdir().unwrap();
        let old = format!("export A=1\n{BEGIN}\ncase $- in\n  *i*)\n    function claude {{ \"$HOME/.local/bin/lightbulb\" claude \"$@\"; }}\n    ;;\nesac\n{END}\n");
        std::fs::write(home.path().join(".zshrc"), &old).unwrap();
        refresh(home.path(), exe).unwrap();
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), format!("export A=1\n{}", block(exe)));
        refresh(home.path(), exe).unwrap();
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), format!("export A=1\n{}", block(exe)), "idempotent");

        let off = tempfile::tempdir().unwrap();
        std::fs::write(off.path().join(".zshrc"), "export A=1\n").unwrap();
        refresh(off.path(), exe).unwrap();
        assert_eq!(std::fs::read_to_string(off.path().join(".zshrc")).unwrap(), "export A=1\n");
        assert!(!off.path().join(".bashrc").exists(), "off creates nothing");
    }

    #[test]
    fn shell_set_creates_and_cleans_only_the_two_rc_files() {
        // In tests this binary is not installed, so the block names the default app path.
        let home = tempfile::tempdir().unwrap();
        std::fs::write(home.path().join(".zshrc"), "export A=1\n").unwrap();
        set(home.path(), true).unwrap();
        assert!(is_on(home.path()));
        assert_eq!(std::fs::read_to_string(home.path().join(".bashrc")).unwrap(), b(), "created");
        set(home.path(), true).unwrap();
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), format!("export A=1\n{}", b()));
        set(home.path(), false).unwrap();
        assert!(!is_on(home.path()));
        assert_eq!(std::fs::read_to_string(home.path().join(".zshrc")).unwrap(), "export A=1\n");
        assert!(!home.path().join(".profile").exists());

        // A bash login file that exists gets the block too (login shells skip ~/.bashrc);
        // none is ever created.
        let login = tempfile::tempdir().unwrap();
        std::fs::write(login.path().join(".profile"), "PATH=x\n").unwrap();
        set(login.path(), true).unwrap();
        assert_eq!(std::fs::read_to_string(login.path().join(".profile")).unwrap(), format!("PATH=x\n{}", b()));
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
