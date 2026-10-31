//! The provider MCP entries this app keeps. The network engine serves the one `lightbulb`
//! server now (`crate::engine`); this app writes only `lightbulb-browser` and takes out the
//! `session-mcp` entry earlier releases wrote under `lightbulb`, never anyone else's.

use serde_json::{json, Value};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::Command;

const SERVER_NAME: &str = "lightbulb";
/// The retired app server's name before `lightbulb`; an install upgraded straight from it still
/// carries this entry, and its `session-mcp` now exits 1.
const LEGACY_SERVER_NAME: &str = "lightbulb_native_sessions";
/// hooked.rs's note when it replaces a hosted entry; kept only because that file names it.
pub(super) const REPLACED_NOTE: &str = "Mac bridge is serving lightbulb; the hosted entry was replaced";

fn cli_home(command: &mut Command, home: &Path) {
    // The reassertion path also runs with an isolated home during CI. Provider
    // CLIs otherwise inherit the runner's real HOME and would mutate its MCP
    // settings instead of the home we are reconciling.
    if std::env::var_os("HOME").as_deref() != Some(home.as_os_str()) {
        command
            .env("HOME", home)
            .env("CODEX_HOME", home.join(".codex"))
            .env_remove("CLAUDE_CONFIG_DIR");
    }
}

/// The session browser's MCP entry: Google's chrome-devtools-mcp attached to this
/// Mac's session-browser port. We write no browser tools of our own.
pub(super) const BROWSER_SERVER_NAME: &str = "lightbulb-browser";
const NODE: &str = if cfg!(target_arch = "aarch64") { "node-aarch64-apple-darwin" } else { "node-x86_64-apple-darwin" };
/// vendor-sidecars.sh installs the pinned package here, beside the bundled `node`.
const BROWSER_ENTRY: &str = "chrome-devtools-mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js";
/// Node loads it with `--env-file`: `mcp add` takes a command line, not an environment, and the
/// server checks npm for updates (CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS) and reports usage unless told not to.
const BROWSER_ENV: &str = "chrome-devtools-mcp/mcp.env";

fn browser_url() -> String {
    format!("http://127.0.0.1:{}", crate::session_browser::PORT)
}

/// The app's bundled node running its bundled, pinned chrome-devtools-mcp: one
/// process per session. Earlier releases used `npx -y chrome-devtools-mcp@latest`,
/// which added an `npm exec` wrapper, a telemetry watchdog and an npm fetch per
/// session. Each session keeps its own server: session_browser.rs tells sessions
/// apart by the CDP client's process tree. `None` when this build ships no bundle.
pub(super) fn browser_command(executable: &Path) -> Option<Vec<String>> {
    let macos = executable.parent()?;
    // Packaged: Contents/MacOS/<exe> -> Contents/Resources/bin. Dev: target/<profile>/bin.
    let bin = [macos.parent().map(|c| c.join("Resources/bin")), Some(macos.join("bin"))]
        .into_iter()
        .flatten()
        .find(|bin| bin.join(NODE).is_file() && bin.join(BROWSER_ENTRY).is_file() && bin.join(BROWSER_ENV).is_file())?;
    Some(vec![
        bin.join(NODE).to_string_lossy().into_owned(),
        format!("--env-file={}", bin.join(BROWSER_ENV).to_string_lossy()),
        bin.join(BROWSER_ENTRY).to_string_lossy().into_owned(),
        "--browserUrl".into(),
        browser_url(),
        "--no-usage-statistics".into(),
    ])
}

/// Any entry a Lightbulb release wrote: the old npx line, or a bundled line at
/// any app path. A same-named server of another shape is someone else's.
fn is_lightbulb_browser(entry: &Value) -> bool {
    let (Some(command), Some(args)) = (entry["command"].as_str(), entry["args"].as_array()) else {
        return false;
    };
    let args: Vec<&str> = args.iter().filter_map(Value::as_str).collect();
    let url = browser_url();
    if command == "npx" && args == ["-y", "chrome-devtools-mcp@latest", "--browserUrl", url.as_str()] {
        return true;
    }
    command.ends_with(&format!("/{NODE}"))
        && args.len() == 5
        && args[0].starts_with("--env-file=") && args[0].ends_with(&format!("/{BROWSER_ENV}"))
        && args[1].ends_with(&format!("/{BROWSER_ENTRY}"))
        && args[2..] == ["--browserUrl", url.as_str(), "--no-usage-statistics"]
}

/// The `lightbulb` entry an earlier release of this app wrote: `<this app's executable, at any
/// path> session-mcp`. Another program's, even one that also says `session-mcp`, is not ours.
fn ours(entry: &Value, executable: &Path) -> bool {
    entry["args"] == json!(["session-mcp"])
        && entry["command"].as_str().is_some_and(|c| Path::new(c).file_name() == executable.file_name())
}

/// The MCP server entry `name` as the provider stores it (`{command, args}`); Null when absent.
fn entry(home: &Path, cli: &Path, provider: &str, name: &str) -> io::Result<Value> {
    if provider == "claude" {
        let doc = match std::fs::read(home.join(".claude.json")) {
            Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Claude MCP config invalid"))?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Value::Null),
            Err(e) => return Err(e),
        };
        return Ok(doc["mcpServers"][name].clone());
    }
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    let output = command.args(["mcp", "get", name, "--json"]).output()?;
    if !output.status.success() {
        return Ok(Value::Null);
    }
    let doc = serde_json::from_slice::<Value>(&output.stdout)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Codex MCP config invalid"))?;
    Ok(doc["transport"].clone())
}

fn change(home: &Path, cli: &Path, provider: &str, verb: &str, name: &str, line: &[&std::ffi::OsStr]) -> io::Result<()> {
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    if provider == "claude" {
        command.args(["mcp", verb, "--scope", "user", name]);
    } else {
        command.args(["mcp", verb, name]);
    }
    if verb == "add" {
        command.arg("--").args(line);
    }
    if command.output()?.status.success() {
        Ok(())
    } else {
        Err(io::Error::other("native MCP config change failed"))
    }
}

/// `change` on the setup path: the provider's config file is backed up once first.
fn edit(home: &Path, cli: &Path, provider: &str, verb: &str, name: &str, line: &[&std::ffi::OsStr]) -> io::Result<()> {
    let config = if provider == "claude" { home.join(".claude.json") } else { codex_config(home) };
    super::backup_once(&config).map_err(io::Error::other)?;
    change(home, cli, provider, verb, name, line)
}

/// `lightbulb-browser`: added while sharing is on, removed (only if it is ours) when it goes off.
/// An entry an earlier release wrote (npx, or another app path) is replaced with the
/// current one. A server of that name that is not ours is left alone and reported.
fn configure_browser(home: &Path, cli: &Path, provider: &str, executable: &Path, enabled: bool, notes: &mut Vec<String>) {
    let Ok(current) = entry(home, cli, provider, BROWSER_SERVER_NAME) else {
        return notes.push(format!("{provider} MCP settings are unavailable"));
    };
    let is_ours = is_lightbulb_browser(&current);
    if !enabled {
        if is_ours && change(home, cli, provider, "remove", BROWSER_SERVER_NAME, &[]).is_err() {
            notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be removed"));
        }
        return;
    }
    if !current.is_null() && !is_ours {
        return notes.push(format!("{provider} already has a {BROWSER_SERVER_NAME} server that is not Lightbulb's; it was left as it is"));
    }
    // Only an unvendored dev build lacks the bundle; it is logged, not shown as a
    // sharing note, and whatever entry exists is left as it is.
    let Some(ours) = browser_command(executable) else {
        return eprintln!("[local-sessions] {BROWSER_SERVER_NAME} is not bundled beside {}", executable.display());
    };
    if current["command"].as_str() == Some(ours[0].as_str()) && current["args"] == json!(&ours[1..]) {
        return;
    }
    // An earlier release's entry: `mcp add` refuses a taken name, so remove it first.
    if is_ours && edit(home, cli, provider, "remove", BROWSER_SERVER_NAME, &[]).is_err() {
        return notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be updated"));
    }
    let line: Vec<&std::ffi::OsStr> = ours.iter().map(std::ffi::OsStr::new).collect();
    if edit(home, cli, provider, "add", BROWSER_SERVER_NAME, &line).is_err() {
        notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be installed"));
    }
}

fn codex_config(home: &Path) -> PathBuf {
    let codex_home = if std::env::var_os("HOME").as_deref() == Some(home.as_os_str()) {
        std::env::var_os("CODEX_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".codex"))
    } else {
        home.join(".codex")
    };
    codex_home.join("config.toml")
}

/// A TOML `[mcp_servers.lightbulb]` as the `{command, args}` the JSON configs store.
fn toml_entry(doc: &toml_edit::DocumentMut) -> Value {
    let Some(server) = doc.get("mcp_servers").and_then(|s| s.get(SERVER_NAME)) else {
        return Value::Null;
    };
    let args: Vec<&str> = server.get("args").and_then(toml_edit::Item::as_array).into_iter().flatten().filter_map(|v| v.as_str()).collect();
    json!({"command": server.get("command").and_then(toml_edit::Item::as_str), "args": args})
}

/// Read-only, no CLI spawn: "configured" when a `lightbulb` server other than our old
/// `session-mcp` entry (the engine's) is there, else "missing".
pub(super) fn status(home: &Path, provider: &str) -> &'static str {
    let read = |path: PathBuf| std::fs::read_to_string(path).ok();
    let entry = if provider == "claude" {
        read(home.join(".claude.json"))
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .map(|doc| doc["mcpServers"][SERVER_NAME].clone())
    } else {
        read(codex_config(home)).and_then(|t| t.parse::<toml_edit::DocumentMut>().ok()).map(|doc| toml_entry(&doc))
    };
    match entry {
        Some(e) if !e.is_null() && e["args"] != json!(["session-mcp"]) => "configured",
        _ => "missing",
    }
}

/// Undo the bare `approval_mode = "approve"` earlier builds wrote under `lightbulb`'s
/// `send_message` (owner ruling 2026-10-02: Codex's own prompt gates it); anything else is the
/// user's and stays.
fn configure_codex_policy(home: &Path) -> io::Result<()> {
    let path = codex_config(home);
    if !path.exists() {
        return Ok(());
    }
    let mut original = String::new();
    super::secure_file(&path)
        .map_err(io::Error::other)?
        .take(1024 * 1024)
        .read_to_string(&mut original)?;
    let mut doc: toml_edit::DocumentMut = original.parse().map_err(io::Error::other)?;
    let Some(server) = doc
        .get_mut("mcp_servers")
        .and_then(toml_edit::Item::as_table_mut)
        .and_then(|servers| servers.get_mut(SERVER_NAME))
        .and_then(toml_edit::Item::as_table_mut)
    else {
        return Ok(());
    };
    if let Some(tools) = server.get_mut("tools").and_then(toml_edit::Item::as_table_mut) {
        let ours = tools.get("send_message").and_then(toml_edit::Item::as_table).is_some_and(|t| {
            t.len() == 1 && t.get("approval_mode").and_then(toml_edit::Item::as_str) == Some("approve")
        });
        if ours {
            tools.remove("send_message");
            if tools.is_empty() {
                server.remove("tools");
            }
        }
    }
    let updated = doc.to_string();
    if updated != original {
        super::backup_once(&path).map_err(io::Error::other)?;
        super::codex::replace(&path, updated.as_bytes()).map_err(io::Error::other)?;
    }
    Ok(())
}

/// hooked.rs writes our `session-mcp` under `lightbulb` for Cursor and Grok CLI; take it out
/// again (only ours), so the engine's is the one `lightbulb`.
fn drop_hooked(home: &Path, executable: &Path, notes: &mut Vec<String>) {
    let write = |path: &Path, text: String| super::backup_once(path).and_then(|()| super::codex::replace(path, text.as_bytes()));
    let cursor = home.join(".cursor/mcp.json");
    if let Some(mut doc) = std::fs::read_to_string(&cursor).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
        if ours(&doc["mcpServers"][SERVER_NAME], executable) {
            doc["mcpServers"].as_object_mut().map(|s| s.remove(SERVER_NAME));
            if write(&cursor, serde_json::to_string_pretty(&doc).unwrap_or_default()).is_err() {
                notes.push("Cursor's old lightbulb entry could not be removed".into());
            }
        }
    }
    let grok = home.join(".grok/config.toml");
    if let Some(mut doc) = std::fs::read_to_string(&grok).ok().and_then(|t| t.parse::<toml_edit::DocumentMut>().ok()) {
        if ours(&toml_entry(&doc), executable) {
            doc["mcp_servers"].as_table_like_mut().map(|s| s.remove(SERVER_NAME));
            if write(&grok, doc.to_string()).is_err() {
                notes.push("Grok CLI's old lightbulb entry could not be removed".into());
            }
        }
    }
}

/// Keep the CLI-owned configs, including other servers and credentials, intact. Enabled or
/// not, our old `session-mcp` `lightbulb` entry goes; any other `lightbulb` stays. Only
/// `lightbulb-browser` follows `enabled`.
pub(super) fn configure(home: &Path, executable: &Path, enabled: bool) -> Vec<String> {
    let mut notes = Vec::new();
    for provider in ["claude", "codex"] {
        let Some(cli) = super::provider_program(home, provider) else {
            continue;
        };
        configure_browser(home, &cli, provider, executable, enabled, &mut notes);
        for name in [SERVER_NAME, LEGACY_SERVER_NAME] {
            match entry(home, &cli, provider, name) {
                Ok(current) if ours(&current, executable) => {
                    if edit(home, &cli, provider, "remove", name, &[]).is_err() {
                        notes.push(format!("{provider} old {name} entry could not be removed"));
                    }
                }
                Ok(_) => {}
                Err(_) => notes.push(format!("{provider} MCP settings are unavailable")),
            }
        }
        if provider == "codex" && configure_codex_policy(home).is_err() {
            notes.push("Codex MCP settings are unavailable".into());
        }
    }
    drop_hooked(home, executable, &mut notes);
    notes
}

/// Engine tools whose arguments and results stay out of the shared transcript: destinations,
/// request ids, message bodies and task contents, plus the retired app server's `send` and
/// `receipt`, which old transcripts still carry. One list for the Claude and Codex scrubbers.
pub(super) const PRIVATE_TOOLS: &[&str] = &[
    "send", "receipt", "send_message", "message_receipt", "check_inbox", "read_messages", "find_tasks", "read_task", "update_task",
    "assign", "cancel_assignment",
];

pub(super) fn private_tool(name: &str) -> bool {
    PRIVATE_TOOLS.contains(&name)
}

/// A Claude tool name (`mcp__lightbulb__read_task`, or the retired app server's
/// `mcp__lightbulb_native_sessions__send` in an old transcript) for one of `PRIVATE_TOOLS`.
pub(super) fn private_call(tool: &str) -> bool {
    tool.strip_prefix("mcp__lightbulb__")
        .or_else(|| tool.strip_prefix("mcp__lightbulb_native_sessions__"))
        .is_some_and(private_tool)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_tool_covers_assignments_but_not_the_worker_listing() {
        assert!(private_tool("assign"));
        assert!(private_tool("cancel_assignment"));
        assert!(!private_tool("find_workers"));
    }

    #[test]
    fn mcp_edits_back_up_the_provider_config_once() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let cli = home.path().join("fake-claude");
        std::fs::write(&cli, "#!/bin/sh\nexit 0\n").unwrap();
        std::fs::set_permissions(&cli, std::fs::Permissions::from_mode(0o755)).unwrap();
        let config = home.path().join(".claude.json");
        std::fs::write(&config, "{\"mine\":1}").unwrap();
        edit(home.path(), &cli, "claude", "add", SERVER_NAME, &[]).unwrap();
        std::fs::write(&config, "{\"mine\":2}").unwrap();
        edit(home.path(), &cli, "claude", "remove", SERVER_NAME, &[]).unwrap();
        assert_eq!(std::fs::read_to_string(home.path().join(".claude.json.lightbulb-backup")).unwrap(), "{\"mine\":1}");
    }


    #[test]
    fn codex_policy_removes_only_our_send_approval() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir(home.path().join(".codex")).unwrap();
        let config = home.path().join(".codex/config.toml");
        let executable = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        std::fs::write(&config, format!(
            "[mcp_servers.lightbulb]\ncommand = \"{}\"\nargs = [\"session-mcp\"]\n\
             [mcp_servers.lightbulb.tools.send_message]\napproval_mode = \"approve\"\n\
             [mcp_servers.lightbulb.tools.message_receipt]\napproval_mode = \"prompt\"\n\
             [mcp_servers.other]\ncommand = \"/bin/other\"\nargs = [\"serve\"]\n",
            executable.display()
        )).unwrap();
        configure_codex_policy(home.path()).unwrap();
        let doc: toml_edit::DocumentMut = std::fs::read_to_string(&config).unwrap().parse().unwrap();
        assert!(doc["mcp_servers"]["lightbulb"]["tools"].get("send_message").is_none());
        assert_eq!(doc["mcp_servers"]["lightbulb"]["tools"]["message_receipt"]["approval_mode"].as_str(), Some("prompt"));
        assert_eq!(doc["mcp_servers"]["other"]["command"].as_str(), Some("/bin/other"));
        // A user's own policy on send_message stays.
        std::fs::write(&config, format!(
            "[mcp_servers.lightbulb]\ncommand = \"{}\"\nargs = [\"session-mcp\"]\n\
             [mcp_servers.lightbulb.tools.send_message]\napproval_mode = \"prompt\"\n",
            executable.display()
        )).unwrap();
        configure_codex_policy(home.path()).unwrap();
        let doc: toml_edit::DocumentMut = std::fs::read_to_string(&config).unwrap().parse().unwrap();
        assert_eq!(doc["mcp_servers"]["lightbulb"]["tools"]["send_message"]["approval_mode"].as_str(), Some("prompt"));
    }


    #[cfg(unix)]
    #[test]
    fn sharing_on_adds_the_browser_entry_and_off_removes_only_ours() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        // A fake `claude` CLI that edits ~/.claude.json the way `claude mcp` does.
        let fake = bin.join("claude");
        std::fs::write(&fake, r#"#!/usr/bin/env python3
import json, os, sys
p = os.path.join(os.environ["HOME"], ".claude.json")
doc = json.load(open(p)) if os.path.exists(p) else {}
servers = doc.setdefault("mcpServers", {})
a = sys.argv[1:]
if a[:2] == ["mcp", "remove"]: servers.pop(a[-1], None)
elif a[:2] == ["mcp", "add"]: servers[a[4]] = {"command": a[6], "args": a[7:]}
json.dump(doc, open(p, "w"))
"#).unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        if super::super::provider_program(h, "claude") != std::fs::canonicalize(&fake).ok() {
            return; // like enable_replaces above: provider discovery may not look in this temp home
        }
        let app = tempfile::tempdir().unwrap();
        let exe = bundled_app(app.path());
        let servers = || serde_json::from_str::<Value>(&std::fs::read_to_string(h.join(".claude.json")).unwrap()).unwrap()["mcpServers"].clone();
        let ours = browser_command(&exe).unwrap();
        configure(h, &exe, true);
        assert_eq!(servers()[BROWSER_SERVER_NAME], json!({"command": ours[0], "args": &ours[1..]}));
        configure(h, &exe, false);
        assert!(servers()[BROWSER_SERVER_NAME].is_null());

        // An existing user's npx entry from an earlier release is replaced, other servers kept.
        let npx = json!({"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:39222"]});
        std::fs::write(h.join(".claude.json"), json!({"mcpServers": {BROWSER_SERVER_NAME: npx, "chrome-devtools": npx}}).to_string()).unwrap();
        let notes = configure(h, &exe, true);
        // (Only Claude's: a CI machine's codex CLI may not install into this temp home at all.)
        assert!(!notes.iter().any(|n| n.starts_with("claude ") && n.contains(BROWSER_SERVER_NAME)), "{notes:?}");
        assert_eq!(servers()[BROWSER_SERVER_NAME], json!({"command": ours[0], "args": &ours[1..]}));
        assert_eq!(servers()["chrome-devtools"], npx, "a server under another name is never touched");

        // The app moved: our bundled entry at the old path follows it.
        let moved = tempfile::tempdir().unwrap();
        let moved_exe = bundled_app(moved.path());
        configure(h, &moved_exe, true);
        assert_eq!(servers()[BROWSER_SERVER_NAME]["command"], browser_command(&moved_exe).unwrap()[0]);
        // Disabling with the old path still removes it: any Lightbulb shape is ours.
        configure(h, &exe, false);
        assert!(servers()[BROWSER_SERVER_NAME].is_null());

        for foreign in [
            json!({"command": "/bin/other", "args": []}),
            // npx chrome-devtools-mcp the person set up themselves, under our name.
            json!({"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]}),
            json!({"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222"]}),
        ] {
            std::fs::write(h.join(".claude.json"), json!({"mcpServers": {BROWSER_SERVER_NAME: foreign}}).to_string()).unwrap();
            let notes = configure(h, &exe, true);
            assert!(notes.iter().any(|n| n.contains("not Lightbulb's")), "{notes:?}");
            assert_eq!(servers()[BROWSER_SERVER_NAME], foreign);
            configure(h, &exe, false);
            assert_eq!(servers()[BROWSER_SERVER_NAME], foreign, "someone else's is never removed");
        }
    }

    /// A fake `.app` with the bundled node and chrome-devtools-mcp entry; returns its executable.
    fn bundled_app(root: &Path) -> PathBuf {
        let bin = root.join("Lightbulb.app/Contents/Resources/bin");
        std::fs::create_dir_all(bin.join(BROWSER_ENTRY).parent().unwrap()).unwrap();
        std::fs::write(bin.join(NODE), b"").unwrap();
        std::fs::write(bin.join(BROWSER_ENTRY), b"").unwrap();
        std::fs::write(bin.join(BROWSER_ENV), b"").unwrap();
        root.join("Lightbulb.app/Contents/MacOS/lightbulb-desktop")
    }

    #[test]
    fn browser_command_runs_the_bundled_server_with_bundled_node() {
        let app = tempfile::tempdir().unwrap();
        let exe = bundled_app(app.path());
        let bin = app.path().join("Lightbulb.app/Contents/Resources/bin");
        assert_eq!(browser_command(&exe).unwrap(), vec![
            bin.join(NODE).to_string_lossy().into_owned(),
            format!("--env-file={}", bin.join(BROWSER_ENV).to_string_lossy()),
            bin.join(BROWSER_ENTRY).to_string_lossy().into_owned(),
            "--browserUrl".to_owned(),
            "http://127.0.0.1:39222".to_owned(),
            "--no-usage-statistics".to_owned(),
        ]);
        assert!(is_lightbulb_browser(&json!({"command": browser_command(&exe).unwrap()[0], "args": &browser_command(&exe).unwrap()[1..]})));
        // Dev build: resources sit in bin/ beside the executable.
        let dev = tempfile::tempdir().unwrap();
        let dev_bin = dev.path().join("bin");
        std::fs::create_dir_all(dev_bin.join(BROWSER_ENTRY).parent().unwrap()).unwrap();
        std::fs::write(dev_bin.join(NODE), b"").unwrap();
        std::fs::write(dev_bin.join(BROWSER_ENTRY), b"").unwrap();
        std::fs::write(dev_bin.join(BROWSER_ENV), b"").unwrap();
        assert_eq!(browser_command(&dev.path().join("lightbulb-desktop")).unwrap()[0], dev_bin.join(NODE).to_string_lossy());
        // No bundle (an unvendored build): no command, never a guess.
        let empty = tempfile::tempdir().unwrap();
        assert!(browser_command(&empty.path().join("Contents/MacOS/lightbulb-desktop")).is_none());
    }

    /// Fake `claude` and `codex` CLIs in `<home>/.local/bin` that keep their MCP entries the way
    /// `entry` reads them: Claude's in ~/.claude.json, Codex's (as `mcp get --json` prints them)
    /// in a side file. False when provider discovery does not look in this temp home.
    #[cfg(unix)]
    pub(crate) fn fake_clis(h: &Path) -> bool {
        use std::os::unix::fs::PermissionsExt;
        let bin = h.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::create_dir_all(h.join(".codex")).unwrap();
        for name in ["claude", "codex"] {
            let fake = bin.join(name);
            std::fs::write(&fake, r#"#!/usr/bin/env python3
import json, os, sys
claude = os.path.basename(sys.argv[0]) == "claude"
p = os.path.join(os.environ["HOME"], ".claude.json" if claude else ".codex/fake-mcp.json")
doc = json.load(open(p)) if os.path.exists(p) else {}
servers = doc.setdefault("mcpServers", {})
a = sys.argv[1:]
if a[:2] == ["mcp", "get"]:
    if a[2] not in servers: sys.exit(1)
    print(json.dumps({"transport": servers[a[2]]})); sys.exit(0)
if a[:2] == ["mcp", "remove"]: servers.pop(a[-1], None)
elif a[:2] == ["mcp", "add"]:
    i = a.index("--"); servers[a[i - 1]] = {"command": a[i + 1], "args": a[i + 2:]}
json.dump(doc, open(p, "w"))
"#).unwrap();
            std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
            if super::super::provider_program(h, name) != std::fs::canonicalize(&fake).ok() {
                return false;
            }
        }
        true
    }

    #[cfg(unix)]
    fn servers(h: &Path, provider: &str) -> Value {
        let file = if provider == "claude" { h.join(".claude.json") } else { h.join(".codex/fake-mcp.json") };
        serde_json::from_str::<Value>(&std::fs::read_to_string(file).unwrap_or_else(|_| "{}".into())).unwrap()["mcpServers"].clone()
    }

    #[cfg(unix)]
    fn set_servers(h: &Path, provider: &str, servers: Value) {
        let file = if provider == "claude" { h.join(".claude.json") } else { h.join(".codex/fake-mcp.json") };
        std::fs::write(file, json!({"mcpServers": servers}).to_string()).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn configure_removes_only_our_session_mcp_entry_and_keeps_lightbulb_browser() {
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        if !fake_clis(h) {
            return;
        }
        let app = tempfile::tempdir().unwrap();
        let exe = bundled_app(app.path());
        let ours = browser_command(&exe).unwrap();
        let browser = json!({"command": ours[0], "args": &ours[1..]});
        let other = json!({"command": "/bin/other", "args": ["serve"]});
        for provider in ["claude", "codex"] {
            for (lightbulb, kept) in [
                (json!({"command": exe.to_string_lossy(), "args": ["session-mcp"]}), false),
                // Ours at an earlier app path.
                (json!({"command": "/Old/Lightbulb.app/Contents/MacOS/lightbulb-desktop", "args": ["session-mcp"]}), false),
                // The engine's, and another program's that also says session-mcp: never ours.
                (json!({"command": "/Users/x/.lightbulb/connect/bin/lightbulb", "args": ["mcp"]}), true),
                (json!({"command": "/opt/other", "args": ["session-mcp"]}), true),
            ] {
                set_servers(h, provider, json!({"lightbulb": lightbulb, LEGACY_SERVER_NAME: lightbulb, BROWSER_SERVER_NAME: browser, "other": other}));
                configure(h, &exe, true);
                let after = servers(h, provider);
                assert_eq!(after["lightbulb"], if kept { lightbulb.clone() } else { Value::Null }, "{provider} {lightbulb}");
                assert_eq!(after[LEGACY_SERVER_NAME], if kept { lightbulb.clone() } else { Value::Null }, "{provider} legacy {lightbulb}");
                assert_eq!(after[BROWSER_SERVER_NAME], browser, "{provider}");
                assert_eq!(after["other"], other, "{provider}");
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn one_lightbulb_server_after_launch() {
        use std::os::unix::fs::PermissionsExt;
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let app_entry = json!({"command": exe.to_string_lossy(), "args": ["session-mcp"]});
        for provider in ["claude", "codex"] {
            for fixture in ["app", "engine", "both"] {
                let home = tempfile::tempdir().unwrap();
                let h = home.path();
                if !fake_clis(h) {
                    return;
                }
                let canonical = h.join(".lightbulb/connect/bin/lightbulb");
                let engine_entry = json!({"command": canonical.to_string_lossy(), "args": ["mcp"]});
                // The bundled engine: its setup takes the `lightbulb` name and drops the old one.
                let bundled = h.join("Lightbulb.app/Contents/Resources/bin/lightbulb");
                std::fs::create_dir_all(bundled.parent().unwrap()).unwrap();
                std::fs::write(&bundled, format!(
                    "#!/bin/sh\ncase \"$1\" in\n  version) echo 'lightbulb 0.6.0' ;;\n  setup)\n    for c in claude codex; do\n      \"$HOME/.local/bin/$c\" mcp remove lightbulb-network\n      \"$HOME/.local/bin/$c\" mcp remove lightbulb\n      \"$HOME/.local/bin/$c\" mcp add lightbulb -- {} mcp\n    done ;;\nesac\n",
                    canonical.display()
                )).unwrap();
                std::fs::set_permissions(&bundled, std::fs::Permissions::from_mode(0o755)).unwrap();
                let installed = |version: &str| {
                    std::fs::create_dir_all(canonical.parent().unwrap()).unwrap();
                    std::fs::write(&canonical, format!("#!/bin/sh\necho 'lightbulb {version}'\n")).unwrap();
                    std::fs::set_permissions(&canonical, std::fs::Permissions::from_mode(0o755)).unwrap();
                    std::fs::write(h.join(".lightbulb/connect/setup-version"), version).unwrap();
                };
                match fixture {
                    "app" => set_servers(h, provider, json!({"lightbulb": app_entry})),
                    "engine" => {
                        installed("0.6.0");
                        set_servers(h, provider, json!({"lightbulb": engine_entry}));
                    }
                    _ => {
                        installed("0.5.0");
                        set_servers(h, provider, json!({"lightbulb": app_entry, "lightbulb-network": engine_entry}));
                    }
                }
                // Launch: the engine first, then the app's own MCP reconcile.
                crate::engine::ensure_at(h, &bundled, std::time::Duration::from_secs(30)).unwrap();
                configure(h, exe, true);
                let after = servers(h, provider);
                assert_eq!(after["lightbulb"], engine_entry, "{provider} {fixture}: {after}");
                assert!(after.get("lightbulb-network").is_none(), "{provider} {fixture}: {after}");
                assert!(!after.to_string().contains("session-mcp"), "{provider} {fixture}: {after}");
            }
        }
    }

}
