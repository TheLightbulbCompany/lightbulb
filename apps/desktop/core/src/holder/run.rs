//! The holder process: one pty, one provider, a terminal model, an offset-addressed output log,
//! and a unix socket. One interactive client at a time; any number of output-only clients, and
//! input-only `type` clients (a remote, admitted teammate's keystrokes relayed by the app).

use super::model::{is_probe_reply, Model};
use super::protocol::*;
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::collections::{HashMap, HashSet, VecDeque};
use std::io::{Result, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::mpsc::{channel, sync_channel, SyncSender, TrySendError};
use std::time::{Duration, Instant};

/// Output kept for `REPLAY`. Older output is answered with `GAP`.
pub const LOG_CAP: usize = 1 << 20;
const ROWS: u16 = 40;
const COLS: u16 = 120;
/// Frames queued for one client before it counts as stalled and is disconnected.
const CLIENT_QUEUE: usize = 1024;
/// Output bytes read but not yet handled before the pty reader waits.
const OUTPUT_BACKLOG: usize = 8 << 20;
/// How long to keep reading output after the provider exits.
const EXIT_DRAIN: Duration = Duration::from_secs(1);
/// How long a provider told to end (END) has to exit before it is killed. Claude Code 2.1.292
/// exits 0.33 s after SIGHUP.
pub const END_KILL_AFTER: Duration = Duration::from_secs(5);

/// `--size=<cols>x<rows>`: the size of the terminal that asked for this session.
pub fn parse_size(arg: &str) -> Option<(u16, u16)> {
    let (cols, rows) = arg.strip_prefix("--size=")?.split_once('x')?;
    Some((cols.parse().ok()?, rows.parse().ok()?))
}

/// The user's login-shell environment, read once (as VS Code does). The provider is started
/// directly with it: running it *through* an interactive shell runs the user's rc prompts
/// (oh-my-zsh "update? [Y/n]") inside the session.
pub fn login_environment() -> Vec<(String, String)> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    // This app's own binary prints the environment it inherits as JSON: exact for any value,
    // and independent of `env -0`, which macOS's `env` lacks before recent releases.
    let captured = std::env::current_exe().ok().and_then(|exe| {
        let exe = format!("'{}'", exe.to_string_lossy().replace('\'', r"'\''"));
        let out = std::process::Command::new(&shell)
            .args(["-l", "-i", "-c", &format!("exec {exe} print-env")])
            .env("DISABLE_AUTO_UPDATE", "true")
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output()
            .ok()
            .filter(|o| o.status.success())?;
        parse_environment(&out.stdout)
    });
    // Never start a provider with an empty environment: fall back to what this process has.
    captured.unwrap_or_else(|| std::env::vars().collect())
}

const ENV_BEGIN: &str = "\u{1e}LIGHTBULB-ENV-BEGIN\u{1e}";
const ENV_END: &str = "\u{1e}LIGHTBULB-ENV-END\u{1e}";

/// The JSON `print-env` writes between its markers (shell startup files may print around it);
/// None unless it holds at least HOME and PATH.
pub fn parse_environment(output: &[u8]) -> Option<Vec<(String, String)>> {
    let text = String::from_utf8_lossy(output);
    let start = text.rfind(ENV_BEGIN)? + ENV_BEGIN.len();
    let end = start + text[start..].find(ENV_END)?;
    let map: std::collections::BTreeMap<String, String> = serde_json::from_str(&text[start..end]).ok()?;
    if !map.contains_key("HOME") || !map.contains_key("PATH") {
        return None;
    }
    Some(
        map.into_iter()
            .filter(|(k, _)| !k.is_empty() && !matches!(k.as_str(), "SHLVL" | "_" | "PWD" | "OLDPWD" | "DISABLE_AUTO_UPDATE"))
            .collect(),
    )
}

/// `lightbulb-desktop print-env`: this process's environment as one JSON object.
pub fn print_environment() {
    let map: std::collections::BTreeMap<String, String> =
        std::env::vars_os().map(|(k, v)| (k.to_string_lossy().into_owned(), v.to_string_lossy().into_owned())).collect();
    println!("{ENV_BEGIN}{}{ENV_END}", serde_json::to_string(&map).unwrap_or_else(|_| "{}".into()));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packaged_codex_keeps_its_npm_update_route_when_run_without_the_launcher() {
        let program = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex";
        let env = provider_environment(vec![], &[program.into()]);
        assert!(env.contains(&("CODEX_MANAGED_BY_NPM".into(), "1".into())));
        assert!(env.contains(&("CODEX_MANAGED_PACKAGE_ROOT".into(), "/opt/homebrew/lib/node_modules/@openai/codex".into())));
        let wrapped = vec!["/bin/sh".into(), "-c".into(), "…".into(), "lightbulb-start".into(), "start-1".into(), "/scope".into(), program.into()];
        let inherited = vec![("CODEX_MANAGED_BY_BUN".into(), "1".into()), ("CODEX_MANAGED_PACKAGE_ROOT".into(), "/other".into())];
        let env = provider_environment(inherited, &wrapped);
        assert!(env.contains(&("CODEX_MANAGED_BY_NPM".into(), "1".into())));
        assert!(!env.iter().any(|(k, v)| k == "CODEX_MANAGED_BY_BUN" || v == "/other"));
        // A genuine Homebrew binary and Claude must not be labelled as npm installs.
        assert!(provider_environment(vec![], &["/opt/homebrew/Caskroom/codex/0.163.0/codex".into()]).is_empty());
    }

    #[test]
    fn holder_size_argument_parses_and_refuses_anything_else() {
        assert_eq!(parse_size("--size=120x40"), Some((120, 40)));
        assert_eq!(parse_size("--"), None);
        assert_eq!(parse_size("--size=x"), None);
        assert_eq!(parse_size("--size=80"), None);
    }

    #[test]
    fn holder_size_follows_the_latest_user_and_a_stale_client_is_only_remembered() {
        let mut latest = None;
        // The Mac's terminal attaches: it is the latest user.
        assert_eq!(latest_user(&mut latest, 1, true, (176, 63)), Some((176, 63)));
        // The app's panel resizes without using the session (a layout change): not applied.
        assert_eq!(latest_user(&mut latest, 2, false, (120, 40)), None);
        // The panel is opened or typed into: it wins.
        assert_eq!(latest_user(&mut latest, 2, true, (120, 40)), Some((120, 40)));
        // Now the panel's own resizes apply without a claim, and the terminal's stale ones do not.
        assert_eq!(latest_user(&mut latest, 2, false, (100, 30)), Some((100, 30)));
        assert_eq!(latest_user(&mut latest, 1, false, (176, 63)), None);
        assert_eq!(latest, Some(2));
    }

    #[test]
    fn holder_input_origin_is_a_teammate_only_for_a_type_client_s_plain_input() {
        assert_eq!(input_origin(INPUT, false), "owner");
        assert_eq!(input_origin(INPUT, true), "teammate");
        assert_eq!(input_origin(OWNER_INPUT, true), "owner");
        assert_eq!(input_origin(OWNER_INPUT, false), "owner");
    }

    /// Owner ruling 2026-10-07: every Claude Code session a holder hosts runs in the classic
    /// renderer, whatever the person's environment asks for. Codex is told by its argv (`provider_argv`).
    #[test]
    fn holder_gives_claude_code_the_classic_renderer_and_codex_nothing() {
        let words = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let classic = |env: &[(&str, &str)], argv: &[&str]| {
            let env = provider_environment(env.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(), &words(argv));
            env.into_iter().filter(|(k, _)| k == CLASSIC_RENDERER).map(|(_, v)| v).collect::<Vec<_>>()
        };
        const START: &str = "lightbulb-start";
        for claude in ["/Users/a/.local/share/claude/versions/2.1.293", "/opt/homebrew/Caskroom/claude-code/2.1.293/claude", "/opt/ClaudeCode.app/Contents/MacOS/claude"] {
            assert!(is_claude_program(Path::new(claude)), "{claude}");
            for argv in [&[claude, "--resume", "abc"][..], &["/bin/sh", "-c", "…", START, "start-1", "/scope", claude, "--model", "opus"],
                &["/bin/sh", "-c", "…", "lightbulb-attach", claude, "8ed71738"]] {
                assert_eq!(classic(&[("PATH", "/bin")], argv), ["1"], "{argv:?}");
            }
            // The person's own choice of renderer does not hold for a shared session: one value, ours.
            assert_eq!(classic(&[(CLASSIC_RENDERER, "0"), ("CLAUDE_CODE_NO_FLICKER", "1")], &[claude]), ["1"]);
        }
        let codex = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex";
        for argv in [&[codex, "--no-daemon", "resume", "x"][..], &["/bin/sh", "-c", "…", START, "start-1", "/scope", codex, "--no-daemon"],
            &["/bin/sh", "-c", "…", "lightbulb-attach", codex, "019a1b2c-0000-7000-8000-000000000000"],
            // A prompt that names Claude, a folder called claude, another program's versions folder.
            &[codex, "--", "run claude"], &["/Users/claude/bin/codex"], &["/other/versions/2.1.293"], &["/bin/sh", "-c", "claude"], &[]] {
            assert_eq!(classic(&[("PATH", "/bin")], argv), Vec::<String>::new(), "{argv:?}");
        }
        // Not ours to set means not ours to remove.
        assert_eq!(classic(&[(CLASSIC_RENDERER, "1")], &[codex]), ["1"]);
    }

    /// Owner ruling 2026-10-08: every Codex session a holder hosts runs without the alternate
    /// screen, as Claude Code runs its classic renderer. The shapes are the ones the test above
    /// names; `tui.alternate_screen=never` tested on Codex 0.159.3 (no `ESC[?1049h` in a pty).
    #[test]
    fn holder_runs_codex_inline_however_it_was_started_and_nothing_else() {
        let words = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let ran = |argv: &[&str]| provider_argv(&words(argv));
        const START: &str = "lightbulb-start";
        const THREAD: &str = "019a1b2c-0000-7000-8000-000000000000";
        for codex in ["/opt/homebrew/bin/codex", "/Users/a b/Application Support/node modules/bin/codex",
            "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"] {
            // New session and `lightbulb codex`, with and without a start link; a resume; a prompt.
            assert_eq!(ran(&[codex]), [codex, "-c", CODEX_INLINE]);
            assert_eq!(ran(&[codex, "--no-daemon", "--sandbox", "read-only", "resume", THREAD]), [codex, "-c", CODEX_INLINE, "--no-daemon", "--sandbox", "read-only", "resume", THREAD]);
            assert_eq!(ran(&[codex, "-m", "gpt-5", "--no-daemon", "--", "--no-alt-screen is a prompt"]), [codex, "-c", CODEX_INLINE, "-m", "gpt-5", "--no-daemon", "--", "--no-alt-screen is a prompt"]);
            assert_eq!(ran(&["/bin/sh", "-c", "…", START, "start-1", "/scope", codex, "--no-daemon", "resume", "--last"]),
                ["/bin/sh", "-c", "…", START, "start-1", "/scope", codex, "-c", CODEX_INLINE, "--no-daemon", "resume", "--last"]);
            assert_eq!(ran(&["/bin/sh", "-c", "…", START, "start-1", "/scope", codex]), ["/bin/sh", "-c", "…", START, "start-1", "/scope", codex, "-c", CODEX_INLINE]);
            // A thread on Codex's shared daemon: the script names the arguments, so it carries the setting.
            let attach = super::super::manager::attach_argv(Path::new(codex), THREAD);
            let script = format!("{} -c tui.alternate_screen=never", super::super::manager::CODEX_ATTACH_SCRIPT);
            assert_eq!(provider_argv(&attach), ["/bin/sh", "-c", script.as_str(), "lightbulb-attach", codex, THREAD]);
            assert!(script.starts_with(r#"exec "$1" resume "$2" "#), "still Codex's arguments at the end, the program and the thread as given");
        }
        // Not Codex: Claude Code (its attach script too), a shell, a prompt or a folder that names Codex.
        let claude = "/Users/a/.local/share/claude/versions/2.1.293";
        let same: [Vec<String>; 7] = [words(&[claude, "--resume", "abc"]), words(&["/bin/sh", "-c", "…", START, "start-1", "/scope", claude, "codex"]),
            super::super::manager::attach_argv(Path::new(claude), "8ed71738"), words(&["/bin/sh", "-c", "codex"]), words(&["/codex/bin/claude"]),
            words(&["/bin/codex-wrapper"]), vec![]];
        for argv in same {
            assert_eq!(provider_argv(&argv), argv);
        }
        // The environment is not how Codex is told: nothing of Claude Code's is set for it.
        assert!(provider_environment(vec![], &words(&["/opt/homebrew/bin/codex"])).is_empty());
    }

    #[test]
    fn holder_environment_parses_and_refuses_an_empty_capture() {
        let wrap = |json: &str| format!("Welcome back!\n{ENV_BEGIN}{json}{ENV_END}\nbye\n").into_bytes();
        let env = parse_environment(&wrap(r#"{"HOME":"/Users/a","PATH":"/usr/bin","SHLVL":"2","MULTI":"a\nb"}"#)).unwrap();
        assert!(env.contains(&("MULTI".into(), "a\nb".into())));
        assert!(!env.iter().any(|(k, _)| k == "SHLVL"));
        assert_eq!(parse_environment(b""), None);
        let kept = provider_environment(vec![("PATH".into(), "/bin".into()), ("CLAUDE_CODE_CHILD_SESSION".into(), "1".into()),
            ("CLAUDE_CODE_SSE_PORT".into(), "1".into()), ("CLAUDE_CODE_USE_BEDROCK".into(), "1".into())], &["/opt/homebrew/bin/codex".into()]);
        assert_eq!(kept.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(), ["PATH", "CLAUDE_CODE_USE_BEDROCK"],
            "another session's markers go; the person's own Claude Code settings stay");
        assert_eq!(parse_environment(&wrap(r#"{"PATH":"/usr/bin"}"#)), None);
        assert_eq!(parse_environment(br#"{"HOME":"/a","PATH":"/b"}"#), None, "markers are required");
    }
}

enum Event {
    Output(Vec<u8>),
    Connect(u64, UnixStream),
    Frame(u64, u8, Vec<u8>),
    Gone(u64),
    Exit(String, u32),
    /// The pty reader stopped: no more output.
    Eof,
}

/// Set on a holder whose own environment is the provider's (a terminal's, from `lightbulb
/// claude`): use it as it is, with no login-shell capture.
pub const INHERIT_ENV: &str = "LIGHTBULB_HOLDER_INHERIT_ENV";

pub fn run(socket: &Path, argv: &[String], cwd: &Path, size: Option<(u16, u16)>) -> Result<()> {
    let env = if std::env::var_os(INHERIT_ENV).is_some() {
        std::env::vars_os()
            .map(|(k, v)| (k.to_string_lossy().into_owned(), v.to_string_lossy().into_owned()))
            .filter(|(k, _)| k != INHERIT_ENV)
            .collect()
    } else {
        login_environment()
    };
    run_with_env(socket, argv, cwd, env, size)
}

/// Variables one Claude Code session sets for its own children. A held session that inherits
/// them (the app or `lightbulb` started from inside another session) would count as that
/// session's child: transcript saving off, the parent's message socket and IDE connection.
const SESSION_MARKERS: &[&str] = &[
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID", "CLAUDE_EFFORT",
    // The parent's job folder: a held session that inherits it writes that job's state.json and
    // names itself "<that job> (2)" (seen 2026-10-26, a throwaway renamed itself after the owner's
    // live job and marked it "stuck on a startup dialog").
    "CLAUDE_JOB_DIR",
];

/// Claude Code's own switch for its classic renderer (code.claude.com/docs/en/fullscreen). Every
/// Claude Code session a holder hosts runs with it (owner ruling 2026-10-07): the fullscreen
/// renderer draws on the alternate screen, so the output has no history and a teammate watching
/// cannot scroll back. It wins over the person's `tui` setting and over `CLAUDE_CODE_NO_FLICKER=1`
/// (the first row of Claude Code's own table; tested on 2.1.293), and there is no setting for it.
pub const CLASSIC_RENDERER: &str = "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN";

/// Whether `program` is Claude Code as this app runs it: `claude`, or the release file its
/// installer links that name to (`claude/versions/<x.y.z>`). Codex is always `codex`.
pub fn is_claude_program(program: &Path) -> bool {
    let name = |p: &Path| p.file_name().and_then(|n| n.to_str()).map(str::to_owned);
    let Some(file) = name(program) else { return false };
    let release = || {
        let versions = program.parent()?;
        (name(versions)? == "versions" && name(versions.parent()?)? == "claude"
            && file.split('.').count() == 3
            && file.split('.').all(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())))
        .then_some(())
    };
    file == "claude" || release().is_some()
}

/// Where the provider's program is in a holder's argv, for every way this app starts one (New
/// session, a remote start and `lightbulb claude|codex` through `session_start::held_argv`; a
/// resume and a pick-up directly; `manager::attach_argv` for a session the provider's own daemon hosts).
fn program_at(argv: &[String]) -> Option<usize> {
    match argv {
        [sh, _, _, name, _, _, _, ..] if sh == "/bin/sh" && name == "lightbulb-start" => Some(6),
        [sh, _, _, name, _, _] if sh == "/bin/sh" && name == "lightbulb-attach" => Some(4),
        [_, ..] => Some(0),
        [] => None,
    }
}

/// Whether a holder's argv runs Claude Code.
pub fn runs_claude_code(argv: &[String]) -> bool {
    program_at(argv).is_some_and(|at| is_claude_program(Path::new(&argv[at])))
}

/// Codex's own setting for its inline mode (`codex --help`: "--no-alt-screen … Runs the TUI in
/// inline mode, preserving terminal scrollback history"; `tui.alternate_screen = "never"` is the
/// same switch in its config, both since 0.81.0). Every Codex session a holder hosts runs with it,
/// for the reason Claude Code runs its classic renderer (`CLASSIC_RENDERER`): on the alternate
/// screen a teammate watching cannot scroll back. Tested on 0.159.3 in a pty: one `ESC[?1049h`
/// and mouse reporting without it, neither with it. Given as a `-c` override, not as the flag:
/// it wins over the person's config.toml, every subcommand takes it, and a Codex that does not
/// know the key ignores it, where an unknown flag stops Codex with a usage error.
pub const CODEX_INLINE: &str = "tui.alternate_screen=never";

/// The argv the holder runs for `argv`: Codex with `CODEX_INLINE`, anything else as it is. The
/// holder still names `argv` in its HELLO, so the app knows its own starts by their shape.
// Codex writes finished lines above its prompt through a scroll region (`ESC[1;<n>r`), which a
// terminal saves and vt100 0.16 does not: the holder's model keeps them itself (`Model::process`),
// so a Codex holder answers HISTORY as a Claude Code one does.
pub fn provider_argv(argv: &[String]) -> Vec<String> {
    let mut out = argv.to_vec();
    let Some(at) = codex_at(argv) else { return out };
    if is_attach(argv) {
        // The program is the script's `$1`; the script ends with Codex's own arguments.
        out[2] = format!("{} -c {CODEX_INLINE}", out[2]);
    } else {
        // Root options go before a subcommand (`resume`) and before `--` and the prompt.
        out.insert(at + 1, CODEX_INLINE.into());
        out.insert(at + 1, "-c".into());
    }
    out
}

/// Where Codex's program is in a holder's argv, when that is what it runs.
fn codex_at(argv: &[String]) -> Option<usize> {
    program_at(argv).filter(|at| Path::new(&argv[*at]).file_name().is_some_and(|name| name == "codex"))
}

/// Whether a holder's argv runs Codex (always inline, `provider_argv`).
pub fn runs_codex(argv: &[String]) -> bool {
    codex_at(argv).is_some()
}

/// The environment the provider `argv` runs is started with: the captured one, minus another
/// session's markers, and for Claude Code with the classic renderer whatever the capture said.
pub fn provider_environment(env: Vec<(String, String)>, argv: &[String]) -> Vec<(String, String)> {
    let classic = runs_claude_code(argv);
    let mut env: Vec<_> = env.into_iter().filter(|(k, _)| !SESSION_MARKERS.contains(&k.as_str()) && !(classic && k == CLASSIC_RENDERER)).collect();
    if classic {
        env.push((CLASSIC_RENDERER.into(), "1".into()));
    }
    // provider_program bypasses codex.js to run its packaged native binary. Preserve
    // the launcher's install metadata too: without it Codex guesses Homebrew from
    // /opt/homebrew, even when npm owns the installation there.
    if let Some(root) = codex_at(argv).and_then(|at| Path::new(&argv[at]).ancestors().find(|p| {
        p.ends_with("node_modules/@openai/codex")
    })) {
        const MANAGED: &[&str] = &["CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM", "CODEX_MANAGED_BY_VITE_PLUS", "CODEX_MANAGED_PACKAGE_ROOT"];
        env.retain(|(k, _)| !MANAGED.contains(&k.as_str()));
        let manager = if root.components().any(|c| c.as_os_str() == ".bun") { "CODEX_MANAGED_BY_BUN" } else { "CODEX_MANAGED_BY_NPM" };
        env.push((manager.into(), "1".into()));
        env.push(("CODEX_MANAGED_PACKAGE_ROOT".into(), root.to_string_lossy().into_owned()));
    }
    env
}

pub fn run_with_env(socket: &Path, argv: &[String], cwd: &Path, env: Vec<(String, String)>, size: Option<(u16, u16)>) -> Result<()> {
    // The terminal that asked for this session, if one did: its first screen is drawn at that
    // size, so a replay of it fits that terminal.
    let (cols, rows) = size.map_or((COLS, ROWS), |(c, r)| (c.clamp(1, 1000), r.clamp(1, 500)));
    let env = provider_environment(env, argv);
    let run = provider_argv(argv);
    let program = run.first().ok_or_else(|| std::io::Error::other("no program"))?;
    let pair = native_pty_system()
        .openpty(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
        .map_err(std::io::Error::other)?;
    let mut cmd = CommandBuilder::new(program);
    cmd.args(&run[1..]);
    cmd.env_clear();
    for (k, v) in env {
        cmd.env(k, v);
    }
    // A GUI app's environment has none of these.
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    if cmd.get_env("LANG").is_none() {
        cmd.env("LANG", "en_US.UTF-8");
    }
    cmd.cwd(cwd);
    // Bind under another name and rename into place so discovery sees the published socket
    // only after it is listening.
    // It is published before the provider starts, so the provider's first hook finds the holder
    // (an early `hello` waits in the listen backlog until the event loop answers it).
    let binding = socket.with_extension("bind");
    let _ = std::fs::remove_file(&binding);
    let listener = UnixListener::bind(&binding)?;
    std::fs::set_permissions(&binding, std::fs::Permissions::from_mode(0o600))?;
    std::fs::rename(&binding, socket)?;

    let started = (|| {
        let child = pair.slave.spawn_command(cmd).map_err(std::io::Error::other)?;
        let reader = pair.master.try_clone_reader().map_err(std::io::Error::other)?;
        let pty = pair.master.take_writer().map_err(std::io::Error::other)?;
        Ok::<_, std::io::Error>((child, reader, pty))
    })();
    let (mut child, mut reader, mut pty) = match started {
        Ok(parts) => parts,
        Err(e) => {
            let _ = std::fs::remove_file(socket);
            return Err(e);
        }
    };
    drop(pair.slave);
    let provider_pid = child.process_id().unwrap_or(0);

    let (tx, rx) = channel::<Event>();
    // Output read but not yet handled. Past OUTPUT_BACKLOG the reader waits, so a provider
    // writing faster than the holder serves is held back by its terminal instead of growing
    // memory. Only output is bounded: bounding input too could deadlock a paste.
    let backlog = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let reader_backlog = backlog.clone();
    let out_tx = tx.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 65536];
        while let Ok(n) = std::io::Read::read(&mut reader, &mut buf) {
            if n == 0 {
                break;
            }
            reader_backlog.fetch_add(n, Ordering::SeqCst);
            if out_tx.send(Event::Output(buf[..n].to_vec())).is_err() {
                break;
            }
            while reader_backlog.load(Ordering::SeqCst) > OUTPUT_BACKLOG {
                std::thread::sleep(Duration::from_millis(5));
            }
        }
        let _ = out_tx.send(Event::Eof);
    });
    let exit_tx = tx.clone();
    // Set once the provider is reaped: its pid may then belong to another process.
    let reaped = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let provider_reaped = reaped.clone();
    std::thread::spawn(move || {
        let (status, code) = match child.wait() {
            Ok(s) => (format!("{s:?}"), s.exit_code()),
            Err(e) => (e.to_string(), 1),
        };
        provider_reaped.store(true, Ordering::SeqCst);
        let _ = exit_tx.send(Event::Exit(status, code));
    });
    std::thread::spawn(move || {
        for (id, conn) in (1u64..).zip(listener.incoming()) {
            let Ok(conn) = conn else { continue };
            let Ok(mut read_side) = conn.try_clone() else { continue };
            if tx.send(Event::Connect(id, conn)).is_err() {
                return;
            }
            let tx = tx.clone();
            std::thread::spawn(move || loop {
                match read_frame(&mut read_side) {
                    Ok((kind, payload)) => {
                        if tx.send(Event::Frame(id, kind, payload)).is_err() {
                            return;
                        }
                    }
                    Err(_) => {
                        let _ = tx.send(Event::Gone(id));
                        return;
                    }
                }
            });
        }
    });

    let mut model = Model::new(rows, cols);
    let classic = runs_claude_code(argv);
    if classic {
        model.classic_claude_code();
    }
    // Inline Codex leaves its lines above the screen too, and the model keeps them.
    let inline = runs_codex(argv);
    if inline {
        model.inline_codex();
    }
    let mut log: VecDeque<u8> = VecDeque::new();
    let mut total: u64 = 0;
    // Connected but not yet introduced: they get nothing until their HELLO is answered.
    let mut pending: HashMap<u64, UnixStream> = HashMap::new();
    let mut clients: HashMap<u64, Client> = HashMap::new();
    let mut interactive: Option<u64> = None;
    let mut watchers: HashSet<u64> = HashSet::new();
    let mut typists: HashSet<u64> = HashSet::new();
    // Interactive `join` clients waiting for the keyboard, oldest first (also watchers and typists).
    let mut joined: Vec<u64> = vec![];
    // `type` clients: a remote teammate's keys, relayed by the app (also typists).
    let mut remote: HashSet<u64> = HashSet::new();
    // The size each client last asked for, and the client that used the session last: the pty is
    // that client's size (tmux's `window-size latest`, owner ruling 2026-10-14). `latest_user`.
    let mut asked: HashMap<u64, (u16, u16)> = HashMap::new();
    let mut latest: Option<u64> = None;
    // Who wrote the last bytes to the pty (HELLO's `lastInput`).
    let mut last_input: Option<&'static str> = None;
    let mut exited: Option<(String, u32, Instant)> = None;
    // Spec 2026-10-07 §5.4: the provider exited; the final screen stays for watchers, in memory only.
    let mut ended = false;
    // When the provider exited (unix ms): HELLO's `endedAt` once `ended`.
    let mut exited_at_ms = 0u64;
    // Its exit status: HELLO's `endedCode` once `ended` (0: it left by itself; a signal is 1).
    let mut exit_code = 0u32;
    // Once `ended`: the picture the person last saw, when the final screen is not it
    // (`Model::end`). Every snapshot is then this picture, and nothing follows it.
    let mut ended_picture: Option<Vec<u8>> = None;
    // The terminal closed before the exit status came: all the output is read.
    let mut drained = false;
    // An END was taken: the provider is on its way out.
    let mut ending = false;

    loop {
        let event = match &exited {
            // The provider is gone: finish once its last output is read (or after a moment,
            // when a leftover child still holds the terminal open).
            Some(_) if drained => Event::Eof,
            Some((_, _, at)) => match rx.recv_timeout(EXIT_DRAIN.saturating_sub(at.elapsed())) {
                Ok(event) => event,
                Err(_) => Event::Eof,
            },
            None => match rx.recv() {
                Ok(event) => event,
                Err(_) => return Ok(()),
            },
        };
        match event {
            Event::Output(bytes) => {
                backlog.fetch_sub(bytes.len(), Ordering::SeqCst);
                if ended_picture.is_some() {
                    // A leftover child still writing: never over the kept picture.
                    continue;
                }
                let answers = model.feed(&bytes);
                if !answers.is_empty() && exited.is_none() {
                    let _ = pty.write_all(&answers);
                }
                let frame = encode(OUTPUT, &with_offset(total, &bytes));
                total += bytes.len() as u64;
                log.extend(&bytes);
                let excess = log.len().saturating_sub(LOG_CAP);
                log.drain(..excess);
                clients.retain(|_, c| c.send(frame.clone()));
            }
            Event::Connect(id, conn) => {
                pending.insert(id, conn);
            }
            Event::Frame(id, HELLO, payload) => {
                let Some(conn) = pending.remove(&id) else { continue };
                let Some(client) = Client::start(conn) else { continue };
                let role = serde_json::from_slice::<serde_json::Value>(&payload).unwrap_or_default();
                // The app's panel joins a session a terminal (`lightbulb claude`) already has: it
                // never takes that terminal's keyboard or size.
                let joining = !ended && role["role"] == "interactive" && role["join"] == true && interactive.is_some();
                // An idle stop (`manager::end_idle`) is END taken only while no terminal or panel has
                // the keyboard, checked and taken in this one turn: nobody can attach in between.
                let idle_end = role["role"] == "origin" && role["idleEnd"] == true && interactive.is_none()
                    && !ending && !ended && exited.is_none() && provider_pid > 1;
                if idle_end {
                    ending = true;
                    model.ending();
                    hang_up(provider_pid, &reaped);
                }
                let (rows, cols) = model.size();
                let facts = serde_json::json!({"protocol": 1, "pid": std::process::id(),
                    "providerPid": provider_pid, "argv": argv, "cwd": cwd, "rows": rows, "cols": cols,
                    "watch": true, "type": true, "joined": joining, "origin": true, "lastInput": last_input, "end": true, "history": classic || inline, "resize": true,
                    "ended": ended, "endedAt": ended.then_some(exited_at_ms), "endedCode": ended.then_some(exit_code),
                    // A terminal (`lightbulb claude`) or the app's panel has the keyboard: an idle stop leaves it be.
                    "attached": interactive.is_some(), "idleEnded": idle_end});
                if !client.send(encode(HELLO, facts.to_string().as_bytes())) {
                    continue;
                }
                if joining {
                    if !client.send(encode(SNAPSHOT, &with_offset(total, &model.snapshot()))) {
                        continue;
                    }
                    watchers.insert(id);
                    typists.insert(id);
                    joined.push(id);
                } else if role["role"] == "interactive" && !ended {
                    // Take the window's size first, so its snapshot is drawn at that size.
                    if let (Some(cols), Some(rows)) = (role["cols"].as_u64(), role["rows"].as_u64()) {
                        let size = (cols.clamp(1, 1000) as u16, rows.clamp(1, 500) as u16);
                        asked.insert(id, size);
                        latest = Some(id);
                        resize_to(&*pair.master, &mut model, &clients, &watchers, size);
                    }
                    if let Some(previous) = interactive.replace(id).filter(|p| *p != id) {
                        if let Some(c) = clients.get(&previous) {
                            c.send(encode(DETACHED, b""));
                        }
                    }
                    // A terminal that just started this session asks for all its output instead
                    // of a snapshot (which clears the screen): it then draws under what that
                    // terminal already shows, as the provider run directly would.
                    let oldest = total - log.len() as u64;
                    let first = match role["replayFrom"].as_u64().filter(|from| (oldest..=total).contains(from)) {
                        Some(from) => {
                            let bytes: Vec<u8> = log.iter().skip((from - oldest) as usize).copied().collect();
                            encode(OUTPUT, &with_offset(from, &bytes))
                        }
                        None => encode(SNAPSHOT, &with_offset(total, &model.snapshot())),
                    };
                    if !client.send(first) {
                        continue;
                    }
                } else if role["role"] == "watch" || (ended && role["role"] == "interactive") {
                    // (After the end an interactive window, the desktop panel included, only watches.)
                    // Output-only, plus the screen to start from, fresh ones on request and every
                    // size change: what a live view needs. It never types and never resizes.
                    if !client.send(encode(SNAPSHOT, &with_offset(total, &ended_picture.clone().unwrap_or_else(|| model.snapshot())))) {
                        continue;
                    }
                    watchers.insert(id);
                } else if role["role"] == "type" {
                    // Input only: it gets no output and never resizes (the window or CLI owns the
                    // size). Dropping its Client still delivers the queued HELLO.
                    typists.insert(id);
                    remote.insert(id);
                    continue;
                } else if role["role"] == "origin" {
                    // The facts were the answer.
                    continue;
                }
                clients.insert(id, client);
            }
            Event::Frame(id, kind @ (INPUT | OWNER_INPUT), payload)
                // Not once End was taken: nothing typed after it may reach a provider that is shutting down.
                if !ended && !ending && (interactive == Some(id) || typists.contains(&id)) && !is_probe_reply(&payload) =>
            {
                // The terminal's own keys take the size back (`latest_user`); a `type` client's
                // keys do not: the app sends its size with them when it has one.
                if interactive == Some(id) && latest != Some(id) {
                    latest = Some(id);
                    if let Some(size) = asked.get(&id) {
                        resize_to(&*pair.master, &mut model, &clients, &watchers, *size);
                    }
                }
                last_input = Some(input_origin(kind, remote.contains(&id)));
                model.typed(&payload, Instant::now());
                let _ = pty.write_all(&payload);
            }
            // From any connection: the socket is this user's alone, and the app's End asks on a
            // short-lived `origin` one.
            Event::Frame(_, END, _) if !ending && !ended && exited.is_none() && provider_pid > 1 => {
                ending = true;
                model.ending();
                hang_up(provider_pid, &reaped);
            }
            Event::Frame(id, RESIZE, payload)
                if !ended && (interactive == Some(id) || remote.contains(&id)) && matches!(payload.len(), 4 | 5) =>
            {
                let cols = u16::from_be_bytes([payload[0], payload[1]]).clamp(1, 1000);
                let rows = u16::from_be_bytes([payload[2], payload[3]]).clamp(1, 500);
                asked.insert(id, (cols, rows));
                // The terminal's window resized: it is used. A `type` client claims only with its flag.
                let claim = interactive == Some(id) || payload.get(4) == Some(&1);
                if let Some(size) = latest_user(&mut latest, id, claim, (cols, rows)) {
                    resize_to(&*pair.master, &mut model, &clients, &watchers, size);
                }
            }
            Event::Frame(id, SNAPSHOT, _) if watchers.contains(&id) => {
                if let Some(client) = clients.get(&id) {
                    client.send(encode(SNAPSHOT, &with_offset(total, &ended_picture.clone().unwrap_or_else(|| model.snapshot()))));
                }
            }
            Event::Frame(id, HISTORY, payload) if watchers.contains(&id) => {
                let (Some((max, _)), Some(client)) = (split_offset(&payload), clients.get(&id)) else { continue };
                client.send(encode(HISTORY, &model.history(max.min(LOG_CAP as u64) as usize)));
                // The screen under those lines, in the same turn: no output falls between the two,
                // so a viewer drawn from both misses no line and has none twice (`screen::pump`).
                client.send(encode(SNAPSHOT, &with_offset(total, &ended_picture.clone().unwrap_or_else(|| model.snapshot()))));
            }
            Event::Frame(id, REPLAY, payload) => {
                let (Some((from, _)), Some(client)) = (split_offset(&payload), clients.get(&id)) else { continue };
                let oldest = total - log.len() as u64;
                if from < oldest {
                    let mut gap = from.to_be_bytes().to_vec();
                    gap.extend_from_slice(&oldest.to_be_bytes());
                    client.send(encode(GAP, &gap));
                }
                let start = from.clamp(oldest, total);
                let bytes: Vec<u8> = log.iter().skip((start - oldest) as usize).copied().collect();
                client.send(encode(OUTPUT, &with_offset(start, &bytes)));
            }
            Event::Frame(..) => {}
            Event::Gone(id) => {
                pending.remove(&id);
                clients.remove(&id);
                watchers.remove(&id);
                typists.remove(&id);
                remote.remove(&id);
                joined.retain(|j| *j != id);
                asked.remove(&id);
                // The latest user left: the terminal's size again, as tmux falls back to the clients left.
                if latest == Some(id) {
                    latest = interactive.filter(|i| *i != id && asked.contains_key(i));
                    if let Some(size) = latest.and_then(|i| asked.get(&i)) {
                        resize_to(&*pair.master, &mut model, &clients, &watchers, *size);
                    }
                }
                if interactive == Some(id) {
                    interactive = None;
                    // The oldest joined client takes the keyboard and then sends its own size.
                    while !joined.is_empty() {
                        let next = joined.remove(0);
                        watchers.remove(&next);
                        typists.remove(&next);
                        if clients.get(&next).is_some_and(|c| c.send(encode(PROMOTED, b""))) {
                            interactive = Some(next);
                            break;
                        }
                    }
                }
            }
            Event::Exit(status, code) => {
                exit_code = code;
                exited = Some((status, code, Instant::now()));
                exited_at_ms = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64);
            }
            Event::Eof => {
                // (None: the terminal closed first; the exit status follows from the wait thread. That
                // is the usual order, a process closes its files before it can be waited for, and
                // waiting EXIT_DRAIN again for output that cannot come held every end back a second.)
                let Some((status, code, exited_at)) = exited.take() else {
                    drained = true;
                    continue;
                };
                // Before anyone hears of the end: whoever asks next gets the kept picture and the lines above it.
                let kept = if is_attach(argv) { None } else { model.end(exited_at) };
                // `startOver`: a terminal that watched the way out has lines above its screen that the
                // kept picture shows again, so the screen relay draws its viewers from the start, when
                // it can send them all of the lines above that picture (`above`, their size in bytes).
                let above = model.starts_over();
                let frame = encode(EXIT, serde_json::json!({ "status": status, "code": code, "startOver": above.is_some(), "above": above }).to_string().as_bytes());
                if is_attach(argv) {
                    // One more client of the provider's own host (spec §4 k): no screen of ours to
                    // keep, and the next viewer needs a new client, so this holder ends with it.
                    for client in clients.values() {
                        client.send(frame.clone());
                    }
                    // Let every writer deliver its last frames (a stalled one gets two seconds).
                    let writers: Vec<_> = clients.into_values().map(|c| c.writer).collect();
                    let deadline = Instant::now() + Duration::from_secs(2);
                    while writers.iter().any(|w| !w.is_finished()) && Instant::now() < deadline {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    let _ = std::fs::remove_file(socket);
                    let _ = std::fs::remove_file(socket.with_extension("thread"));
                    return Ok(());
                }
                // Every client hears the end (the CLI exits with the code, the screen relay stays
                // and asks for the final screen). The socket, model and log stay until the app
                // stops this holder (`manager::stop`): nothing is written anywhere.
                clients.retain(|_, c| c.send(frame.clone()));
                ended = true;
                ended_picture = kept;
                interactive = None;
                latest = None;
                typists.clear();
                remote.clear();
                joined.clear();
                // A resumed Codex thread must bind to its new holder, never to this one.
                let _ = std::fs::remove_file(socket.with_extension("thread"));
            }
        }
    }
}

/// Lightbulb's own attach client (`manager::attach_argv`), by the name that marks it.
fn is_attach(argv: &[String]) -> bool {
    argv.len() == 6 && argv[0] == "/bin/sh" && argv[3] == "lightbulb-attach"
}

/// Who typed an input: a `type` client's plain INPUT is a teammate's; everything else (the
/// interactive client, a joined panel, OWNER_INPUT) is the owner's.
fn input_origin(kind: u8, remote: bool) -> &'static str {
    if remote && kind != OWNER_INPUT { "teammate" } else { "owner" }
}

/// RESIZE as the holder tells watch clients: `u16 BE cols`, `u16 BE rows`.
fn size_frame(cols: u16, rows: u16) -> Vec<u8> {
    let mut payload = cols.to_be_bytes().to_vec();
    payload.extend_from_slice(&rows.to_be_bytes());
    encode(RESIZE, &payload)
}

/// Whether `id`'s RESIZE to `size` applies (owner ruling 2026-10-14, tmux's `window-size latest`):
/// a client that uses the session (`claim`: a terminal's own window, or the app's panel opened or
/// typed into) becomes the latest user; only the latest user's size is the pty's. A resize from
/// any other client is only remembered, for when it uses the session again.
pub(crate) fn latest_user(latest: &mut Option<u64>, id: u64, claim: bool, size: (u16, u16)) -> Option<(u16, u16)> {
    if claim {
        *latest = Some(id);
    }
    (*latest == Some(id)).then_some(size)
}

/// The pty, the model and every watcher at `(cols, rows)`; nothing when it is that size already.
fn resize_to(master: &dyn portable_pty::MasterPty, model: &mut Model, clients: &HashMap<u64, Client>, watchers: &HashSet<u64>, (cols, rows): (u16, u16)) {
    if model.size() == (rows, cols) {
        return;
    }
    let _ = master.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
    model.resize(rows, cols);
    tell(clients, watchers, &size_frame(cols, rows));
}

fn tell(clients: &HashMap<u64, Client>, watchers: &HashSet<u64>, frame: &[u8]) {
    for id in watchers {
        if let Some(client) = clients.get(id) {
            client.send(frame.to_vec());
        }
    }
}

/// A connected client. Frames go through a bounded queue to its own writer thread, so a window
/// that stops reading can never block the holder; when its queue fills it is disconnected.
/// End the provider as a closing terminal would: hang up its process group, then kill what is
/// left of it after END_KILL_AFTER.
fn hang_up(provider_pid: u32, reaped: &std::sync::atomic::AtomicBool) {
    // The provider leads its terminal's process group (the pty made it a session
    // leader): what a closing terminal hangs up on.
    let group = -(provider_pid as libc::pid_t);
    if !reaped.load(Ordering::SeqCst) {
        unsafe { libc::kill(group, libc::SIGHUP) };
    }
    std::thread::spawn(move || {
        // The whole group, not only its leader: a subprocess that ignores the hang-up
        // would otherwise keep working in the folder after End. A group id is not
        // reused while a member lives, and signal 0 fails once none does.
        let alive = || unsafe { libc::kill(group, 0) } == 0;
        let deadline = Instant::now() + END_KILL_AFTER;
        while Instant::now() < deadline && alive() {
            std::thread::sleep(Duration::from_millis(50));
        }
        if alive() {
            unsafe { libc::kill(group, libc::SIGKILL) };
        }
    });
}

struct Client {
    queue: SyncSender<Vec<u8>>,
    stream: UnixStream,
    writer: std::thread::JoinHandle<()>,
}

impl Client {
    fn start(stream: UnixStream) -> Option<Self> {
        let mut writer = stream.try_clone().ok()?;
        let (queue, frames) = sync_channel::<Vec<u8>>(CLIENT_QUEUE);
        let writer = std::thread::spawn(move || {
            for frame in frames {
                if writer.write_all(&frame).is_err() {
                    return;
                }
            }
        });
        Some(Self { queue, stream, writer })
    }

    /// False when the client is gone or too slow; the caller drops it.
    fn send(&self, frame: Vec<u8>) -> bool {
        match self.queue.try_send(frame) {
            Ok(()) => true,
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                let _ = self.stream.shutdown(std::net::Shutdown::Both);
                false
            }
        }
    }
}

