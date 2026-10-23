"""Bringing a host that missed a deploy up to date (#61).

GPU hosts are stopped most of the time, so most deploys miss some of them: when one comes
back (powered on by the secretary, from a console, by anyone) its agents run what was
deployed before. The secretary sees that in their health readings (each reports the host's
/etc/agentic-lab/applied.yaml) and catches the host up to what the rest of the lab runs:
`sudo lab apply-main --catch-up` deploys the commit of main this host (the secretary's)
was last deployed with, to the hosts that run an OLDER commit of main. Nothing new is
deployed (only what's already reviewed and running elsewhere), and a host on a newer or
unreviewed commit is left alone (shown, never rolled back).

  - not while a worker there has tasks running, queued or waiting for approval: the
    secretary waits for that, and the host itself checks again right before it applies
    (a deploy restarts the worker);
  - at most MAX_ATTEMPTS failed deploys per host and commit, with a pause between them;
    then it stops, marks the host stale and tells the admins (tried again after a day, or
    when the lab's commit changes). A host that wasn't reachable yet, or was busy, isn't
    a failed attempt;
  - one deploy at a time (the deploy lock): if one is running, the catch-up waits; one
    that never reports back is given up after RUN_TIMEOUT_MIN;
  - off with `auto_catch_up: false` in lab.yaml (the web UI still shows stale hosts).

Which commit is older is git's answer in the secretary's clone of the config repo. Before
deciding, the clone is made to know the lab's commit (cloned, or main fetched: `refresh`,
at most every CHECK_GAP_MIN per commit, #95). If it still can't, nothing is deployed, the
admins are told once, and the web UI shows why ("catch-up blocked").
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile

MAX_ATTEMPTS = 3
PAUSE_MIN = (5, 20)         # minutes before the 2nd and 3rd attempt
FORGET_AFTER_H = 24         # failed attempts this old don't count any more
RUN_TIMEOUT_MIN = 30        # a catch-up deploy that hasn't reported by then is over
MIN_GAP_S = 120             # between two catch-up deploys (e.g. while a host boots)
ACTIVE = ("running", "queued", "awaiting_approval", "waiting")   # a deploy restarts these
BUSY_NOTICE_H = 6           # a host busy this long is said (once), and asked less often
BUSY_GAP_MIN = 15           # then
OUTCOME = re.compile(r"^CATCH-UP (\S+) (\w+)", re.M)     # deploy() prints one per host
CHECK_GAP_MIN = 10          # between two tries to make the clone know a commit (#95)

log = logging.getLogger("agentic_lab.secretary.catchup")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def target(state):
    """(config, framework) commits the lab runs: the secretary host's. None when it isn't a
    reviewed commit of main (an --unreviewed apply is never spread by a catch-up)."""
    ap = (state or {}).get("applied") or {}
    if not ap.get("config_commit") or ap.get("unreviewed"):
        return None
    return ap["config_commit"], ap.get("framework_commit")


def differing(state, sec_host, want=None):
    """{host: what it runs} for hosts whose agents report other commits than `want`
    (default: the lab's)."""
    want = want or target(state)
    out = {}
    if want is None:
        return out
    for a in ((state or {}).get("agents") or {}).values():
        ap, host = a.get("applied"), a.get("host")
        if not (a.get("up") and ap and host) or host == sec_host:
            continue
        theirs = (ap.get("config_commit"), ap.get("framework_commit") if want[1] else None)
        if theirs != tuple(want) or ap.get("unreviewed"):
            out[host] = ap
    return out


def is_ancestor(repo, old, new):
    """Is `old` a commit before `new` (True), not (False), or can't git tell (None)?"""
    if not (repo and old and new) or old == new:
        return False if old == new else None
    try:
        r = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", old, new],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)


def behind(state, sec_host, repo):
    """{host: what it runs} for the hosts to catch up: on a reviewed commit strictly before
    the lab's (never a newer or unreviewed one: that would be a rollback)."""
    want = target(state)
    return {h: ap for h, ap in differing(state, sec_host).items()
            if not ap.get("unreviewed") and is_ancestor(repo, ap.get("config_commit"), want[0])}


def active_work(lab, host, state):
    """Why a deploy would interrupt work on `host` now (None if it wouldn't)."""
    for aid, a in lab.agents.items():
        if a.get("host") != host or a.get("role") != "worker":
            continue
        q = ((state or {}).get("agents", {}).get(aid) or {}).get("queue") or {}
        n = {k: q[k] for k in ACTIVE if q.get(k)}
        if n:
            return f"{lab.display_name(aid)} has " + ", ".join(
                f"{v} {k.replace('_', ' ')}" for k, v in n.items())
    return None


def commits_between(repo, old, new):
    """How many commits `old` is behind `new` in `repo` (None if git can't tell)."""
    if not (repo and old and new):
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "rev-list", "--count", f"{old}..{new}"],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main_commit(repo):
    """The clone's main (for showing how far hosts are behind when the lab itself runs an
    unreviewed commit)."""
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "origin/main"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


_counted = {}


def stale_info(state, sec_host, config_repo=None, gave_up=(), blocked=None):
    """{host: how it differs} for the web UI: config commits behind (None: unknown), the
    framework differing, ahead (newer or unreviewed: left alone), the catch-up given up,
    or blocked (`blocked`: commit -> why the clone can't tell, CatchUp.blocked)."""
    want = target(state)
    if want is None:                # the lab runs something unreviewed: compare with main
        m = main_commit(config_repo) if config_repo else None
        want = (m, None) if m else None
    out = {}
    for host, ap in differing(state, sec_host, want).items():
        old = ap.get("config_commit")
        key = (config_repo, old, want[0])
        n, older = _counted.get(key) or (commits_between(*key), is_ancestor(*key))
        if older is not None:           # only what git could tell: "can't" may change (#95)
            _counted[key] = (n, older)
        ahead = bool(ap.get("unreviewed")) or (older is False and old != want[0])
        out[host] = {"config_commit": old, "config_behind": None if ahead else n,
                     "framework_differs": want[1] is not None
                     and ap.get("framework_commit") != want[1],
                     "ahead": ahead, "gave_up": host in gave_up,
                     "blocked": None if ahead else (blocked or {}).get(want[0])}
    return out


def outcomes(log):
    """{host: done | failed | busy | unreachable | current | ahead} from a catch-up's log."""
    try:
        with open(log) as f:
            return dict(OUTCOME.findall(f.read()))
    except OSError:
        return {}


class CatchUp:
    """What the catch-ups did, kept across the secretary's restarts (a deploy restarts it)."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.attempts = d.get("attempts") or {}    # "host commit" -> {"n", "last"}
        self.running = d.get("running")            # {"log", "hosts", "target", "started"}
        self.gave_up = d.get("gave_up") or {}      # host -> the commit it gave up on
        self.last_start = d.get("last_start")
        self.awaiting = d.get("awaiting") or {}    # host -> commit deployed, not yet seen
        self.busy = d.get("busy") or {}            # host -> {"since", "last", "said"}
        self.checked = d.get("checked") or {}      # commit -> {"ok", "last"}: clone knows it?
        self.blocked = d.get("blocked") or {}      # commit -> why the clone can't tell
        self.blocked_said = d.get("blocked_said") or {}    # commit -> told the admins

    def save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump({"attempts": self.attempts, "running": self.running,
                       "gave_up": self.gave_up, "last_start": self.last_start,
                       "awaiting": self.awaiting, "busy": self.busy,
                       "checked": self.checked, "blocked": self.blocked,
                       "blocked_said": self.blocked_said}, f)
        os.replace(tmp, self.path)

    def _attempt(self, host, commit, now):
        a = self.attempts.get(f"{host} {commit}")
        if a and now - dt.datetime.fromisoformat(a["last"]) > dt.timedelta(hours=FORGET_AFTER_H):
            del self.attempts[f"{host} {commit}"]     # a day later: try again
            self.gave_up.pop(host, None)
            return None
        return a

    def due(self, host, commit, now):
        b = self.busy.get(host)
        if b and b.get("last") and now - dt.datetime.fromisoformat(b["since"]) > \
                dt.timedelta(hours=BUSY_NOTICE_H) and now - dt.datetime.fromisoformat(
                    b["last"]) < dt.timedelta(minutes=BUSY_GAP_MIN):
            return False                        # long busy: asked less often
        a = self._attempt(host, commit, now)
        if a is None or a["n"] == 0:
            return True
        if a["n"] >= MAX_ATTEMPTS:
            return False
        return now >= dt.datetime.fromisoformat(a["last"]) + dt.timedelta(
            minutes=PAUSE_MIN[a["n"] - 1])

    def failed(self, host, commit, now):
        a = self.attempts.setdefault(f"{host} {commit}", {"n": 0})
        a.update(n=a["n"] + 1, last=now.isoformat(timespec="seconds"))


async def _refresh(cu, lab, refresh, commit, hosts, announce, now):
    """Make the clone know `commit` before deciding (#95). Tried at most every
    CHECK_GAP_MIN, also once it worked (the clone may be lost since); a failure is logged,
    kept for the web UI, and told the admins once per break."""
    for d in (cu.checked, cu.blocked, cu.blocked_said):
        for c in [c for c in d if c != commit]:     # only the lab's commit matters
            del d[c]
    c = cu.checked.get(commit) or {}
    if c.get("last") and now - dt.datetime.fromisoformat(c["last"]) \
            < dt.timedelta(minutes=CHECK_GAP_MIN):
        return
    cu.checked[commit] = {"ok": False, "last": now.isoformat(timespec="seconds")}
    try:
        await asyncio.to_thread(refresh, commit)
    except Exception as e:
        why = (str(e) or type(e).__name__)[:200]
        cu.blocked[commit] = why
        log.warning("catch-up blocked: can't check main's history for %s: %s",
                    ", ".join(hosts), why)
        if not cu.blocked_said.get(commit):
            cu.blocked_said[commit] = True
            admins = ", ".join(h for h, x in lab.humans.items() if (x or {}).get("admin"))
            await announce(f":warning: {', '.join(hosts)} missed a deploy, but I can't check "
                           f"main's history to bring {'it' if len(hosts) == 1 else 'them'} "
                           f"up to {commit[:7]}: {why}. Admins ({admins}): fix that, or "
                           "deploy by hand.")
        return
    cu.checked[commit]["ok"] = True
    cu.blocked.pop(commit, None)
    cu.blocked_said.pop(commit, None)       # a later break of it is told again


async def step(cu, lab, state, sec_host, start, result, announce, repo=None, now=None,
               refresh=None):
    """One round. `start()` starts `lab apply-main --catch-up` and returns its log (raises
    with "already running" in the message while another deploy holds the lock);
    `result(log)` -> None while it runs, else (ok, tail); `announce(text)` (async);
    `repo`: the config repository's clone (which commit is older); `refresh(commit)`: makes
    that clone know `commit` (cloning or fetching main), raising with the reason if it
    can't (#95)."""
    now = now or _now()
    want = target(state)
    if want is None:
        return
    commit = want[0]
    other = differing(state, sec_host)
    if refresh is not None and other and lab.data.get("auto_catch_up", True):
        await _refresh(cu, lab, refresh, commit, sorted(other), announce, now)
    stale = behind(state, sec_host, repo)
    current = {a.get("host") for a in (state.get("agents") or {}).values()
               if a.get("up") and a.get("host") not in other}
    for key in list(cu.attempts):               # caught up since: forget its failures
        host, _ = key.split(" ", 1)
        if host in current:
            del cu.attempts[key]
            cu.gave_up.pop(host, None)
    if cu.running:
        r = result(cu.running["log"])
        started = dt.datetime.fromisoformat(cu.running.get("started") or now.isoformat())
        if r is None and now - started < dt.timedelta(minutes=RUN_TIMEOUT_MIN):
            cu.save()
            return                              # still deploying
        said = outcomes(cu.running["log"])
        for h in cu.running["hosts"]:           # only a real failure counts as an attempt
            if said.get(h) == "busy":           # the host itself said "not now"
                b = cu.busy.setdefault(h, {"since": started.isoformat(timespec="seconds")})
                b["last"] = now.isoformat(timespec="seconds")
            if said.get(h, "failed") == "failed":
                cu.failed(h, cu.running["target"], started)
            elif said.get(h) == "done":
                cu.awaiting[h] = cu.running["target"]    # announced once it reports it
        cu.running = None
    for host, c in list(cu.awaiting.items()):   # caught up: say so
        if host in current:
            del cu.awaiting[host]
            if c == commit:
                await announce(f":white_check_mark: {host} is up (updated to {commit[:7]}).")
    if not lab.data.get("auto_catch_up", True):
        cu.save()
        return
    for host in stale:
        a = cu._attempt(host, commit, now)
        if a and a["n"] >= MAX_ATTEMPTS and cu.gave_up.get(host) != commit:
            cu.gave_up[host] = commit
            admins = ", ".join(h for h, x in lab.humans.items() if (x or {}).get("admin"))
            await announce(f":warning: {host} missed deploys and {MAX_ATTEMPTS} attempts to "
                           f"bring it up to {commit[:7]} failed: it stays stale (see the web "
                           f"UI). Admins ({admins}): deploy it by hand once it's fixed.")
    todo = []
    for h in stale:
        why = active_work(lab, h, state)
        if not why:
            cu.busy.pop(h, None)
        else:
            b = cu.busy.setdefault(h, {"since": now.isoformat(timespec="seconds")})
            if now - dt.datetime.fromisoformat(b["since"]) > dt.timedelta(hours=BUSY_NOTICE_H) \
                    and not b.get("said"):
                b["said"] = True
                await announce(f":hourglass: {h} missed a deploy and has been busy for over "
                               f"{BUSY_NOTICE_H} h ({why}): it's brought up to date once "
                               "it's free.")
            continue
        if cu.due(h, commit, now):
            todo.append(h)
    recent = cu.last_start and now - dt.datetime.fromisoformat(cu.last_start) < \
        dt.timedelta(seconds=MIN_GAP_S)
    if not todo or recent:
        cu.save()
        return
    try:
        log = await asyncio.to_thread(start)
    except Exception as e:
        if "already running" not in str(e):     # another deploy: wait for it
            for h in todo:
                cu.failed(h, commit, now)
        cu.save()
        return
    cu.last_start = now.isoformat(timespec="seconds")
    cu.running = {"log": log, "hosts": todo, "target": commit,
                  "started": now.isoformat(timespec="seconds")}
    cu.save()
