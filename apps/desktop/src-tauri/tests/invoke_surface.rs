//! The page's whole reach into this Mac is the `generate_handler!` list in `lib.rs`. A page is a
//! remote origin, so no engine operation (approve, admit, policy, invite) is a command at all, and
//! every command that widens what agents may do asks a native "Allow" before it writes. The one
//! door to the engine is `engine_ask`, read-only: `engine_client`'s tests pin its allowlist and what it cuts.
//! `network_connect` and `network_leave` change a network only after a native alert, and take nothing from the page.

const LIB: &str = include_str!("../src/lib.rs");

const ENGINE_DOOR: &str = "engine_client::engine_ask";

/// Every command, with the call that writes the change when it widens access (None: it does
/// not widen access, and why).
const COMMANDS: &[(&str, Option<&str>, &str)] = &[
    ("local_sessions::enrollment::local_sessions_auth", None, "reads the sharing sign-in"),
    ("local_sessions::enrollment::local_sessions_status", None, "read"),
    ("local_sessions::enrollment::local_session_audience", None, "read"),
    // Owner ruling 2026-10-26 (Q4): turning the service on asks nothing; each session is shared by its person.
    ("local_sessions::enrollment::local_sessions_set", None, "the sharing service; no session is shared by it"),
    ("local_sessions::enrollment::local_sessions_repair", None, "re-applies what consent already set"),
    ("local_sessions::enrollment::local_sessions_revoke", None, "narrows"),
    ("send_auth_token", None, "the page's own sign-in"),
    ("desktop_unpair", None, "narrows"),
    ("is_desktop", None, "read"),
    ("desktop_device_id", None, "read"),
    ("get_node_status", None, "read"),
    ("updater_identity", None, "read"),
    ("apply_update_manifest", None, "a signed update; the signature is the check"),
    ("file_access_list_paths", None, "read"),
    ("file_access_add_path", Some("file_access::add_path("), "a folder agents may use"),
    ("file_access_remove_path", None, "narrows"),
    ("file_access_default_path", None, "read"),
    ("local_mcp_list", None, "read"),
    ("local_mcp_set", Some("local_mcp::set_servers("), "a command run at every start"),
    ("local_mcp_remove", None, "narrows"),
    ("claude_export::export_claude_context", None, "reads, into a folder already approved"),
    ("session_pickup::session_pickup", None, "a session its person picks up"),
    ("notify::native_notify", None, "a banner"),
    ("notify::notification_permission", None, "macOS asks itself"),
    ("desktop_permissions::desktop_permissions_get", None, "read"),
    ("desktop_permissions::desktop_permissions_set", Some("save_to("), "what agents may do on this Mac"),
    ("agent_chrome::agent_chrome_status", None, "read"),
    ("agent_chrome::agent_chrome_raise", None, "shows a window"),
    ("session_start::terminal_options", None, "read"),
    ("session_start::terminal_start", None, "only in an approved folder"),
    ("shell_integration::shell_integration_status", None, "read"),
    // ponytail: Owner ruling 2026-10-26 (Q4) wants one confirm_native when "Share every new session"
    // goes on; shell_integration.rs belongs to an open PR (#4384), so the ask lands after it.
    ("shell_integration::shell_integration_set", None, "Q4 ask follows #4384"),
    ("holder::panel::terminal_panel", None, "shows a held terminal"),
    // Exempt by name from the guard below because it is read-only: directory, sessions, team.get. Joining is native code.
    (ENGINE_DOOR, None, "read-only, filtered"),
    ("network_connect::network_status", None, "read"),
    ("network_connect::network_connect", Some("consent_to_connect("), "starts a join with no ticket from the page"),
    ("network_connect::network_leave", Some("leave_team("), "leaves the network"),
    ("network_connect::network_notice_seen", None, "clears a notice; the OS banner stays the tell"),
    ("local_sessions::enrollment::share_engine_session", Some("mod_socket::share("), "publishes a session; the row is read from the engine, not the page"),
    ("engine::engine_fix", None, "re-applies the bundled engine; takes nothing"),
];

/// Named by the sidebar page; each is classified in `COMMANDS` and takes no engine operation from the page.
const NAMED_FOR_THE_SIDEBAR: &[&str] = &["local_sessions::enrollment::share_engine_session", "engine::engine_fix"];

fn handler_list() -> Vec<String> {
    let start = LIB.find("tauri::generate_handler![").expect("generate_handler!") + "tauri::generate_handler![".len();
    let end = start + LIB[start..].find(']').unwrap();
    LIB[start..end].split(',').map(str::trim).filter(|c| !c.is_empty()).map(str::to_owned).collect()
}

/// The source of `fn <name>(` up to the next line that is a lone `}`.
fn body(command: &str) -> String {
    let (module, name) = command.rsplit_once("::").unwrap_or(("", command));
    let file = if module.is_empty() { "lib".to_owned() } else { module.replace("::", "/") };
    let path = format!("{}/src/{file}.rs", env!("CARGO_MANIFEST_DIR"));
    let source = std::fs::read_to_string(&path).unwrap_or_else(|_| panic!("{path}"));
    let at = source.find(&format!("fn {name}(")).unwrap_or_else(|| panic!("fn {name} in {path}"));
    let rest = &source[at..];
    rest[..rest.find("\n}\n").unwrap()].to_owned()
}

#[test]
fn no_engine_op_is_a_tauri_command() {
    for command in handler_list().into_iter().filter(|c| c != ENGINE_DOOR && !NAMED_FOR_THE_SIDEBAR.contains(&c.as_str()) && !c.starts_with("network_connect::")) {
        for op in ["approve", "admit", "policy", "invite", "engine", "network", "allow"] {
            assert!(!command.to_lowercase().contains(op), "{command} looks like an engine operation; the engine raises its own dialogs");
        }
    }
}

#[test]
fn network_commands_take_nothing_from_the_page() {
    let commands = handler_list();
    let network: Vec<&String> = commands.iter().filter(|c| c.contains("network")).collect();
    assert_eq!(network.len(), 4, "{network:?}");
    for command in network {
        let body = body(command);
        let arguments = &body[body.find('(').unwrap() + 1..body.find(')').unwrap()];
        assert_eq!(arguments, "app: tauri::AppHandle", "{command} takes only the app handle");
    }
}

#[test]
fn every_widening_command_calls_confirm_native() {
    let listed: Vec<String> = handler_list();
    let table: Vec<String> = COMMANDS.iter().map(|(c, ..)| (*c).to_owned()).collect();
    assert_eq!(listed, table, "every command is classified here, in generate_handler! order");
    for (command, write, _) in COMMANDS {
        let Some(write) = write else { continue };
        let body = body(command);
        let asks = body.find("confirm_native(").unwrap_or_else(|| panic!("{command} widens access without confirm_native"));
        let writes = body.find(write).unwrap_or_else(|| panic!("{command}: {write} not found"));
        assert!(asks < writes, "{command} must ask before it writes");
    }
}
