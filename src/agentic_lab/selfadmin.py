"""The lab administered from Slack: the secretary proposes changes as pull requests and,
once a human merged them, deploys `main` (DESIGN.md §11a).

The same rule as from the console: only what's merged to the config repo's `main` is ever
deployed. The secretary:
  - keeps its own clone of the config repo (CLONE), with its own deploy key (KEY, write
    access: it pushes branches, it never merges);
  - proposes a change: a branch with the new lab.yaml (checked first), pushed; it hands
    the admin the pull request link;
  - after the merge, on an admin's click in Slack, runs `sudo lab apply-main`: the one
    root command it may run (/etc/sudoers.d/agentic-lab). It takes no arguments and
    deploys only the config repo's `main`, so even a misled secretary can only deploy what
    a human reviewed.
Both folders are in the agent's secret and state folders, which its sandbox can't read.
"""
import datetime as dt
import os
import re
import subprocess
import tempfile

import yaml

from . import configrepo as CR
from .registry import check as lab_check
from .registry import load as load_lab

KEY = "~/.config/agentic-lab/config-repo-key"
CLONE = "~/.local/state/agentic-lab/config-repo"
APPLY_MAIN = "/etc/agentic-lab/apply-main.yaml"     # written by lab apply (root, 644)
LOG_DIR = "/var/log/agentic-lab"
RESULT = "APPLY-RESULT:"


class AdminError(RuntimeError):
    pass


def ssh_command(key=KEY):
    return (f"ssh -i {os.path.expanduser(key)} -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=accept-new")


class ConfigCopy:
    """The secretary's clone of the config repo."""

    def __init__(self, lab_rel="lab.yaml", clone=CLONE, author=("Lab secretary", "secretary@lab")):
        self.root = os.path.expanduser(clone)
        self.rel = lab_rel
        self.author = author

    def git(self, *args, timeout=60):
        env = {**os.environ, "GIT_SSH_COMMAND": ssh_command(),
               "GIT_AUTHOR_NAME": self.author[0], "GIT_AUTHOR_EMAIL": self.author[1],
               "GIT_COMMITTER_NAME": self.author[0], "GIT_COMMITTER_EMAIL": self.author[1]}
        r = subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode:
            raise AdminError(f"git {' '.join(args[:2])}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def _path(self, name):
        d = os.path.dirname(self.rel)
        return os.path.join(d, name) if d else name

    def read_main(self):
        """{lab.yaml, framework.lock, commit} as on origin/main, just fetched."""
        if not os.path.isdir(os.path.join(self.root, ".git")):
            raise AdminError("the secretary has no copy of the config repo yet: add its deploy "
                             "key to the repo (sudo lab apply prints it), then sudo lab apply")
        self.git("fetch", "--quiet", "origin", CR.BRANCH)
        ref = f"origin/{CR.BRANCH}"
        out = {"commit": self.git("rev-parse", ref).strip()}
        for name in CR.FILES:
            try:
                out[name] = self.git("show", f"{ref}:{self._path(name)}")
            except AdminError:
                out[name] = None
        return out

    def propose(self, summary, lab_yaml, framework_commit=None):
        """A branch off main with the new lab.yaml (and pin), checked, pushed.
        Returns {branch, link, diff}."""
        main = self.read_main()
        with tempfile.TemporaryDirectory() as tmp:     # check before anything is written
            p = os.path.join(tmp, "lab.yaml")
            with open(p, "w") as f:
                f.write(lab_yaml)
            try:
                errors = [i for i in lab_check(load_lab(p)) if i.level == "error"]
            except Exception as e:
                raise AdminError(f"lab.yaml doesn't load: {e}") from None
            if errors:
                raise AdminError("lab check failed:\n" + "\n".join(f"- {i}" for i in errors))
        slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:40] or "change"
        branch = f"lab/{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M}-{slug}"
        self.git("checkout", "--quiet", "-B", branch, f"origin/{CR.BRANCH}")
        with open(os.path.join(self.root, self.rel), "w") as f:
            f.write(lab_yaml if lab_yaml.endswith("\n") else lab_yaml + "\n")
        if framework_commit:
            lock = yaml.safe_load(main.get("framework.lock") or "") or {}
            fw = lock.get("framework") or {}
            fw["commit"] = framework_commit
            with open(os.path.join(self.root, self._path("framework.lock")), "w") as f:
                f.write("# The framework version this lab runs. Move it deliberately:\n"
                        "#   lab apply --update-framework   (then a PR)\n")
                yaml.safe_dump({"framework": fw}, f, sort_keys=False)
        self.git("add", "--", self.rel, self._path("framework.lock"))
        diff = self.git("diff", "--cached")
        if not diff.strip():
            self.git("checkout", "--quiet", "--detach", f"origin/{CR.BRANCH}")
            raise AdminError("that's what main already says: nothing to change")
        self.git("commit", "--quiet", "-m", summary)
        self.git("push", "--quiet", "-u", "origin", branch)
        self.git("checkout", "--quiet", "--detach", f"origin/{CR.BRANCH}")
        url = self.git("remote", "get-url", "origin").strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        link = (f"https://github.com/{m.group(1)}/compare/{CR.BRANCH}...{branch}?expand=1"
                if m else None)
        return {"branch": branch, "link": link, "diff": diff}

    def not_applied(self, applied_commit):
        """Commits on main since the one deployed (one line each)."""
        main = self.read_main()["commit"]
        if not applied_commit:
            return main, ["(nothing recorded as applied yet)"]
        if applied_commit == main:
            return main, []
        try:
            return main, self.git("log", "--oneline", f"{applied_commit}..{main}").splitlines()
        except AdminError:
            return main, ["(the applied commit isn't on main: it was applied --unreviewed)"]


def applied(path=CR.APPLIED):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, ValueError):
        return {}


def start_apply():
    """Run `sudo lab apply-main`; returns the log file the deployment writes."""
    r = subprocess.run(["sudo", "-n", "/usr/local/bin/lab", "apply-main"], capture_output=True,
                       text=True, timeout=60)
    m = re.search(r"log: (\S+)", r.stdout)
    if r.returncode or not m:
        raise AdminError(f"couldn't start the deployment: {(r.stderr or r.stdout).strip()[-300:]}")
    return m.group(1)


def apply_result(log):
    """None while running; (ok, the end of the log) when done."""
    try:
        with open(log) as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(rf"^{RESULT} (\w+)", text, re.M)
    if not m:
        return None
    return m.group(1) == "ok", text[-1500:]
