"""One git worktree per Paperclip issue: made, resumed, refused on a short disk, reaped when closed."""

import subprocess

import pytest

from bridge import sync, turn, worktrees

A, B = "aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "disk_free_mb", lambda: (4000, 4500))
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    repo = home / "profiles" / "forge" / ".run" / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    return repo


def test_each_issue_gets_its_own_worktree_and_branch(clone):
    (a, a_branch), = worktrees.prepare("forge", A)[0]
    (b, b_branch), = worktrees.prepare("forge", B)[0]

    assert a != b and a.parent.name == f"task-{A}" and a.name == "app"
    assert (a_branch, b_branch) == ("task/aaaaaaaa", "task/bbbbbbbb")
    assert git(a, "branch", "--show-current") == a_branch
    assert git(b, "branch", "--show-current") == b_branch
    assert "task's worktree" in worktrees.prompt_line("forge", A, [(a, a_branch)])


def test_the_next_run_resumes_the_same_branch_with_its_commits(clone):
    (path, _), = worktrees.prepare("forge", A)[0]
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "half done")

    (again, _), = worktrees.prepare("forge", A)[0]
    assert again == path and git(again, "log", "-1", "--format=%s") == "half done"

    # the box restarted: .scratch is gone, the branch in the saved clone is not
    subprocess.run(["rm", "-rf", str(path.parent)], check=True)
    (restored, _), = worktrees.prepare("forge", A)[0]
    assert git(restored, "log", "-1", "--format=%s") == "half done"


def test_a_new_worktree_is_refused_on_a_short_disk_but_an_existing_one_is_reused(clone, monkeypatch):
    worktrees.prepare("forge", A)
    monkeypatch.setattr(sync, "disk_free_mb", lambda: (1200, 1600))  # outer is the lower one

    assert worktrees.prepare("forge", A)[1] is None
    made, refused = worktrees.prepare("forge", B)
    assert made == [] and "1200 MB free" in refused and "1500 MB" in refused
    assert not (worktrees._root("forge") / f"task-{B}").exists()


def test_reap_removes_closed_and_deleted_tasks_and_leaves_open_and_live_ones(clone):
    c = "cccccccc-3333-4333-8333-333333333333"
    for issue in (A, B, c):
        worktrees.prepare("forge", issue)
    status = {A: "done", B: "in_progress", c: "gone"}

    assert sorted(worktrees.reap("forge", set(), status=lambda _agent, issue: status[issue])) == sorted([A, c])
    left = sorted(p.name for p in worktrees._root("forge").iterdir())
    assert left == [f"task-{B}"]
    assert A not in git(clone, "worktree", "list")

    worktrees.prepare("forge", A)  # reopened
    assert worktrees.reap("forge", {A}, status=lambda *_: "cancelled") == [B]  # A has a live run


def test_reap_removes_a_task_whose_branch_was_merged_and_deleted_upstream(clone):
    (path, name), = worktrees.prepare("forge", A)[0]
    git(path, "push", "-q", "-u", "origin", name)
    git(clone, "push", "-q", "origin", f":{name}")  # the PR merged with --delete-branch

    assert worktrees.reap("forge", set(), status=lambda *_: "in_review") == [A]


def test_no_clone_means_no_worktree_and_the_prompt_says_where_to_make_it(home):
    assert worktrees.prepare("forge", A) == ([], None)
    line = worktrees.prompt_line("forge", A, [])
    assert f"/.scratch/wt/task-{A}/<repo>" in line and "-b task/aaaaaaaa" in line


def test_reap_keeps_a_task_while_any_of_its_repos_branches_is_live(clone, tmp_path):
    lib_origin = tmp_path / "lib.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(tmp_path / "origin.git"), str(lib_origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(lib_origin), str(clone.parent / "lib")], check=True)
    task = worktrees._root("forge") / f"task-{A}"
    name = dict((p.name, n) for p, n in worktrees.prepare("forge", A)[0])["app"]
    for repo in ("app", "lib"):
        git(task / repo, "push", "-q", "-u", "origin", name)
    git(clone, "push", "-q", "origin", f":{name}")  # app's PR merged; lib's is still open

    assert worktrees.reap("forge", set(), status=lambda *_: "in_progress") == []
    git(clone.parent / "lib", "push", "-q", "origin", f":{name}")
    assert worktrees.reap("forge", set(), status=lambda *_: "in_progress") == [A]


def test_a_refused_task_run_fails_with_the_disk_message(home, monkeypatch):
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    monkeypatch.setattr(worktrees, "prepare", lambda *_: ([], "Not started: the workspace disk has 900 MB free"))
    identity = {"runId": "run-1", "agentId": "forge", "companyId": "c", "issueId": A,
                "sessionId": f"paperclip:company:c:agent:forge:issue:{A}", "paperclipApiUrl": "http://pc"}

    outcome, error, output, _ = turn._run_native_paperclip(identity, {"input": "x", "instructions": "y"})
    assert (outcome, output) == ("failed", "") and error.startswith("Not started: the workspace disk")
