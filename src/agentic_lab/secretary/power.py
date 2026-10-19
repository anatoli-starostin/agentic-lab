"""VM power: the secretary starts and stops the lab's cloud hosts (DESIGN.md §4.1).

Nebius, through its CLI, authenticated as a service account (only the secretary holds the
key). A host can be powered if lab.yaml gives it `kind: cloud`, `provider: nebius` and its
`instance` id. The secretary's own host is never powered off by it.

Idle stop: a host whose `power` is `on_demand` and that runs workers is stopped once all
its workers have had nothing running, queued or waiting, and its GPUs nothing on them
(nvidia-smi, via the workers' health checks), for `idle_stop_min` minutes (required on
on-demand hosts; 0: never). The same "busy"
decides who may power a host off (only admins, when busy).

Managers on a host don't keep it busy: it's judged by its workers alone, and stopping it
only takes its managers offline in Slack until it's powered on again. A manager's own work
(a turn, following a task elsewhere) isn't measured, so a host can say
`idle_stop_with_managers: false` to count any manager on it as busy.
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
GPU_IDLE_READINGS = 5           # a GPU is idle only at 0% in this many readings in a row
_gpu_recent = {}                # worker -> its GPUs' highest utilization, last readings


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


def note_gpu(snapshot):
    """Remember each worker's GPU utilization over the last readings (one per collection),
    and write its highest into the snapshot (`gpu_recent_max`): a GPU counts as idle only
    after 0% in GPU_IDLE_READINGS readings in a row."""
    from collections import deque
    for aid, a in (snapshot.get("agents") or {}).items():
        if a.get("role") != "worker" or not a.get("up"):
            _gpu_recent.pop(aid, None)
            continue
        utils = [g.get("util") or 0 for g in a.get("gpus") or []]
        hist = _gpu_recent.setdefault(aid, deque(maxlen=GPU_IDLE_READINGS))
        hist.append(max(utils) if utils else 0)
        a["gpu_recent_max"] = max(hist)
        a["gpu_readings"] = len(hist)


def worker_busy(name, seen):
    """Why a worker's machine is busy (None if it isn't), from its health readings: tasks
    running, queued or waiting, someone's ~/.lab-keep-awake, or the GPUs in use by anything (nvidia-smi: a process on a
    GPU, the lab's or anyone's, or any utilization above 0% in its last readings)."""
    q = seen.get("queue") or {}
    n = {k: q.get(k, 0) for k in BUSY if q.get(k)}
    if n:
        return f"{name} has " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in n.items())
    holders = seen.get("keep_awake") or []
    if holders:                         # someone working on the machine directly
        who = ", ".join(h["user"] + (f" ({h['note']})" if h.get("note") else "")
                        for h in holders)
        return f"kept awake by {who} (~/.lab-keep-awake)"
    procs = seen.get("gpu_processes") or []
    if procs:
        names = ", ".join(sorted({p.get("name") or "?" for p in procs}))
        return f"{len(procs)} process(es) on the GPU ({names})"
    now = max([g.get("util") or 0 for g in seen.get("gpus") or []] or [0])
    recent = max(now, seen.get("gpu_recent_max") or 0)
    if recent > 0:
        return (f"GPU {now}% busy" if now > 0 else
                f"GPU was {recent}% busy in the last {GPU_IDLE_READINGS} readings")
    if seen.get("gpus") and seen.get("gpu_readings", GPU_IDLE_READINGS) < GPU_IDLE_READINGS:
        return "GPU not yet 0% for long enough"
    return None


def counts_busy(lab, host, aid):
    """Does a non-worker agent on `host` make it busy by being there?"""
    a = lab.agents[aid]
    return not (a.get("role") == "manager"
                and (lab.hosts.get(host) or {}).get("idle_stop_with_managers", True))


def busy(lab, host, snapshot):
    """Why powering `host` off would interrupt work (None if it wouldn't): a worker busy
    (see worker_busy), or a manager there if the host has idle_stop_with_managers: false."""
    for aid, a in lab.agents.items():
        if a.get("host") != host:
            continue
        if a.get("role") != "worker":
            if not counts_busy(lab, host, aid):
                continue                # judged by the host's workers alone
            if a.get("role") == "manager":
                return (f"it runs {lab.display_name(aid)} (manager), whose own work isn't "
                        "measured (the host has idle_stop_with_managers: false)")
            return f"it runs {lab.display_name(aid)} ({a.get('role')})"
        s = (snapshot or {}).get("agents", {}).get(aid, {})
        if not s.get("up"):
            continue                    # a stopped machine's workers are down: nothing to lose
        why = worker_busy(lab.display_name(aid), s)
        if why:
            return why
    return None


class IdleWatch:
    """Remembers since when each on-demand host has been idle."""

    def __init__(self):
        self.idle_since = {}

    def candidates(self, lab, snapshot, now=None):
        """Hosts to stop now: on demand, with workers (and managers, unless
        idle_stop_with_managers: false), all workers idle for idle_stop_min."""
        now = now or dt.datetime.now(dt.timezone.utc)
        out = []
        for host, h in lab.hosts.items():
            h = h or {}
            if h.get("power") != "on_demand" or not h.get("idle_stop_min"):
                continue
            here = [a for a, x in lab.agents.items() if x.get("host") == host]
            agents = [a for a in here if lab.agents[a].get("role") == "worker"]
            if not agents or any(counts_busy(lab, host, a) for a in here if a not in agents):
                continue
            seen = [snapshot["agents"].get(a, {}) for a in agents]
            if not all(s.get("up") for s in seen):
                self.idle_since.pop(host, None)     # down or unreachable: not ours to judge
                continue
            if any(worker_busy(a, s) for a, s in zip(agents, seen)):
                self.idle_since.pop(host, None)
                continue
            since = self.idle_since.setdefault(host, now)
            if now - since >= dt.timedelta(minutes=h["idle_stop_min"]):
                out.append(host)
        return out
