"""framework.lock: a lab runs a pinned, committed framework version."""
import subprocess

import pytest

from agentic_lab import lock


def repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    return path


def commit(path, msg="more"):
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", msg], check=True)


def test_pin_check_and_update(tmp_path):
    src = repo(tmp_path / "framework")
    lab = tmp_path / "config" / "lab.yaml"
    lab.parent.mkdir()
    lab.write_text("lab: x\n")
    assert "pinned framework" in lock.check(lab, src)          # first apply pins it
    assert "as pinned" in lock.check(lab, src)
    commit(src)
    with pytest.raises(lock.LockError, match="pinned to"):
        lock.check(lab, src)                                  # a newer checkout: refused
    assert "moved the pin" in lock.check(lab, src, update=True)
    assert "as pinned" in lock.check(lab, src)


def test_dirty_checkout_refused(tmp_path):
    src = repo(tmp_path / "framework")
    (src / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "f.txt"], check=True)
    lab = tmp_path / "lab.yaml"
    lab.write_text("lab: x\n")
    with pytest.raises(lock.LockError, match="uncommitted"):
        lock.check(lab, src)
