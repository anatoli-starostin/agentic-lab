"""The lab administered from Slack: the secretary proposes changes as pull requests and,
once a human merged them, deploys `main` (DESIGN.md §11a).

The same rule as from the console: only what's merged to the config repo's `main` is ever
deployed. The secretary:
  - keeps its own clone of the config repo (CLONE), reached either
      * with its own deploy key (KEY, write access: it pushes branches, it never merges), or
      * as its GitHub identity (`github:` on the secretary in lab.yaml: a bot account or a
        GitHub App), over HTTPS with that identity's token, which only its service holds;
  - proposes a change: a branch with the new lab.yaml (checked first), pushed; with a
    GitHub identity it opens the pull request itself, else it hands the admin the link.
    Every commit says which agent made it (a `Lab-Agent:` trailer: an identity may be
    shared by several agents);
  - after the merge, on an admin's click in Slack, runs `sudo lab apply-main`: the one
    root command it may run (/etc/sudoers.d/agentic-lab). It takes no arguments and
    deploys only the config repo's `main`, so even a misled secretary can only deploy what
    a human reviewed.
Both folders are in the agent's secret and state folders, which its sandbox can't read.
"""
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

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


def https_url(url):
    """git@github.com:o/r.git / ssh://git@github.com/o/r -> https://github.com/o/r.git"""
    m = re.match(r"^(?:ssh://)?git@github\.com[:/](.+?)(?:\.git)?/?$", url or "")
    return f"https://github.com/{m.group(1)}.git" if m else url


def github_repo(url):
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


class ConfigCopy:
    """The secretary's clone of the config repo. `github`: its GitHub identity, or None
    (the deploy key): {"env": git's environment from github.prepare, "token_file": that
    identity's current token, "url": the config repo}. `agent`: who commits, for the
    `Lab-Agent:` trailer."""

    def __init__(self, lab_rel="lab.yaml", clone=CLONE, author=("Lab secretary", "secretary@lab"),
                 github=None, agent=None):
        self.root = os.path.expanduser(clone)
        self.rel = lab_rel
        self.author = author
        self.github = github
        self.agent = agent

    def git(self, *args, timeout=60, cwd=True):
        env = {**os.environ,
               "GIT_AUTHOR_NAME": self.author[0], "GIT_AUTHOR_EMAIL": self.author[1],
               "GIT_COMMITTER_NAME": self.author[0], "GIT_COMMITTER_EMAIL": self.author[1]}
        if self.github:
            env.update(self.github["env"])          # its credential helper, by repository
            env["GIT_TERMINAL_PROMPT"] = "0"
        else:
            env["GIT_SSH_COMMAND"] = ssh_command()
        r = subprocess.run(["git", *(["-C", self.root] if cwd else []), *args],
                           capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode:
            raise AdminError(f"git {' '.join(args[:2])}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def _ensure_clone(self):
        """With a GitHub identity the service clones over HTTPS itself (lab apply only
        clones with the deploy key); an older SSH clone switches to HTTPS."""
        if not self.github:
            return
        url = https_url(self.github["url"])
        if not os.path.isdir(os.path.join(self.root, ".git")):
            os.makedirs(os.path.dirname(self.root), mode=0o700, exist_ok=True)
            self.git("clone", "--quiet", url, self.root, cwd=False, timeout=120)
        elif self.git("remote", "get-url", "origin").strip() != url:
            self.git("remote", "set-url", "origin", url)

    def _path(self, name):
        d = os.path.dirname(self.rel)
        return os.path.join(d, name) if d else name

    def read_main(self):
        """{lab.yaml, framework.lock, commit} as on origin/main, just fetched."""
        self._ensure_clone()
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
        """A branch off main with the new lab.yaml (and pin), checked, pushed; with a
        GitHub identity, the pull request opened. Returns {branch, link, diff, pr}: `pr` the
        pull request's URL (None: `link` is where to open it by hand)."""
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
        self.git("commit", "--quiet", "-m", self.message(summary))
        self.git("push", "--quiet", "-u", "origin", branch)
        self.git("checkout", "--quiet", "--detach", f"origin/{CR.BRANCH}")
        repo = github_repo(self.git("remote", "get-url", "origin").strip())
        link = (f"https://github.com/{repo}/compare/{CR.BRANCH}...{branch}?expand=1"
                if repo else None)
        pr = None
        if self.github and repo:
            pr = self.open_pr(repo, branch, summary, diff)
        return {"branch": branch, "link": link, "diff": diff, "pr": pr}

    def message(self, summary):
        """The commit message: the summary, and which agent made it."""
        return summary + (f"\n\nLab-Agent: {self.agent}" if self.agent else "")

    def open_pr(self, repo, branch, summary, diff):
        """Open the pull request as the secretary's GitHub identity; its URL, or None
        (then the compare link is handed over instead)."""
        try:
            with open(self.github["token_file"]) as f:
                token = f.read().strip()
        except OSError:
            return None
        body = (f"Proposed by {self.agent or 'the lab secretary'}, asked in Slack.\n\n"
                "The lab deploys only what's merged to `main`: review and merge (agents "
                "never merge).")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls", method="POST",
            data=json.dumps({"title": summary[:250], "head": branch, "base": CR.BRANCH,
                             "body": body}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("html_url")
        except (urllib.error.URLError, OSError, ValueError):
            return None

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


def start_apply(catch_up=False):
    """Run `sudo lab apply-main` (`--catch-up`: only the hosts that missed the deploy this
    host runs); returns the log file the deployment writes."""
    r = subprocess.run(["sudo", "-n", "/usr/local/bin/lab", "apply-main"]
                       + (["--catch-up"] if catch_up else []), capture_output=True,
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
