"""VM power: the secretary starts and stops the lab's cloud hosts (DESIGN.md §4.1).

Nebius, through its CLI, authenticated as a service account (only the secretary holds the
key). A host can be powered if lab.yaml gives it `kind: cloud`, `provider: nebius` and its
`instance` id. The secretary's own host is never powered off by it.

Idle stop: a host whose `power` is `on_demand` and that runs only workers is stopped once all
its workers have had nothing running, queued or waiting for `idle_stop_min` minutes.
"""
import asyncio
import datetime as dt
import json
import os

NEBIUS = os.path.expanduser("~/.nebius/bin/nebius")
if not os.path.exists(NEBIUS) and os.path.exists("/usr/local/bin/nebius"):
    NEBIUS = "/usr/local/bin/nebius"
PROFILE = "lab"
BUSY = ("running", "queued", "awaiting_approval")


class PowerError(RuntimeError):
    pass


async def _cli(*args, timeout=120):
    if not os.path.exists(NEBIUS):
        raise PowerError("the Nebius CLI isn't installed for the secretary (lab apply installs "
                         "it once the Nebius service account is given)")
    p = await asyncio.create_subprocess_exec(
        NEBIUS, *args, "--profile", PROFILE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise PowerError(f"nebius {' '.join(args[:3])}: timed out") from None
    if p.returncode:
        raise PowerError(f"nebius {' '.join(args[:3])}: {err.decode(errors='replace')[-300:]}")
    return out.decode()


def powerable(lab, host):
    """(ok, reason): may the secretary power this host?"""
    h = lab.hosts.get(host)
    if h is None:
        return False, f"no host {host!r} in lab.yaml"
    if h.get("kind") != "cloud" or h.get("provider") != "nebius" or not h.get("instance"):
        return False, f"{host} isn't a Nebius VM with an instance id in lab.yaml"
    sec_host = next((a.get("host") for a in lab.agents.values() if a.get("role") == "secretary"),
                    None)
    if host == sec_host:
        return False, f"{host} runs the secretary itself"
    return True, ""


async def status(lab, host):
    ok, why = powerable(lab, host)
    if not ok:
        raise PowerError(why)
    d = json.loads(await _cli("compute", "instance", "get", "--id", lab.hosts[host]["instance"],
                              "--format", "json"))
    return (d.get("status") or {}).get("state", "UNKNOWN")


async def start(lab, host):
    ok, why = powerable(lab, host)
    if not ok:
        raise PowerError(why)
    await _cli("compute", "instance", "start", "--id", lab.hosts[host]["instance"])


async def stop(lab, host):
    ok, why = powerable(lab, host)
    if not ok:
        raise PowerError(why)
    await _cli("compute", "instance", "stop", "--id", lab.hosts[host]["instance"])


class IdleWatch:
    """Remembers since when each on-demand host has been idle."""

    def __init__(self):
        self.idle_since = {}

    def candidates(self, lab, snapshot, now=None):
        """Hosts to stop now: on demand, only workers, all idle for idle_stop_min."""
        now = now or dt.datetime.now(dt.timezone.utc)
        out = []
        for host, h in lab.hosts.items():
            h = h or {}
            if h.get("power") != "on_demand" or not h.get("idle_stop_min"):
                continue
            agents = [a for a, x in lab.agents.items() if x.get("host") == host]
            if not agents or any(lab.agents[a].get("role") != "worker" for a in agents):
                continue
            seen = [snapshot["agents"].get(a, {}) for a in agents]
            if not all(s.get("up") for s in seen):
                self.idle_since.pop(host, None)     # down or unreachable: not ours to judge
                continue
            busy = any(sum((s.get("queue") or {}).get(k, 0) for k in BUSY) for s in seen)
            if busy:
                self.idle_since.pop(host, None)
                continue
            since = self.idle_since.setdefault(host, now)
            if now - since >= dt.timedelta(minutes=h["idle_stop_min"]):
                out.append(host)
        return out
