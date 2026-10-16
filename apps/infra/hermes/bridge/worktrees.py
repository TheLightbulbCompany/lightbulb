"""One git worktree per Paperclip issue, on the workspace's one microVM.

Paperclip's own `git_worktree` execution workspaces run `git worktree add` on the PAPERCLIP host
(fork `workspace-runtime.ts`), and the hermes_gateway adapter sends us only input, instructions and
the session key, so the bridge makes them here: for every clone at `<profile>/.run/<repo>`, a worktree
at `<profile>/.scratch/wt/task-<issueId>/<repo>` on branch `task/<issueId[:8]>`. The clone's objects are
shared, and pnpm/uv already share one store per profile `$HOME` (hardlinks, same filesystem), so a
worktree costs only its checkout. The path and branch are fixed per issue: a run that died leaves its
worktree and branch for the issue's next run, which continues there.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from bridge import sync

log = logging.getLogger("bridge.worktrees")

#: Refuse a NEW task worktree under this much free disk (min of the inner and outer filesystems; the
#: outer never gets freed space back, agentcore-session-disk memo). An existing one is always reused.
MIN_FREE_MB = 1500
CLOSED = ("done", "cancelled")
_LOCK = threading.Lock()  # ponytail: one lock for every agent's git worktree ops; per-repo if it ever queues


def _root(agent_id: str) -> Path:
    return sync._profile_dir(agent_id) / sync.SCRATCH_DIR / "wt"


def branch(issue_id: str) -> str:
    return f"task/{issue_id[:8]}"


def _clones(agent_id: str) -> list[Path]:
    run = sync._profile_dir(agent_id) / ".run"
    return sorted(p for p in run.iterdir() if (p / ".git").exists()) if run.is_dir() else []


def _git(agent_id: str, repo: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    """git as the agent: its profile $HOME and gh config, so a partial clone can fetch its blobs."""
    env = {**os.environ, "HOME": str(sync._profile_dir(agent_id) / sync.PROFILE_HOME), **sync.cli_credential_env(agent_id)}
    return subprocess.run(["git", "-C", str(repo), "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args],
                          env=env, capture_output=True, text=True, timeout=timeout)


def prepare(agent_id: str, issue_id: str) -> tuple[list[tuple[Path, str]], str | None]:
    """This issue's worktrees as (path, branch), creating the missing ones; or a refusal for the task
    when one is missing and the disk is short. No clone at `.run/` -> nothing to do."""
    clones = _clones(agent_id)
    task = _root(agent_id) / f"task-{issue_id}"
    name = branch(issue_id)
    with _LOCK:
        missing = [c for c in clones if not (task / c.name).exists()]
        free = min(sync.disk_free_mb()) if missing else None
        if free is not None and free < MIN_FREE_MB:
            return [], (f"Not started: the workspace disk has {free} MB free and a new task worktree needs "
                        f"{MIN_FREE_MB} MB. It starts once another task finishes or the workspace restarts.")
        for clone in missing:
            _git(agent_id, clone, "worktree", "prune")
            dest = str(task / clone.name)
            has_branch = _git(agent_id, clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0
            made = _git(agent_id, clone, "worktree", "add", dest, name) if has_branch else (
                _git(agent_id, clone, "worktree", "add", "-b", name, dest, "origin/HEAD"))
            if made.returncode != 0 and not has_branch:  # a clone without origin/HEAD
                made = _git(agent_id, clone, "worktree", "add", "-b", name, dest)
            if made.returncode != 0:
                log.warning("worktree add failed agent=%s issue=%s repo=%s: %s", agent_id, issue_id, clone.name,
                            made.stderr.strip()[-300:])
            else:
                log.info("worktree ready agent=%s issue=%s repo=%s branch=%s", agent_id, issue_id, clone.name, name)
    return [(task / c.name, name) for c in clones if (task / c.name).exists()], None


def issue_status(agent_id: str, issue_id: str) -> str | None:
    """The issue's Paperclip status with the agent's own key; "gone" on a 404, None when unknown."""
    env = {}
    try:
        from agent.secret_scope import build_profile_secret_scope
        env = build_profile_secret_scope(sync._profile_dir(agent_id))
    except ImportError:  # tests outside the Hermes image
        pass
    url, key = env.get("PAPERCLIP_API_URL"), env.get("PAPERCLIP_API_KEY")
    if not url or not key:
        return None
    request = urllib.request.Request(f"{url.rstrip('/')}/api/issues/{issue_id}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response).get("status")
    except urllib.error.HTTPError as exc:
        return "gone" if exc.code == 404 else None
    except (OSError, ValueError):
        return None


def reap(agent_id: str, live_issues, status=issue_status) -> list[str]:
    """Remove the task worktrees whose issue is done, cancelled or deleted, or whose branch was merged
    and deleted upstream (`[gone]` after a prune-fetch). Runs at the start of each of the agent's
    issue runs, so orphans from a previous process go at the first one. `live_issues()` (issues with an
    open run) is asked again under the lock right before each removal: a run registers before it can
    `prepare`, and `prepare` takes the same lock, so a run that started during the fetch is never reaped.
    ponytail: lazy -- a closed task's worktree waits for the agent's next issue run, which is also
    the only moment the disk guard needs the space."""
    root = _root(agent_id)
    tasks = [p for p in root.glob("task-*") if p.is_dir() and p.name[5:] not in live_issues()] if root.is_dir() else []
    if not tasks:
        return []
    clones = _clones(agent_id)
    for clone in clones:
        _git(agent_id, clone, "fetch", "--prune", "--quiet", "origin", timeout=120)
    reaped = []
    with _LOCK:
        for task in tasks:
            issue_id = task.name[5:]
            track = [_git(agent_id, c, "for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch(issue_id)}").stdout.strip()
                     for c in clones if (task / c.name).exists()]
            merged = bool(track) and all(t == "[gone]" for t in track)  # every repo's branch, not just one
            if not (merged or status(agent_id, issue_id) in (*CLOSED, "gone")) or issue_id in live_issues():
                continue
            for clone in clones:
                _git(agent_id, clone, "worktree", "remove", "--force", str(task / clone.name))
            shutil.rmtree(task, ignore_errors=True)
            for clone in clones:
                _git(agent_id, clone, "worktree", "prune")
            reaped.append(issue_id)
            log.info("worktree reaped agent=%s issue=%s", agent_id, issue_id)
    return reaped


def prompt_line(agent_id: str, issue_id: str, worktrees: list[tuple[Path, str]]) -> str:
    if not worktrees:  # no clone yet: the agent clones, then makes the worktree where the next run expects it
        task = _root(agent_id) / f"task-{issue_id}"
        return (f"If this task needs a repository, clone it first, then make this task's worktree with "
                f'`git -C <clone> worktree add "{task}/<repo>" -b {branch(issue_id)} origin/HEAD` and work only '
                "there: the task's next runs continue in it.")
    where = "; ".join(f"{path} (branch {name})" for path, name in worktrees)
    return (f"This task's worktree: {where}. Work only there; it and its branch stay yours across this task's runs, "
            "so if it already has commits, continue from them. Commit and push often. A new branch starts at the "
            "clone's last fetched origin/HEAD: fetch and rebase before you build on it.")
