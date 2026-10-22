"""Bringing a host that missed a deploy up to date (#61).

GPU hosts are stopped most of the time, so most deploys miss some of them: when one comes
back (powered on by the secretary, from a console, by anyone) its agents run what was
deployed before. The secretary sees that in their health readings (each reports the host's
/etc/agentic-lab/applied.yaml) and catches the host up to what the rest of the lab runs:
`sudo lab apply-main --catch-up` deploys the commit of main this host (the secretary's)
was last deployed with, to the hosts that run something else. Nothing new is deployed:
only what's already reviewed and running elsewhere.

  - not while a worker there has tasks running, queued or waiting for approval (deferred
    until it's free: a deploy restarts the worker);
  - at most MAX_ATTEMPTS per host and commit, with a pause between them; then it stops,
    marks the host stale and tells the admins, until the lab's commit changes;
  - one deploy at a time (the deploy lock): if one is running, the catch-up waits;
  - off with `auto_catch_up: false` in lab.yaml (the web UI still shows stale hosts).
"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import tempfile

MAX_ATTEMPTS = 3
PAUSE_MIN = (5, 20)         # minutes before the 2nd and 3rd attempt
ACTIVE = ("running", "queued", "awaiting_approval")     # a deploy would restart these


def _now():
    return dt.datetime.now(dt.timezone.utc)


def target(state):
    """(config, framework) commits the lab runs: the secretary host's. None when it isn't a
    reviewed commit of main (an --unreviewed apply is never spread by a catch-up)."""
    ap = (state or {}).get("applied") or {}
    if not ap.get("config_commit") or ap.get("unreviewed"):
        return None
    return ap["config_commit"], ap.get("framework_commit")


def behind(state, sec_host):
    """{host: what it runs} for hosts whose agents report other commits than the lab's."""
    want = target(state)
    out = {}
    for a in ((state or {}).get("agents") or {}).values():
        ap, host = a.get("applied"), a.get("host")
        if not (a.get("up") and ap and host) or host == sec_host:
            continue
        if want is None:
            continue
        if (ap.get("config_commit"), ap.get("framework_commit")) != want or ap.get("unreviewed"):
            out[host] = ap
    return out


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


_counted = {}


def stale_info(state, sec_host, config_repo=None, gave_up=()):
    """{host: how far behind} for the web UI: config commits behind (None: unknown), the
    framework differing, whether the catch-up gave up."""
    want = target(state)
    out = {}
    for host, ap in behind(state, sec_host).items():
        n = None
        if want and ap.get("config_commit"):
            key = (config_repo, ap["config_commit"], want[0])
            if key not in _counted:
                _counted[key] = commits_between(*key)
            n = _counted[key]
        out[host] = {"config_commit": ap.get("config_commit"), "config_behind": n,
                     "framework_differs": bool(want) and ap.get("framework_commit") != want[1],
                     "gave_up": host in gave_up}
    return out


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
        self.running = d.get("running")            # {"log", "hosts", "target"} or None
        self.gave_up = d.get("gave_up") or {}      # host -> the commit it gave up on

    def save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump({"attempts": self.attempts, "running": self.running,
                       "gave_up": self.gave_up}, f)
        os.replace(tmp, self.path)

    def due(self, host, commit, now):
        a = self.attempts.get(f"{host} {commit}")
        if a is None:
            return True
        if a["n"] >= MAX_ATTEMPTS:
            return False
        return now >= dt.datetime.fromisoformat(a["last"]) + dt.timedelta(
            minutes=PAUSE_MIN[a["n"] - 1])


async def step(cu, lab, state, sec_host, start, result, announce, now=None):
    """One round. `start()` starts `lab apply-main --catch-up` and returns its log (raises
    with "already running" in the message while another deploy holds the lock);
    `result(log)` -> None while it runs, else (ok, tail); `announce(text)` (async)."""
    now = now or _now()
    want = target(state)
    stale = behind(state, sec_host)
    if want is None:
        return
    commit = want[0]
    for key in list(cu.attempts):               # caught up since: say so, forget it
        host, c = key.split(" ", 1)
        if host not in stale and any(a.get("host") == host and a.get("up")
                                     for a in (state.get("agents") or {}).values()):
            del cu.attempts[key]
            cu.gave_up.pop(host, None)
            if c == commit:
                await announce(f":white_check_mark: {host} is up (updated to {commit[:7]}).")
    if cu.running:
        r = result(cu.running["log"])
        if r is None:
            cu.save()
            return                              # still deploying
        cu.running = None
    if not lab.data.get("auto_catch_up", True):
        cu.save()
        return
    for host in stale:
        a = cu.attempts.get(f"{host} {commit}")
        if a and a["n"] >= MAX_ATTEMPTS and cu.gave_up.get(host) != commit:
            cu.gave_up[host] = commit
            admins = ", ".join(h for h, x in lab.humans.items() if (x or {}).get("admin"))
            await announce(f":warning: {host} missed deploys and {MAX_ATTEMPTS} attempts to "
                           f"bring it up to {commit[:7]} failed: it stays stale (see the web "
                           f"UI). Admins ({admins}): deploy it by hand once it's fixed.")
    todo = [h for h in stale if cu.due(h, commit, now) and not active_work(lab, h, state)]
    if not todo:
        cu.save()
        return
    try:
        log = await asyncio.to_thread(start)
    except Exception as e:
        if "already running" in str(e):
            cu.save()
            return                              # another deploy: wait for it
        log = None
    for h in todo:
        a = cu.attempts.setdefault(f"{h} {commit}", {"n": 0})
        a.update(n=a["n"] + 1, last=now.isoformat(timespec="seconds"))
    cu.running = {"log": log, "hosts": todo, "target": commit} if log else None
    cu.save()
