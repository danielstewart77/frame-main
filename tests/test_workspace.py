import subprocess

from workspace import Workspace


def test_ensure_creates_bare_repo_memory_and_identity(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    assert workspace.exists()
    assert (workspace.origin / "HEAD").exists()
    assert workspace.transcripts.is_dir()
    assert "identity" in workspace.identity_text()


def test_ensure_is_idempotent_and_preserves_identity(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    workspace.set_identity("# identity\n\nDaniel's operator workspace.")
    workspace.ensure()
    assert "operator workspace" in workspace.identity_text()


def test_workspaces_are_isolated_per_user(tmp_path):
    one = Workspace(tmp_path, "user-1").ensure()
    two = Workspace(tmp_path, "user-2").ensure()
    one.set_identity("only mine")
    assert two.identity_text() != "only mine"
    assert one.origin != two.origin


def test_diff_reads_a_pushed_session_branch(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    clone = tmp_path / "clone"
    _run(["git", "clone", str(workspace.origin), str(clone)])
    _run(["git", "config", "user.email", "a@b.c"], cwd=clone)
    _run(["git", "config", "user.name", "tester"], cwd=clone)
    _run(["git", "checkout", "-b", "session/abc"], cwd=clone)
    (clone / "hello.txt").write_text("hello from the container\n")
    _run(["git", "add", "-A"], cwd=clone)
    _run(["git", "commit", "-m", "turn 1"], cwd=clone)
    _run(["git", "push", "origin", "session/abc"], cwd=clone)

    assert "session/abc" in workspace.branches()
    diff = workspace.diff("session/abc")
    assert "hello from the container" in diff
    assert workspace.diff("session/nope") == ""


def test_clone_url_points_at_the_bare_repo(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    assert workspace.clone_url() == str(workspace.origin)


def test_destroy_removes_everything(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    workspace.destroy()
    assert not workspace.path.exists()


def _run(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout
