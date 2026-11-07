//! A phone/web start becomes a session only when that provider's own start hook
//! names its native conversation. The hook writes this before waking Desktop,
//! so a closed relay socket cannot lose the identity.

use super::*;

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Link {
    start_id: String,
    provider: String,
    conversation: String,
    launcher: Process,
    bound_generation: Option<String>,
}

#[derive(Serialize, Deserialize, PartialEq, Eq)]
struct Claim {
    provider: String,
    conversation: String,
}

fn path(scope: &Path, provider: &str, conversation: &str) -> PathBuf {
    scope.join(format!(
        "start-{}.json",
        fingerprint(&json!([provider, conversation]).to_string())
    ))
}

fn valid_start_id(id: &str) -> bool {
    uuid::Uuid::parse_str(id).is_ok_and(|parsed| parsed.to_string() == id)
}

fn scope_for_hook(home: &Path, key: &str) -> Result<PathBuf> {
    if key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid launch scope".into());
    }
    let base = home
        .join("Library/Application Support")
        .join(crate::KEYCHAIN_SERVICE)
        .join("local-sessions");
    let enrollment: Enrollment = private_read(&base.join("enrollment.json"))?;
    if !enrollment.enabled
        || enrollment.paused
        || enrollment.remote_disabled
        || enrollment::scope_key(&enrollment) != key
    {
        return Err("launch scope changed".into());
    }
    let scope = base.join(key);
    private_dir(&scope)?;
    Ok(scope)
}

/// Hook process only. Missing env means this session started in a native app,
/// with no Lightbulb start operation to claim.
pub(super) fn record_from_hook(provider: &str, hook: &Value) -> Result<()> {
    if hook["hook_event_name"] != "SessionStart" || !matches!(provider, "claude" | "codex") {
        return Ok(());
    }
    let (Ok(start_id), Ok(scope), Ok(pid)) = (
        std::env::var("LIGHTBULB_SESSION_START_ID"),
        std::env::var("LIGHTBULB_SESSION_SCOPE"),
        std::env::var("LIGHTBULB_SESSION_LAUNCH_PID"),
    ) else {
        return Ok(());
    };
    if !valid_start_id(&start_id) || pid.parse::<u32>().is_err() {
        return Err("invalid launch identity".into());
    }
    let conversation = hook["session_id"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or("missing native conversation")?;
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("local profile unavailable")?;
    let launcher = process(pid.parse().map_err(|_| "invalid launch pid")?)?;
    record(&home, &start_id, &scope, provider, conversation, launcher)
}

/// A start's link to its native conversation, under the launch `scope` key. `launcher` is the
/// process that runs the conversation: the provider the holder started, or, for a held Codex on
/// the shared daemon, the daemon (`codex::start_on_daemon`).
pub(super) fn record(home: &Path, start_id: &str, scope: &str, provider: &str, conversation: &str, launcher: Process) -> Result<()> {
    if !valid_start_id(start_id) {
        return Err("invalid launch identity".into());
    }
    let scope = scope_for_hook(home, scope)?;
    let link = Link {
        start_id: start_id.into(),
        provider: provider.into(),
        conversation: conversation.into(),
        launcher,
        bound_generation: None,
    };
    // Descendant sessions can inherit the launch environment. Only the first
    // provider conversation may claim this operation; later children fail closed.
    let claim = Claim {
        provider: provider.into(),
        conversation: conversation.into(),
    };
    let claim_path = scope.join(format!("claim-{}.json", link.start_id));
    create_once(&claim_path, &claim)?;
    if private_read::<Claim>(&claim_path)? != claim {
        return Err("launch already belongs to another conversation".into());
    }
    create_once(&path(&scope, provider, conversation), &link)
}

fn create_once(path: &Path, value: &impl Serialize) -> Result<()> {
    let dir = open_directory(path.parent().ok_or("missing link directory")?)?;
    let name = std::ffi::CString::new(path.file_name().ok_or("missing link filename")?.as_bytes())
        .map_err(|_| "invalid link filename")?;
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            0o600,
        )
    };
    if fd < 0 {
        return if path.try_exists().unwrap_or(false) {
            Ok(())
        } else {
            Err("cannot create launch link".into())
        };
    }
    let mut file = unsafe { fs::File::from_raw_fd(fd) };
    file.write_all(&serde_json::to_vec(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())?;
    dir.sync_all().map_err(|e| e.to_string())
}

pub(super) fn bind(scope: &Path, native: &NativeSession, generation: &str) -> Result<bool> {
    if native.restriction.is_some() {
        return Ok(false);
    }
    let file = path(scope, &native.provider, &native.conversation);
    if !file.try_exists().map_err(|e| e.to_string())? {
        return Ok(false);
    }
    let Ok(mut link) = private_read::<Link>(&file) else {
        return Ok(false);
    };
    if !valid_start_id(&link.start_id)
        || link.provider != native.provider
        || link.conversation != native.conversation
    {
        return Ok(false);
    }
    if private_read::<Claim>(&scope.join(format!("claim-{}.json", link.start_id))).ok()
        != Some(Claim {
            provider: link.provider.clone(),
            conversation: link.conversation.clone(),
        })
    {
        return Ok(false);
    }
    if link.bound_generation.is_some() {
        return Ok(false);
    }
    if !process(link.launcher.pid).is_ok_and(|running| running == link.launcher)
        || native.process != link.launcher
    {
        return Ok(false);
    }
    link.bound_generation = Some(generation.into());
    private_write(&file, &link)?;
    Ok(true)
}

/// The start a register may claim: bound to this generation, on a runtime verified now.
pub(super) fn linked(scope: &Path, native: &NativeSession, generation: &str) -> Option<String> {
    if native.restriction.is_some() {
        return None;
    }
    bound(scope, native, generation)
}

/// The start this generation was bound to, whatever the runtime's state now. The binding was
/// made while the runtime was verified; an end reports it, and an ended row grants no control.
pub(super) fn bound(scope: &Path, native: &NativeSession, generation: &str) -> Option<String> {
    let link: Link = private_read(&path(scope, &native.provider, &native.conversation)).ok()?;
    (valid_start_id(&link.start_id)
        && link.provider == native.provider
        && link.conversation == native.conversation
        && link.bound_generation.as_deref() == Some(generation))
    .then_some(link.start_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn start_link_is_exact_native_conversation_and_one_generation() {
        let temp = tempfile::tempdir().unwrap();
        fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let launcher = process(std::process::id()).unwrap();
        let native = NativeSession {
            provider: "claude".into(),
            conversation: "native-1".into(),
            profile: "profile".into(),
            process: launcher.clone(),
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "2.1.269".into(),
            restriction: None,
            status: "idle".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            direct_holder: false,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: true,
            attention: Default::default(),
        };
        let record = Link {
            start_id: "11111111-1111-4111-8111-111111111111".into(),
            provider: "claude".into(),
            conversation: native.conversation.clone(),
            launcher,
            bound_generation: None,
        };
        create_once(&path(temp.path(), "claude", "native-1"), &record).unwrap();
        create_once(
            &temp.path().join(format!("claim-{}.json", record.start_id)),
            &Claim {
                provider: "claude".into(),
                conversation: "native-1".into(),
            },
        )
        .unwrap();
        assert!(linked(temp.path(), &native, "generation-1").is_none());
        assert!(bind(temp.path(), &native, "generation-1").unwrap());
        assert_eq!(
            linked(temp.path(), &native, "generation-1"),
            Some(record.start_id)
        );
        assert!(!bind(temp.path(), &native, "generation-2").unwrap());
        assert!(linked(temp.path(), &native, "generation-2").is_none());
        let mut restricted = native.clone();
        restricted.restriction = Some("runtime-unverified".into());
        assert!(linked(temp.path(), &restricted, "generation-1").is_none());
        assert_eq!(bound(temp.path(), &restricted, "generation-1").as_deref(), Some("11111111-1111-4111-8111-111111111111"));
        assert!(bound(temp.path(), &restricted, "generation-2").is_none());
        let mut other = native;
        other.conversation = "native-2".into();
        create_once(
            &path(temp.path(), "claude", "native-2"),
            &Link {
                start_id: "11111111-1111-4111-8111-111111111111".into(),
                provider: "claude".into(),
                conversation: "native-2".into(),
                launcher: process(std::process::id()).unwrap(),
                bound_generation: None,
            },
        )
        .unwrap();
        assert!(!bind(temp.path(), &other, "generation-1").unwrap());
        assert!(linked(temp.path(), &other, "generation-1").is_none());
    }
}
