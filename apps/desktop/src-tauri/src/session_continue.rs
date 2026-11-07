//! `lightbulb continue '<link>'` (spec 2026-10-05-public-session-links §3.6): take a copy of a
//! shared Claude Code session and resume it here, in this terminal, as the receiver's own
//! session. Their Mac, their Claude, their plan; the original session and its Mac are never
//! contacted. Everything the server sends is untrusted: it is checked with pick-up's own checks.
use crate::session_pickup::{
    self as pickup, anonymous_git, git, git_run, normalize_remote, Downloads,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub(crate) struct Link {
    pub web: String,
    pub api: String,
    pub token: String,
}

fn url_safe(s: &str) -> bool {
    (16..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// The link must be on this build's own site (`api[-dev].X` ↔ `[dev.]X`): its token and the
/// verifier are sent nowhere else.
pub(crate) fn parse_link(raw: &str, api_base: &str) -> Option<Link> {
    let url = url::Url::parse(raw.trim()).ok()?;
    let api_host = url::Url::parse(api_base).ok()?.host_str()?.to_string();
    let web_host = match api_host.strip_prefix("api-dev.") {
        Some(rest) => format!("dev.{rest}"),
        None => api_host.strip_prefix("api.")?.to_string(),
    };
    let token = url.fragment()?;
    (url.scheme() == "https"
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.host_str() == Some(web_host.as_str())
        && url.path() == "/s"
        && url_safe(token))
    .then(|| Link {
        web: format!("https://{web_host}"),
        api: format!("{}/api/v1", api_base.trim_end_matches('/')),
        token: token.to_string(),
    })
}

pub(crate) fn verifier_and_challenge() -> (String, String) {
    use base64::Engine;
    let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let verifier = b64.encode(rand::random::<[u8; 32]>());
    let challenge = b64.encode(Sha256::digest(verifier.as_bytes()));
    (verifier, challenge)
}

/// The confirm page's redirect: `GET /done?code=…`. Anything else (a favicon) gets a 404 and is
/// waited past. Blocks until it arrives; Ctrl-C ends the command.
// ponytail: no deadline; add one if people leave the command waiting.
pub(crate) fn wait_for_code(listener: &std::net::TcpListener) -> Option<String> {
    loop {
        let (mut stream, _) = listener.accept().ok()?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let mut line = String::new();
        let _ = BufReader::new(&stream).read_line(&mut line);
        let code = line
            .strip_prefix("GET /done?code=")
            .and_then(|rest| rest.split(' ').next())
            .filter(|c| url_safe(c))
            .map(str::to_string);
        let (status, body) = match code {
            Some(_) => (
                "200 OK",
                "Continuing in your terminal. You can close this tab.",
            ),
            None => ("404 Not Found", "Not found"),
        };
        let _ = write!(
            stream,
            "HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        if code.is_some() {
            return code;
        }
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CopyCode {
    mode: String,
    remote: Option<String>,
    commit: Option<String>,
    snapshot: Option<String>,
    session_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CopyTicket {
    title: Option<String>,
    from: String,
    conversation_id: String,
    claude_version: String,
    #[serde(default)]
    cwd_relative: String,
    downloads: Downloads,
    code: CopyCode,
}

/// Text from the server or the sharer's Mac, made safe to print: no control characters (C0, ESC,
/// DEL, C1), so it cannot move the cursor, rewrite the screen or write the clipboard.
pub(crate) fn printable(s: &str) -> String {
    s.chars().filter(|c| !c.is_control()).collect()
}

/// A version string as `claude --version` prints one.
fn version_ok(v: &str) -> bool {
    (1..=64).contains(&v.len())
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))
}

/// `<dns host>/<path>`, already normalized, that git may fetch as `https://<it>.git`. No IP
/// literal, no localhost, no userinfo, query, fragment or whitespace: the sharer's Mac chose it.
fn https_remote(r: &str) -> bool {
    let Some((host, path)) = r.split_once('/') else {
        return false;
    };
    let labels: Vec<&str> = host.split('.').collect();
    let host_ok = labels.len() >= 2
        && labels.iter().all(|l| {
            !l.is_empty()
                && !l.starts_with('-')
                && !l.ends_with('-')
                && l.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
        && !labels
            .last()
            .is_some_and(|tld| tld.bytes().all(|b| b.is_ascii_digit()))
        && !host.eq_ignore_ascii_case("localhost")
        && !host.to_ascii_lowercase().ends_with(".localhost");
    let path_ok = !path.is_empty()
        && path
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._~/-".contains(&b))
        && path
            .split('/')
            .all(|seg| !seg.is_empty() && seg != "." && seg != "..");
    host_ok && path_ok && normalize_remote(&format!("https://{r}")).as_deref() == Some(r)
}

pub(crate) fn parse_copy(value: Value) -> Result<CopyTicket, String> {
    let bad = || "Lightbulb sent a copy this app will not use.".to_string();
    let t: CopyTicket = serde_json::from_value(value).map_err(|_| bad())?;
    let remote_ok = t.code.remote.as_deref().is_some_and(https_remote);
    let mode_ok = match t.code.mode.as_str() {
        "none" => t.downloads.code.is_none(),
        "clone" => {
            remote_ok
                && t.code.commit.as_deref().is_some_and(pickup::hex_sha)
                && t.downloads.code.is_none()
        }
        "bundle" => {
            remote_ok
                && t.code.snapshot.as_deref().is_some_and(pickup::hex_sha)
                && t.code
                    .session_id
                    .as_deref()
                    .is_some_and(|s| uuid::Uuid::parse_str(s).is_ok())
                && t.downloads.code.is_some()
        }
        _ => false,
    };
    (uuid::Uuid::parse_str(&t.conversation_id).is_ok()
        && version_ok(&t.claude_version)
        && pickup::downloads_ok(&t.downloads)
        && mode_ok)
        .then_some(t)
        .ok_or_else(bad)
}

/// Runs an anonymous git command (see `anonymous_git`); true when it exits 0 in time.
async fn anonymous(cwd: &Path, args: &[&str], timeout: Duration) -> bool {
    tokio::time::timeout(timeout, anonymous_git(cwd, args).status())
        .await
        .is_ok_and(|status| status.is_ok_and(|s| s.success()))
}

/// A clone of `remote` to work in: the current folder's repository when its origin matches,
/// else a fresh `git clone` into `./<name>`. The bool says whether it was cloned just now.
/// `public`: clone with none of this person's credentials (the sharer's Mac said it is public,
/// and the sharer could name one of this person's private repositories).
pub(crate) async fn clone_of(
    here: &Path,
    remote: &str,
    public: bool,
) -> Result<(PathBuf, bool), String> {
    if let Ok(top) = git(here, &["rev-parse", "--show-toplevel"], None).await {
        let top = PathBuf::from(top);
        let origin = git(&top, &["remote", "get-url", "origin"], None)
            .await
            .ok()
            .and_then(|o| normalize_remote(&o));
        if origin.as_deref() == Some(remote) {
            return Ok((top, false));
        }
    }
    let name = remote
        .rsplit('/')
        .next()
        .filter(|n| {
            !n.starts_with(['.', '-'])
                && n.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        })
        .ok_or("the repository name is not usable here")?;
    let dest = here.join(name);
    if dest.exists() {
        // Reuse ./<name> when it is its own repository and its origin is this remote, so a run
        // from the parent folder still gets the code.
        let own = git(&dest, &["rev-parse", "--show-toplevel"], None).await.ok();
        let origin = git(&dest, &["remote", "get-url", "origin"], None)
            .await
            .ok()
            .and_then(|o| normalize_remote(&o));
        if origin.as_deref() == Some(remote)
            && own.is_some_and(|t| {
                std::fs::canonicalize(&t).ok() == std::fs::canonicalize(&dest).ok()
            })
        {
            return Ok((dest, false));
        }
        return Err(format!(
            "./{name} already exists and is not a clone of {remote}; move it or run this from another folder"
        ));
    }
    let url = format!("https://{remote}.git");
    eprintln!("Cloning {remote}…");
    let args = ["clone", "-q", "--", url.as_str(), name];
    let cloned = if public {
        anonymous(here, &args, Duration::from_secs(900)).await
    } else {
        git_run(here, &args, None, Duration::from_secs(900))
            .await?
            .0
    };
    if cloned {
        Ok((dest, true))
    } else {
        Err("git clone failed".into())
    }
}

/// The folder Claude starts in. A failure prints why and falls back to the conversation only.
async fn get_code(t: &CopyTicket, dir: &Path, here: &Path) -> PathBuf {
    let title = t.title.as_deref().unwrap_or("");
    let short: String = uuid::Uuid::new_v4().simple().to_string()[..6].to_string();
    let result: Result<PathBuf, String> = async {
        let remote = t.code.remote.as_deref().unwrap_or_default();
        match t.code.mode.as_str() {
            "clone" => {
                let commit = t.code.commit.as_deref().unwrap_or_default();
                let branch = format!("continue/{}-{short}", pickup::slug(title));
                let (repo, fresh) = clone_of(here, remote, true).await?;
                // Fetched from the public URL, anonymously, unless this clone already has it.
                let have = git(
                    &repo,
                    &["cat-file", "-e", &format!("{commit}^{{commit}}")],
                    None,
                )
                .await
                .is_ok();
                if !have {
                    let url = format!("https://{remote}.git");
                    let fetch = [
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "fetch",
                        "-q",
                        "--",
                        url.as_str(),
                        commit,
                    ];
                    anonymous(&repo, &fetch, Duration::from_secs(300)).await;
                }
                if fresh {
                    git(&repo, &["checkout", "-q", "-b", &branch, commit], None).await?;
                    return Ok(pickup::workdir(&repo, &t.cwd_relative));
                }
                pickup::exclude_worktrees(&repo).await?;
                let tree = repo
                    .join(".claude/worktrees")
                    .join(format!("continue-{}-{short}", pickup::slug(title)));
                git(
                    &repo,
                    &[
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        &branch,
                        &tree.to_string_lossy(),
                        commit,
                    ],
                    None,
                )
                .await?;
                Ok(pickup::workdir(&tree, &t.cwd_relative))
            }
            "bundle" => {
                let (repo, _) = clone_of(here, remote, false).await?;
                let sid = t.code.session_id.as_deref().unwrap_or_default();
                let snapshot = t.code.snapshot.as_deref().unwrap_or_default();
                let (_, tree) = pickup::import_code(
                    &repo,
                    &dir.join("code.bundle"),
                    sid,
                    &short,
                    title,
                    snapshot,
                )
                .await
                .map_err(|e| e["message"].as_str().unwrap_or("import failed").to_string())?;
                Ok(pickup::workdir(&tree, &t.cwd_relative))
            }
            _ => Ok(here.to_path_buf()),
        }
    }
    .await;
    result.unwrap_or_else(|reason| {
        eprintln!(
            "Couldn't get the code ({}). Continuing with the conversation only.",
            printable(&reason)
        );
        here.to_path_buf()
    })
}

async fn ensure_mod(claude: &Path, home: &Path) {
    use crate::local_sessions::plugin;
    let state = home
        .join("Library/Application Support")
        .join(crate::KEYCHAIN_SERVICE)
        .join("local-sessions");
    // Before the list: a plugin that is listed can still hold a file an older app wrote, and
    // Claude then refuses it. The app does this same sync at launch, but may not have run yet.
    let root = plugin::write_files(&state);
    let listed = tokio::time::timeout(
        Duration::from_secs(15),
        tokio::process::Command::new(claude)
            .args(["plugin", "list"])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await;
    if listed
        .is_ok_and(|o| o.is_ok_and(|o| String::from_utf8_lossy(&o.stdout).contains(plugin::ID)))
    {
        // Listed is not current: Claude may still hold a cached copy from an older app.
        if let Ok(root) = root {
            let claude = claude.to_owned();
            let _ = tokio::task::spawn_blocking(move || plugin::refresh(&claude, &root)).await;
        }
        return;
    }
    if let Err(e) = plugin::install_now(&state, home) {
        eprintln!("lightbulb: the Lightbulb mod was not installed ({e}).");
    }
}

/// Read by the mod's band (Task 8). Names come from the server: no control characters.
pub(crate) fn write_marker(
    home: &Path,
    session: &str,
    from: &str,
    title: Option<&str>,
) -> std::io::Result<()> {
    let clean = |s: &str| {
        s.chars()
            .filter(|c| !c.is_control())
            .take(80)
            .collect::<String>()
    };
    let dir = home.join(".lightbulb/continued");
    std::fs::create_dir_all(&dir)?;
    std::fs::write(
        dir.join(format!("{session}.json")),
        json!({"from": clean(from), "title": title.map(clean)}).to_string(),
    )
}

/// The server's refusal, by its problem code. Its message names the reason (the owner turned the
/// link off, paused the session, or their Mac stopped sharing); the fallback covers a body
/// without one.
fn refusal(body: &Value) -> String {
    let fallback = match body["code"].as_str() {
        // The verifier did not match: another local program answered the confirm page first.
        // PKCE makes that a refusal, nothing worse.
        Some("LOCAL_SESSION_FORBIDDEN") => {
            return "Another program answered first. Run the command again.".into()
        }
        Some("LOCAL_SESSION_NOT_FOUND") => "The owner turned this link off.",
        Some("LOCAL_SESSION_CONFLICT") => "The owner stopped sharing this session.",
        Some("LOCAL_SESSION_OFFLINE") => "The owner's Mac stopped sharing sessions.",
        _ => "Lightbulb refused the copy.",
    };
    body["message"].as_str().unwrap_or(fallback).to_string()
}

async fn redeem(
    client: &reqwest::Client,
    api: &str,
    code: &str,
    verifier: &str,
) -> Result<Value, String> {
    for _ in 0..450 {
        let res = client
            .post(format!("{api}/public/session-copies/redeem"))
            .json(&json!({"code": code, "verifier": verifier}))
            .send()
            .await
            .map_err(|_| "Lightbulb could not be reached.".to_string())?;
        let ok = res.status().is_success();
        let body: Value = res.json().await.unwrap_or(Value::Null);
        if !ok {
            return Err(refusal(&body));
        }
        if body["state"] == "ready" {
            return Ok(body);
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    Err("The copy took too long to prepare. Press Continue again.".into())
}

async fn run(raw: &str) -> Result<std::convert::Infallible, String> {
    let link = parse_link(raw, crate::LIGHTBULB_API_BASE)
        .ok_or("That is not a Lightbulb session link for this app.")?;
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("HOME is not set.")?;
    let claude = crate::local_sessions::provider_program(&home, "claude")
        .ok_or("Claude Code is not installed on this Mac.")?;
    let here = std::env::current_dir().map_err(|_| "This folder no longer exists.")?;
    let (verifier, challenge) = verifier_and_challenge();
    let listener =
        std::net::TcpListener::bind("127.0.0.1:0").map_err(|_| "Could not listen on this Mac.")?;
    let port = listener
        .local_addr()
        .map_err(|_| "Could not listen on this Mac.")?
        .port();
    let confirm = format!(
        "{}/s?port={port}&challenge={challenge}#{}",
        link.web, link.token
    );
    eprintln!("Confirm in your browser. If it did not open, visit:\n  {confirm}");
    let _ = std::process::Command::new("/usr/bin/open")
        .arg(&confirm)
        .status();
    let code = tokio::task::spawn_blocking(move || wait_for_code(&listener))
        .await
        .ok()
        .flatten()
        .ok_or("No confirmation arrived.")?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|_| "Could not start a request.")?;
    eprintln!("Preparing the copy…");
    let ticket = parse_copy(redeem(&client, &link.api, &code, &verifier).await?)?;
    let installed = pickup::claude_version(&claude).await.unwrap_or_default();
    if pickup::older(&installed, &ticket.claude_version) {
        return Err(format!(
            "Update Claude Code to continue this session (it needs {}).",
            ticket.claude_version
        ));
    }
    let root = home.join(".lightbulb/continue");
    std::fs::create_dir_all(&root).map_err(|_| "~/.lightbulb is not writable.")?;
    pickup::prune(&root);
    let dir = root.join(uuid::Uuid::new_v4().simple().to_string());
    std::fs::create_dir_all(&dir).map_err(|_| "~/.lightbulb is not writable.")?;
    let transcript = pickup::fetch_downloads(&ticket.downloads, &ticket.conversation_id, &dir)
        .await
        .map_err(|e| {
            e["message"]
                .as_str()
                .unwrap_or("The download failed.")
                .to_string()
        })?;
    let cwd = get_code(&ticket, &dir, &here).await;
    ensure_mod(&claude, &home).await;
    let session = uuid::Uuid::new_v4().to_string();
    let _ = write_marker(&home, &session, &ticket.from, ticket.title.as_deref());
    let argv = pickup::argv(&claude, &transcript, &session);
    let err = std::process::Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(cwd)
        .exec();
    Err(format!("could not run claude: {err}"))
}

/// `lightbulb continue '<link>'`: the process exit code (on success this process becomes Claude).
pub fn main(link: &str) -> i32 {
    let Ok(rt) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return 1;
    };
    match rt.block_on(run(link)) {
        Ok(never) => match never {},
        Err(message) => {
            eprintln!("lightbulb: {}", printable(&message));
            1
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_refusal_names_its_reason() {
        let say = |code: &str, message: Option<&str>| {
            super::refusal(&serde_json::json!({"code": code, "message": message}))
        };
        assert!(say("LOCAL_SESSION_FORBIDDEN", Some("x")).starts_with("Another program"));
        assert_eq!(
            say(
                "LOCAL_SESSION_CONFLICT",
                Some("The owner stopped sharing this session.")
            ),
            "The owner stopped sharing this session."
        );
        assert_eq!(
            say("LOCAL_SESSION_OFFLINE", None),
            "The owner's Mac stopped sharing sessions."
        );
        assert_eq!(
            say("LOCAL_SESSION_NOT_FOUND", None),
            "The owner turned this link off."
        );
        assert_eq!(say("RATE_LIMITED", None), "Lightbulb refused the copy.");
    }

    use super::*;
    use std::io::Read;

    const API: &str = "https://api-dev.thelightbulb.company";
    const TOKEN: &str = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";

    #[test]
    fn only_this_builds_site_and_a_well_formed_token() {
        let link = parse_link(&format!("https://dev.thelightbulb.company/s#{TOKEN}"), API).unwrap();
        assert_eq!(link.web, "https://dev.thelightbulb.company");
        assert_eq!(link.api, "https://api-dev.thelightbulb.company/api/v1");
        assert_eq!(link.token, TOKEN);
        assert_eq!(
            parse_link(
                &format!("https://thelightbulb.company/s#{TOKEN}"),
                "https://api.thelightbulb.company"
            )
            .unwrap()
            .web,
            "https://thelightbulb.company"
        );
        for bad in [
            format!("https://thelightbulb.company/s#{TOKEN}"), // the other environment
            format!("https://evil.example/s#{TOKEN}"),
            format!("http://dev.thelightbulb.company/s#{TOKEN}"),
            format!("https://dev.thelightbulb.company/x#{TOKEN}"),
            "https://dev.thelightbulb.company/s#short".to_string(),
            format!("https://dev.thelightbulb.company/s#{TOKEN}%20"),
        ] {
            assert!(parse_link(&bad, API).is_none(), "{bad}");
        }
    }

    #[test]
    fn the_challenge_is_the_verifiers_sha256() {
        use base64::Engine;
        let (verifier, challenge) = verifier_and_challenge();
        assert_eq!(verifier.len(), 43);
        let want = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(Sha256::digest(verifier.as_bytes()));
        assert_eq!(challenge, want);
    }

    #[test]
    fn the_loopback_takes_only_done_with_a_code() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let client = std::thread::spawn(move || {
            for path in ["/favicon.ico", &format!("/done?code={TOKEN}")] {
                let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
                write!(s, "GET {path} HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
                let mut answer = String::new();
                let _ = s.read_to_string(&mut answer);
            }
        });
        assert_eq!(wait_for_code(&listener).as_deref(), Some(TOKEN));
        client.join().unwrap();
    }

    fn ticket(code: Value, bundle: Value) -> Value {
        let url =
            "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/session-copies/c/t-000000.gz";
        json!({"state": "ready", "title": "Fix login", "from": "Alice", "conversationId": "11111111-2222-4333-8444-555555555555",
            "claudeVersion": "2.1.283", "cwdRelative": "", "code": code,
            "downloads": {"transcript": [{"index": 0, "url": url, "bytes": 3, "sha256": "a".repeat(64)}], "code": bundle, "sidecars": null}})
    }

    #[test]
    fn a_copy_ticket_is_checked_before_any_download() {
        assert!(parse_copy(ticket(json!({"mode": "none"}), Value::Null)).is_ok());
        assert!(parse_copy(ticket(
            json!({"mode": "clone", "remote": "github.com/o/r", "commit": "a".repeat(40)}),
            Value::Null
        ))
        .is_ok());
        assert!(parse_copy(ticket(
            json!({"mode": "clone", "remote": "github.com/o/r", "commit": "main"}),
            Value::Null
        ))
        .is_err());
        assert!(parse_copy(ticket(
            json!({"mode": "none"}),
            json!({"url": "https://evil.example/x", "bytes": 1, "sha256": "a".repeat(64)})
        ))
        .is_err());
        assert!(parse_copy(ticket(
            json!({"mode": "bundle", "remote": "github.com/o/r", "snapshot": "b".repeat(40),
            "sessionId": "11111111-2222-4333-8444-555555555555"}),
            Value::Null
        ))
        .is_err()); // bundle mode needs the bundle
        let mut off_bucket = ticket(json!({"mode": "none"}), Value::Null);
        off_bucket["downloads"]["transcript"][0]["url"] =
            json!("https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/other/x");
        assert!(parse_copy(off_bucket).is_err());
    }

    #[tokio::test]
    async fn the_current_folder_is_used_when_it_is_a_clone_of_the_remote() {
        let dir = tempfile::tempdir().unwrap();
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(dir.path())
                .status()
                .unwrap()
                .success())
        };
        g(&["init", "-q"]);
        g(&["remote", "add", "origin", "git@github.com:o/r.git"]);
        let here = std::fs::canonicalize(dir.path()).unwrap();
        assert_eq!(
            clone_of(&here, "github.com/o/r", true).await.unwrap().0,
            here
        );
    }

    #[tokio::test]
    async fn a_matching_clone_in_the_current_folder_is_reused() {
        let dir = tempfile::tempdir().unwrap();
        let here = std::fs::canonicalize(dir.path()).unwrap();
        let sub = here.join("r");
        std::fs::create_dir(&sub).unwrap();
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(&sub)
                .status()
                .unwrap()
                .success())
        };
        g(&["init", "-q"]);
        g(&["remote", "add", "origin", "https://github.com/o/r.git"]);
        let (path, fresh) = clone_of(&here, "github.com/o/r", true).await.unwrap();
        assert_eq!((path, fresh), (sub.clone(), false));
        std::fs::create_dir(here.join("x")).unwrap();
        let err = clone_of(&here, "github.com/o/x", true).await.unwrap_err();
        assert!(err.contains("not a clone"), "{err}");
    }

    #[tokio::test]
    async fn clone_mode_in_a_clone_adds_a_worktree_at_the_commit() {
        let dir = tempfile::tempdir().unwrap();
        let here = std::fs::canonicalize(dir.path()).unwrap();
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(&here)
                .status()
                .unwrap()
                .success())
        };
        g(&["init", "-q"]);
        g(&[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "one",
        ]);
        g(&["remote", "add", "origin", "git@github.com:o/r.git"]);
        let head = String::from_utf8(
            std::process::Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&here)
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap();
        // The commit is already here, so nothing is fetched: no network in tests.
        let t = parse_copy(ticket(
            json!({"mode": "clone", "remote": "github.com/o/r", "commit": head.trim()}),
            Value::Null,
        ))
        .unwrap();
        let cwd = get_code(&t, &here, &here).await;
        assert!(cwd.starts_with(here.join(".claude/worktrees")), "{cwd:?}");
        assert!(cwd.join(".git").exists());
        let exclude = std::fs::read_to_string(here.join(".git/info/exclude")).unwrap();
        assert!(exclude.contains(".claude/worktrees/"));
    }

    #[test]
    fn a_copy_names_only_a_public_https_remote_and_a_plain_version() {
        let clone = |remote: &str| {
            ticket(
                json!({"mode": "clone", "remote": remote, "commit": "a".repeat(40)}),
                Value::Null,
            )
        };
        assert!(parse_copy(clone("gitlab.example.com/group/sub/r")).is_ok());
        for bad in [
            "127.0.0.1/o/r",
            "localhost/o/r",
            "a.localhost/o/r",
            "u@github.com/o/r",
            "github.com/o/r?x=1",
            "github.com/o/r#x",
            "github.com/o r",
            "github.com/o/r\n",
            "github.com",
            "github.com/../r",
            "-x.com/o/r",
        ] {
            assert!(parse_copy(clone(bad)).is_err(), "{bad}");
        }
        let mut odd = ticket(json!({"mode": "none"}), Value::Null);
        odd["claudeVersion"] = json!("2.1\u{1b}]52;c;eA==\u{7}");
        assert!(parse_copy(odd).is_err());
        assert_eq!(printable("a\u{1b}[31mb\u{9b}c\u{7f}"), "a[31mbc");
    }

    #[test]
    fn a_link_with_userinfo_or_a_query_is_refused() {
        for bad in [
            format!("https://u@dev.thelightbulb.company/s#{TOKEN}"),
            format!("https://dev.thelightbulb.company/s?x=1#{TOKEN}"),
        ] {
            assert!(parse_link(&bad, API).is_none(), "{bad}");
        }
    }

    #[test]
    fn the_loopback_waits_past_a_malformed_code() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let client = std::thread::spawn(move || {
            for path in [
                "/done?code=bad!code%20here0000".to_string(),
                format!("/done?code={TOKEN}"),
            ] {
                let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
                write!(s, "GET {path} HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
                let mut answer = String::new();
                let _ = s.read_to_string(&mut answer);
            }
        });
        assert_eq!(wait_for_code(&listener).as_deref(), Some(TOKEN));
        client.join().unwrap();
    }

    #[test]
    fn the_marker_names_the_fork() {
        let home = tempfile::tempdir().unwrap();
        write_marker(home.path(), "fork-1", "Alice\u{1b}[31m", Some("Fix login")).unwrap();
        let text =
            std::fs::read_to_string(home.path().join(".lightbulb/continued/fork-1.json")).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&text).unwrap(),
            json!({"from": "Alice[31m", "title": "Fix login"})
        );
    }

    #[tokio::test]
    async fn continue_syncs_the_mod_files_when_the_plugin_is_already_listed() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let claude = home.path().join("claude");
        std::fs::write(&claude, "#!/bin/sh\necho lightbulb@lightbulb\n").unwrap();
        std::fs::set_permissions(&claude, std::fs::Permissions::from_mode(0o755)).unwrap();
        let plugin = home
            .path()
            .join("Library/Application Support")
            .join(crate::KEYCHAIN_SERVICE)
            .join("local-sessions/claude-mods/lightbulb");
        let stale = plugin.join("hooks/hooks.json");
        std::fs::create_dir_all(stale.parent().unwrap()).unwrap();
        std::fs::write(&stale, "an older app's hooks module").unwrap();
        ensure_mod(&claude, home.path()).await;
        assert!(!stale.exists());
        assert!(plugin.join("mod/hooks.json").is_file());
    }

    #[tokio::test]
    async fn continue_refreshes_claudes_copy_when_the_listed_plugin_is_an_older_version() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let plugin = home
            .path()
            .join("Library/Application Support")
            .join(crate::KEYCHAIN_SERVICE)
            .join("local-sessions/claude-mods/lightbulb");
        let claude = home.path().join("claude");
        std::fs::write(
            &claude,
            "#!/bin/sh\necho \"$*\" >> \"$0.log\"\ncase \"$*\" in\n\"plugin list --json\") cat \"$0.list\";;\n\"plugin list\") echo lightbulb@lightbulb;;\nesac\n",
        )
        .unwrap();
        std::fs::set_permissions(&claude, std::fs::Permissions::from_mode(0o755)).unwrap();
        let listed = json!([{"id": "lightbulb@lightbulb", "version": "unknown", "readFromFolder": plugin}]);
        std::fs::write(home.path().join("claude.list"), listed.to_string()).unwrap();
        ensure_mod(&claude, home.path()).await;
        let log = std::fs::read_to_string(home.path().join("claude.log")).unwrap();
        assert_eq!(log, "plugin list\nplugin list --json\nplugin update lightbulb@lightbulb\n");
    }
}
