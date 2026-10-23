"""One status for each host and agent (#74), computed once into the snapshot: the Home tab
and the web UI both render it, so they say the same thing.

    host:  {word, level, hint}   running / stopped / starting / stopping / always on /
                                 unreachable / no agents / power unknown
    agent: {word, level, hint, reason, since}   up / host stopped / down

`level` (ok / warn / bad / muted) is how each view colours it. An agent that doesn't
answer is "host stopped" when its host is powered off (expected: the secretary can power it
on), and "down" with the reason and since when otherwise (a real problem).
"""
import datetime as dt

from . import power as P

POWER = {"RUNNING": ("running", "ok"), "STOPPED": ("stopped", "muted"),
         "STARTING": ("starting", "warn"), "STOPPING": ("stopping", "warn"),
         "DELETED": ("deleted", "bad")}
OFF = ("stopped", "stopping", "starting")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def host_status(host, v, lab, agents, sec_host):
    if host == sec_host:
        return {"word": "always on", "level": "ok", "hint": "the secretary's host"}
    state = v.get("state")
    if state in POWER:
        word, level = POWER[state]
        return {"word": word, "level": level,
                "hint": "ask the secretary to power it on" if state == "STOPPED" else ""}
    if state or v.get("error"):
        return {"word": "power unknown", "level": "warn", "hint": v.get("error") or ""}
    here = [a for a in agents.values() if a.get("host") == host]
    if not here:
        return {"word": "no agents", "level": "muted", "hint": "no power control"}
    if any(a.get("up") for a in here):
        return {"word": "running", "level": "ok", "hint": "no power control"}
    return {"word": "unreachable", "level": "bad",
            "hint": "no power control; its agents don't answer"}


def agent_status(aid, a, host, prev, now, state, lab):
    if a.get("up"):
        out = {"word": "up", "level": "ok", "hint": ""}
        if a.get("role") == "manager" and P.managers_only(lab, a.get("host")):
            busy = P.manager_busy(a.get("name") or aid, a, P._pending(state, aid))
            out["hint"] = f"busy: {busy}" if busy else "idle"
        return out
    if (host.get("status") or {}).get("word") in OFF:
        return {"word": "host stopped", "level": "muted",
                "hint": "ask the secretary to power it on"}
    since = (prev.get("status") or {}).get("since") \
        if (prev.get("status") or {}).get("word") == "down" else None
    return {"word": "down", "level": "bad", "hint": "", "reason": a.get("error") or "",
            "since": since or now}


def annotate(state, lab, sec_host, prev=None, now=None):
    """Add `status` to every host (all of lab.yaml's) and agent of `state`. `prev`: the
    previous snapshot, for since when an agent has been down."""
    now = now or _now()
    agents = state.get("agents") or {}
    hosts = state.setdefault("hosts", {})
    for h in lab.hosts:
        hosts.setdefault(h, {})
    for h, v in hosts.items():
        v["status"] = host_status(h, v, lab, agents, sec_host)
    before = (prev or {}).get("agents") or {}
    for aid, a in agents.items():
        a["status"] = agent_status(aid, a, hosts.get(a.get("host")) or {},
                                   before.get(aid) or {}, now, state, lab)
    return state
