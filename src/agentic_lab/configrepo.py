"""The lab's configuration comes from git: what `lab apply` deploys is `main` of the lab's
private config repository, never the files on disk (DESIGN.md §11a).

    ~/lab-config/            the admin's working copy (edit here, on a branch)
      lab.yaml
      framework.lock

Every change goes: edit on a branch → push → a pull request → a human reviews and merges →
`lab apply` fetches and deploys exactly the merged commit. So what runs is always a reviewed
commit, and every host records which one (/etc/agentic-lab/applied.yaml). Uncommitted edits
and branches can be previewed (`lab plan --working-tree`) but not deployed, except with
`lab apply --unreviewed` (break glass: recorded as such).

git runs as the admin (SUDO_USER) under sudo: their working copy, their deploy key; for
`lab apply-main` (from Slack), as the secretary (LAB_GIT_USER), with its own clone and key.
"""
import datetime as dt
import os
import re
import subprocess
import tempfile

BRANCH = "main"
FILES = ("lab.yaml", "framework.lock")
APPLIED = "/etc/agentic-lab/applied.yaml"


class ConfigError(RuntimeError):
    pass


def git(repo, *args, timeout=60):
    user = os.environ.get("LAB_GIT_USER") or os.environ.get("SUDO_USER")
    as_admin = ["sudo", "-u", user, "-H"] if os.geteuid() == 0 and user else []
    r = subprocess.run([*as_admin, "git", "-C", repo, *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode:
        raise ConfigError(f"git {' '.join(args[:2])} in {repo}: {r.stderr.strip()[-300:]}")
    return r.stdout


def repo_of(lab_path):
    """(the repository's top folder, lab.yaml's path inside it)."""
    d = os.path.dirname(os.path.abspath(lab_path))
    try:
        top = git(d, "rev-parse", "--show-toplevel").strip()
    except ConfigError:
        raise ConfigError(f"{lab_path} isn't in a git repository: the lab's configuration "
                          "lives in its private config repo (docs/install.md, step 4)") from None
    return top, os.path.relpath(os.path.abspath(lab_path), top)


def fetch_main(lab_path):
    """Fetch, and copy lab.yaml (+ framework.lock) as of origin/main into a temporary
    folder. Returns (path of that lab.yaml, info) where info says what was taken."""
    top, rel = repo_of(lab_path)
    git(top, "fetch", "--quiet", "origin", BRANCH)
    ref = f"origin/{BRANCH}"
    commit = git(top, "rev-parse", ref).strip()
    out = tempfile.mkdtemp(prefix="lab-config-")
    d = os.path.dirname(rel)
    for name in FILES:
        try:
            content = git(top, "show", f"{ref}:{os.path.join(d, name) if d else name}")
        except ConfigError:
            if name == "lab.yaml":
                raise ConfigError(f"{rel} isn't on {BRANCH} yet: merge it first") from None
            continue
        with open(os.path.join(out, name), "w") as f:
            f.write(content)
    return os.path.join(out, "lab.yaml"), {"config_commit": commit, "branch": BRANCH,
                                           "differs": local_differs(top, rel, ref)}


def local_differs(top, rel, ref):
    """Does the working copy hold something main doesn't (edits, commits, a branch)?"""
    d = os.path.dirname(rel)
    paths = [os.path.join(d, n) if d else n for n in FILES]
    try:
        return bool(git(top, "diff", "--name-only", ref, "--", *paths).strip())
    except ConfigError:
        return True


def working_tree_info(lab_path):
    top, rel = repo_of(lab_path)
    head = git(top, "rev-parse", "HEAD").strip()
    dirty = bool(git(top, "status", "--porcelain", "--", os.path.dirname(rel) or ".").strip())
    return {"config_commit": head, "branch": git(top, "branch", "--show-current").strip(),
            "unreviewed": True, "dirty": dirty}


def pr_link(lab_path, branch):
    """GitHub's 'open a pull request' page for `branch`, from the repo's origin URL."""
    top, _ = repo_of(lab_path)
    url = git(top, "remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        return None
    return f"https://github.com/{m.group(1)}/compare/{BRANCH}...{branch}?expand=1"


def applied_record(info, framework_commit, by):
    """What /etc/agentic-lab/applied.yaml says on each host after an apply."""
    return {**info, "framework_commit": framework_commit, "applied_by": by,
            "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
