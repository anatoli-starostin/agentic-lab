"""VM power: the secretary starts and stops the lab's cloud hosts (DESIGN.md §4.1).

A host can be powered if lab.yaml gives it `kind: cloud`, a provider below and its
`instance` id. The secretary's own host is never powered off by it.
- Nebius, through its CLI, authenticated as a service account (only the secretary holds the
  key).
- AWS EC2 (#68), through boto3 with the secretary's own access key (an IAM user limited to
  the lab's tagged instances, given through the intake), never the default credential
  chain: an EC2 host's instance role is never picked up. In the host's `region:`.

Idle stop: a host with `idle_stop_min` above 0 that runs workers is stopped once all
its workers have had nothing running, queued or waiting, and its GPUs nothing on them
(nvidia-smi, via the workers' health checks), for `idle_stop_min` minutes (required on
cloud hosts; 0: never). The same "busy"
decides who may power a host off (only admins, when busy).

Managers on a host with workers don't keep it busy: it's judged by its workers alone, and
stopping it only takes its managers offline in Slack until it's powered on again
(`idle_stop_with_managers: false` counts any manager on it as busy instead). A host that
runs only managers is judged by their activity (#70, manager_busy): no turn going and none
for `idle_stop_min`, no task they still follow, no approval of theirs pending.
"""
import asyncio
import datetime as dt
import json
import os

NEBIUS = os.path.expanduser("~/.nebius/bin/nebius")
if not os.path.exists(NEBIUS) and os.path.exists("/usr/local/bin/nebius"):
    NEBIUS = "/usr/local/bin/nebius"
PROFILE = "lab"
BUSY = ("running", "queued", "awaiting_approval", "waiting")
GPU_IDLE_READINGS = 5           # a GPU is idle only at 0% in this many readings in a row
STALE_AFTER_MIN = 10       # idle timers older than this (the secretary was down) start over
DOWN_GRACE_MIN = 60             # a running host whose worker is down this long is announced
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


# AWS instance states in the lab's words (the Nebius CLI's, which the rest of the lab uses)
AWS_STATES = {"pending": "STARTING", "running": "RUNNING", "stopping": "STOPPING",
              "stopped": "STOPPED", "shutting-down": "STOPPING", "terminated": "DELETED"}
NAMES = {"nebius": "Nebius", "aws": "AWS"}


def powerable(lab, host):
    """(ok, reason): may the secretary power this host?"""
    h = lab.hosts.get(host)
    if h is None:
        return False, f"no host {host!r} in lab.yaml"
    if h.get("kind") != "cloud":
        return False, f"{host} isn't a cloud VM: the lab can't power it"
    if h.get("provider") not in NAMES:
        return False, (f"{host} is on {h.get('provider') or 'an unknown provider'}: the lab "
                       "has no power control for it; start or stop it in the provider's console")
    if not h.get("instance"):
        return False, (f"{host} needs its {NAMES[h['provider']]} instance id in lab.yaml "
                       "(instance:) to be powered")
    sec_host = next((a.get("host") for a in lab.agents.values() if a.get("role") == "secretary"),
                    None)
    if host == sec_host:
        return False, f"{host} runs the secretary itself"
    return True, ""


AWS_KEY = "~/.config/agentic-lab/aws-key.json"     # {access_key_id, secret_access_key}


def _ec2(region, key=AWS_KEY):
    """An EC2 client with the secretary's key, explicitly: no default credential chain, so
    an instance role or environment credentials are never used (tests replace it)."""
    try:
        with open(os.path.expanduser(key)) as f:
            k = json.load(f)
        creds = {"aws_access_key_id": k["access_key_id"],
                 "aws_secret_access_key": k["secret_access_key"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise PowerError("the secretary has no AWS key (an admin: lab secret request "
                         "service:secretary aws-key, then lab apply)") from None
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise PowerError("boto3 isn't installed for the secretary (lab apply installs it "
                         "when lab.yaml has AWS hosts)") from None
    # endpoints from the environment or a config file can't redirect the signed calls
    cfg = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3},
                 ignore_configured_endpoint_urls=True)
    try:
        return boto3.client("ec2", region_name=region, config=cfg, **creds)
    except Exception as e:      # no region anywhere, a bad profile...
        raise PowerError(f"AWS: {e} (set region: on the host in lab.yaml)") from None


async def _aws(h, call):
    """One EC2 call for host `h`, off the event loop; AWS errors become PowerErrors."""
    def run():
        try:
            return getattr(_ec2(h.get("region")), call)(InstanceIds=[h["instance"]])
        except PowerError:
            raise
        except Exception as e:  # no credentials, access denied, unknown instance...
            raise PowerError(f"AWS {call}: {str(e)[:300]}") from None
    return await asyncio.to_thread(run)


def _host(lab, host):
    ok, why = powerable(lab, host)
    if not ok:
        raise PowerError(why)
    return lab.hosts[host]


async def status(lab, host):
    h = _host(lab, host)
    if h["provider"] == "aws":
        r = await _aws(h, "describe_instances")
        state = [i["State"]["Name"] for res in r.get("Reservations", [])
                 for i in res.get("Instances", [])]
        return AWS_STATES.get(state[0], state[0].upper()) if state else "UNKNOWN"
    d = json.loads(await _cli("compute", "instance", "get", "--id", h["instance"],
                              "--format", "json"))
    return (d.get("status") or {}).get("state", "UNKNOWN")


async def start(lab, host):
    h = _host(lab, host)
    if h["provider"] == "aws":
        await _aws(h, "start_instances")
        return
    await _cli("compute", "instance", "start", "--id", h["instance"])


async def stop(lab, host):
    h = _host(lab, host)
    if h["provider"] == "aws":
        await _aws(h, "stop_instances")
        return
    await _cli("compute", "instance", "stop", "--id", h["instance"])


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
    running, queued or waiting (for approval or on their long runs), long runs going,
    someone's ~/.lab-keep-awake, or the GPUs in use by anything (nvidia-smi: a process on a
    GPU, the lab's or anyone's, or any utilization above 0% in its last readings)."""
    q = seen.get("queue") or {}
    n = {k: q.get(k, 0) for k in BUSY if q.get(k)}
    if n:
        return f"{name} has " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in n.items())
    runs = seen.get("runs") or []
    if runs:                            # long runs (worker/runs.py), even CPU-only ones
        return f"{name} has {len(runs)} long run(s) going (" + ", ".join(
            r.get("name") or r.get("id") or "?" for r in runs) + ")"
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


def manager_busy(name, seen, pending=0):
    """Why a manager keeps its host busy (None if it doesn't), from its activity (its status
    endpoint, Manager.activity) and its pending approvals. Not reachable: busy, since
    nothing can tell. #69's live conversations (keepalive) would be one more check here."""
    if not seen.get("up"):
        return f"{name} can't be measured (not reachable)"
    act = seen.get("activity")
    if act is None:
        return f"{name} can't be measured (it doesn't report its activity: an older framework?)"
    if act.get("turns_in_progress"):
        return f"{name} is answering"
    if act.get("followed_tasks_open"):
        return f"{name} follows {act['followed_tasks_open']} unfinished task(s)"
    if pending:
        return f"{name} has {pending} approval(s) pending"
    return None


def managers_only(lab, host):
    """The host's agents, if they're all managers (#70); else []."""
    here = [a for a, x in lab.agents.items() if x.get("host") == host]
    return here if here and all(lab.agents[a].get("role") == "manager" for a in here) else []


def _pending(snapshot, aid):
    return sum(r.get("agent") == aid
               for r in ((snapshot or {}).get("approvals") or {}).get("pending") or [])


def counts_busy(lab, host, aid):
    """Does a non-worker agent on `host` make it busy by being there? A manager doesn't
    when a worker there judges the host (unless idle_stop_with_managers: false); on a host
    with no worker it does: nothing measures it, and stopping it would take a member's
    manager offline, so only an admin may."""
    a = lab.agents[aid]
    has_worker = any(x.get("host") == host and x.get("role") == "worker"
                     for x in lab.agents.values())
    return not (a.get("role") == "manager" and has_worker
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
            if a.get("role") == "manager" and managers_only(lab, host):
                why = manager_busy(lab.display_name(aid), (snapshot or {}).get(
                    "agents", {}).get(aid, {}), _pending(snapshot, aid))
                if why:
                    return why
                continue
            if a.get("role") == "manager":
                why = ("the host has idle_stop_with_managers: false" if any(
                    x.get("host") == host and x.get("role") == "worker"
                    for x in lab.agents.values()) else "no worker there to judge the host by")
                return (f"it runs {lab.display_name(aid)} (manager), whose own work isn't "
                        f"measured ({why})")
            return f"it runs {lab.display_name(aid)} ({a.get('role')})"
        s = (snapshot or {}).get("agents", {}).get(aid, {})
        if not s.get("up"):
            continue                    # a stopped machine's workers are down: nothing to lose
        why = worker_busy(lab.display_name(aid), s)
        if why:
            return why
    return None


class IdleWatch:
    """Remembers since when each on-demand host has been idle (and since when a host's
    worker has been down), across the secretary's restarts when given a `path` (#60)."""

    def __init__(self, path=None):
        self.path = path
        self.idle_since, self.down_since = {}, {}
        try:
            with open(path) as f:
                d = json.load(f)
            saved = dt.datetime.fromisoformat(d["saved"]) if d.get("saved") else None
            fresh = saved is not None and (dt.datetime.now(dt.timezone.utc) - saved
                                           < dt.timedelta(minutes=STALE_AFTER_MIN))
            # after a long outage nobody watched the hosts: idle timers start over
            self.idle_since = {h: dt.datetime.fromisoformat(t)
                               for h, t in (d.get("idle_since") or {}).items()} if fresh else {}
            self.down_since = {h: dt.datetime.fromisoformat(t)
                               for h, t in (d.get("down_since") or {}).items()}
        except (TypeError, OSError, ValueError):
            pass

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({**{k: {h: t.isoformat() for h, t in getattr(self, k).items()}
                          for k in ("idle_since", "down_since")},
                       "saved": dt.datetime.now(dt.timezone.utc).isoformat()}, f)
        os.replace(tmp, self.path)

    def down_too_long(self, now=None, grace=dt.timedelta(minutes=DOWN_GRACE_MIN)):
        """Hosts whose worker has been down (or unknown) for longer than the grace."""
        now = now or dt.datetime.now(dt.timezone.utc)
        return [h for h, t in self.down_since.items() if now - t >= grace]

    def candidates(self, lab, snapshot, now=None, power=None):
        """Hosts to stop now: idle_stop_min above 0, with workers (and managers, unless
        idle_stop_with_managers: false), all workers idle for idle_stop_min. `power`:
        host -> its power state, if known (a stopped host's workers aren't "down")."""
        now = now or dt.datetime.now(dt.timezone.utc)
        out = []
        for host, h in lab.hosts.items():
            h = h or {}
            if not h.get("idle_stop_min"):
                continue
            here = [a for a, x in lab.agents.items() if x.get("host") == host]
            agents = managers_only(lab, host) or [
                a for a in here if lab.agents[a].get("role") == "worker"]
            if not agents or any(counts_busy(lab, host, a) for a in here if a not in agents):
                continue
            seen = [snapshot["agents"].get(a, {}) for a in agents]
            if not all(s.get("up") for s in seen):
                self.idle_since.pop(host, None)     # down or unreachable: not ours to judge,
                if (power or {}).get(host, "RUNNING") == "RUNNING":
                    self.down_since.setdefault(host, now)   # but said once it lasts (#60)
                else:
                    self.down_since.pop(host, None)     # stopped: of course they're down
                continue
            self.down_since.pop(host, None)
            if any(manager_busy(a, s, _pending(snapshot, a)) if lab.agents[a].get("role")
                   == "manager" else worker_busy(a, s) for a, s in zip(agents, seen)):
                self.idle_since.pop(host, None)
                continue
            since = self.idle_since.setdefault(host, now)
            # a turn (or a start) since the last look counts: idle only from the latest one
            last = [dt.datetime.fromisoformat(t) for s in seen
                    for t in ((s.get("activity") or {}).get("last_turn_at"),
                              (s.get("activity") or {}).get("started_at")) if t]
            if last and max(last) > since:
                since = self.idle_since[host] = max(last)
            if now - since >= dt.timedelta(minutes=h["idle_stop_min"]):
                out.append(host)
        return out


async def recheck(lab, host, workers, snapshot, manager_activity=None):
    """Right before an idle stop: is the host still idle? Its workers' health (and on a
    managers-only host, its managers' activity: `manager_activity(aid)`, async) read again
    now (the snapshot may be minutes old, #60). Returns why it's busy, or None."""
    agents = dict((snapshot or {}).get("agents") or {})
    for aid in managers_only(lab, host):
        try:
            act = await manager_activity(aid)
        except Exception as e:
            return f"couldn't re-check {lab.display_name(aid)} ({e})"
        agents[aid] = {**agents.get(aid, {}), "up": True, "activity": act}
    for aid, a in lab.agents.items():
        if a.get("host") != host or a.get("role") != "worker":
            continue
        try:
            h = await workers.call(aid, "health_check", retries=0)
        except Exception as e:
            return f"couldn't re-check {lab.display_name(aid)} ({e})"
        prev = agents.get(aid, {})
        agents[aid] = {"up": True, "queue": h.get("tasks"), "runs": h.get("runs"),
                       "keep_awake": h.get("keep_awake"), "gpus": h.get("gpus"),
                       "gpu_processes": h.get("gpu_processes"),
                       "gpu_recent_max": prev.get("gpu_recent_max"),
                       "gpu_readings": prev.get("gpu_readings")}
    return busy(lab, host, {"agents": agents, "approvals": (snapshot or {}).get("approvals")})
