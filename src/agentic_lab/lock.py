"""framework.lock: which framework version a lab runs (DESIGN.md §11a).

A lab's configuration lives in the lab's own (private) repository: lab.yaml, and next to it
framework.lock, which pins the framework to one commit:

    framework:
      repo: https://github.com/anatoli-starostin/agentic-lab
      commit: 0ba63b2...

`lab apply` ships only that commit (as on the config repo's main). Upgrading is a reviewed change:
move the pin (`lab apply --update-framework`), then a PR with framework.lock.
"""
import os
import subprocess

import yaml

NAME = "framework.lock"


class LockError(RuntimeError):
    pass


def path_for(lab_path):
    return os.path.join(os.path.dirname(os.path.abspath(lab_path)), NAME)


def git(source, *args):
    r = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise LockError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def current(source):
    """(repo URL, commit, dirty?) of a framework checkout."""
    try:
        url = git(source, "remote", "get-url", "origin")
    except LockError:
        url = None
    commit = git(source, "rev-parse", "HEAD")
    dirty = bool(git(source, "status", "--porcelain", "--untracked-files=no"))
    return url, commit, dirty


def write(lab_path, source):
    url, commit, dirty = current(source)
    if dirty:
        raise LockError(f"{source} has uncommitted changes: pin a committed version")
    with open(path_for(lab_path), "w") as f:
        f.write("# The framework version this lab runs. Move it deliberately:\n"
                "#   lab apply --update-framework   (then commit this file)\n")
        yaml.safe_dump({"framework": {"repo": url, "commit": commit}}, f, sort_keys=False)
    return commit


def read(lab_path):
    try:
        with open(path_for(lab_path)) as f:
            return (yaml.safe_load(f) or {}).get("framework") or {}
    except FileNotFoundError:
        return None


def check(lab_path, source, update=False):
    """Before shipping `source`: is it the pinned commit, with nothing uncommitted?
    Returns a note for the admin. `update` moves the pin to `source`'s commit."""
    url, commit, dirty = current(source)
    if dirty:
        raise LockError(f"{source} has uncommitted changes; commit them (or stash) first: "
                        "the lab only runs committed framework versions")
    pinned = read(lab_path)
    if update or pinned is None:
        write(lab_path, source)
        verb = "pinned" if pinned is None else f"moved the pin from {pinned.get('commit', '?')[:10]} to"
        return (f"{verb} framework {commit[:10]} in {path_for(lab_path)}: commit and push it "
                "with the lab's configuration")
    if pinned.get("commit") != commit:
        raise LockError(
            f"the framework checkout is at {commit[:10]}, but this lab is pinned to "
            f"{pinned.get('commit', '?')[:10]} ({NAME}). Check out the pinned commit "
            f"(git -C {source} checkout {pinned.get('commit')}), or upgrade on purpose: "
            "lab apply --update-framework, then a PR with the new framework.lock")
    return f"framework {commit[:10]} (as pinned)"
