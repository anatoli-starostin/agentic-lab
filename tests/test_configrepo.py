"""The configuration comes from origin/main of the config repo, never the working copy."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_lab import configrepo as CR

SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SUDO_USER", raising=False)

    def git(cwd, *a):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    origin, work = tmp_path / "origin.git", tmp_path / "lab-config"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    shutil.copy(SINGLE, work / "lab.yaml")
    (work / "framework.lock").write_text("framework: {repo: x, commit: abc}\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "initial")
    git(work, "push", "-q", "origin", "HEAD:main")
    return work, git


def test_apply_takes_main_not_the_working_copy(repo):
    work, git = repo
    lab_path, info = CR.fetch_main(work / "lab.yaml")
    assert not info["differs"] and len(info["config_commit"]) == 40
    assert open(lab_path).read() == (work / "lab.yaml").read_text()
    assert os.path.exists(os.path.join(os.path.dirname(lab_path), "framework.lock"))

    # an edit on a branch, even committed, isn't what gets deployed
    git(work, "checkout", "-qb", "add-worker")
    (work / "lab.yaml").write_text((work / "lab.yaml").read_text() + "# edited\n")
    git(work, "commit", "-qam", "edit")
    lab_path2, info2 = CR.fetch_main(work / "lab.yaml")
    assert info2["differs"] and "# edited" not in open(lab_path2).read()
    assert info2["config_commit"] == info["config_commit"]
    wt = CR.working_tree_info(work / "lab.yaml")
    assert wt["unreviewed"] and wt["branch"] == "add-worker"


def test_not_a_repo_and_pr_link(repo, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    shutil.copy(SINGLE, loose / "lab.yaml")
    with pytest.raises(CR.ConfigError, match="isn't in a git repository"):
        CR.fetch_main(loose / "lab.yaml")
    work, git = repo
    git(work, "remote", "set-url", "origin", "git@github.com:acme/lab-config.git")
    assert CR.pr_link(work / "lab.yaml", "add-worker") == \
        "https://github.com/acme/lab-config/compare/main...add-worker?expand=1"
